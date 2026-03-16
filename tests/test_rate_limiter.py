"""Tests for AccountManager's rate limiting, logging, and quota logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.account_manager import AccountManager


TEST_ACCOUNT_ID = "rate-test"


async def _create_manager_with_account(test_settings) -> AccountManager:
    mgr = AccountManager(test_settings)
    await mgr.init_db()

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=False)
    mock_client.get_me = AsyncMock(return_value=MagicMock(
        id=1, username="u", first_name="U"
    ))
    mock_client.add_event_handler = MagicMock()
    mock_client.remove_event_handler = MagicMock()

    with patch("app.telethon_client.create_client", return_value=mock_client):
        await mgr.add_account(
            account_id=TEST_ACCOUNT_ID,
            api_id=0,
            api_hash="test",
            max_messages_per_day=5,
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
    return mgr


async def test_init_db_creates_schema(test_settings):
    mgr = AccountManager(test_settings)
    await mgr.init_db()
    count = await mgr.get_today_send_count(TEST_ACCOUNT_ID)
    assert count == 0
    await mgr.shutdown_all()


async def test_log_send_writes_records(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    await mgr.log_send(TEST_ACCOUNT_ID, "@user1", "hello", 42, "success")
    await mgr.log_send(TEST_ACCOUNT_ID, "@user2", "hi", None, "error", "some error")
    count = await mgr.get_today_send_count(TEST_ACCOUNT_ID)
    assert count == 1
    await mgr.shutdown_all()


async def test_get_today_send_count_only_today(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    await mgr.db.execute(
        "INSERT INTO send_log (account_id, recipient, message, telegram_message_id, status, error, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (TEST_ACCOUNT_ID, "@old", "msg", 1, "success", None, "2020-01-01T00:00:00+00:00"),
    )
    await mgr.db.commit()

    await mgr.log_send(TEST_ACCOUNT_ID, "@today", "msg", 2, "success")
    count = await mgr.get_today_send_count(TEST_ACCOUNT_ID)
    assert count == 1
    await mgr.shutdown_all()


async def test_is_quota_available(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    assert await mgr.is_quota_available(TEST_ACCOUNT_ID, 2) is True
    await mgr.log_send(TEST_ACCOUNT_ID, "@u1", "m", 1, "success")
    assert await mgr.is_quota_available(TEST_ACCOUNT_ID, 2) is True
    await mgr.log_send(TEST_ACCOUNT_ID, "@u2", "m", 2, "success")
    assert await mgr.is_quota_available(TEST_ACCOUNT_ID, 2) is False
    await mgr.shutdown_all()


async def test_worker_applies_delay(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    send_fn = AsyncMock(return_value=99)

    with patch("app.telethon_client.send_message", send_fn):
        await mgr.mark_authorized(TEST_ACCOUNT_ID, 1, "u")
        future = await mgr.enqueue_message(TEST_ACCOUNT_ID, "@user", "hello")
        result = await asyncio.wait_for(future, timeout=5)
        assert result == 99

    await mgr.shutdown_all()


async def test_worker_handles_send_error(test_settings):
    import pytest
    mgr = await _create_manager_with_account(test_settings)
    send_fn = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("app.telethon_client.send_message", send_fn):
        await mgr.mark_authorized(TEST_ACCOUNT_ID, 1, "u")
        future = await mgr.enqueue_message(TEST_ACCOUNT_ID, "@user", "hello")
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(future, timeout=5)

    await mgr.shutdown_all()
