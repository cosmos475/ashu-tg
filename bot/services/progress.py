"""
bot/services/progress.py — Progress message manager.

Responsibilities:
  - Send the initial download progress message in the owner's private chat.
  - Edit that message at most once every PROGRESS_UPDATE_INTERVAL seconds.
  - Handle Telegram FloodWait exceptions by sleeping and retrying.
  - Delete the progress message after a download completes or fails.

Design:
  - All state (message_id, chat_id, last_edit timestamp) is held in a single
    _ProgressState dataclass instance at module level.
  - No threading locks needed: the processing thread is the only writer, and
    the webhook thread only reads is_processing (in processor.py).
  - Every public function is safe to call even if the message was never sent
    or was already deleted.
"""

import logging
import time
from dataclasses import dataclass, field

import telebot
import telebot.apihelper

import config

logger = logging.getLogger(__name__)

# Minimum seconds between upload progress message edits
_UPLOAD_PROGRESS_INTERVAL = 5.0

# Bot instance — imported lazily inside functions to avoid circular imports
# (app.py imports handlers which may import this module).
_bot: telebot.TeleBot | None = None


def set_bot(bot_instance: telebot.TeleBot) -> None:
    """
    Inject the bot instance. Called once from processor.py before processing starts.
    Avoids circular import: app → handlers → processor → progress → app.
    """
    global _bot
    _bot = bot_instance


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

@dataclass
class _ProgressState:
    chat_id: int | None = None
    message_id: int | None = None
    last_edit_time: float = field(default_factory=lambda: 0.0)
    current_task_num: int = 0
    total_tasks: int = 0
    current_caption: str = ""
    phase: str = "download"   # "download" or "upload"


_state = _ProgressState()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init(chat_id: int, total_tasks: int) -> None:
    """
    Prepare the progress tracker for a new processing session.
    Resets all state. Must be called before the first send().

    Args:
        chat_id:     Owner's private chat ID.
        total_tasks: Total number of URLs in this session.
    """
    global _state
    _state = _ProgressState(
        chat_id=chat_id,
        total_tasks=total_tasks,
    )
    logger.debug("Progress tracker initialised | chat_id=%s total=%d", chat_id, total_tasks)


def send(task_num: int, caption: str) -> None:
    """
    Send the initial progress message for a new task.
    Stores the returned message_id for future edits.

    Args:
        task_num: 1-based index of the current task.
        caption:  Caption/title of the current URL being downloaded.
    """
    if _bot is None or _state.chat_id is None:
        return

    _state.current_task_num = task_num
    _state.current_caption = caption
    _state.last_edit_time = 0.0  # reset throttle for new task

    text = _format_message(task_num, _state.total_tasks, caption)

    try:
        msg = _safe_send(_state.chat_id, text)
        if msg:
            _state.message_id = msg.message_id
            logger.debug("Progress message sent | message_id=%s", _state.message_id)
    except Exception as exc:
        logger.warning("Failed to send progress message: %s", exc)
        _state.message_id = None


def update(downloaded_bytes: int, total_bytes: int | None) -> None:
    """
    Edit the progress message with current download progress.
    Throttled: silently skipped if called within PROGRESS_UPDATE_INTERVAL seconds
    of the last edit. This prevents Telegram FloodWait from triggering.

    Args:
        downloaded_bytes: Bytes downloaded so far.
        total_bytes:      Total file size in bytes, or None if unknown.
    """
    if _bot is None or _state.message_id is None or _state.chat_id is None:
        return

    now = time.monotonic()
    if now - _state.last_edit_time < config.PROGRESS_UPDATE_INTERVAL:
        return  # Throttled — too soon to edit again

    text = _format_message(
        _state.current_task_num,
        _state.total_tasks,
        _state.current_caption,
        downloaded_bytes=downloaded_bytes,
        total_bytes=total_bytes,
    )

    success = _safe_edit(_state.chat_id, _state.message_id, text)
    if success:
        _state.last_edit_time = now


def delete() -> None:
    """
    Delete the progress message from the chat.
    Called after a download completes or fails to keep the chat clean.
    Safe to call even if the message was never sent or already deleted.
    """
    if _bot is None or _state.message_id is None or _state.chat_id is None:
        return

    try:
        _bot.delete_message(_state.chat_id, _state.message_id)
        logger.debug("Progress message deleted | message_id=%s", _state.message_id)
    except telebot.apihelper.ApiTelegramException as exc:
        # Message may already be deleted or too old — not an error
        logger.debug("delete_message ignored: %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error deleting progress message: %s", exc)
    finally:
        _state.message_id = None


def send_upload(task_num: int, caption: str, file_size: int) -> None:
    """
    Switch the progress message from download phase to upload phase.
    Edits the existing message in place (no new message sent).
    Called by uploader.py at the start of a large file upload.

    Args:
        task_num:  1-based task index.
        caption:   Caption of the item being uploaded.
        file_size: Total file size in bytes (for the progress display).
    """
    if _bot is None or _state.chat_id is None:
        return

    _state.phase = "upload"
    _state.current_task_num = task_num
    _state.current_caption = caption
    _state.last_edit_time = 0.0  # reset throttle

    text = _format_upload_message(task_num, _state.total_tasks, caption, 0, file_size)

    if _state.message_id:
        # Reuse existing progress message — edit it to upload phase
        _safe_edit(_state.chat_id, _state.message_id, text)
    else:
        # No existing message — send a new one
        msg = _safe_send(_state.chat_id, text)
        if msg:
            _state.message_id = msg.message_id


def update_upload(uploaded_bytes: int, total_bytes: int) -> None:
    """
    Edit the progress message with current upload progress.
    Throttled to _UPLOAD_PROGRESS_INTERVAL seconds between edits.

    Args:
        uploaded_bytes: Bytes sent so far.
        total_bytes:    Total file size in bytes.
    """
    if _bot is None or _state.message_id is None or _state.chat_id is None:
        return

    now = time.monotonic()
    if now - _state.last_edit_time < _UPLOAD_PROGRESS_INTERVAL:
        return

    text = _format_upload_message(
        _state.current_task_num,
        _state.total_tasks,
        _state.current_caption,
        uploaded_bytes,
        total_bytes,
    )
    success = _safe_edit(_state.chat_id, _state.message_id, text)
    if success:
        _state.last_edit_time = now


# ---------------------------------------------------------------------------
# Internal: Telegram API wrappers with FloodWait handling
# ---------------------------------------------------------------------------

_MAX_FLOOD_WAIT_RETRIES = 3


def _safe_send(chat_id: int, text: str) -> telebot.types.Message | None:
    """
    Send a message, retrying on FloodWait up to _MAX_FLOOD_WAIT_RETRIES times.
    Returns the sent Message object or None on failure.
    """
    for attempt in range(1, _MAX_FLOOD_WAIT_RETRIES + 1):
        try:
            return _bot.send_message(chat_id, text, parse_mode="HTML")

        except telebot.apihelper.ApiTelegramException as exc:
            wait = _extract_flood_wait(exc)
            if wait and attempt < _MAX_FLOOD_WAIT_RETRIES:
                logger.warning("FloodWait on send: sleeping %ds (attempt %d)", wait, attempt)
                time.sleep(wait + 1)
            else:
                logger.warning("send_message failed: %s", exc)
                return None

        except Exception as exc:
            logger.warning("send_message unexpected error: %s", exc)
            return None

    return None


def _safe_edit(chat_id: int, message_id: int, text: str) -> bool:
    """
    Edit a message, retrying on FloodWait.
    Returns True on success, False on failure (does not raise).
    """
    for attempt in range(1, _MAX_FLOOD_WAIT_RETRIES + 1):
        try:
            _bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML")
            return True

        except telebot.apihelper.ApiTelegramException as exc:
            # "message is not modified" — content unchanged, not an error
            if "message is not modified" in str(exc).lower():
                return True

            wait = _extract_flood_wait(exc)
            if wait and attempt < _MAX_FLOOD_WAIT_RETRIES:
                logger.warning("FloodWait on edit: sleeping %ds (attempt %d)", wait, attempt)
                time.sleep(wait + 1)
            else:
                logger.debug("edit_message_text failed (non-fatal): %s", exc)
                return False

        except Exception as exc:
            logger.debug("edit_message_text unexpected error: %s", exc)
            return False

    return False


def _extract_flood_wait(exc: telebot.apihelper.ApiTelegramException) -> int | None:
    """
    Parse the retry_after value from a Telegram FloodWait (429) exception.
    Returns the number of seconds to wait, or None if not a FloodWait error.
    """
    try:
        if exc.result_json and exc.result_json.get("error_code") == 429:
            params = exc.result_json.get("parameters", {})
            return int(params.get("retry_after", 5))
    except Exception:
        pass
    # Fallback: parse from string representation
    if "retry after" in str(exc).lower():
        import re
        match = re.search(r'retry after (\d+)', str(exc), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Internal: message formatting
# ---------------------------------------------------------------------------

def _format_message(
    task_num: int,
    total_tasks: int,
    caption: str,
    downloaded_bytes: int | None = None,
    total_bytes: int | None = None,
) -> str:
    """
    Build the progress message text.

    Format (with progress):
        ⬇️ Downloading [45 / 300]
        My Video Caption

        Progress: 67% | 134.2 MB / 200.0 MB

    Format (initial, no progress yet):
        ⬇️ Downloading [45 / 300]
        My Video Caption

        Starting download…
    """
    from bot.utils.file_utils import format_size  # local import avoids circular dep

    header = f"⬇️ <b>Downloading [{task_num} / {total_tasks}]</b>"
    # Escape HTML special chars in caption to prevent parse errors
    safe_caption = _escape_html(caption)

    if downloaded_bytes is not None and total_bytes and total_bytes > 0:
        pct = min(int(downloaded_bytes / total_bytes * 100), 100)
        dl_str = format_size(downloaded_bytes)
        total_str = format_size(total_bytes)
        progress_line = f"Progress: <b>{pct}%</b> | {dl_str} / {total_str}"
    elif downloaded_bytes is not None:
        dl_str = format_size(downloaded_bytes)
        progress_line = f"Downloaded: <b>{dl_str}</b> (total size unknown)"
    else:
        progress_line = "Starting download…"

    return f"{header}\n{safe_caption}\n\n{progress_line}"


def _escape_html(text: str) -> str:
    """Escape the minimal HTML entities needed for Telegram's HTML parse mode."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_upload_message(
    task_num: int,
    total_tasks: int,
    caption: str,
    uploaded_bytes: int,
    total_bytes: int,
) -> str:
    """
    Build the upload progress message text.

    Format:
        ⬆️ Uploading [45 / 300]
        My Video Caption

        Progress: 34% | 304.0 MB / 892.0 MB
    """
    from bot.utils.file_utils import format_size

    header = f"⬆️ <b>Uploading [{task_num} / {total_tasks}]</b>"
    safe_caption = _escape_html(caption)

    if total_bytes > 0:
        pct = min(int(uploaded_bytes / total_bytes * 100), 100)
        up_str = format_size(uploaded_bytes)
        total_str = format_size(total_bytes)
        progress_line = f"Progress: <b>{pct}%</b> | {up_str} / {total_str}"
    else:
        progress_line = f"Uploading: <b>{format_size(uploaded_bytes)}</b>"

    return f"{header}\n{safe_caption}\n\n{progress_line}"
