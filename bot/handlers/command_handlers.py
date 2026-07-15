"""
bot/handlers/command_handlers.py — Command handlers.

Registers handlers for all bot commands:
  /start    — Greet owner, persist owner_id to MongoDB.
  /help     — Send full command reference.
  /status   — Show current session progress (counts only, no URLs).
  /cancel   — Request cancellation of active session.
  /settings — Show current bot configuration.
  /ping     — Liveness check with timestamp.
  /setchannel — Trigger channel setup flow (delegates to channel_handlers).

All handlers validate ownership via is_owner() before doing anything.
Non-owner messages are silently ignored.

Import pattern: `from app import bot` — bot lives in app.py.
Handlers are registered on the bot instance via decorators at import time.
"""

import logging
from datetime import datetime, timezone

from bot_instance import bot
from bot.db.repositories import session_repo, settings_repo, task_repo
from bot.services import processor
from bot.utils.validators import is_owner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def handle_start(message) -> None:
    """Greet the owner and persist their user ID."""
    if not is_owner(message):
        return

    owner_id = message.from_user.id
    settings_repo.save_owner(owner_id)

    logger.info("Owner started bot | owner_id=%s", owner_id)

    bot.send_message(
        message.chat.id,
        "👋 <b>Welcome!</b>\n\n"
        "I am your private media uploader bot.\n\n"
        "To get started:\n"
        "1. Use /setchannel to configure your destination channel.\n"
        "2. Send me a <b>.txt</b> file with your captions and URLs.\n"
        "3. I will download and upload everything automatically.\n\n"
        "Use /help to see all available commands.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["help"])
def handle_help(message) -> None:
    """Send the full command reference."""
    if not is_owner(message):
        return

    logger.info("Owner requested help | owner_id=%s", message.from_user.id)

    bot.send_message(
        message.chat.id,
        "📖 <b>Available Commands</b>\n\n"
        "/start — Initialize the bot\n"
        "/setchannel — Set destination channel\n"
        "/status — Show current processing progress\n"
        "/cancel — Stop processing after current task\n"
        "/settings — View current configuration\n"
        "/ping — Check if bot is alive\n"
        "/help — Show this message\n\n"
        "<b>How to use:</b>\n"
        "Send me a <b>.txt</b> file. Each URL must be on its own line. "
        "The line immediately above each URL is used as the caption.\n\n"
        "<b>Supported formats:</b> Video (MP4, M3U8), PDF, HTML",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["status"])
def handle_status(message) -> None:
    """Show current session progress. Never exposes URLs."""
    if not is_owner(message):
        return

    logger.info("Owner requested status | owner_id=%s", message.from_user.id)

    if not processor.is_running():
        bot.send_message(message.chat.id, "ℹ️ No active processing session.")
        return

    session_id = processor.get_current_session_id()
    if not session_id:
        bot.send_message(message.chat.id, "ℹ️ No active processing session.")
        return

    session = session_repo.get_session(session_id)
    if not session:
        bot.send_message(message.chat.id, "⚠️ Could not retrieve session data.")
        return

    counts = task_repo.get_status_counts(session_id)

    total    = session.get("total", 0)
    success  = counts.get("success", 0)
    failed   = counts.get("failed", 0)
    skipped  = counts.get("skipped", 0)
    pending  = counts.get("pending", 0)
    processing = counts.get("processing", 0)

    completed  = success + failed + skipped
    remaining  = pending + processing
    current    = completed + 1 if remaining > 0 else total

    bot.send_message(
        message.chat.id,
        f"⚙️ <b>Processing Status</b>\n\n"
        f"Total URLs   : <b>{total}</b>\n"
        f"Current Task : <b>{current} / {total}</b>\n"
        f"Completed    : <b>{completed}</b>\n"
        f"✅ Success   : <b>{success}</b>\n"
        f"❌ Failed    : <b>{failed}</b>\n"
        f"⏭ Skipped   : <b>{skipped}</b>\n"
        f"⏳ Remaining : <b>{remaining}</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["cancel"])
def handle_cancel(message) -> None:
    """Request cancellation. Processing stops after current task completes."""
    if not is_owner(message):
        return

    logger.info("Owner requested cancel | owner_id=%s", message.from_user.id)

    if not processor.is_running():
        bot.send_message(message.chat.id, "ℹ️ No active session to cancel.")
        return

    processor.cancel()
    bot.send_message(
        message.chat.id,
        "🚫 <b>Cancellation requested.</b>\n"
        "Stopping the current download/upload immediately.\n"
        "Already uploaded files remain in the channel.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["settings"])
def handle_settings(message) -> None:
    """Show current bot configuration."""
    if not is_owner(message):
        return

    logger.info("Owner requested settings | owner_id=%s", message.from_user.id)

    settings = settings_repo.get_settings()

    if settings and settings.get("channel_id"):
        channel_title = settings.get("channel_title") or "Unknown"
        channel_id    = settings.get("channel_id")
        username      = settings.get("channel_username")
        channel_str   = f"{channel_title}"
        if username:
            channel_str += f" (@{username})"
    else:
        channel_str = "❌ Not configured — use /setchannel"

    import config as _config
    bot.send_message(
        message.chat.id,
        f"⚙️ <b>Bot Settings</b>\n\n"
        f"Owner ID       : <code>{_config.OWNER_ID}</code>\n"
        f"Channel        : {channel_str}\n"
        f"Max File Size  : 2 GB\n"
        f"Max Retries    : {_config.MAX_RETRIES}\n"
        f"Processing     : {'🟢 Active' if processor.is_running() else '⚪ Idle'}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /ping
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["ping"])
def handle_ping(message) -> None:
    """Liveness check — confirms the bot and Render service are alive."""
    if not is_owner(message):
        return

    logger.info("Ping | owner_id=%s", message.from_user.id)

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bot.send_message(
        message.chat.id,
        f"🏓 <b>Pong!</b>\n<code>{now}</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /setchannel — delegates state management to channel_handlers
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["setchannel"])
def handle_setchannel(message) -> None:
    """
    Begin the channel setup flow.
    Sets the awaiting-forward flag in channel_handlers, then instructs the owner
    to forward any message from their private channel.
    """
    if not is_owner(message):
        return

    logger.info("Owner initiated setchannel | owner_id=%s", message.from_user.id)

    # Import here to avoid module-level circular dependency
    from bot.handlers.channel_handlers import set_awaiting_forward
    set_awaiting_forward(True)

    bot.send_message(
        message.chat.id,
        "📡 <b>Channel Setup</b>\n\n"
        "Please <b>forward any message</b> from your private channel to me.\n\n"
        "Make sure I am already an <b>administrator</b> in that channel "
        "with permission to post messages.\n\n"
        "Send /start to cancel this setup.",
        parse_mode="HTML",
    )
