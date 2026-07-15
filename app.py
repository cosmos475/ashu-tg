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

import asyncio
import logging
import os
import threading

import telebot
import flask

import config  # noqa: F401 — imported for side effects (dirs, logging, validation)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Python 3.14 compatibility workaround for Pyrogram 2.0.106.
# Pyrogram's sync.py calls asyncio.get_event_loop() at import time; Python
# 3.14 raises RuntimeError if no event loop is set on the current thread
# (previously this silently created one). This sets a loop on the main
# thread purely so that import succeeds. It is unrelated to, and does not
# replace, the dedicated background event loop Pyrogram's client actually
# runs on (see bot/services/pyrogram_client.py).
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# ---------------------------------------------------------------------------
# Run mode: "webhook" (default, Render/production) or "polling" (local/Termux)
# Set RUN_MODE=polling in your local shell to skip webhook registration and
# use TeleBot's long-polling instead. Production behavior is unchanged when
# RUN_MODE is unset.
# ---------------------------------------------------------------------------
RUN_MODE: str = os.getenv("RUN_MODE", "webhook").strip().lower()

# ---------------------------------------------------------------------------
# Bot instance
# Single shared instance, defined in bot_instance.py so that app.py (loaded
# both as '__main__' and, via handler imports, as 'app') and every handler
# module all reference the exact same TeleBot object.
# ---------------------------------------------------------------------------
from bot_instance import bot  # noqa: E402

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
#
# Guarded against double execution: running `python app.py` loads this file
# as '__main__', and the first handler that does `from app import bot`
# triggers Python to separately import it again as 'app' (two distinct
# entries in sys.modules for the same file, each running this file's
# top-level code independently). A guard flag stored on both possible
# sys.modules entries ('__main__' and 'app') ensures the startup sequence
# below only executes once, regardless of which one runs first.
# ---------------------------------------------------------------------------
import sys as _sys


def _startup_already_ran() -> bool:
    for _name in ("__main__", "app"):
        _mod = _sys.modules.get(_name)
        if _mod is not None and getattr(_mod, "_STARTUP_DONE", False):
            return True
    return False


def _mark_startup_done() -> None:
    for _name in ("__main__", "app"):
        _mod = _sys.modules.get(_name)
        if _mod is not None:
            _mod._STARTUP_DONE = True


if not _startup_already_ran():
    _mark_startup_done()

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

    # In polling mode there is no Gunicorn/WSGI server to keep the process
    # alive, so block the main thread here for as long as the polling
    # thread is running. Render/webhook mode is unaffected: Gunicorn keeps
    # the process alive by serving `app`, and this branch is never reached
    # there since RUN_MODE defaults to "webhook".
    if RUN_MODE == "polling":
        for _thread in threading.enumerate():
            if _thread.name == "telebot-polling":
                _thread.join()
                break
