"""
bot/services/uploader.py — Telegram file uploader (Pyrogram MTProto).

Public interface unchanged from the HTTP Bot API version:
  set_bot(bot_instance)  — kept for processor.py compatibility; no-op for
                            telebot, also starts the Pyrogram client.
  upload(path, url_type, caption, channel_id) -> None
  class UploadError(Exception)

Backend changed: uploads now go through Pyrogram's MTProto client, which
chunks large files internally (no full-file RAM buffering) and supports
uploads up to ~2 GB — well past the 50 MB HTTP Bot API multipart limit.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path

import telebot
from pyrogram.errors import FloodWait, RPCError

import config
from bot.services import pyrogram_client
from bot.utils.file_utils import format_size, get_file_size

logger = logging.getLogger(__name__)

_UPLOAD_PROGRESS_INTERVAL = 5.0

# Pyrogram RPCError subclasses that should never be retried.
_PERMANENT_ERROR_NAMES = {
    "PeerIdInvalid", "ChatAdminRequired", "ChannelPrivate",
    "UserIsBlocked", "ChatWriteForbidden",
}


class UploadError(Exception):
    """Raised when an upload fails after all retries or due to a permanent error."""


class UploadCancelled(UploadError):
    """Raised when an upload is aborted because /cancel was requested."""


def set_bot(bot_instance: telebot.TeleBot) -> None:
    """
    Kept for interface compatibility with processor.py.
    Also ensures the Pyrogram client + event loop are running (idempotent).
    """
    pyrogram_client.start()


def upload(
    path: Path,
    url_type: str,
    caption: str,
    channel_id: int,
    cancel_event: threading.Event | None = None,
) -> None:
    """
    Upload a local file to the Telegram channel via Pyrogram MTProto.
    cancel_event: if set, the in-flight task is cancelled immediately and
    UploadCancelled is raised without retrying.

    Raises:
        UploadError on permanent failure or exhausted retries.
        UploadCancelled if cancel_event was set.
    """
    if not path.exists():
        raise UploadError(f"File not found for upload: {path}")

    if cancel_event is not None and cancel_event.is_set():
        raise UploadCancelled("Upload cancelled before starting")

    file_size = get_file_size(path)
    safe_caption = _truncate_caption(caption)

    logger.info(
        "Upload started | type=%s file=%s size=%s channel=%s",
        url_type, path.name, format_size(file_size), channel_id,
    )

    last_exc: Exception = UploadError("Unknown upload error")

    for attempt in range(1, config.MAX_RETRIES + 1):

        if cancel_event is not None and cancel_event.is_set():
            logger.info("Upload cancelled before attempt %d | file=%s", attempt, path.name)
            raise UploadCancelled("Upload cancelled by user")

        try:
            logger.debug(
                "DIAG pre-run_coro | thread=%s client_started=%s loop_exists=%s",
                threading.current_thread().name,
                pyrogram_client._client is not None,
                pyrogram_client._loop is not None,
            )

            logger.debug("DIAG creating _send() coroutine | file=%s", path.name)
            coro = _send(path, url_type, safe_caption, channel_id, file_size)
            logger.debug("DIAG _send() coroutine object created | file=%s", path.name)

            logger.debug(
                "DIAG calling run_coro() | thread=%s file=%s",
                threading.current_thread().name, path.name,
            )
            pyrogram_client.run_coro(
                coro,
                cancel_event=cancel_event,
            )
            logger.debug(
                "DIAG run_coro() returned | thread=%s file=%s",
                threading.current_thread().name, path.name,
            )
            logger.info(
                "Upload complete | type=%s file=%s size=%s attempt=%d",
                url_type, path.name, format_size(file_size), attempt,
            )
            return

        except asyncio.CancelledError as exc:
            logger.info("Upload cancelled mid-transfer | file=%s", path.name)
            raise UploadCancelled("Upload cancelled by user") from exc

        except FloodWait as exc:
            wait = exc.value
            if wait <= 60:
                logger.warning(
                    "FloodWait %ds on upload attempt %d/%d | file=%s",
                    wait, attempt, config.MAX_RETRIES, path.name,
                    exc_info=True,
                )
                if cancel_event is not None:
                    if cancel_event.wait(timeout=wait + 1):
                        raise UploadCancelled("Upload cancelled by user")
                else:
                    time.sleep(wait + 1)
                last_exc = exc
                continue
            logger.error("FloodWait too long (%ds) | file=%s", wait, path.name, exc_info=True)
            raise UploadError(f"FloodWait too long ({wait}s)") from exc

        except RPCError as exc:
            if type(exc).__name__ in _PERMANENT_ERROR_NAMES:
                logger.error(
                    "Permanent Pyrogram error on upload | file=%s: %s",
                    path.name, exc, exc_info=True,
                )
                raise UploadError(f"Permanent error: {exc}") from exc

            last_exc = exc
            logger.exception(
                "Pyrogram RPC error attempt %d/%d | file=%s",
                attempt, config.MAX_RETRIES, path.name,
            )

        except (OSError, IOError) as exc:
            last_exc = exc
            logger.exception(
                "File read error attempt %d/%d | file=%s",
                attempt, config.MAX_RETRIES, path.name,
            )

        except Exception as exc:
            last_exc = exc
            logger.exception(
                "Unexpected error attempt %d/%d | file=%s",
                attempt, config.MAX_RETRIES, path.name,
            )

        if attempt < config.MAX_RETRIES:
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.info("Retrying upload in %.1fs …", wait)
            if cancel_event is not None:
                if cancel_event.wait(timeout=wait):
                    logger.info("Upload cancelled during retry backoff | file=%s", path.name)
                    raise UploadCancelled("Upload cancelled by user")
            else:
                time.sleep(wait)

    logger.error(
        "Upload failed after %d attempts | file=%s: %s",
        config.MAX_RETRIES, path.name, last_exc, exc_info=last_exc,
    )
    raise UploadError(
        f"Upload failed after {config.MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


async def _send(
    path: Path,
    url_type: str,
    caption: str,
    channel_id: int,
    file_size: int,
) -> None:
    """
    Coroutine run inside the Pyrogram event loop.
    Pyrogram streams the file from disk internally in chunks — never
    loads the full file into RAM.
    """
    from bot.services import progress as progress_module

    client = pyrogram_client.get_client()

    last_progress_time = [0.0]

    def _progress(current: int, total: int) -> None:
        # TEMP DIAGNOSTIC: log every callback invocation (unthrottled) to
        # confirm whether Pyrogram ever calls this at all.
        logger.debug(
            "DIAG _progress called | current=%s total=%s file=%s",
            current, total, path.name,
        )
        now = time.monotonic()
        if now - last_progress_time[0] >= _UPLOAD_PROGRESS_INTERVAL:
            last_progress_time[0] = now
            try:
                progress_module.update_upload(uploaded_bytes=current, total_bytes=total)
            except Exception:
                logger.debug("Upload progress update failed (non-fatal)", exc_info=True)

    logger.debug(
        "DIAG entering send_%s | file=%s size=%s channel=%s",
        "video" if url_type == "video" else "document",
        path.name, file_size, channel_id,
    )

    if url_type == "video":
        await client.send_video(
            chat_id=channel_id,
            video=str(path),
            caption=caption,
            supports_streaming=True,
            progress=_progress,
        )
    else:
        await client.send_document(
            chat_id=channel_id,
            document=str(path),
            caption=caption,
            progress=_progress,
        )

    logger.debug(
        "DIAG returned from send_%s | file=%s",
        "video" if url_type == "video" else "document",
        path.name,
    )


def _truncate_caption(caption: str) -> str:
    """HTML-escape and truncate caption to MAX_CAPTION_LENGTH."""
    safe = (
        caption
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if len(safe) > config.MAX_CAPTION_LENGTH:
        safe = safe[: config.MAX_CAPTION_LENGTH - 1] + "…"
    return safe
