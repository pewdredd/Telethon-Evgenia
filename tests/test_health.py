from unittest.mock import AsyncMock, patch

from tests.conftest import TEST_ACCOUNT_ID


async def test_health_valid_key(client):
    resp = await client.get("/health", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert isinstance(data["accounts"], list)
    assert len(data["accounts"]) >= 1


async def test_health_invalid_key(client):
    resp = await client.get("/health", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


async def test_health_missing_key(client):
    resp = await client.get("/health")
    assert resp.status_code in (401, 403)


async def test_account_health_authorized(client):
    resp = await client.get(
        f"/accounts/{TEST_ACCOUNT_ID}/health",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["authorized"] is True
    assert data["account"] == "@testuser"


async def test_account_health_not_authorized(client):
    with patch("app.telethon_client.get_me", AsyncMock(return_value=None)):
        resp = await client.get(
            f"/accounts/{TEST_ACCOUNT_ID}/health",
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["authorized"] is False


async def test_account_health_not_found(client):
    resp = await client.get(
        "/accounts/nonexistent/health",
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404
