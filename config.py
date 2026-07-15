"""
config.py — Central configuration module.

Loads all environment variables, validates required values at startup,
and creates necessary runtime directories (tmp/, logs/).

Every other module imports from here. Never hardcode values elsewhere.
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env file (only active in local development; ignored on Render)
# ---------------------------------------------------------------------------
load_dotenv()


# ---------------------------------------------------------------------------
# Helper: fetch a required env var or abort at startup
# ---------------------------------------------------------------------------
def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"[CONFIG ERROR] Required environment variable '{name}' is missing or empty.")
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"[CONFIG ERROR] Environment variable '{name}' must be an integer. Got: {raw!r}")


# ---------------------------------------------------------------------------
# Core secrets & identifiers
# ---------------------------------------------------------------------------
BOT_TOKEN: str = _require("BOT_TOKEN")
OWNER_ID: int = _require_int("OWNER_ID")
API_ID: int = _require_int("API_ID")
API_HASH: str = _require("API_HASH")
MONGO_URI: str = _require("MONGO_URI")
WEBHOOK_SECRET: str = _require("WEBHOOK_SECRET")   # Telegram webhook secret token
WEBHOOK_URL: str = _require("WEBHOOK_URL")         # Full public URL, e.g. https://yourapp.onrender.com

# ---------------------------------------------------------------------------
# Optional / defaulted settings
# ---------------------------------------------------------------------------
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "telegram_bot").strip()
PORT: int = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
TMP_DIR: Path = BASE_DIR / "tmp"
LOGS_DIR: Path = BASE_DIR / "logs"

# ---------------------------------------------------------------------------
# Processing limits
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES: int = 2 * 1024 * 1024 * 1024   # 2 GB — Pyrogram MTProto upload limit
MAX_CAPTION_LENGTH: int = 1024                        # Telegram caption character limit
MAX_RETRIES: int = 3                                  # Max attempts per task before marking failed
RETRY_BACKOFF_BASE: float = 2.0                       # Exponential backoff base (seconds)
PROGRESS_UPDATE_INTERVAL: float = 3.0                 # Minimum seconds between progress message edits

# ---------------------------------------------------------------------------
# Create runtime directories at import time
# Render's ephemeral filesystem won't have these — must create on every start.
# ---------------------------------------------------------------------------
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging configuration
# Structured format includes timestamp, level, module name, and message.
# RotatingFileHandler prevents logs/ from filling Render's ephemeral disk.
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Root logger
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_DATE_FORMAT,
    handlers=[
        # Console output (visible in Render logs dashboard)
        logging.StreamHandler(sys.stdout),
        # Rotating file: max 5 MB per file, keep 3 backups
        RotatingFileHandler(
            LOGS_DIR / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)

# Silence noisy third-party loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telebot").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# --- TEMP DIAGNOSTIC: Pyrogram DEBUG logging ---
# Enabled to trace upload-hang investigation (DC connect/export, session
# pool creation, save_file_part activity). Remove once root cause is found.
# Handlers are attached to the root logger at INFO level, so DEBUG records
# from specific loggers below only surface because we lower those loggers'
# own level; we also need the handlers themselves to not filter DEBUG out —
# StreamHandler/RotatingFileHandler have no level set (defaults to NOTSET,
# i.e. pass everything), so this is sufficient without changing root level.
logging.getLogger("pyrogram").setLevel(logging.DEBUG)
logging.getLogger("bot.services.uploader").setLevel(logging.DEBUG)
