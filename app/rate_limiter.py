"""Rate limiting, message queueing, and send logging.

This is the core anti-ban protection layer. It enforces a daily quota,
queues outgoing messages with random delays, and logs every send attempt
to an SQLite database for auditing and quota tracking.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from app.config import Settings

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS send_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient           TEXT NOT NULL,
    message             TEXT NOT NULL,
    telegram_message_id INTEGER,
    status              TEXT NOT NULL,
    error               TEXT,
    sent_at             TEXT NOT NULL
)
"""

_db: aiosqlite.Connection | None = None


# --- Database ---

async def init_db(db_path: str) -> None:
    """Open the SQLite connection and create the ``send_log`` table if needed."""
    global _db
    _db = await aiosqlite.connect(db_path)
    await _db.execute(_CREATE_TABLE)
    await _db.commit()


async def close_db() -> None:
    """Close the SQLite connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def log_send(
    recipient: str,
    message: str,
    telegram_message_id: int | None,
    status: str,
    error: str | None = None,
) -> None:
    """Insert a send attempt record into ``send_log``.

    Args:
        recipient: Recipient identifier (username or user_id as string).
        message: Message text that was sent.
        telegram_message_id: Telegram's message ID on success, ``None`` on error.
        status: ``"success"`` or ``"error"``.
        error: Error description (optional, used when status is ``"error"``).

    Raises:
        RuntimeError: If the database is not initialized.
    """
    if _db is None:
        raise RuntimeError("Database not initialized")
    await _db.execute(
        "INSERT INTO send_log (recipient, message, telegram_message_id, status, error, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recipient, message, telegram_message_id, status, error, datetime.now(UTC).isoformat()),
    )
    await _db.commit()


async def get_today_send_count() -> int:
    """Return the number of successful sends since midnight UTC today.

    Raises:
        RuntimeError: If the database is not initialized.
    """
    if _db is None:
        raise RuntimeError("Database not initialized")
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with _db.execute(
        "SELECT COUNT(*) FROM send_log WHERE status='success' AND sent_at >= ?",
        (today_start,),
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def is_quota_available(max_per_day: int) -> bool:
    """Check whether today's successful send count is below the daily limit."""
    return await get_today_send_count() < max_per_day


# --- Queue & Worker ---

SendFn = Callable[[str | int, str], Awaitable[int]]
"""Signature for the message-sending callable (matches ``telethon_client.send_message``)."""


@dataclass
class _QueueItem:
    """Internal queue entry holding a pending message and its result future."""

    recipient: str | int
    message: str
    future: asyncio.Future[int]


_queue: asyncio.Queue[_QueueItem] | None = None
_worker_task: asyncio.Task | None = None


def _get_queue() -> asyncio.Queue[_QueueItem]:
    """Return the message queue, creating it lazily if needed."""
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def _worker(send_fn: SendFn, settings: Settings) -> None:
    """Background loop that processes queued messages one-by-one.

    For each item:
    1. Sleep for a random delay (``min_delay_seconds`` .. ``max_delay_seconds``)
    2. Re-check the daily quota
    3. Call ``send_fn`` to deliver the message
    4. Log the result to SQLite
    5. Resolve or reject the caller's ``Future``
    """
    queue = _get_queue()
    while True:
        item = await queue.get()
        try:
            delay = random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
            await asyncio.sleep(delay)

            if not await is_quota_available(settings.max_messages_per_day):
                item.future.set_exception(
                    RuntimeError("Daily message quota exhausted")
                )
                await log_send(str(item.recipient), item.message, None, "error", "quota_exhausted")
                continue

            message_id = await send_fn(item.recipient, item.message)
            await log_send(str(item.recipient), item.message, message_id, "success")
            item.future.set_result(message_id)
        except Exception as exc:
            error_msg = str(exc)
            await log_send(str(item.recipient), item.message, None, "error", error_msg)
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            queue.task_done()


def start_worker(send_fn: SendFn, settings: Settings) -> None:
    """Create a fresh queue and spawn the background worker task."""
    global _worker_task, _queue
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_worker(send_fn, settings))


async def stop_worker() -> None:
    """Cancel the worker task and discard the queue."""
    global _worker_task, _queue
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    _queue = None


async def enqueue_message(recipient: str | int, message: str) -> asyncio.Future[int]:
    """Add a message to the send queue.

    Args:
        recipient: ``@username`` or numeric Telegram user ID.
        message: Message text to send.

    Returns:
        A ``Future`` that resolves with the Telegram message ID once the
        message is actually sent, or rejects with an exception on failure.
    """
    queue = _get_queue()
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    await queue.put(_QueueItem(recipient=recipient, message=message, future=future))
    return future
