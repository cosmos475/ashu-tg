"""
bot/db/repositories/settings_repo.py — Settings repository.

Manages the single 'settings' document (_id = "bot_settings").
Stores owner_id, channel_id, and channel metadata.

All writes use upsert so the document is created on first use
and updated on subsequent calls without any special init step.
"""

import logging
from typing import Any

import pymongo.errors

from bot.db.client import get_db

logger = logging.getLogger(__name__)

# Fixed document ID — there is always exactly one settings document.
_SETTINGS_ID = "bot_settings"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_settings() -> dict[str, Any] | None:
    """
    Return the settings document, or None if not yet configured.
    """
    try:
        return get_db()["settings"].find_one({"_id": _SETTINGS_ID})
    except pymongo.errors.PyMongoError as exc:
        logger.error("get_settings failed: %s", exc)
        return None


def get_channel_id() -> int | None:
    """
    Return the saved destination channel ID, or None if not set.
    """
    doc = get_settings()
    if doc:
        return doc.get("channel_id")
    return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_channel(
    channel_id: int,
    channel_title: str,
    channel_username: str | None = None,
) -> bool:
    """
    Persist the destination channel details.
    Creates the settings document if it does not exist (upsert).

    Returns True on success, False on database error.
    """
    try:
        get_db()["settings"].update_one(
            {"_id": _SETTINGS_ID},
            {
                "$set": {
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "channel_username": channel_username,
                }
            },
            upsert=True,
        )
        logger.info(
            "Channel saved | channel_id=%s title=%r", channel_id, channel_title
        )
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("save_channel failed: %s", exc)
        return False


def save_owner(owner_id: int) -> bool:
    """
    Persist the owner's Telegram user ID.
    Called once during /start if not already stored.

    Returns True on success, False on database error.
    """
    try:
        get_db()["settings"].update_one(
            {"_id": _SETTINGS_ID},
            {"$set": {"owner_id": owner_id}},
            upsert=True,
        )
        logger.info("Owner saved | owner_id=%s", owner_id)
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("save_owner failed: %s", exc)
        return False
