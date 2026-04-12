"""Admin command handlers for the Telegram bot."""

from aiogram import Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message

from app.account_manager import AccountManager


class AdminFilter(BaseFilter):
    """Pass only if the sender is in bot_admins."""

    async def __call__(self, message: Message, bot_admins: list[int]) -> bool:
        return message.from_user is not None and message.from_user.id in bot_admins

router = Router(name="admin")


# ---------------------------------------------------------------------------
# /start — admin-only (non-admins handled by registration router)
# ---------------------------------------------------------------------------

@router.message(Command("start"), AdminFilter())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет, админ! Доступные команды:\n"
        "/add_user <telegram_id> — добавить пользователя\n"
        "/remove_user <telegram_id> — удалить пользователя\n"
        "/users — список пользователей\n"
        "/accounts — список аккаунтов"
    )


# ---------------------------------------------------------------------------
# Admin-only router: remaining handlers require admin privileges
# ---------------------------------------------------------------------------

admin_router = Router(name="admin_only")
admin_router.message.filter(AdminFilter())


# ---------------------------------------------------------------------------
# /add_user <telegram_id>
# ---------------------------------------------------------------------------

@admin_router.message(Command("add_user"))
async def cmd_add_user(
    message: Message, command: CommandObject, manager: AccountManager
) -> None:
    if not command.args:
        await message.answer("Использование: /add_user <telegram_id>")
        return

    try:
        telegram_id = int(command.args.strip())
    except ValueError:
        await message.answer("telegram_id должен быть числом.")
        return

    await manager.add_bot_user(telegram_id, added_by=message.from_user.id)
    await message.answer(f"Пользователь {telegram_id} добавлен.")


# ---------------------------------------------------------------------------
# /remove_user <telegram_id>
# ---------------------------------------------------------------------------

@admin_router.message(Command("remove_user"))
async def cmd_remove_user(
    message: Message, command: CommandObject, manager: AccountManager
) -> None:
    if not command.args:
        await message.answer("Использование: /remove_user <telegram_id>")
        return

    try:
        telegram_id = int(command.args.strip())
    except ValueError:
        await message.answer("telegram_id должен быть числом.")
        return

    removed = await manager.remove_bot_user(telegram_id)
    if removed:
        await message.answer(f"Пользователь {telegram_id} удалён.")
    else:
        await message.answer(f"Пользователь {telegram_id} не найден.")


# ---------------------------------------------------------------------------
# /users
# ---------------------------------------------------------------------------

@admin_router.message(Command("users"))
async def cmd_users(message: Message, manager: AccountManager) -> None:
    bot_users = await manager.list_bot_users()
    if not bot_users:
        await message.answer("Список пользователей пуст.")
        return

    accounts = await manager.list_accounts()
    # Map telegram_id → account_id for registered accounts
    tg_to_account: dict[int, str] = {}
    for acc in accounts:
        tg_id = acc.get("telegram_id")
        if tg_id:
            tg_to_account[tg_id] = acc["account_id"]

    lines: list[str] = []
    for i, user in enumerate(bot_users, 1):
        tg_id = user["telegram_id"]
        account_id = tg_to_account.get(tg_id)
        if account_id:
            status = f"зарегистрирован ({account_id})"
        else:
            status = "не зарегистрирован"
        lines.append(f"{i}. {tg_id} — {status}")

    await message.answer("Пользователи:\n\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# /accounts
# ---------------------------------------------------------------------------

@admin_router.message(Command("accounts"))
async def cmd_accounts(message: Message, manager: AccountManager) -> None:
    accounts = await manager.list_accounts()
    if not accounts:
        await message.answer("Нет зарегистрированных аккаунтов.")
        return

    lines: list[str] = []
    for acc in accounts:
        username = acc.get("username") or "—"
        if username != "—":
            username = f"@{username}"
        today = acc.get("today_sent", 0)
        limit = acc.get("max_messages_per_day", 25)
        status = acc.get("status", "unknown")
        lines.append(
            f"account_id: {acc['account_id']}\n"
            f"  Статус: {status}\n"
            f"  Username: {username}\n"
            f"  Отправлено: {today}/{limit}"
        )

    await message.answer("Аккаунты:\n\n" + "\n\n".join(lines))
