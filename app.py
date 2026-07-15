"""
app.py — Flask application entry point.

Responsibilities:
- Create the Flask app.
- Register the single Telegram webhook route.
- Validate the Telegram webhook secret token on every incoming request.
- Pass verified updates to the telebot dispatcher.
- Register the bot's webhook URL with Telegram at startup.

Gunicorn starts this file via: gunicorn app:app
"""

import logging
import os
import threading

import telebot
import flask

import config  # noqa: F401 — imported for side effects (dirs, logging, validation)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Run mode: "webhook" (default, Render/production) or "polling" (local/Termux)
# Set RUN_MODE=polling in your local shell to skip webhook registration and
# use TeleBot's long-polling instead. Production behavior is unchanged when
# RUN_MODE is unset.
# ---------------------------------------------------------------------------
RUN_MODE: str = os.getenv("RUN_MODE", "webhook").strip().lower()

# ---------------------------------------------------------------------------
# Bot instance
# Imported by handlers (Chat 6). Created here so there is a single instance.
# ---------------------------------------------------------------------------
bot = telebot.TeleBot(
    token=config.BOT_TOKEN,
    threaded=False,   # We manage our own processing thread; disable telebot's internal threading
    parse_mode="HTML",
)

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = flask.Flask(__name__)


@app.route(f"/{config.BOT_TOKEN}", methods=["POST"])
def webhook() -> flask.Response:
    """
    Receives incoming Telegram updates via webhook.

    Security: Telegram sends the WEBHOOK_SECRET in the
    'X-Telegram-Bot-Api-Secret-Token' header. Requests without a valid
    secret are rejected with 403 to prevent unauthorized access.
    """
    # --- Secret token validation ---
    incoming_secret = flask.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if incoming_secret != config.WEBHOOK_SECRET:
        logger.warning("Rejected webhook request: invalid or missing secret token.")
        return flask.Response("Forbidden", status=403)

    # --- Parse and dispatch the update ---
    if flask.request.headers.get("Content-Type") == "application/json":
        json_data = flask.request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
        return flask.Response("OK", status=200)

    logger.warning("Received non-JSON request on webhook endpoint.")
    return flask.Response("Bad Request", status=400)


@app.route("/health", methods=["GET"])
def health() -> flask.Response:
    """
    Health check endpoint.
    Used by Render's health check and for /ping command verification.
    Returns 200 OK if the service is running.
    """
    return flask.Response("OK", status=200)


# ---------------------------------------------------------------------------
# Register webhook with Telegram at startup
# ---------------------------------------------------------------------------
def register_webhook() -> None:
    """
    Tells Telegram where to send updates (our webhook URL).
    Called once at process start, before serving requests.
    Idempotent — safe to call on every Render restart.
    """
    webhook_url = f"{config.WEBHOOK_URL.rstrip('/')}/{config.BOT_TOKEN}"
    try:
        bot.remove_webhook()
        bot.set_webhook(
            url=webhook_url,
            secret_token=config.WEBHOOK_SECRET,
            # Allow Telegram some time to retry if our service is cold-starting
            drop_pending_updates=True,
        )
        logger.info("Webhook registered: %s", webhook_url)
    except Exception as exc:
        logger.error("Failed to register webhook: %s", exc)
        raise


def _start_polling() -> None:
    """
    Local/Termux mode: run TeleBot long-polling in a background daemon
    thread instead of registering a webhook. Used only when
    RUN_MODE=polling. Production webhook flow is untouched.
    """
    bot.remove_webhook()
    logger.info("Polling mode: webhook removed, starting long-polling.")

    def _poll_loop() -> None:
        bot.infinity_polling(skip_pending=True)

    threading.Thread(target=_poll_loop, name="telebot-polling", daemon=True).start()


# ---------------------------------------------------------------------------
# Handler registration (imported here to register handlers onto `bot`)
# Chat 6 will populate these modules. The imports are guarded so that
# missing modules during early development do not crash the app.
# ---------------------------------------------------------------------------
def _register_handlers() -> None:
    try:
        import bot.handlers.command_handlers   # noqa: F401
        import bot.handlers.file_handlers      # noqa: F401
        import bot.handlers.channel_handlers   # noqa: F401
        logger.info("All handlers registered.")
    except ImportError as exc:
        # During Chat 1–5 development the handler modules don't exist yet.
        # This warning is expected and harmless until Chat 6.
        logger.warning("Handler modules not yet available: %s", exc)


# ---------------------------------------------------------------------------
# Startup sequence
# ---------------------------------------------------------------------------
_register_handlers()

if RUN_MODE == "polling":
    _start_polling()
else:
    register_webhook()

from bot.services import pyrogram_client
try:
    pyrogram_client.start()
except Exception:
    logger.exception("pyrogram_client.start() failed at startup")
    raise

logger.info(
    "Bot service started | owner_id=%s | port=%s",
    config.OWNER_ID,
    config.PORT,
)
