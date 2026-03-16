from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.account_manager import AccountManager
from app.config import Settings, get_settings


def get_test_settings(tmp_path) -> Settings:
    return Settings(
        api_key="test-key",
        max_messages_per_day=5,
        min_delay_seconds=0,
        max_delay_seconds=0,
        db_path=str(tmp_path / "test.db"),
        sessions_dir=str(tmp_path / "sessions"),
    )


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return get_test_settings(tmp_path)


@pytest.fixture
def mock_send_message() -> AsyncMock:
    return AsyncMock(return_value=42)


@pytest_asyncio.fixture
async def manager(test_settings):
    mgr = AccountManager(test_settings)
    await mgr.init_db()
    yield mgr
    await mgr.shutdown_all()


TEST_ACCOUNT_ID = "test-account"


@pytest_asyncio.fixture
async def client(tmp_path, mock_send_message):
    settings = get_test_settings(tmp_path)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_me = AsyncMock(return_value=MagicMock(
        id=12345, username="testuser", first_name="Test"
    ))
    mock_client.add_event_handler = MagicMock()
    mock_client.remove_event_handler = MagicMock()

    from app.main import app, get_manager

    mgr = AccountManager(settings)
    await mgr.init_db()

    with patch("app.telethon_client.create_client", return_value=mock_client), \
         patch("app.telethon_client.send_message", mock_send_message), \
         patch("app.telethon_client.get_me", AsyncMock(return_value={"id": 12345, "username": "testuser"})):

        # Create a test account directly in DB + start it
        await mgr.add_account(
            account_id=TEST_ACCOUNT_ID,
            api_id=0,
            api_hash="test",
            max_messages_per_day=5,
            min_delay_seconds=0,
            max_delay_seconds=0,
        )
        await mgr.mark_authorized(TEST_ACCOUNT_ID, 12345, "testuser")

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_manager] = lambda: mgr

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    await mgr.shutdown_all()
    app.dependency_overrides.clear()
