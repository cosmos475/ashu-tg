"""
bot/services/downloader.py — File downloader service.

Supports three download strategies, selected automatically:
  1. yt-dlp   — m3u8 streams and any URL yt-dlp recognises.
  2. requests  — Direct file downloads (PDF, HTML, MP4, etc.) with streaming.

Selection logic:
  - If url_type == "video" OR the URL path ends with .m3u8 → try yt-dlp first,
    fall back to requests on yt-dlp failure.
  - If url_type in ("pdf", "html", "unknown") → use requests directly.

Size validation:
  - For requests downloads: send a HEAD request first and check Content-Length.
    If the file exceeds MAX_FILE_SIZE_BYTES (2 GB), raise FileTooLargeError.
  - For yt-dlp downloads: size is checked after yt-dlp reports it internally;
    if the reported size exceeds the limit, raise FileTooLargeError.

Progress reporting:
  - For requests: progress.update() called every _CHUNK_SIZE bytes.
  - For yt-dlp: a custom progress hook feeds progress.update().

Retry logic:
  - Transient network errors trigger a retry with exponential backoff.
  - Non-retryable errors (FileTooLargeError, ValueError) re-raised immediately.

Temp file cleanup:
  - yt-dlp creates intermediate .part and segment files during m3u8 merging.
    These are cleaned up in the finally block via _cleanup_ytdlp_tmp().
  - All callers must wrap download() in a try/finally that calls
    file_utils.delete_file() on the returned path — even on failure.

Public API:
  download(url, url_type, task_num, total_tasks, caption) -> Path
    Returns the absolute Path of the downloaded file in TMP_DIR.
    Raises: FileTooLargeError, DownloadError
"""

import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import requests.exceptions
import yt_dlp

import config
from bot.services import progress as progress_module
from bot.utils.file_utils import delete_file, format_size, get_tmp_path, sanitize_filename

logger = logging.getLogger(__name__)

# Download chunk size for streaming requests (512 KB)
_CHUNK_SIZE = 512 * 1024

# Timeout for requests: (connect_timeout, read_timeout) in seconds
_REQUEST_TIMEOUT = (15, 60)

# yt-dlp socket timeout
_YTDLP_SOCKET_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DownloadError(Exception):
    """Raised when a download fails after all retries."""


class FileTooLargeError(Exception):
    """Raised when the remote file exceeds MAX_FILE_SIZE_BYTES."""


class DownloadCancelled(DownloadError):
    """Raised when a download is aborted because /cancel was requested."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def download(
    url: str,
    url_type: str,
    task_num: int,
    total_tasks: int,
    caption: str,
    cancel_event: threading.Event | None = None,
) -> Path:
    """
    Download a file from the given URL to the tmp/ directory.

    Automatically selects the best download strategy based on url_type.
    Reports progress to progress.py during the download.
    Retries up to MAX_RETRIES times on transient network errors.

    Args:
        url:         Validated http/https URL string.
        url_type:    One of "video", "pdf", "html", "unknown".
        task_num:    1-based task index (for progress display).
        total_tasks: Total task count in this session (for progress display).
        caption:     Human-readable caption for the progress message.

    Returns:
        Absolute Path to the downloaded file in TMP_DIR.

    Raises:
        FileTooLargeError: File exceeds 2 GB limit.
        DownloadError:     All retries exhausted without success.
    """
    # Send the initial progress message before the first attempt
    progress_module.send(task_num, caption)

    use_ytdlp = _should_use_ytdlp(url, url_type)
    last_exc: Exception = DownloadError("Unknown error")

    for attempt in range(1, config.MAX_RETRIES + 1):

        if cancel_event is not None and cancel_event.is_set():
            logger.info("Download cancelled before attempt %d | url=%s", attempt, url[:80])
            raise DownloadCancelled("Download cancelled by user")

        try:
            if use_ytdlp:
                logger.info(
                    "yt-dlp download | attempt=%d url=%s", attempt, url[:80]
                )
                path = _download_with_ytdlp(
                    url, task_num, total_tasks, caption, cancel_event=cancel_event
                )
            else:
                logger.info(
                    "requests download | attempt=%d url=%s", attempt, url[:80]
                )
                path = _download_with_requests(
                    url, url_type, task_num, total_tasks, caption, cancel_event=cancel_event
                )

            logger.info("Download complete | path=%s", path.name)
            return path

        except FileTooLargeError:
            # Non-retryable — propagate immediately
            raise

        except DownloadCancelled:
            # Non-retryable — propagate immediately, no backoff.
            logger.info("Download cancelled mid-transfer | url=%s", url[:80])
            raise

        except (DownloadError, requests.exceptions.RequestException, Exception) as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                wait = config.RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "Download attempt %d/%d failed: %s — retrying in %.1fs",
                    attempt, config.MAX_RETRIES, exc, wait, exc_info=True,
                )
                if cancel_event is not None:
                    if cancel_event.wait(timeout=wait):
                        logger.info(
                            "Download cancelled during retry backoff | url=%s", url[:80]
                        )
                        raise DownloadCancelled("Download cancelled by user") from exc
                else:
                    time.sleep(wait)
            else:
                logger.error(
                    "Download failed after %d attempts: %s | url=%s",
                    config.MAX_RETRIES, exc, url[:80], exc_info=True,
                )

    raise DownloadError(str(last_exc)) from last_exc


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

def _should_use_ytdlp(url: str, url_type: str) -> bool:
    """
    Return True if yt-dlp should be the primary download strategy.
    yt-dlp is preferred for video URLs and m3u8 streams.
    """
    if url_type == "video":
        return True
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


# ---------------------------------------------------------------------------
# Strategy 1: yt-dlp
# ---------------------------------------------------------------------------

def _download_with_ytdlp(
    url: str,
    task_num: int,
    total_tasks: int,
    caption: str,
    cancel_event: threading.Event | None = None,
) -> Path:
    """
    Download using yt-dlp. Handles m3u8 streams and direct video URLs.

    yt-dlp merges audio+video streams using ffmpeg and writes a single output file.
    Intermediate .part files and segment files are cleaned up in the finally block.

    Returns the Path to the merged output file.
    Raises FileTooLargeError or DownloadError.
    """
    # Determine output template — yt-dlp appends the real extension automatically
    tmp_stem = get_tmp_path("ytdlp_video").with_suffix("")
    output_template = str(tmp_stem) + ".%(ext)s"

    # Track the final file path discovered by the progress hook
    result: dict = {"path": None, "too_large": False, "cancelled": False}

    def _progress_hook(d: dict) -> None:
        """Called by yt-dlp on each progress event."""
        status = d.get("status")

        if status == "downloading":
            if cancel_event is not None and cancel_event.is_set():
                result["cancelled"] = True
                raise yt_dlp.utils.DownloadError("Cancelled by user — aborting")

            downloaded = d.get("downloaded_bytes", 0) or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")

            # Early size check during download
            if total and total > config.MAX_FILE_SIZE_BYTES:
                result["too_large"] = True
                raise yt_dlp.utils.DownloadError("File too large — aborting")

            progress_module.update(
                downloaded_bytes=downloaded,
                total_bytes=total,
            )

        elif status == "finished":
            filepath = d.get("filename") or d.get("info_dict", {}).get("_filename")
            if filepath:
                result["path"] = Path(filepath)

        elif status == "error":
            logger.warning("yt-dlp reported error status for: %s", url[:60])

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": False,
        "socket_timeout": _YTDLP_SOCKET_TIMEOUT,
        "retries": 0,           # We handle retries ourselves in download()
        "noplaylist": True,     # Never download playlists; single item only
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)",
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if result["cancelled"]:
                raise DownloadCancelled("Download cancelled by user")

            if result["too_large"]:
                raise FileTooLargeError(
                    f"File exceeds {format_size(config.MAX_FILE_SIZE_BYTES)} limit"
                )

            # Resolve the actual output file path
            final_path = _resolve_ytdlp_output(info, output_template, result.get("path"))

            if not final_path or not final_path.exists():
                raise DownloadError("yt-dlp finished but output file not found")

            # Post-download size check (in case total was not known during download)
            file_size = final_path.stat().st_size
            if file_size > config.MAX_FILE_SIZE_BYTES:
                delete_file(final_path)
                raise FileTooLargeError(
                    f"File size {format_size(file_size)} exceeds "
                    f"{format_size(config.MAX_FILE_SIZE_BYTES)} limit"
                )

            return final_path

    except FileTooLargeError:
        raise

    except DownloadCancelled:
        raise

    except yt_dlp.utils.DownloadError as exc:
        if result["cancelled"]:
            raise DownloadCancelled("Download cancelled by user") from exc
        logger.exception("yt-dlp download error | url=%s", url[:80])
        raise DownloadError(f"yt-dlp error: {exc}") from exc

    except Exception as exc:
        logger.exception("yt-dlp unexpected error | url=%s", url[:80])
        raise DownloadError(f"yt-dlp unexpected error: {exc}") from exc

    finally:
        _cleanup_ytdlp_tmp(str(tmp_stem))


def _resolve_ytdlp_output(
    info: dict | None,
    output_template: str,
    hook_path: Path | None,
) -> Path | None:
    """
    Determine the actual file yt-dlp wrote.

    Priority:
      1. Path reported by the progress hook (most reliable for merged files).
      2. Path constructed from info dict + output template.
      3. Glob for any file matching the template stem.
    """
    if hook_path and hook_path.exists():
        return hook_path

    if info:
        try:
            # yt-dlp replaces %(ext)s with the actual extension
            ext = info.get("ext") or "mp4"
            candidate = Path(output_template.replace("%(ext)s", ext))
            if candidate.exists():
                return candidate
        except Exception:
            pass

    # Fallback: glob for any file starting with the template stem
    stem = Path(output_template.replace(".%(ext)s", ""))
    parent = stem.parent
    prefix = stem.name
    matches = list(parent.glob(f"{prefix}.*"))
    # Exclude .part files (incomplete downloads)
    matches = [p for p in matches if not p.suffix == ".part"]
    if matches:
        return max(matches, key=lambda p: p.stat().st_size)

    return None


def _cleanup_ytdlp_tmp(stem_path: str) -> None:
    """
    Remove yt-dlp intermediate files: .part files, .ytdl files, and segment temps.
    Called in a finally block to ensure cleanup even on failure.
    """
    parent = Path(stem_path).parent
    prefix = Path(stem_path).name

    cleaned = 0
    for tmp_file in parent.glob(f"{prefix}*"):
        suffix = tmp_file.suffix.lower()
        if suffix in (".part", ".ytdl", ".temp", ".tmp") or ".f" in tmp_file.name:
            if delete_file(tmp_file):
                cleaned += 1

    if cleaned:
        logger.debug("Cleaned %d yt-dlp temp file(s)", cleaned)


# ---------------------------------------------------------------------------
# Strategy 2: requests streaming download
# ---------------------------------------------------------------------------

def _download_with_requests(
    url: str,
    url_type: str,
    task_num: int,
    total_tasks: int,
    caption: str,
    cancel_event: threading.Event | None = None,
) -> Path:
    """
    Download a file using requests with streaming (no full RAM loading).

    Steps:
      1. Send HEAD request to check Content-Length (skip if > 2 GB).
      2. Open a streaming GET request.
      3. Write chunks to disk, calling progress.update() every chunk.
      4. Verify the final file size > 0.

    Returns Path to the downloaded file.
    Raises FileTooLargeError or DownloadError.
    """
    # Step 1: pre-flight size check
    total_bytes = _head_content_length(url)
    if total_bytes is not None and total_bytes > config.MAX_FILE_SIZE_BYTES:
        raise FileTooLargeError(
            f"Remote file size {format_size(total_bytes)} exceeds "
            f"{format_size(config.MAX_FILE_SIZE_BYTES)} limit"
        )

    # Determine a safe output filename
    filename = _filename_from_url(url, url_type)
    output_path = get_tmp_path(filename)

    try:
        with requests.get(
            url,
            stream=True,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"},
            allow_redirects=True,
        ) as response:
            response.raise_for_status()

            # Prefer Content-Length from the GET response (may differ from HEAD)
            cl = response.headers.get("Content-Length")
            if cl and cl.isdigit():
                total_bytes = int(cl)
                if total_bytes > config.MAX_FILE_SIZE_BYTES:
                    raise FileTooLargeError(
                        f"Remote file size {format_size(total_bytes)} exceeds "
                        f"{format_size(config.MAX_FILE_SIZE_BYTES)} limit"
                    )

            downloaded = 0
            with open(output_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue

                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelled("Download cancelled by user")

                    fh.write(chunk)
                    downloaded += len(chunk)

                    # Live size guard: stop if we exceed limit mid-download
                    if downloaded > config.MAX_FILE_SIZE_BYTES:
                        raise FileTooLargeError(
                            f"Download exceeded {format_size(config.MAX_FILE_SIZE_BYTES)} limit"
                        )

                    progress_module.update(
                        downloaded_bytes=downloaded,
                        total_bytes=total_bytes,
                    )

        # Verify the file was written
        file_size = output_path.stat().st_size if output_path.exists() else 0
        if file_size == 0:
            raise DownloadError("Downloaded file is empty (0 bytes)")

        logger.info(
            "requests download complete | file=%s size=%s",
            output_path.name, format_size(file_size),
        )
        return output_path

    except FileTooLargeError:
        delete_file(output_path)
        raise

    except DownloadCancelled:
        delete_file(output_path)
        raise

    except requests.exceptions.Timeout as exc:
        delete_file(output_path)
        logger.exception("Request timed out | url=%s", url[:80])
        raise DownloadError(f"Request timed out: {exc}") from exc

    except requests.exceptions.HTTPError as exc:
        delete_file(output_path)
        logger.exception("HTTP error | url=%s", url[:80])
        raise DownloadError(f"HTTP error {exc.response.status_code}: {exc}") from exc

    except requests.exceptions.ConnectionError as exc:
        delete_file(output_path)
        logger.exception("Connection error | url=%s", url[:80])
        raise DownloadError(f"Connection error: {exc}") from exc

    except requests.exceptions.RequestException as exc:
        delete_file(output_path)
        logger.exception("Request error | url=%s", url[:80])
        raise DownloadError(f"Request error: {exc}") from exc

    except (OSError, IOError) as exc:
        delete_file(output_path)
        logger.exception("File write error | url=%s", url[:80])
        raise DownloadError(f"File write error: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _head_content_length(url: str) -> int | None:
    """
    Send a HEAD request and return Content-Length in bytes, or None if unavailable.
    Times out quickly — we don't want to slow down processing for a size check.
    """
    try:
        resp = requests.head(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"},
        )
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            return int(cl)
    except Exception as exc:
        logger.debug("HEAD request failed (non-fatal): %s", exc)
    return None


def _filename_from_url(url: str, url_type: str) -> str:
    """
    Derive a safe filename from the URL.
    Falls back to a type-based default if the URL path is not useful.

    Extension is inferred from url_type if not present in the URL.
    """
    _type_ext = {
        "video": ".mp4",
        "pdf": ".pdf",
        "html": ".html",
        "unknown": ".bin",
    }

    try:
        path = urlparse(url).path
        raw_name = os.path.basename(path)
        if raw_name and "." in raw_name:
            return sanitize_filename(raw_name, fallback="download")
    except Exception:
        pass

    ext = _type_ext.get(url_type, ".bin")
    return sanitize_filename(f"download{ext}", fallback="download.bin")
