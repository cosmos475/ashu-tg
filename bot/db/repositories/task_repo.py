"""
bot/db/repositories/task_repo.py — Tasks repository.

Each URL in a TXT file becomes one task document.
Tasks track per-URL state: status, retry count, error message, timestamps.

task_id is a uuid4 string used in logs for traceability.
All bulk inserts use insert_many for efficiency.
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


def _new_task_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def insert_tasks(session_id: str, items: list[dict[str, str]]) -> bool:
    """
    Bulk-insert all tasks for a session in a single database call.

    Args:
        session_id: Parent session identifier.
        items: List of dicts with keys 'caption', 'url', 'url_type'.
               url_type may be 'unknown' at insertion time and updated later.

    Returns True on success, False on error.
    """
    if not items:
        logger.warning("insert_tasks called with empty list | session_id=%s", session_id)
        return False

    docs: list[dict[str, Any]] = [
        {
            "task_id": _new_task_id(),
            "session_id": session_id,
            "caption": item.get("caption", "No Caption"),
            "url": item["url"],
            "url_type": item.get("url_type", "unknown"),
            "status": "pending",
            "retry_count": 0,
            "error": None,
            "created_at": _now(),
            "completed_at": None,
        }
        for item in items
    ]

    try:
        result = get_db()["tasks"].insert_many(docs, ordered=True)
        logger.info(
            "Tasks inserted | session_id=%s count=%d", session_id, len(result.inserted_ids)
        )
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("insert_tasks failed | session_id=%s: %s", session_id, exc)
        return False


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_pending_tasks(session_id: str) -> list[dict[str, Any]]:
    """
    Return all pending tasks for the session, in insertion order.
    Called by the processor to iterate tasks sequentially.
    """
    try:
        cursor = (
            get_db()["tasks"]
            .find({"session_id": session_id, "status": "pending"})
            .sort("created_at", pymongo.ASCENDING)
        )
        return list(cursor)

    except pymongo.errors.PyMongoError as exc:
        logger.error(
            "get_pending_tasks failed | session_id=%s: %s", session_id, exc
        )
        return []


def get_status_counts(session_id: str) -> dict[str, int]:
    """
    Return a dict of status → count for a session.
    Used by /status command and completion summary.

    Example return: {'pending': 45, 'success': 12, 'failed': 3, 'skipped': 1}
    """
    try:
        pipeline = [
            {"$match": {"session_id": session_id}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        result = get_db()["tasks"].aggregate(pipeline)
        return {doc["_id"]: doc["count"] for doc in result}

    except pymongo.errors.PyMongoError as exc:
        logger.error(
            "get_status_counts failed | session_id=%s: %s", session_id, exc
        )
        return {}


def get_failed_tasks(session_id: str) -> list[dict[str, Any]]:
    """
    Return all failed tasks for the session.
    Used in the completion summary to report failures.
    """
    try:
        cursor = get_db()["tasks"].find(
            {"session_id": session_id, "status": "failed"},
            {"caption": 1, "url": 1, "error": 1, "_id": 0},
        )
        return list(cursor)

    except pymongo.errors.PyMongoError as exc:
        logger.error(
            "get_failed_tasks failed | session_id=%s: %s", session_id, exc
        )
        return []


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def mark_processing(task_id: str) -> bool:
    """Mark a task as currently being processed."""
    return _update_status(task_id, "processing")


def mark_success(task_id: str) -> bool:
    """Mark a task as successfully completed."""
    return _update_status(task_id, "success", completed=True)


def mark_failed(task_id: str, error: str) -> bool:
    """
    Mark a task as permanently failed after all retries are exhausted.

    Args:
        task_id: Target task identifier.
        error: Human-readable error description for logging and summary.
    """
    try:
        get_db()["tasks"].update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "failed",
                    "error": error,
                    "completed_at": _now(),
                }
            },
        )
        logger.info("Task failed | task_id=%s error=%r", task_id, error)
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("mark_failed | task_id=%s: %s", task_id, exc)
        return False


def mark_skipped(task_id: str, reason: str) -> bool:
    """
    Mark a task as skipped (e.g. file > 2 GB, invalid URL).

    Args:
        task_id: Target task identifier.
        reason: Human-readable skip reason.
    """
    try:
        get_db()["tasks"].update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "skipped",
                    "error": reason,
                    "completed_at": _now(),
                }
            },
        )
        logger.info("Task skipped | task_id=%s reason=%r", task_id, reason)
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("mark_skipped | task_id=%s: %s", task_id, exc)
        return False


def increment_retry(task_id: str) -> int | None:
    """
    Atomically increment retry_count for a task.

    Returns the new retry_count value, or None on error.
    Used by the processor to decide whether to retry or mark as failed.
    """
    try:
        result = get_db()["tasks"].find_one_and_update(
            {"task_id": task_id},
            {"$inc": {"retry_count": 1}},
            return_document=pymongo.ReturnDocument.AFTER,
            projection={"retry_count": 1},
        )
        if result:
            return result["retry_count"]
        return None

    except pymongo.errors.PyMongoError as exc:
        logger.error("increment_retry | task_id=%s: %s", task_id, exc)
        return None


def update_url_type(task_id: str, url_type: str) -> bool:
    """
    Update the detected url_type for a task.
    Called after detector.py resolves the type during processing.
    """
    try:
        get_db()["tasks"].update_one(
            {"task_id": task_id},
            {"$set": {"url_type": url_type}},
        )
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error("update_url_type | task_id=%s: %s", task_id, exc)
        return False


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _update_status(task_id: str, status: str, completed: bool = False) -> bool:
    """Generic status update. Sets completed_at if completed=True."""
    fields: dict[str, Any] = {"status": status}
    if completed:
        fields["completed_at"] = _now()

    try:
        get_db()["tasks"].update_one(
            {"task_id": task_id},
            {"$set": fields},
        )
        return True

    except pymongo.errors.PyMongoError as exc:
        logger.error(
            "_update_status(%r) | task_id=%s: %s", status, task_id, exc
        )
        return False
