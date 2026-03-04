"""Incoming message listener for tracking lead replies.

Listens for private messages via Telethon's NewMessage event, filters for
known recipients (leads already contacted via send_log), logs them to an
``incoming_log`` SQLite table, and dispatches to registered callbacks and
an optional webhook URL.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypedDict

import aiosqlite
import httpx
from telethon import TelegramClient, events

from app.config import Settings

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS incoming_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id           TEXT NOT NULL,
    sender_username     TEXT,
    message_text        TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    received_at         TEXT NOT NULL,
    processed           INTEGER NOT NULL DEFAULT 0
)
"""

_db: aiosqlite.Connection | None = None
_client: TelegramClient | None = None
_http_client: httpx.AsyncClient | None = None
_handlers: list[Callable[["IncomingMessage"], Awaitable[None]]] = []


class IncomingMessage(TypedDict):
    id: int
    sender_id: str
    sender_username: str | None
    message_text: str
    telegram_message_id: int
    chat_id: int
    received_at: str
    processed: bool


# --- Database ---

async def init_db(db_path: str) -> None:
    global _db
    _db = await aiosqlite.connect(db_path)
    await _db.execute(_CREATE_TABLE)
    await _db.commit()


async def close_db() -> None:
    global _db, _http_client
    if _db is not None:
        await _db.close()
        _db = None
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def log_incoming(
    sender_id: str,
    sender_username: str | None,
    message_text: str,
    telegram_message_id: int,
    chat_id: int,
) -> int:
    if _db is None:
        raise RuntimeError("Listener database not initialized")
    cursor = await _db.execute(
        "INSERT INTO incoming_log "
        "(sender_id, sender_username, message_text, telegram_message_id, chat_id, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sender_id, sender_username, message_text, telegram_message_id, chat_id,
         datetime.now(UTC).isoformat()),
    )
    await _db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_recent_incoming(
    limit: int = 50,
    unprocessed_only: bool = False,
) -> list[IncomingMessage]:
    if _db is None:
        raise RuntimeError("Listener database not initialized")
    query = "SELECT id, sender_id, sender_username, message_text, telegram_message_id, chat_id, received_at, processed FROM incoming_log"
    if unprocessed_only:
        query += " WHERE processed = 0"
    query += " ORDER BY id DESC LIMIT ?"
    async with _db.execute(query, (limit,)) as cursor:
        rows = await cursor.fetchall()
    return [
        IncomingMessage(
            id=row[0],
            sender_id=row[1],
            sender_username=row[2],
            message_text=row[3],
            telegram_message_id=row[4],
            chat_id=row[5],
            received_at=row[6],
            processed=bool(row[7]),
        )
        for row in rows
    ]


async def mark_processed(incoming_id: int) -> None:
    if _db is None:
        raise RuntimeError("Listener database not initialized")
    await _db.execute(
        "UPDATE incoming_log SET processed = 1 WHERE id = ?",
        (incoming_id,),
    )
    await _db.commit()


# --- Recipient matching ---

async def _is_known_recipient(sender_id: int, sender_username: str | None) -> bool:
    if _db is None:
        return False
    try:
        conditions = ["recipient = ?"]
        params: list[str] = [str(sender_id)]
        if sender_username:
            clean = sender_username.lstrip("@")
            conditions.append("recipient = ?")
            params.append(f"@{clean}")
            conditions.append("recipient = ?")
            params.append(clean)
        where = " OR ".join(conditions)
        async with _db.execute(
            f"SELECT COUNT(*) FROM send_log WHERE ({where}) AND status = 'success'",
            params,
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0] if row else 0) > 0
    except Exception:
        logger.exception("Error checking known recipient")
        return False


# --- Callback mechanism ---

def add_incoming_handler(callback: Callable[[IncomingMessage], Awaitable[None]]) -> None:
    _handlers.append(callback)


def remove_incoming_handler(callback: Callable[[IncomingMessage], Awaitable[None]]) -> None:
    _handlers.remove(callback)


async def _dispatch_callbacks(msg: IncomingMessage) -> None:
    for handler in _handlers:
        try:
            await handler(msg)
        except Exception:
            logger.exception("Error in incoming message handler")


# --- Webhook forwarding ---

async def _forward_to_webhook(msg: IncomingMessage, webhook_url: str) -> None:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=10)
    try:
        await _http_client.post(webhook_url, json=dict(msg))
    except Exception:
        logger.exception("Error forwarding to webhook %s", webhook_url)


# --- Event handler ---

async def _on_new_message(event: events.NewMessage.Event, settings: Settings) -> None:
    sender = await event.get_sender()
    if sender is None:
        return

    text = event.message.text
    if not text:
        return

    sender_id = sender.id
    sender_username = getattr(sender, "username", None)

    if not await _is_known_recipient(sender_id, sender_username):
        return

    row_id = await log_incoming(
        sender_id=str(sender_id),
        sender_username=sender_username,
        message_text=text,
        telegram_message_id=event.message.id,
        chat_id=event.chat_id,
    )

    msg = IncomingMessage(
        id=row_id,
        sender_id=str(sender_id),
        sender_username=sender_username,
        message_text=text,
        telegram_message_id=event.message.id,
        chat_id=event.chat_id,
        received_at=datetime.now(UTC).isoformat(),
        processed=False,
    )

    await _dispatch_callbacks(msg)

    if settings.incoming_webhook_url:
        await _forward_to_webhook(msg, settings.incoming_webhook_url)


# --- Lifecycle ---

async def start_listener(client: TelegramClient | None, settings: Settings) -> None:
    global _client
    if client is None:
        logger.warning("No Telethon client; listener not started")
        return
    _client = client
    _client.add_event_handler(
        lambda event: _on_new_message(event, settings),
        events.NewMessage(incoming=True, outgoing=False, func=lambda e: e.is_private),
    )
    logger.info("Incoming message listener started")


async def stop_listener() -> None:
    global _client
    _handlers.clear()
    _client = None
    logger.info("Incoming message listener stopped")
