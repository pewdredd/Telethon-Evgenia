from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings, get_settings
from app.rate_limiter import close_db, init_db, start_worker, stop_worker


def get_test_settings(tmp_path) -> Settings:
    return Settings(
        telegram_api_id=0,
        telegram_api_hash="test",
        telegram_session_name="test",
        api_key="test-key",
        max_messages_per_day=5,
        min_delay_seconds=0,
        max_delay_seconds=0,
        db_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return get_test_settings(tmp_path)


@pytest.fixture
def mock_send_message() -> AsyncMock:
    return AsyncMock(return_value=42)


@pytest_asyncio.fixture
async def db(test_settings):
    await init_db(test_settings.db_path)
    yield
    await close_db()


@pytest_asyncio.fixture
async def worker(db, mock_send_message, test_settings):
    start_worker(mock_send_message, test_settings)
    yield mock_send_message
    await stop_worker()


@pytest_asyncio.fixture
async def client(tmp_path, mock_send_message):
    settings = get_test_settings(tmp_path)

    import app.telethon_client as tc_mod

    original_start = tc_mod.start_client
    original_stop = tc_mod.stop_client
    original_get_me = tc_mod.get_me
    original_send = tc_mod.send_message

    tc_mod.start_client = AsyncMock()
    tc_mod.stop_client = AsyncMock()
    tc_mod.get_me = AsyncMock(return_value={"id": 12345, "username": "testuser"})
    tc_mod.send_message = mock_send_message

    from app.main import app

    app.dependency_overrides[get_settings] = lambda: settings

    # ASGITransport doesn't run lifespan, so init manually
    await init_db(settings.db_path)
    start_worker(mock_send_message, settings)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    await stop_worker()
    await close_db()

    app.dependency_overrides.clear()
    tc_mod.start_client = original_start
    tc_mod.stop_client = original_stop
    tc_mod.get_me = original_get_me
    tc_mod.send_message = original_send
