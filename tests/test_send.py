from unittest.mock import AsyncMock


async def test_send_success(client, mock_send_message):
    resp = await client.post(
        "/send",
        json={"recipient": "@someuser", "message": "Hello!"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["message_id"] == 42


async def test_send_invalid_key(client):
    resp = await client.post(
        "/send",
        json={"recipient": "@someuser", "message": "Hello!"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


async def test_send_empty_message(client):
    resp = await client.post(
        "/send",
        json={"recipient": "@someuser", "message": ""},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


async def test_send_quota_exhausted(client, mock_send_message):
    # Send 5 messages (quota limit)
    for i in range(5):
        resp = await client.post(
            "/send",
            json={"recipient": f"@user{i}", "message": f"msg{i}"},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200

    # 6th should be rejected with 429
    resp = await client.post(
        "/send",
        json={"recipient": "@user6", "message": "msg6"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 429


async def test_send_telethon_error(client, mock_send_message):
    mock_send_message.side_effect = RuntimeError("User privacy settings prevent sending messages")
    resp = await client.post(
        "/send",
        json={"recipient": "@someuser", "message": "Hello!"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "privacy" in data["error"].lower()


async def test_send_numeric_recipient(client, mock_send_message):
    resp = await client.post(
        "/send",
        json={"recipient": "123456", "message": "Hello!"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # Verify Telethon received an int
    mock_send_message.assert_called_with(123456, "Hello!")
