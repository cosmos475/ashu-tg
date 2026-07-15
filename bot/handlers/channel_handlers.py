"""
bot/handlers/channel_handlers.py — Channel setup handler.

Manages the /setchannel flow:
  1. command_handlers.handle_setchannel() calls set_awaiting_forward(True).
  2. The next message the owner sends is examined here.
  3. If it is a forwarded message from a channel, the channel ID is extracted,
     admin status is verified, and the channel is saved to MongoDB.
  4. If the owner sends /start or any other command, the flow is cancelled.

State is a simple module-level boolean — safe because there is only one owner.

Import pattern: `from app import bot`
"""

import logging

import telebot

from app import bot
from bot.db.repositories import settings_repo
from bot.utils.validators import is_owner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# True while we are waiting for the owner to forward a channel message.
_awaiting_forward: bool = False


def set_awaiting_forward(state: bool) -> None:
    """
    Set or clear the awaiting-forward flag.
    Called by command_handlers.handle_setchannel() to begin the flow,
    and internally to end it after success or cancellation.
    """
    global _awaiting_forward
    _awaiting_forward = state
    logger.debug("awaiting_forward set to %s", state)


def is_awaiting_forward() -> bool:
    """Return True if we are currently waiting for a forwarded channel message."""
    return _awaiting_forward


# ---------------------------------------------------------------------------
# Handler: forwarded message during channel setup
# ---------------------------------------------------------------------------

@bot.message_handler(
    func=lambda msg: (
        # Only active while awaiting a forwarded message
        _awaiting_forward
        # Only for the owner in private chat (is_owner check done inside)
    )
)
def handle_forwarded_message(message: telebot.types.Message) -> None:
    """
    Intercept all messages while _awaiting_forward is True.

    Accepts: forwarded messages from a channel.
    Rejects: non-forwarded messages, messages from non-channels, commands.
    Cancels: if owner sends /start (any command resets the flow).
    """
    if not is_owner(message):
        return

    # If the owner sends a command while we're waiting, cancel the flow
    if message.text and message.text.startswith("/"):
        set_awaiting_forward(False)
        logger.info("Channel setup cancelled by command | owner_id=%s", message.from_user.id)
        # Let the command flow through to its own handler naturally — don't reply here
        return

    # Must be a forwarded message from a channel
    forward_chat = message.forward_from_chat
    if forward_chat is None:
        bot.send_message(
            message.chat.id,
            "⚠️ That does not appear to be a forwarded channel message.\n\n"
            "Please <b>forward any message</b> from your private channel, "
            "or send /start to cancel.",
            parse_mode="HTML",
        )
        return

    if forward_chat.type != "channel":
        bot.send_message(
            message.chat.id,
            "⚠️ The forwarded message must come from a <b>channel</b>, "
            "not a group or user.\n\nPlease try again or send /start to cancel.",
            parse_mode="HTML",
        )
        return

    channel_id    = forward_chat.id
    channel_title = forward_chat.title or "Unknown Channel"
    channel_username = forward_chat.username  # May be None for private channels

    logger.info(
        "Channel detected from forward | channel_id=%s title=%r",
        channel_id, channel_title,
    )

    # Verify the bot has admin rights in the channel
    if not _verify_bot_is_admin(message.chat.id, channel_id, channel_title):
        return   # _verify_bot_is_admin sends the error message itself

    # Save to MongoDB
    saved = settings_repo.save_channel(
        channel_id=channel_id,
        channel_title=channel_title,
        channel_username=channel_username,
    )

    set_awaiting_forward(False)

    if saved:
        channel_display = channel_title
        if channel_username:
            channel_display += f" (@{channel_username})"

        bot.send_message(
            message.chat.id,
            f"✅ <b>Channel configured successfully!</b>\n\n"
            f"Channel : <b>{_escape(channel_display)}</b>\n"
            f"Channel ID : <code>{channel_id}</code>\n\n"
            f"You can now send me a .txt file to start processing.",
            parse_mode="HTML",
        )
        logger.info(
            "Channel saved | channel_id=%s title=%r username=%r",
            channel_id, channel_title, channel_username,
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ Failed to save the channel to the database. Please try again.",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verify_bot_is_admin(
    owner_chat_id: int,
    channel_id: int,
    channel_title: str,
) -> bool:
    """
    Check that the bot is an administrator in the given channel.

    Sends an error message to the owner if the check fails.
    Returns True if admin, False otherwise.
    """
    try:
        bot_info = bot.get_me()
        member = bot.get_chat_member(channel_id, bot_info.id)

        if member.status not in ("administrator", "creator"):
            logger.warning(
                "Bot is not admin in channel | channel_id=%s status=%s",
                channel_id, member.status,
            )
            bot.send_message(
                owner_chat_id,
                f"❌ <b>Permission error.</b>\n\n"
                f"The bot is not an administrator in <b>{_escape(channel_title)}</b>.\n\n"
                f"Please add the bot as an admin with permission to post messages, "
                f"then try again.",
                parse_mode="HTML",
            )
            return False

        logger.info(
            "Bot admin verified | channel_id=%s status=%s", channel_id, member.status
        )
        return True

    except Exception as exc:
        logger.error(
            "Admin check failed | channel_id=%s: %s", channel_id, exc
        )
        bot.send_message(
            owner_chat_id,
            f"❌ <b>Could not verify bot permissions.</b>\n\n"
            f"Error: <code>{_escape(str(exc)[:100])}</code>\n\n"
            f"Make sure the bot is an administrator in the channel and try again.",
            parse_mode="HTML",
        )
        return False


def _escape(text: str) -> str:
    """Minimal HTML entity escaping for safe insertion into parse_mode=HTML messages."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
