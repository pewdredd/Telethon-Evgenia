"""Tests for account CRUD endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import TEST_ACCOUNT_ID


async def test_create_account(client):
    with patch("app.telethon_client.create_client") as mock_create:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.get_me = AsyncMock(return_value=MagicMock(
            id=1, username="u", first_name="U"
        ))
        mock_client.add_event_handler = MagicMock()
        mock_create.return_value = mock_client

        resp = await client.post(
            "/accounts",
            json={"account_id": "new-acct", "api_id": 123, "api_hash": "abc123"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["account_id"] == "new-acct"
    assert data["status"] == "pending"


async def test_create_duplicate_account(client):
    resp = await client.post(
        "/accounts",
        json={"account_id": TEST_ACCOUNT_ID, "api_id": 123, "api_hash": "abc"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400


async def test_list_accounts(client):
    resp = await client.get("/accounts", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["count"] >= 1
    ids = [a["account_id"] for a in data["accounts"]]
    assert TEST_ACCOUNT_ID in ids


async def test_get_account(client):
    resp = await client.get(
        f"/accounts/{TEST_ACCOUNT_ID}",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["account_id"] == TEST_ACCOUNT_ID


async def test_get_account_not_found(client):
    resp = await client.get(
        "/accounts/nonexistent",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404


async def test_update_account(client):
    resp = await client.patch(
        f"/accounts/{TEST_ACCOUNT_ID}",
        json={"max_messages_per_day": 10},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["max_messages_per_day"] == 10


async def test_delete_account(client):
    # First create an account to delete
    with patch("app.telethon_client.create_client") as mock_create:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.get_me = AsyncMock(return_value=MagicMock(
            id=2, username="u2", first_name="U2"
        ))
        mock_client.add_event_handler = MagicMock()
        mock_create.return_value = mock_client

        await client.post(
            "/accounts",
            json={"account_id": "to-delete", "api_id": 1, "api_hash": "x"},
            headers={"X-API-Key": "test-key"},
        )

    resp = await client.delete(
        "/accounts/to-delete",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's gone
    resp = await client.get(
        "/accounts/to-delete",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404
