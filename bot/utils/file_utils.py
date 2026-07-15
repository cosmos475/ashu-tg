"""
bot/utils/file_utils.py — File system utilities.

Responsibilities:
  - sanitize_filename(): make arbitrary strings safe for use as filenames.
  - get_tmp_path(): return a safe absolute path inside the tmp/ directory.
  - delete_file(): safely delete a file; never raises, always logs.
  - ensure_dir(): create a directory if it doesn't exist.

All functions are pure utilities with no side effects beyond the filesystem.
They are called by downloader.py and uploader.py.
"""

import logging
import os
import re
import uuid
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Characters that are unsafe in filenames across Linux / Windows / macOS
# ---------------------------------------------------------------------------
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Maximum filename length (bytes). Most Linux filesystems allow 255.
# We use a conservative 180 to leave room for extensions and suffixes.
_MAX_FILENAME_LENGTH = 180


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, fallback: str = "file") -> str:
    """
    Convert an arbitrary string into a safe, filesystem-friendly filename.

    Steps:
      1. Replace unsafe characters with underscores.
      2. Collapse multiple consecutive underscores/spaces into one.
      3. Strip leading/trailing dots, spaces, and underscores.
      4. Enforce maximum length (truncate, preserving extension if present).
      5. If the result is empty, use the fallback string.

    Args:
        name:     Input string (e.g. a URL filename or caption).
        fallback: Used when the sanitized result would be empty.

    Returns:
        A clean, non-empty filename string (without directory path).

    Example:
        sanitize_filename("My Video: Part 1/2!") → "My_Video__Part_1_2"
        sanitize_filename("") → "file"
    """
    if not name or not name.strip():
        return fallback

    # Step 1: replace unsafe characters
    safe = _UNSAFE_CHARS_RE.sub("_", name)

    # Step 2: collapse whitespace and repeated underscores
    safe = re.sub(r'[\s_]+', '_', safe)

    # Step 3: strip leading/trailing junk
    safe = safe.strip("._- ")

    # Step 4: enforce length — preserve extension if present
    if len(safe) > _MAX_FILENAME_LENGTH:
        stem, _, ext = safe.rpartition(".")
        if ext and len(ext) <= 10:
            # Has a recognisable extension — preserve it
            max_stem = _MAX_FILENAME_LENGTH - len(ext) - 1
            safe = stem[:max_stem] + "." + ext
        else:
            safe = safe[:_MAX_FILENAME_LENGTH]

    # Step 5: fallback for empty result
    if not safe:
        return fallback

    return safe


def get_tmp_path(filename: str) -> Path:
    """
    Return an absolute Path inside config.TMP_DIR for a given filename.

    The filename is sanitized automatically.
    A short UUID prefix is prepended to prevent collisions between
    concurrent or sequential tasks that share the same filename.

    Args:
        filename: Desired filename (may be unsanitized).

    Returns:
        Absolute Path object pointing to tmp/<uuid>_<sanitized_filename>.
        The file does not yet exist; the caller is responsible for creating it.
    """
    safe_name = sanitize_filename(filename)
    unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    path = config.TMP_DIR / unique_name

    # Ensure TMP_DIR exists (Render ephemeral fs may reset it)
    ensure_dir(config.TMP_DIR)

    return path


def delete_file(path: str | Path) -> bool:
    """
    Safely delete a file. Never raises an exception.

    Args:
        path: Path to the file to delete (str or Path).

    Returns:
        True if the file was deleted or did not exist.
        False if deletion failed due to a permissions or OS error.
    """
    target = Path(path)

    if not target.exists():
        logger.debug("delete_file: file not found (already deleted?): %s", target)
        return True

    try:
        target.unlink()
        logger.debug("Deleted tmp file: %s", target.name)
        return True

    except PermissionError as exc:
        logger.error("delete_file: permission denied for %s: %s", target, exc)
        return False

    except OSError as exc:
        logger.error("delete_file: OS error deleting %s: %s", target, exc)
        return False


def ensure_dir(path: str | Path) -> None:
    """
    Create a directory (and any missing parents) if it does not already exist.
    Safe to call multiple times — uses exist_ok=True.

    Args:
        path: Directory path to create.
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("ensure_dir: failed to create %s: %s", target, exc)
        raise


def get_file_size(path: str | Path) -> int:
    """
    Return the size of a file in bytes, or 0 if it does not exist / is unreadable.

    Args:
        path: Path to the file.

    Returns:
        File size in bytes, or 0 on error.
    """
    try:
        return os.path.getsize(path)
    except OSError as exc:
        logger.debug("get_file_size: cannot stat %s: %s", path, exc)
        return 0


def format_size(size_bytes: int) -> str:
    """
    Format a byte count into a human-readable string.

    Examples:
        512         → "512 B"
        1536        → "1.5 KB"
        1_048_576   → "1.0 MB"
        1_073_741_824 → "1.0 GB"

    Args:
        size_bytes: Non-negative integer byte count.

    Returns:
        Human-readable size string.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    else:
        return f"{size_bytes / 1024 ** 3:.2f} GB"
