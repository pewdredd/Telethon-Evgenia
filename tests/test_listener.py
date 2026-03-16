"""Tests for incoming message logging via AccountManager."""

from unittest.mock import AsyncMock, MagicMock, patch

from app.account_manager import AccountManager

from tests.conftest import TEST_ACCOUNT_ID


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
        )
    return mgr


async def test_log_incoming_writes_record(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    row_id = await mgr.log_incoming(
        account_id=TEST_ACCOUNT_ID,
        sender_id="123",
        sender_username="alice",
        message_text="hello",
        telegram_message_id=1001,
        chat_id=123,
    )
    assert row_id is not None
    messages = await mgr.get_recent_incoming(account_id=TEST_ACCOUNT_ID)
    assert len(messages) == 1
    assert messages[0]["sender_id"] == "123"
    assert messages[0]["message_text"] == "hello"
    assert messages[0]["processed"] is False
    assert messages[0]["account_id"] == TEST_ACCOUNT_ID
    await mgr.shutdown_all()


async def test_get_recent_incoming_limit_and_order(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    for i in range(5):
        await mgr.log_incoming(
            account_id=TEST_ACCOUNT_ID,
            sender_id=str(i),
            sender_username=None,
            message_text=f"msg{i}",
            telegram_message_id=i,
            chat_id=i,
        )
    messages = await mgr.get_recent_incoming(account_id=TEST_ACCOUNT_ID, limit=3)
    assert len(messages) == 3
    assert messages[0]["sender_id"] == "4"
    assert messages[2]["sender_id"] == "2"
    await mgr.shutdown_all()


async def test_unprocessed_only_filter(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    id1 = await mgr.log_incoming(TEST_ACCOUNT_ID, "1", None, "a", 1, 1)
    await mgr.log_incoming(TEST_ACCOUNT_ID, "2", None, "b", 2, 2)
    await mgr.mark_processed(id1)

    all_msgs = await mgr.get_recent_incoming(account_id=TEST_ACCOUNT_ID)
    assert len(all_msgs) == 2

    unprocessed = await mgr.get_recent_incoming(
        account_id=TEST_ACCOUNT_ID, unprocessed_only=True
    )
    assert len(unprocessed) == 1
    assert unprocessed[0]["sender_id"] == "2"
    await mgr.shutdown_all()


async def test_mark_processed(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    row_id = await mgr.log_incoming(TEST_ACCOUNT_ID, "1", None, "x", 1, 1)
    await mgr.mark_processed(row_id)
    messages = await mgr.get_recent_incoming(account_id=TEST_ACCOUNT_ID)
    assert messages[0]["processed"] is True
    await mgr.shutdown_all()


async def test_is_known_recipient_by_user_id(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    await mgr.log_send(TEST_ACCOUNT_ID, "12345", "hi", 1, "success")

    assert await mgr.is_known_recipient(TEST_ACCOUNT_ID, 12345, None) is True
    assert await mgr.is_known_recipient(TEST_ACCOUNT_ID, 99999, None) is False
    await mgr.shutdown_all()


async def test_is_known_recipient_by_username(test_settings):
    mgr = await _create_manager_with_account(test_settings)
    await mgr.log_send(TEST_ACCOUNT_ID, "@alice", "hi", 1, "success")

    assert await mgr.is_known_recipient(TEST_ACCOUNT_ID, 0, "alice") is True
    assert await mgr.is_known_recipient(TEST_ACCOUNT_ID, 0, "@alice") is True
    assert await mgr.is_known_recipient(TEST_ACCOUNT_ID, 0, "bob") is False
    await mgr.shutdown_all()


async def test_get_incoming_endpoint(client):
    resp = await client.get(
        f"/accounts/{TEST_ACCOUNT_ID}/incoming",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["messages"] == []
    assert data["count"] == 0


async def test_aggregated_incoming_endpoint(client):
    resp = await client.get("/incoming", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["messages"] == []
