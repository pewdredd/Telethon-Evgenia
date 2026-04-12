"""Bot middleware for access control."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.account_manager import AccountManager


class WhitelistMiddleware(BaseMiddleware):
    """Block updates from users not in the whitelist or admin list."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        manager: AccountManager = data["manager"]
        bot_admins: list[int] = data["bot_admins"]

        user = None
        if isinstance(event, Update):
            if event.message:
                user = event.message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user

        if user is None:
            return await handler(event, data)

        if user.id in bot_admins or await manager.is_bot_user(user.id):
            return await handler(event, data)

        # Reject: send message if possible
        if isinstance(event, Update) and event.message:
            await event.message.answer(
                "Доступ запрещён. Обратитесь к администратору."
            )
        return None
