"""
bot/services/pyrogram_client.py — Pyrogram MTProto client singleton.

Runs one Pyrogram Client inside one dedicated asyncio event loop on a
background daemon thread. The rest of the app (sync) dispatches coroutines
into this loop via run_coroutine_threadsafe() and blocks on the future.

No polling, no separate web server — this loop only services outgoing
MTProto calls (uploads). Incoming updates still go through the Flask
webhook + pyTelegramBotAPI exactly as before.
"""

import asyncio
import concurrent.futures
import logging
import threading
import time

from pyrogram import Client

import config

logger = logging.getLogger(__name__)

_client: Client | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()


def start() -> None:
    """
    Start the background event loop + Pyrogram client.
    Call once at app startup. Idempotent.
    """
    global _thread
    if _thread is not None:
        return

    _thread = threading.Thread(target=_run_loop, daemon=True, name="pyrogram-loop")
    _thread.start()
    _ready.wait(timeout=30)
    if not _ready.is_set():
        raise RuntimeError("Pyrogram client failed to start within 30s")
    logger.info("Pyrogram client started.")


def get_client() -> Client:
    """Return the running Pyrogram Client instance."""
    if _client is None:
        raise RuntimeError("Pyrogram client not started — call start() first")
    return _client


def run_coro(
    coro,
    timeout: float = 1800.0,
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.5,
):
    """
    Submit a coroutine to the Pyrogram event loop from any sync thread
    and block until it completes. Used by the uploader for every upload.

    Args:
        coro:          Coroutine to run (e.g. client.send_video(...)).
        timeout:       Max seconds to wait (default 30 min — large file uploads).
        cancel_event:  Optional threading.Event. If set while waiting, the
                       in-flight coroutine is cancelled immediately (via
                       asyncio's future-chaining cancellation) instead of
                       blocking until it finishes or times out.
        poll_interval: How often to check cancel_event while waiting.

    Returns:
        The coroutine's result.

    Raises:
        asyncio.CancelledError if cancel_event was set.
        Whatever exception the coroutine raises, or TimeoutError.
    """
    if _loop is None:
        raise RuntimeError("Pyrogram event loop not running")

    future = asyncio.run_coroutine_threadsafe(coro, _loop)

    if cancel_event is None:
        return future.result(timeout=timeout)

    deadline = time.monotonic() + timeout
    while True:
        if cancel_event.is_set():
            logger.info("Cancellation requested — cancelling active Pyrogram task.")
            future.cancel()
            try:
                # Give the loop a brief moment to unwind the cancelled task
                # cleanly, but don't block the caller waiting for it —
                # releasing the processing lock promptly matters more than
                # confirming the remote task fully tore down.
                future.result(timeout=5.0)
            except Exception:
                logger.debug(
                    "Cancelled Pyrogram task teardown (non-fatal)", exc_info=True
                )
            raise asyncio.CancelledError("Upload cancelled by user")

        try:
            return future.result(timeout=poll_interval)
        except concurrent.futures.TimeoutError:
            if time.monotonic() >= deadline:
                logger.error(
                    "Upload exceeded %.0fs timeout — cancelling task", timeout
                )
                future.cancel()
                raise TimeoutError(f"Upload exceeded {timeout}s timeout")
            continue


def _run_loop() -> None:
    """Thread target: create event loop, start Pyrogram client, run forever."""
    global _client, _loop

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    try:
        _client = Client(
            name="uploader_bot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            no_updates=True,
        )
    except Exception:
        logger.exception("Pyrogram Client construction failed")
        _ready.set()
        raise

    async def _startup():
        await _client.start()
        logger.info("Pyrogram MTProto session established.")

    try:
        _loop.run_until_complete(_startup())
        _ready.set()
        _loop.run_forever()
    except Exception:
        logger.exception("Pyrogram loop crashed")
        _ready.set()
        raise
