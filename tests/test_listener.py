from unittest.mock import AsyncMock

import pytest

from app.listener import (
    _is_known_recipient,
    add_incoming_handler,
    close_db,
    get_recent_incoming,
    init_db,
    log_incoming,
    mark_processed,
    remove_incoming_handler,
)
from app.rate_limiter import init_db as rl_init_db, close_db as rl_close_db, log_send


async def test_init_db_creates_incoming_table(test_settings):
    await init_db(test_settings.db_path)
    messages = await get_recent_incoming()
    assert messages == []
    await close_db()


async def test_log_incoming_writes_record(listener_db):
    row_id = await log_incoming(
        sender_id="123",
        sender_username="alice",
        message_text="hello",
        telegram_message_id=1001,
        chat_id=123,
    )
    assert row_id is not None
    messages = await get_recent_incoming()
    assert len(messages) == 1
    assert messages[0]["sender_id"] == "123"
    assert messages[0]["message_text"] == "hello"
    assert messages[0]["processed"] is False


async def test_get_recent_incoming_limit_and_order(listener_db):
    for i in range(5):
        await log_incoming(
            sender_id=str(i),
            sender_username=None,
            message_text=f"msg{i}",
            telegram_message_id=i,
            chat_id=i,
        )
    messages = await get_recent_incoming(limit=3)
    assert len(messages) == 3
    # newest first (DESC order)
    assert messages[0]["sender_id"] == "4"
    assert messages[2]["sender_id"] == "2"


async def test_unprocessed_only_filter(listener_db):
    id1 = await log_incoming("1", None, "a", 1, 1)
    await log_incoming("2", None, "b", 2, 2)
    await mark_processed(id1)

    all_msgs = await get_recent_incoming()
    assert len(all_msgs) == 2

    unprocessed = await get_recent_incoming(unprocessed_only=True)
    assert len(unprocessed) == 1
    assert unprocessed[0]["sender_id"] == "2"


async def test_mark_processed(listener_db):
    row_id = await log_incoming("1", None, "x", 1, 1)
    await mark_processed(row_id)
    messages = await get_recent_incoming()
    assert messages[0]["processed"] is True


async def test_is_known_recipient_by_user_id(test_settings):
    """Recipient found by numeric user_id stored as string in send_log."""
    await rl_init_db(test_settings.db_path)
    await init_db(test_settings.db_path)
    await log_send("12345", "hi", 1, "success")

    assert await _is_known_recipient(12345, None) is True
    assert await _is_known_recipient(99999, None) is False

    await close_db()
    await rl_close_db()


async def test_is_known_recipient_by_username(test_settings):
    """Recipient found by @username in send_log."""
    await rl_init_db(test_settings.db_path)
    await init_db(test_settings.db_path)
    await log_send("@alice", "hi", 1, "success")

    assert await _is_known_recipient(0, "alice") is True
    assert await _is_known_recipient(0, "@alice") is True
    assert await _is_known_recipient(0, "bob") is False

    await close_db()
    await rl_close_db()


async def test_is_known_recipient_ignores_errors(test_settings):
    """Returns False when DB is not initialized (no crash)."""
    result = await _is_known_recipient(123, "user")
    assert result is False


async def test_callback_mechanism(listener_db):
    received = []
    handler = AsyncMock(side_effect=lambda msg: received.append(msg))

    add_incoming_handler(handler)
    row_id = await log_incoming("1", "user", "hi", 1, 1)

    from app.listener import IncomingMessage, _dispatch_callbacks
    msg = IncomingMessage(
        id=row_id,
        sender_id="1",
        sender_username="user",
        message_text="hi",
        telegram_message_id=1,
        chat_id=1,
        received_at="2026-01-01T00:00:00",
        processed=False,
    )
    await _dispatch_callbacks(msg)

    handler.assert_called_once()
    assert received[0]["sender_id"] == "1"

    remove_incoming_handler(handler)


async def test_get_incoming_endpoint(client):
    resp = await client.get("/incoming", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["messages"] == []
    assert data["count"] == 0
