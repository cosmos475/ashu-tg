"""
bot/parser/txt_parser.py — TXT file parser.

Parses an uploaded TXT file and returns a list of {caption, url} dicts.

Parsing strategy:
  1. Read all lines, strip trailing whitespace.
  2. Reconstruct wrapped URLs — if a non-URL line looks like a URL continuation
     (no spaces, starts with common URL path chars), join it to the previous line.
  3. Scan every (possibly joined) line for an http:// or https:// occurrence.
  4. If the URL starts at the beginning of the line → caption comes from the
     nearest non-empty, non-URL line above.
  5. If the URL appears mid-line → everything before it is the caption.
  6. Validate every URL with urllib.parse before including it.
  7. Fallback caption: "No Caption".

Edge cases handled:
  - Blank lines between pairs (ignored safely)
  - URL wrapped across two lines (rejoined)
  - Two consecutive URLs (each gets "No Caption")
  - "http" appearing inside a normal word (e.g. github.com) — rejected by
    requiring the URL to start with http:// or https:// (scheme required)
  - Empty file or file with zero valid URLs
  - Duplicate URLs (flagged in summary but still included; dedup is caller's choice)
  - Captions longer than MAX_CAPTION_LENGTH (truncated with warning)
  - BOM / mixed encodings (handled via utf-8-sig → utf-8 fallback)
"""

import logging
import re
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

# Regex: find http:// or https:// anywhere in a string.
# Captures from the scheme to the end of the line (greedy).
_URL_RE = re.compile(r'https?://\S+')

# Characters that commonly appear in wrapped URL continuations.
# A continuation line has no spaces and consists only of URL-safe characters.
_URL_CONTINUATION_RE = re.compile(r'^[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+$')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_txt_file(filepath: str) -> tuple[list[dict[str, str]], dict[str, int]]:
    """
    Parse a TXT file and return extracted URL/caption pairs plus a summary.

    Args:
        filepath: Absolute path to the downloaded TXT file.

    Returns:
        A tuple of:
          - items: list of dicts, each with keys 'caption' (str) and 'url' (str).
          - summary: dict with keys 'total', 'valid', 'invalid', 'duplicate'.

    Raises:
        ValueError: If the file is empty or contains no valid URLs.
        OSError:    If the file cannot be read.
    """
    raw_lines = _read_lines(filepath)

    if not raw_lines:
        raise ValueError("The uploaded TXT file is empty.")

    # Step 1: reconstruct wrapped URLs
    joined_lines = _rejoin_wrapped_urls(raw_lines)

    # Step 2: extract (caption, url) pairs
    items = _extract_pairs(joined_lines)

    if not items:
        raise ValueError("No valid URLs were found in the uploaded file.")

    # Step 3: build summary
    seen_urls: set[str] = set()
    duplicates = 0
    for item in items:
        url = item["url"]
        if url in seen_urls:
            duplicates += 1
        seen_urls.add(url)

    summary = {
        "total": len(items),
        "valid": len(items),
        "duplicate": duplicates,
    }

    logger.info(
        "Parsed %s | total=%d duplicate=%d",
        filepath, summary["total"], summary["duplicate"],
    )
    return items, summary


# ---------------------------------------------------------------------------
# Internal: file reading
# ---------------------------------------------------------------------------

def _read_lines(filepath: str) -> list[str]:
    """
    Read all lines from the file.
    Tries UTF-8-with-BOM first (common on Windows), falls back to UTF-8,
    then to Latin-1 (which never fails — safe last resort).
    Returns lines with trailing whitespace stripped; blank lines preserved
    as empty strings (needed for caption detection).
    """
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(filepath, encoding=encoding) as fh:
                lines = [line.rstrip() for line in fh]
            logger.debug("Read %d lines from %s (encoding=%s)", len(lines), filepath, encoding)
            return lines
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            logger.error("Cannot read file %s: %s", filepath, exc)
            raise

    # Should never reach here (latin-1 is a universal fallback)
    raise OSError(f"Cannot decode file: {filepath}")


# ---------------------------------------------------------------------------
# Internal: URL wrapping reconstruction
# ---------------------------------------------------------------------------

def _rejoin_wrapped_urls(lines: list[str]) -> list[str]:
    """
    Detect and rejoin URLs that are visually wrapped across two lines.

    A line is considered a URL continuation if ALL of these are true:
      - The previous non-empty line contains a URL (or looks like the start of one)
      - The current line has no spaces
      - The current line matches URL-safe character pattern
      - The current line does NOT start with http:// or https://

    Example input:
        https://example.com/very/long/path?token=abc
        def456xyz==
    becomes:
        https://example.com/very/long/path?token=abcdef456xyz==

    This handles base64-encoded tokens and query strings split across lines.
    """
    result: list[str] = []

    for line in lines:
        if not line:
            # Preserve blank lines for caption detection
            result.append(line)
            continue

        # Check if this line could be a continuation of the previous URL line
        if (
            result                                          # there is a previous line
            and _URL_RE.search(result[-1])                  # previous line has a URL
            and not _URL_RE.match(line)                     # this line doesn't start a new URL
            and " " not in line                             # no spaces (URL-like)
            and _URL_CONTINUATION_RE.match(line)            # only URL-safe chars
            and len(line) > 8                               # avoid merging very short words
        ):
            # Merge into the previous line
            result[-1] = result[-1] + line
            logger.debug("Rejoined wrapped URL: ...%s", line[:40])
        else:
            result.append(line)

    return result


# ---------------------------------------------------------------------------
# Internal: pair extraction
# ---------------------------------------------------------------------------

def _extract_pairs(lines: list[str]) -> list[dict[str, str]]:
    """
    Scan lines for URLs and pair each with its caption.

    For every URL found (via _URL_RE):
      - If the URL starts at index 0 of the line → look upward for caption.
      - If the URL appears mid-line → text before the URL is the caption.
    """
    items: list[dict[str, str]] = []

    for i, line in enumerate(lines):
        match = _URL_RE.search(line)
        if not match:
            continue

        url = match.group(0).rstrip(".,;:!?)")  # strip trailing punctuation
        url = _clean_url(url)

        if not _is_valid_url(url):
            logger.warning("Skipping invalid URL: %r", url)
            continue

        # Determine caption
        prefix = line[: match.start()].strip()

        if prefix:
            # URL is mid-line → prefix is the caption
            caption = prefix
        else:
            # URL is at start of line → look upward
            caption = _find_caption_above(lines, i)

        # Sanitize and enforce length limit
        caption = _sanitize_caption(caption)

        items.append({"caption": caption, "url": url})

    return items


def _find_caption_above(lines: list[str], url_line_index: int) -> str:
    """
    Walk upward from url_line_index to find the nearest non-empty,
    non-URL line. Returns "No Caption" if none found.
    """
    for j in range(url_line_index - 1, -1, -1):
        candidate = lines[j].strip()
        if not candidate:
            continue  # skip blank lines
        if _URL_RE.search(candidate):
            break  # hit another URL — stop searching
        return candidate

    return "No Caption"


# ---------------------------------------------------------------------------
# Internal: URL validation and cleaning
# ---------------------------------------------------------------------------

def _clean_url(url: str) -> str:
    """Remove common trailing punctuation that is not part of the URL."""
    # Repeatedly strip trailing chars that are unlikely to end a real URL
    while url and url[-1] in ".,;:!?)>\"'":
        url = url[:-1]
    return url


def _is_valid_url(url: str) -> bool:
    """
    Validate that a URL has a proper scheme and netloc.
    Rejects bare domains, mailto: links, and malformed strings.
    """
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
            and "." in parsed.netloc   # requires at least one dot in host
        )
    except Exception:
        return False


def _sanitize_caption(caption: str) -> str:
    """
    Enforce caption length limit and strip unsafe control characters.
    Truncates with '…' if over MAX_CAPTION_LENGTH.
    """
    # Remove control characters (keep printable + standard whitespace)
    caption = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', caption).strip()

    if not caption:
        return "No Caption"

    if len(caption) > config.MAX_CAPTION_LENGTH:
        logger.warning(
            "Caption truncated from %d to %d chars", len(caption), config.MAX_CAPTION_LENGTH
        )
        caption = caption[: config.MAX_CAPTION_LENGTH - 1] + "…"

    return caption
