"""
bot/services/processor.py — Session orchestrator.

This is the central execution engine of the bot.

Responsibilities:
  - Accept a new processing session (TXT file items + owner chat ID).
  - Spin up a single daemon background thread for sequential URL processing.
  - For each task: download → upload → delete local file → update MongoDB.
  - Handle cancellation, retries, skips, and failures cleanly.
  - Send the completion summary to the owner after all tasks finish.
  - Reset all state so a new session can start immediately after.

Thread model:
  - One daemon thread runs the entire session.
  - The webhook thread (Flask) can read is_processing safely.
  - _cancel_event is set by the /cancel command handler and polled inside
    the download/upload loops for immediate cancellation.
  - Python's GIL makes simple bool reads/writes thread-safe on CPython.

Public API:
  start_session(chat_id, session_id, tasks) -> None
  cancel() -> None
  is_running() -> bool
  get_current_session_id() -> str | None
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import telebot

import config
from bot.db.repositories import session_repo, task_repo
from bot.services import downloader as downloader_module
from bot.services import progress as progress_module
from bot.services import uploader as uploader_module
from bot.services.downloader import DownloadError, DownloadCancelled, FileTooLargeError
from bot.services.uploader import UploadError, UploadCancelled
from bot.utils.file_utils import delete_file, format_size

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — read by handlers, written only by this module
# ---------------------------------------------------------------------------

# True while a processing session is active. Checked by file_handlers.py.
is_processing: bool = False

# Set by cancel(). Checked between AND during tasks (download/upload loops
# poll this event too, so cancellation takes effect immediately instead of
# only between tasks).
_cancel_event: threading.Event = threading.Event()

# Active session ID — used by /status command.
_current_session_id: str | None = None

# Bot instance — injected once via set_bot().
_bot: telebot.TeleBot | None = None

# Lock for is_processing flag to be safe across threads
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

def set_bot(bot_instance: telebot.TeleBot) -> None:
    """
    Inject the shared bot instance.
    Must be called once at startup (from app.py or handler init).
    Also propagates the instance to uploader and progress modules.
    """
    global _bot
    _bot = bot_instance
    uploader_module.set_bot(bot_instance)
    progress_module.set_bot(bot_instance)
    logger.debug("Bot instance injected into processor, uploader, progress.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    """Return True if a session is currently being processed."""
    return is_processing


def get_current_session_id() -> str | None:
    """Return the active session_id, or None if idle."""
    return _current_session_id


def cancel() -> None:
    """
    Request cancellation of the active session.

    Cancellation is immediate: it interrupts the current download or upload
    in progress (not just "after the current task"), cleans up the partial
    temp file, marks remaining tasks as skipped, and releases the
    processing lock so a new session can start right away.

    Safe to call even when no session is running (no-op).
    """
    _cancel_event.set()
    logger.info("Cancellation requested.")


def start_session(
    chat_id: int,
    session_id: str,
    tasks: list[dict],
) -> None:
    """
    Start processing a new session in a background daemon thread.

    Args:
        chat_id:    Owner's private chat ID (for progress and summary messages).
        session_id: MongoDB session_id (already created by file_handlers.py).
        tasks:      List of task dicts from task_repo.get_pending_tasks().

    Raises:
        RuntimeError: If a session is already running (caller should check
                      is_running() before calling this).
    """
    global is_processing, _current_session_id

    with _state_lock:
        if is_processing:
            raise RuntimeError("A session is already running.")
        is_processing = True
        _cancel_event.clear()
        _current_session_id = session_id

    # Initialise progress tracker for this session
    progress_module.init(chat_id=chat_id, total_tasks=len(tasks))

    thread = threading.Thread(
        target=_run_session,
        args=(chat_id, session_id, tasks),
        daemon=True,   # Daemon: won't block Render from shutting down
        name=f"session-{session_id[:8]}",
    )
    thread.start()
    logger.info(
        "Processing thread started | session_id=%s tasks=%d thread=%s",
        session_id, len(tasks), thread.name,
    )


# ---------------------------------------------------------------------------
# Processing thread
# ---------------------------------------------------------------------------

def _run_session(
    chat_id: int,
    session_id: str,
    tasks: list[dict],
) -> None:
    """
    Main processing loop. Runs entirely inside the daemon thread.

    Top-level try/except ensures is_processing is always reset,
    even on unexpected exceptions (e.g. MongoDB outage, OOM).
    """
    started_at = datetime.now(tz=timezone.utc)
    final_status = "completed"

    try:
        logger.info("Session started | session_id=%s total=%d", session_id, len(tasks))
        _notify(chat_id, f"⚙️ Processing started — <b>{len(tasks)}</b> URLs queued.")

        final_status = _process_tasks(chat_id, session_id, tasks)

    except Exception as exc:
        # Unexpected crash in the processing loop itself
        logger.exception(
            "Unexpected exception in processing thread | session_id=%s: %s",
            session_id, exc,
        )
        final_status = "failed"
        _notify(
            chat_id,
            "❌ An unexpected error occurred and processing was stopped.\n"
            f"<code>{exc}</code>",
        )

    finally:
        _finish_session(chat_id, session_id, started_at, final_status)
        _reset_state()
        logger.info(
            "Processing thread exited | session_id=%s status=%s",
            session_id, final_status,
        )


def _process_tasks(
    chat_id: int,
    session_id: str,
    tasks: list[dict],
) -> str:
    """
    Iterate through all tasks sequentially.

    Returns the final session status string:
      "completed"  — all tasks processed (some may have failed/skipped)
      "cancelled"  — /cancel was requested
    """
    total = len(tasks)

    for task_num, task in enumerate(tasks, start=1):

        # ── Cancellation check ────────────────────────────────────────────
        if _cancel_event.is_set():
            logger.info(
                "Session cancelled at task %d/%d | session_id=%s",
                task_num, total, session_id,
            )
            # Mark all remaining pending tasks as skipped
            _skip_remaining_tasks(tasks[task_num - 1:], reason="Session cancelled")
            return "cancelled"

        task_id = task["task_id"]
        url = task["url"]
        url_type = task["url_type"]
        caption = task["caption"]

        logger.info(
            "Task started [%d/%d] | task_id=%s type=%s url=%s",
            task_num, total, task_id, url_type, url[:80],
        )

        task_repo.mark_processing(task_id)
        _process_single_task(
            chat_id=chat_id,
            session_id=session_id,
            task_id=task_id,
            url=url,
            url_type=url_type,
            caption=caption,
            task_num=task_num,
            total=total,
        )

    return "completed"


def _process_single_task(
    chat_id: int,
    session_id: str,
    task_id: str,
    url: str,
    url_type: str,
    caption: str,
    task_num: int,
    total: int,
) -> None:
    """
    Execute the full download → upload → cleanup cycle for one task.

    Outcome is always written to MongoDB before returning.
    Never raises — all exceptions are caught and converted to DB status updates.
    """
    channel_id = _get_channel_id()
    if channel_id is None:
        reason = "No destination channel configured"
        logger.error("Task skipped: %s | task_id=%s", reason, task_id)
        task_repo.mark_skipped(task_id, reason)
        session_repo.increment_counts(session_id, skipped=1)
        return

    downloaded_path: Path | None = None

    try:
        # ── Download ──────────────────────────────────────────────────────
        logger.info("Download starting | task_id=%s url=%s", task_id, url[:80])

        downloaded_path = downloader_module.download(
            url=url,
            url_type=url_type,
            task_num=task_num,
            total_tasks=total,
            caption=caption,
            cancel_event=_cancel_event,
        )

        logger.info(
            "Download complete | task_id=%s file=%s size=%s",
            task_id,
            downloaded_path.name,
            format_size(downloaded_path.stat().st_size),
        )

        # Switch progress message from ⬇️ Downloading to ⬆️ Uploading
        file_size = downloaded_path.stat().st_size
        progress_module.send_upload(
            task_num=task_num,
            caption=caption,
            file_size=file_size,
        )

        # ── Upload ────────────────────────────────────────────────────────
        logger.info(
            "Upload starting | task_id=%s type=%s channel=%s",
            task_id, url_type, channel_id,
        )

        uploader_module.upload(
            path=downloaded_path,
            url_type=url_type,
            caption=caption,
            channel_id=channel_id,
            cancel_event=_cancel_event,
        )

        logger.info("Upload complete | task_id=%s", task_id)

        # ── Success ───────────────────────────────────────────────────────
        task_repo.mark_success(task_id)
        session_repo.increment_counts(session_id, success=1)

    except (DownloadCancelled, UploadCancelled):
        # /cancel was pressed — stop this task immediately without retrying.
        # The outer loop's cancel check (top of _process_tasks) will skip
        # any remaining tasks and release the processing lock right after.
        logger.info("Task cancelled by user | task_id=%s", task_id)
        task_repo.mark_skipped(task_id, "Cancelled by user")
        session_repo.increment_counts(session_id, skipped=1)

    except FileTooLargeError as exc:
        # Non-retryable: file exceeds 2 GB limit
        reason = str(exc)
        logger.warning("Task skipped (too large) | task_id=%s: %s", task_id, reason)
        task_repo.mark_skipped(task_id, reason)
        session_repo.increment_counts(session_id, skipped=1)
        _notify(
            chat_id,
            f"⚠️ Skipped task {task_num}/{total} — file too large.\n"
            f"<i>{_escape(caption[:80])}</i>",
        )

    except (DownloadError, UploadError) as exc:
        # Retryable errors are already retried inside downloader/uploader.
        # By the time we get here, all retries are exhausted.
        reason = str(exc)
        logger.error(
            "Task failed | task_id=%s: %s", task_id, reason
        )
        task_repo.mark_failed(task_id, reason)
        session_repo.increment_counts(session_id, failed=1)

    except Exception as exc:
        # Unexpected error — log full traceback, mark failed
        reason = f"Unexpected error: {exc}"
        logger.exception("Unexpected task error | task_id=%s", task_id)
        task_repo.mark_failed(task_id, reason)
        session_repo.increment_counts(session_id, failed=1)

    finally:
        # ── Always clean up ───────────────────────────────────────────────
        progress_module.delete()    # Remove progress message from chat

        if downloaded_path is not None:
            delete_file(downloaded_path)   # Delete local file (never raises)

        task_repo.update_url_type(task_id, url_type)  # Persist confirmed type


# ---------------------------------------------------------------------------
# Session finalisation
# ---------------------------------------------------------------------------

def _finish_session(
    chat_id: int,
    session_id: str,
    started_at: datetime,
    status: str,
) -> None:
    """
    Mark the session complete in MongoDB and send the summary to the owner.
    """
    session_repo.finish_session(session_id, status)
    session = session_repo.get_session(session_id)

    if not session:
        logger.warning("Could not retrieve session for summary | session_id=%s", session_id)
        return

    duration = _format_duration(started_at)

    status_emoji = {
        "completed": "✅",
        "cancelled": "🚫",
        "failed": "❌",
    }.get(status, "ℹ️")

    summary = (
        f"{status_emoji} <b>Session {status.capitalize()}</b>\n\n"
        f"Total URLs   : <b>{session.get('total', 0)}</b>\n"
        f"Successful   : <b>{session.get('success', 0)}</b>\n"
        f"Failed       : <b>{session.get('failed', 0)}</b>\n"
        f"Skipped      : <b>{session.get('skipped', 0)}</b>\n"
        f"Duration     : <b>{duration}</b>"
    )

    # Append failed task details if any
    failed_tasks = task_repo.get_failed_tasks(session_id)
    if failed_tasks:
        summary += f"\n\n<b>Failed tasks ({len(failed_tasks)}):</b>"
        for i, ft in enumerate(failed_tasks[:10], 1):  # Cap at 10 to avoid message length issues
            err = (ft.get("error") or "unknown error")[:60]
            cap = _escape(ft.get("caption", "No Caption")[:40])
            summary += f"\n{i}. <i>{cap}</i> — {_escape(err)}"
        if len(failed_tasks) > 10:
            summary += f"\n… and {len(failed_tasks) - 10} more."

    _notify(chat_id, summary)
    logger.info(
        "Session summary sent | session_id=%s status=%s success=%d failed=%d skipped=%d",
        session_id,
        status,
        session.get("success", 0),
        session.get("failed", 0),
        session.get("skipped", 0),
    )


def _skip_remaining_tasks(tasks: list[dict], reason: str) -> None:
    """Mark a list of tasks as skipped in MongoDB (used on cancellation)."""
    for task in tasks:
        task_repo.mark_skipped(task["task_id"], reason)
        session_repo.increment_counts(task["session_id"], skipped=1)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _reset_state() -> None:
    """Reset all module-level flags after a session ends."""
    global is_processing, _current_session_id
    with _state_lock:
        is_processing = False
        _cancel_event.clear()
        _current_session_id = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_channel_id() -> int | None:
    """Fetch the configured destination channel ID from MongoDB."""
    try:
        from bot.db.repositories import settings_repo
        return settings_repo.get_channel_id()
    except Exception as exc:
        logger.error("Failed to fetch channel_id: %s", exc)
        return None


def _notify(chat_id: int, text: str) -> None:
    """
    Send a plain HTML message to the owner's private chat.
    Never raises — failures are logged and silently ignored.
    """
    if _bot is None:
        logger.warning("_notify called before bot instance set.")
        return
    try:
        _bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to send notification to owner: %s", exc)


def _format_duration(started_at: datetime) -> str:
    """
    Return a human-readable duration string from started_at to now.
    Example: "5m 32s", "1h 12m", "45s"
    """
    delta = datetime.now(tz=timezone.utc) - started_at
    total_seconds = int(delta.total_seconds())

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def _escape(text: str) -> str:
    """Minimal HTML escaping for safe insertion into parse_mode=HTML messages."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
