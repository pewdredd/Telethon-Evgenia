import asyncio
from unittest.mock import AsyncMock

import pytest

from app.rate_limiter import (
    close_db,
    enqueue_message,
    get_today_send_count,
    init_db,
    is_quota_available,
    log_send,
    start_worker,
    stop_worker,
)


async def test_init_db_creates_schema(test_settings):
    await init_db(test_settings.db_path)
    count = await get_today_send_count()
    assert count == 0
    await close_db()


async def test_log_send_writes_records(db):
    await log_send("@user1", "hello", 42, "success")
    await log_send("@user2", "hi", None, "error", "some error")
    count = await get_today_send_count()
    assert count == 1  # only successful


async def test_get_today_send_count_only_today(db):
    from app.rate_limiter import _db
    await _db.execute(
        "INSERT INTO send_log (recipient, message, telegram_message_id, status, error, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("@old", "msg", 1, "success", None, "2020-01-01T00:00:00+00:00"),
    )
    await _db.commit()

    await log_send("@today", "msg", 2, "success")
    count = await get_today_send_count()
    assert count == 1  # old record not counted


async def test_is_quota_available(db):
    assert await is_quota_available(2) is True
    await log_send("@u1", "m", 1, "success")
    assert await is_quota_available(2) is True
    await log_send("@u2", "m", 2, "success")
    assert await is_quota_available(2) is False


async def test_worker_applies_delay(db, test_settings):
    send_fn = AsyncMock(return_value=99)
    start_worker(send_fn, test_settings)

    future = await enqueue_message("@user", "hello")
    result = await asyncio.wait_for(future, timeout=5)
    assert result == 99
    send_fn.assert_called_once_with("@user", "hello")

    await stop_worker()


async def test_worker_handles_send_error(db, test_settings):
    send_fn = AsyncMock(side_effect=RuntimeError("boom"))
    start_worker(send_fn, test_settings)

    future = await enqueue_message("@user", "hello")
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(future, timeout=5)

    await stop_worker()
