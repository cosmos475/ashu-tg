"""
bot/db/client.py — MongoDB client singleton.

Provides a single MongoClient instance shared across all repositories.
Implements connection retry with exponential backoff at startup so that
a brief MongoDB Atlas unavailability does not crash the bot on Render restart.

Usage (in repositories):
    from bot.db.client import get_db
    db = get_db()
    collection = db["collection_name"]
"""

import logging
import time

import pymongo
import pymongo.errors
from pymongo import MongoClient
from pymongo.database import Database

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — created once, reused by all repositories.
# ---------------------------------------------------------------------------
_client: MongoClient | None = None
_db: Database | None = None


def get_db() -> Database:
    """
    Return the shared MongoDB Database instance.
    Initialises the connection on first call (lazy singleton).
    Thread-safe for read access after initialisation.
    """
    global _client, _db

    if _db is not None:
        return _db

    _client, _db = _connect_with_retry()
    _ensure_indexes(_db)
    return _db


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect_with_retry() -> tuple[MongoClient, Database]:
    """
    Attempt to connect to MongoDB with exponential backoff.
    Retries up to MAX_RETRIES times before raising.

    Backoff schedule (base=2.0):
        attempt 1 → 2 s
        attempt 2 → 4 s
        attempt 3 → 8 s
    """
    last_exc: Exception | None = None

    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            logger.info("MongoDB connect attempt %d/%d …", attempt, config.MAX_RETRIES)

            client: MongoClient = MongoClient(
                config.MONGO_URI,
                serverSelectionTimeoutMS=10_000,   # 10 s per attempt
                connectTimeoutMS=10_000,
                socketTimeoutMS=30_000,
            )

            # ping forces an actual network round-trip to verify connectivity
            client.admin.command("ping")

            db: Database = client[config.MONGO_DB_NAME]
            logger.info("MongoDB connected — database: %s", config.MONGO_DB_NAME)
            return client, db

        except pymongo.errors.PyMongoError as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "MongoDB connection failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt, config.MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

    # All attempts exhausted — fatal startup error
    logger.error("MongoDB connection failed after %d attempts.", config.MAX_RETRIES)
    raise RuntimeError(
        f"Cannot connect to MongoDB after {config.MAX_RETRIES} attempts."
    ) from last_exc


def _ensure_indexes(db: Database) -> None:
    """
    Create required indexes if they do not already exist.
    pymongo's create_index is idempotent — safe to call on every startup.
    """
    try:
        # tasks.session_id — fast per-session queries (status counts, task list)
        db["tasks"].create_index(
            [("session_id", pymongo.ASCENDING)],
            name="idx_tasks_session_id",
            background=True,
        )

        # tasks.status — fast filtering by status (pending, failed, etc.)
        db["tasks"].create_index(
            [("status", pymongo.ASCENDING)],
            name="idx_tasks_status",
            background=True,
        )

        # Compound index for the most common query: tasks for a session by status
        db["tasks"].create_index(
            [("session_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING)],
            name="idx_tasks_session_status",
            background=True,
        )

        logger.info("MongoDB indexes verified.")

    except pymongo.errors.PyMongoError as exc:
        # Non-fatal — indexes are a performance optimisation, not a correctness requirement
        logger.warning("Failed to create indexes: %s", exc)
