"""
bot/services/detector.py — URL type detector.

Determines whether a URL points to a Video, PDF, HTML page, or unknown type.

Detection strategy (in order):
  1. Send an HTTP HEAD request to the URL and inspect the Content-Type header.
  2. If HEAD fails or returns no useful Content-Type, fall back to the file extension.
  3. If neither strategy resolves the type, return 'unknown'.

This module is stateless — pure functions only, no side effects.
It is called twice:
  - During TXT parsing (for the pre-processing summary shown to the owner).
  - During processing (to confirm type before upload).

Content-Type mapping:
  video/*                → "video"
  application/mp4        → "video"
  application/x-mpegURL  → "video"   (m3u8 streams)
  vnd.apple.mpegurl      → "video"   (m3u8 streams)
  application/pdf        → "pdf"
  text/html              → "html"
  application/xhtml+xml  → "html"
  anything else          → "unknown" (extension fallback attempted)
"""

import logging
from urllib.parse import urlparse

import requests
import requests.exceptions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type constants — used as literals across the codebase
# ---------------------------------------------------------------------------
TYPE_VIDEO = "video"
TYPE_PDF = "pdf"
TYPE_HTML = "html"
TYPE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Content-Type prefix → type mapping (checked via str.startswith)
# ---------------------------------------------------------------------------
_CONTENT_TYPE_MAP: list[tuple[str, str]] = [
    # Video types
    ("video/", TYPE_VIDEO),
    ("application/mp4", TYPE_VIDEO),
    ("application/x-mpegurl", TYPE_VIDEO),       # m3u8
    ("application/vnd.apple.mpegurl", TYPE_VIDEO),  # m3u8 (Apple)
    ("application/octet-stream", TYPE_UNKNOWN),  # ambiguous — fall through to extension
    # Document types
    ("application/pdf", TYPE_PDF),
    # HTML types
    ("text/html", TYPE_HTML),
    ("application/xhtml+xml", TYPE_HTML),
]

# ---------------------------------------------------------------------------
# File extension → type mapping (fallback)
# ---------------------------------------------------------------------------
_EXTENSION_MAP: dict[str, str] = {
    # Video
    ".mp4": TYPE_VIDEO,
    ".m3u8": TYPE_VIDEO,
    ".mkv": TYPE_VIDEO,
    ".avi": TYPE_VIDEO,
    ".mov": TYPE_VIDEO,
    ".wmv": TYPE_VIDEO,
    ".flv": TYPE_VIDEO,
    ".webm": TYPE_VIDEO,
    ".ts": TYPE_VIDEO,
    ".m2ts": TYPE_VIDEO,
    ".mpeg": TYPE_VIDEO,
    ".mpg": TYPE_VIDEO,
    # PDF
    ".pdf": TYPE_PDF,
    # HTML
    ".html": TYPE_HTML,
    ".htm": TYPE_HTML,
    ".xhtml": TYPE_HTML,
}

# HEAD request timeout — short, we only need headers not the full body
_HEAD_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_url_type(url: str) -> str:
    """
    Detect the content type of a URL.

    Args:
        url: A validated http:// or https:// URL string.

    Returns:
        One of: 'video', 'pdf', 'html', 'unknown'.
    """
    # Strategy 1: HTTP HEAD request
    detected = _detect_via_head(url)
    if detected != TYPE_UNKNOWN:
        logger.debug("Type via HEAD: %s → %s", url[:60], detected)
        return detected

    # Strategy 2: file extension fallback
    detected = _detect_via_extension(url)
    if detected != TYPE_UNKNOWN:
        logger.debug("Type via extension: %s → %s", url[:60], detected)
        return detected

    logger.info("Type unknown for URL: %s", url[:80])
    return TYPE_UNKNOWN


def detect_all(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Run detect_url_type for a list of {caption, url} dicts.
    Adds a 'url_type' key to each dict in-place and returns the list.

    Used during the pre-processing summary phase.
    Network errors per URL are caught internally; type defaults to 'unknown'.
    """
    for item in items:
        try:
            item["url_type"] = detect_url_type(item["url"])
        except Exception as exc:
            logger.warning("detect_all: error for %s: %s", item.get("url", "?")[:60], exc)
            item["url_type"] = TYPE_UNKNOWN
    return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_via_head(url: str) -> str:
    """
    Send an HTTP HEAD request and parse the Content-Type header.
    Returns TYPE_UNKNOWN if the request fails or the type is not recognised.

    Uses a browser-like User-Agent to avoid 403s from strict servers.
    Follows up to 5 redirects.
    """
    try:
        response = requests.head(
            url,
            timeout=_HEAD_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; TelegramBot/1.0)"
                ),
            },
        )

        content_type = response.headers.get("Content-Type", "").lower().strip()
        if not content_type:
            return TYPE_UNKNOWN

        # Strip parameters like '; charset=utf-8'
        content_type = content_type.split(";")[0].strip()

        for prefix, url_type in _CONTENT_TYPE_MAP:
            if content_type.startswith(prefix):
                # application/octet-stream is ambiguous — fall through to extension
                if url_type == TYPE_UNKNOWN:
                    break
                return url_type

    except requests.exceptions.Timeout:
        logger.debug("HEAD timeout for: %s", url[:60])
    except requests.exceptions.SSLError:
        logger.debug("HEAD SSL error for: %s", url[:60])
    except requests.exceptions.ConnectionError:
        logger.debug("HEAD connection error for: %s", url[:60])
    except requests.exceptions.RequestException as exc:
        logger.debug("HEAD request error for %s: %s", url[:60], exc)

    return TYPE_UNKNOWN


def _detect_via_extension(url: str) -> str:
    """
    Extract the file extension from the URL path and look it up.
    Strips query strings and fragments before extracting the extension.

    Returns TYPE_UNKNOWN if no matching extension is found.
    """
    try:
        path = urlparse(url).path.lower()
        # Find the last dot after the last slash
        last_segment = path.rsplit("/", 1)[-1]
        if "." in last_segment:
            ext = "." + last_segment.rsplit(".", 1)[-1]
            # Strip any query-string remnants
            ext = ext.split("?")[0].split("&")[0]
            return _EXTENSION_MAP.get(ext, TYPE_UNKNOWN)
    except Exception as exc:
        logger.debug("Extension detection error for %s: %s", url[:60], exc)

    return TYPE_UNKNOWN
