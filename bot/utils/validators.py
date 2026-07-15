"""
bot/utils/validators.py — Access control utilities.

Single responsibility: verify that an incoming Telegram message
or callback originates from the configured owner.

All handlers must call is_owner() before processing any request.
Non-owner messages are silently ignored (no reply sent) to avoid
leaking information about the bot's existence.
"""

import logging

import telebot.types

import config

logger = logging.getLogger(__name__)


def is_owner(message: telebot.types.Message) -> bool:
    """
    Return True if the message was sent by the configured owner.

    Checks:
      - message.from_user exists (guards against channel posts)
      - message.from_user.id matches OWNER_ID from config
      - message.chat.type == 'private' (bot only works in private chat)

    Args:
        message: Incoming telebot Message object.

    Returns:
        True if the sender is the owner in a private chat, False otherwise.
    """
    if message.from_user is None:
        logger.debug("Rejected: message has no from_user (likely a channel post).")
        return False

    if message.chat.type != "private":
        logger.debug(
            "Rejected: message from chat_type=%s (owner_check requires private).",
            message.chat.type,
        )
        return False

    if message.from_user.id != config.OWNER_ID:
        logger.warning(
            "Rejected: unauthorised user_id=%s attempted access.",
            message.from_user.id,
        )
        return False

    return True


def is_owner_query(call: telebot.types.CallbackQuery) -> bool:
    """
    Return True if a callback query originates from the owner.

    Used for inline keyboard confirmations (e.g. "Confirm processing?" button).

    Args:
        call: Incoming telebot CallbackQuery object.

    Returns:
        True if the caller is the owner, False otherwise.
    """
    if call.from_user is None:
        return False

    if call.from_user.id != config.OWNER_ID:
        logger.warning(
            "Rejected callback: unauthorised user_id=%s.",
            call.from_user.id,
        )
        return False

    return True
