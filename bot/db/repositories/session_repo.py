"""
bot/db/repositories/session_repo.py — Sessions repository.

Each TXT file upload creates one session document that tracks
overall progress: total URLs, success/failed/skipped counts, and status.

session_id is a uuid4 string used in logs for traceability.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pymongo.errors

from bot.db.client import get_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_session_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_session(total: int) -> str | None:
    """
    Insert a new session document with status 'running'.

    Args:
        total: Total number of URLs parsed from the TXT file.

    Returns:
        session_id (str) on success, None on database error.
    """
    session_id = _new_session_id()
    doc: dict[str, Any] = {
        "session_id": session_id,
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "total": total,
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    try:
        get_db()["sessions"].insert_one(doc)
        logger.info("Session created | session_id=%s total=%d", session_id, total)
        return session_id

    except pymongo.errors.PyMongoError as exc:
        logger.error("create_session failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_session(session_id: str) -> dict[str, Any] | None:
    """Return the session document for the given session_id."""
    try:
        return get_db()["sessions"].find_one({"session_id": session_id})
    except pymongo.errors.PyMongoError as exc:
        logger.error("get_session failed | session_id=%s: %s", session_id, exc)
        return None


def get_active_session() -> dict[str, Any] | None:
    """Return the currently running session document, if any."""
    try:
        return get_db()["sessions"].find_one({"status": "running"})
    except pymongo.errors.PyMongoError as exc:
        logger.error("get_active_session failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def increment_counts(
    session_id: str,
    success: int = 0,
    failed: int = 0,
    skipped: int = 0,
) -> bool:
    """
    Atomically increment success/failed/skipped counters.
    Uses $inc to avoid race conditions (safe even if called rapidly).

    Returns True on success, False on error.
    """
    inc: dict[str, int] = {}
    if success:
        inc["success"] = success
    if failed:
        inc["failed"] = failed
    if skipped:
        inc["skipped"] = skipped

    if not inc:
        return True  # Nothing to update

    try:
        get_db()["sessions"].update_one(
            {"session_id": session_id},
            {"$inc": inc},
        )
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error(
            "increment_counts failed | session_id=%s: %s", session_id, exc
        )
        return False


def finish_session(session_id: str, status: str) -> bool:
    """
    Mark a session as finished with the given status.

    Args:
        session_id: Target session.
        status: One of 'completed', 'cancelled', 'failed'.

    Returns True on success, False on error.
    """
    valid_statuses = {"completed", "cancelled", "failed"}
    if status not in valid_statuses:
        logger.warning(
            "finish_session called with invalid status %r — defaulting to 'failed'", status
        )
        status = "failed"

    try:
        get_db()["sessions"].update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "status": status,
                    "finished_at": _now(),
                }
            },
        )
        logger.info("Session finished | session_id=%s status=%s", session_id, status)
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("finish_session failed | session_id=%s: %s", session_id, exc)
        return False
