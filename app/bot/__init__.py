"""Telegram bot package — Dispatcher setup, start/stop helpers."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.account_manager import AccountManager
from app.bot.admin import admin_router, router as start_router
from app.bot.middleware import WhitelistMiddleware
from app.bot.registration import router as registration_router
from app.config import Settings

logger = logging.getLogger(__name__)


def create_bot(settings: Settings, manager: AccountManager) -> tuple[Bot, Dispatcher]:
    """Build a configured Bot and Dispatcher (does not start polling)."""
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    # Make manager and admin list available to all handlers/middleware
    dp["manager"] = manager
    dp["bot_admins"] = settings.bot_admins

    # Outer middleware — runs before any router, enforces whitelist
    dp.update.outer_middleware(WhitelistMiddleware())

    # Routers:
    # - start_router handles /start (admin-only variant).
    # - admin_router is registered BEFORE registration_router so its /cancel
    #   handler for AdminEdit states wins over registration's StateFilter("*").
    #   admin_router has AdminFilter, so non-admin traffic passes through to
    #   registration_router unchanged.
    dp.include_router(start_router)
    dp.include_router(admin_router)
    dp.include_router(registration_router)

    return bot, dp


async def start_polling(bot: Bot, dp: Dispatcher) -> asyncio.Task[None]:
    """Launch polling in a background task and return the task handle."""
    task = asyncio.create_task(dp.start_polling(bot))
    logger.info("Bot polling started")
    return task


async def stop_polling(bot: Bot, dp: Dispatcher, task: asyncio.Task[None]) -> None:
    """Gracefully stop polling and close the bot session."""
    await dp.stop_polling()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await bot.session.close()
    logger.info("Bot polling stopped")
