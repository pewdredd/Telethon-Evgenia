"""Admin command handlers for the Telegram bot."""

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.account_manager import AccountManager


class AdminFilter(BaseFilter):
    """Pass only if the sender is in bot_admins."""

    async def __call__(self, message: Message, bot_admins: list[int]) -> bool:
        return message.from_user is not None and message.from_user.id in bot_admins

router = Router(name="admin")

KB_ADMIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Пользователи"), KeyboardButton(text="Аккаунты")],
    ],
    resize_keyboard=True,
)


# ---------------------------------------------------------------------------
# /start — admin-only (non-admins handled by registration router)
# ---------------------------------------------------------------------------

@router.message(Command("start"), AdminFilter())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет, админ! Доступные команды:\n"
        "/add_user <id или @username> — добавить пользователя\n"
        "/remove_user <id или @username> — удалить пользователя\n"
        "/users — список пользователей\n"
        "/accounts — список аккаунтов",
        reply_markup=KB_ADMIN,
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
        await message.answer("Использование: /add_user <id или @username>")
        return

    arg = command.args.strip()
    try:
        telegram_id = int(arg)
        await manager.add_bot_user(added_by=message.from_user.id, telegram_id=telegram_id)
        await message.answer(f"Пользователь {telegram_id} добавлен.")
    except ValueError:
        username = arg.lstrip("@")
        await manager.add_bot_user(added_by=message.from_user.id, username=username)
        await message.answer(
            f"Пользователь @{username} добавлен.\n"
            "Telegram ID привяжется когда он напишет /start боту."
        )


# ---------------------------------------------------------------------------
# /remove_user <telegram_id>
# ---------------------------------------------------------------------------

@admin_router.message(Command("remove_user"))
async def cmd_remove_user(
    message: Message, command: CommandObject, manager: AccountManager
) -> None:
    if not command.args:
        await message.answer("Использование: /remove_user <id или @username>")
        return

    arg = command.args.strip()
    removed = await manager.remove_bot_user(arg)
    if removed:
        await message.answer(f"Пользователь {arg} удалён.")
    else:
        await message.answer(f"Пользователь {arg} не найден.")


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
        uname = user.get("username")
        label = f"@{uname}" if uname else str(tg_id) if tg_id else "?"
        if tg_id and uname:
            label = f"@{uname} ({tg_id})"
        account_id = tg_to_account.get(tg_id) if tg_id else None
        if account_id:
            status = f"зарегистрирован ({account_id})"
        elif tg_id:
            status = "не зарегистрирован"
        else:
            status = "ожидает /start"
        lines.append(f"{i}. {label} — {status}")

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


# ---------------------------------------------------------------------------
# Reply-keyboard text handlers (mirror /users, /accounts)
# ---------------------------------------------------------------------------

@admin_router.message(F.text == "Пользователи")
async def btn_users(message: Message, manager: AccountManager) -> None:
    await cmd_users(message, manager)


@admin_router.message(F.text == "Аккаунты")
async def btn_accounts(message: Message, manager: AccountManager) -> None:
    await cmd_accounts(message, manager)
