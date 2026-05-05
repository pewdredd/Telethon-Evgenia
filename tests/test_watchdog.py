import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.account_manager import AccountManager
from app.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        api_key="test-key",
        max_messages_per_day=5,
        min_delay_seconds=0,
        max_delay_seconds=0,
        db_path=str(tmp_path / "wd.db"),
        sessions_dir=str(tmp_path / "sessions"),
        watchdog_interval_seconds=0,
        max_reconnect_attempts=2,
        reconnect_backoff_base_seconds=0,
    )


def _mock_client(connected_sequence):
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_me = AsyncMock(return_value=MagicMock(id=1, username="u", first_name="t"))
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    iterator = iter(connected_sequence)

    def _is_connected():
        try:
            return next(iterator)
        except StopIteration:
            return True

    client.is_connected = MagicMock(side_effect=_is_connected)
    return client


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = AccountManager(_settings(tmp_path))
    await mgr.init_db()
    yield mgr
    await mgr.shutdown_all()


async def test_watchdog_reconnects_when_disconnected(manager):
    # Connected at startup, then disconnected on next watchdog tick, then back up.
    client = _mock_client([True, False, True, True, True])
    with patch("app.telethon_client.create_client", return_value=client):
        await manager.add_account(account_id="acc", api_id=1, api_hash="h")
        await manager.mark_authorized("acc", 1, "u")
        # Let watchdog run a couple of cycles (interval=0).
        for _ in range(20):
            await asyncio.sleep(0)
        assert client.connect.await_count >= 2  # initial + reconnect


async def test_restart_account_recreates_client(manager):
    client_a = _mock_client([True] * 20)
    client_b = _mock_client([True] * 20)
    clients = iter([client_a, client_b])
    with patch("app.telethon_client.create_client", side_effect=lambda *a, **kw: next(clients)):
        await manager.add_account(account_id="acc", api_id=1, api_hash="h")
        await manager.mark_authorized("acc", 1, "u")
        await manager.restart_account("acc")
        # First client should have been disconnected; second should be active in state.
        client_a.disconnect.assert_awaited()
        state = manager.get_account("acc")
        assert state.client is client_b


async def test_inline_reconnect_in_worker(manager):
    # Worker should call connect() if client is_connected==False before send.
    # Use a sequence: True (startup auth check) → False (worker pre-send) → True (after reconnect).
    client = _mock_client([True, False] + [True] * 30)
    sent = AsyncMock(return_value=(99, 1))
    with patch("app.telethon_client.create_client", return_value=client), \
         patch("app.telethon_client.send_message", sent):
        await manager.add_account(account_id="acc", api_id=1, api_hash="h")
        await manager.mark_authorized("acc", 1, "u")
        fut = await manager.enqueue_message("acc", "@u", "hi")
        await asyncio.wait_for(fut, timeout=2.0)
        # connect() called at startup + at least once inline before send.
        assert client.connect.await_count >= 2
        sent.assert_awaited_once()
