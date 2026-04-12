import pytest
import pytest_asyncio

from app.account_manager import AccountManager
from app.config import Settings


@pytest_asyncio.fixture
async def manager(tmp_path):
    settings = Settings(
        api_key="test-key",
        db_path=str(tmp_path / "test.db"),
        sessions_dir=str(tmp_path / "sessions"),
    )
    mgr = AccountManager(settings)
    await mgr.init_db()
    yield mgr
    await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_add_bot_user(manager):
    await manager.add_bot_user(telegram_id=111, added_by=999)
    assert await manager.is_bot_user(111) is True
    assert await manager.is_bot_user(222) is False


@pytest.mark.asyncio
async def test_add_bot_user_idempotent(manager):
    await manager.add_bot_user(telegram_id=111, added_by=999)
    await manager.add_bot_user(telegram_id=111, added_by=888)
    users = await manager.list_bot_users()
    assert len(users) == 1
    assert users[0]["added_by"] == 999  # first insert wins


@pytest.mark.asyncio
async def test_remove_bot_user(manager):
    await manager.add_bot_user(telegram_id=111, added_by=999)
    assert await manager.remove_bot_user(111) is True
    assert await manager.is_bot_user(111) is False


@pytest.mark.asyncio
async def test_remove_nonexistent(manager):
    assert await manager.remove_bot_user(999) is False


@pytest.mark.asyncio
async def test_list_bot_users(manager):
    await manager.add_bot_user(telegram_id=111, added_by=999)
    await manager.add_bot_user(telegram_id=222, added_by=999)
    await manager.add_bot_user(telegram_id=333, added_by=888)
    users = await manager.list_bot_users()
    assert len(users) == 3
    ids = [u["telegram_id"] for u in users]
    assert set(ids) == {111, 222, 333}
    assert all("added_at" in u for u in users)
