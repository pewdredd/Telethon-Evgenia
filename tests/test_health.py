from unittest.mock import AsyncMock


async def test_health_valid_key(client):
    resp = await client.get("/health", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["authorized"] is True
    assert data["account"] == "@testuser"


async def test_health_invalid_key(client):
    resp = await client.get("/health", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


async def test_health_missing_key(client):
    resp = await client.get("/health")
    # APIKeyHeader returns 401 when header is absent (auto_error=True)
    assert resp.status_code in (401, 403)


async def test_health_not_authorized(client):
    import app.telethon_client as tc_mod
    original = tc_mod.get_me
    tc_mod.get_me = AsyncMock(return_value=None)
    try:
        resp = await client.get("/health", headers={"X-API-Key": "test-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authorized"] is False
        assert data["account"] is None
    finally:
        tc_mod.get_me = original
