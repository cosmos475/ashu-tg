"""
bot/handlers/file_handlers.py — TXT file upload handler.

Handles the complete flow from receiving a .txt file to starting processing:

  1. Validate ownership.
  2. Check no session is already running.
  3. Check a destination channel is configured.
  4. Download the TXT file from Telegram to tmp/.
  5. Parse URLs and captions via txt_parser.
  6. Detect URL types via detector.
  7. Show analysis summary to owner.
  8. Ask for confirmation via InlineKeyboard (✅ Yes / ❌ No).
  9. On confirmation: create session, insert tasks, start processor.
  10. On rejection: notify owner, clean up.
  11. Delete the local TXT file in all code paths.

Callback data keys:
  "confirm_start:<session_token>"  — owner confirmed processing
  "cancel_start:<session_token>"   — owner rejected processing

A lightweight in-memory pending_session dict holds parsed data between
the file upload and the confirmation button press. It is keyed by a
short UUID token embedded in the callback data, preventing stale confirmations.
"""

import logging
import uuid
from pathlib import Path

import telebot

from app import bot
from bot.db.repositories import session_repo, settings_repo, task_repo
from bot.parser.txt_parser import parse_txt_file
from bot.services import processor
from bot.utils.file_utils import delete_file, get_tmp_path
from bot.utils.validators import is_owner, is_owner_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory pending session store
# key:   confirmation token (str, short UUID)
# value: dict with keys: chat_id, items, parse_summary
# ---------------------------------------------------------------------------
_pending: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Handler: incoming document (TXT file)
# ---------------------------------------------------------------------------

@bot.message_handler(content_types=["document"])
def handle_document(message: telebot.types.Message) -> None:
    """
    Entry point for all document uploads.
    Only processes .txt / text/plain files. All others are ignored silently.
    """
    if not is_owner(message):
        return

    doc = message.document
    if doc is None:
        return

    # Accept only plain text files (.txt)
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    if mime != "text/plain" and not name.endswith(".txt"):
        bot.send_message(
            message.chat.id,
            "⚠️ Please send a <b>.txt</b> file.",
            parse_mode="HTML",
        )
        return

    logger.info(
        "TXT file received | owner_id=%s file=%s size=%s",
        message.from_user.id,
        doc.file_name,
        doc.file_size,
    )

    _process_txt_upload(message)


def _process_txt_upload(message: telebot.types.Message) -> None:
    """
    Full TXT upload flow. Handles all guard checks, parsing, and confirmation.
    """
    chat_id = message.chat.id

    # ── Guard: session already running ────────────────────────────────────
    if processor.is_running():
        bot.send_message(
            chat_id,
            "⚠️ A processing session is already running.\n"
            "Please wait for it to finish or use /cancel first.",
        )
        return

    # ── Guard: channel not configured ─────────────────────────────────────
    channel_id = settings_repo.get_channel_id()
    if channel_id is None:
        bot.send_message(
            chat_id,
            "⚠️ No destination channel is configured.\n"
            "Please use /setchannel first.",
        )
        return

    txt_path: Path | None = None

    try:
        # ── Download TXT from Telegram ─────────────────────────────────────
        txt_path = _download_txt(message)
        if txt_path is None:
            bot.send_message(chat_id, "❌ Failed to download the file. Please try again.")
            return

        # ── Parse ──────────────────────────────────────────────────────────
        try:
            items, parse_summary = parse_txt_file(str(txt_path))
        except ValueError as exc:
            bot.send_message(chat_id, f"❌ {exc}")
            return

        # ── Show summary and ask for confirmation ──────────────────────────
        # NOTE: URL type detection (detector.detect_all) is intentionally NOT
        # done here. It makes one HEAD request per URL and can take minutes for
        # large files, causing Telegram to retry the webhook and duplicate messages.
        # Type detection happens per-task inside the processor thread instead.
        # url_type defaults to "unknown" and is updated by processor as each
        # task runs via task_repo.update_url_type().

        token = uuid.uuid4().hex[:12]
        _pending[token] = {
            "chat_id": chat_id,
            "items": items,
            "parse_summary": parse_summary,
        }

        summary_text = (
            f"📋 <b>File Analysis</b>\n\n"
            f"Total URLs   : <b>{parse_summary['total']}</b>\n"
            f"♻️ Duplicates : <b>{parse_summary['duplicate']}</b>\n\n"
            f"URL types will be detected automatically during processing.\n\n"
            f"Start processing?"
        )

        keyboard = telebot.types.InlineKeyboardMarkup()
        keyboard.row(
            telebot.types.InlineKeyboardButton(
                "✅ Yes, start",
                callback_data=f"confirm_start:{token}",
            ),
            telebot.types.InlineKeyboardButton(
                "❌ No, cancel",
                callback_data=f"cancel_start:{token}",
            ),
        )

        bot.send_message(
            chat_id,
            summary_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as exc:
        logger.exception("Unexpected error in TXT upload flow: %s", exc)
        bot.send_message(chat_id, f"❌ An unexpected error occurred: {exc}")

    finally:
        # Always delete the local TXT file
        if txt_path is not None:
            delete_file(txt_path)


# ---------------------------------------------------------------------------
# Handler: confirmation callback
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_start:"))
def handle_confirm_start(call: telebot.types.CallbackQuery) -> None:
    """Owner pressed ✅ Yes — create session and start processing."""
    if not is_owner_query(call):
        bot.answer_callback_query(call.id, "Unauthorized.")
        return

    token = call.data.split(":", 1)[1]
    pending = _pending.pop(token, None)

    bot.answer_callback_query(call.id)

    if pending is None:
        # Token expired or already used
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            "⚠️ This confirmation has expired. Please upload the file again.",
        )
        return

    # Guard: check again in case something changed while owner was reading summary
    if processor.is_running():
        bot.send_message(
            call.message.chat.id,
            "⚠️ A session started while you were reviewing. Please wait or /cancel.",
        )
        return

    chat_id = pending["chat_id"]
    items   = pending["items"]
    total   = len(items)

    # Remove confirmation buttons
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    logger.info(
        "Owner confirmed processing | chat_id=%s total=%d", chat_id, total
    )

    try:
        # Create session
        session_id = session_repo.create_session(total)
        if session_id is None:
            bot.send_message(chat_id, "❌ Failed to create processing session. Please try again.")
            return

        # Insert all tasks
        inserted = task_repo.insert_tasks(session_id, items)
        if not inserted:
            bot.send_message(chat_id, "❌ Failed to insert tasks. Please try again.")
            return

        # Load tasks from DB (preserves insertion order and task_ids)
        tasks = task_repo.get_pending_tasks(session_id)
        if not tasks:
            bot.send_message(chat_id, "❌ No tasks found after insertion. Please try again.")
            return

        # Inject bot into processor (idempotent — safe to call every session)
        processor.set_bot(bot)

        # Start the processing thread
        processor.start_session(
            chat_id=chat_id,
            session_id=session_id,
            tasks=tasks,
        )

        logger.info(
            "Session started | session_id=%s total=%d", session_id, total
        )

    except RuntimeError as exc:
        # processor.start_session raises RuntimeError if already running
        bot.send_message(chat_id, f"⚠️ {exc}")

    except Exception as exc:
        logger.exception("Failed to start session: %s", exc)
        bot.send_message(chat_id, f"❌ Failed to start processing: {exc}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_start:"))
def handle_cancel_start(call: telebot.types.CallbackQuery) -> None:
    """Owner pressed ❌ No — discard pending session data."""
    if not is_owner_query(call):
        bot.answer_callback_query(call.id, "Unauthorized.")
        return

    token = call.data.split(":", 1)[1]
    _pending.pop(token, None)   # Discard pending data

    bot.answer_callback_query(call.id, "Cancelled.")

    # Remove the confirmation buttons from the summary message
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    bot.send_message(
        call.message.chat.id,
        "🚫 Processing cancelled. Send another .txt file to start again.",
    )
    logger.info("Owner cancelled processing | owner_id=%s", call.from_user.id)


# ---------------------------------------------------------------------------
# Internal: download TXT file from Telegram
# ---------------------------------------------------------------------------

def _download_txt(message: telebot.types.Message) -> Path | None:
    """
    Download the attached TXT document to tmp/.
    Returns the local Path on success, or None on failure.
    """
    doc = message.document
    filename = doc.file_name or "upload.txt"
    output_path = get_tmp_path(filename)

    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)

        with open(output_path, "wb") as fh:
            fh.write(downloaded)

        logger.info(
            "TXT downloaded | file=%s size=%d bytes",
            output_path.name, len(downloaded),
        )
        return output_path

    except Exception as exc:
        logger.error("Failed to download TXT: %s", exc)
        delete_file(output_path)
        return None
