"""Admin command handlers for the Telegram bot."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from app.account_manager import AccountManager
from app.bot.states import AdminEdit


class AdminFilter(BaseFilter):
    """Pass only if the sender is in bot_admins. Works for Message and CallbackQuery."""

    async def __call__(
        self, event: TelegramObject, bot_admins: list[int]
    ) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in bot_admins


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
        "/accounts — аккаунты (inline-меню управления)",
        reply_markup=KB_ADMIN,
    )


# ---------------------------------------------------------------------------
# Admin-only router: remaining handlers require admin privileges
# ---------------------------------------------------------------------------

admin_router = Router(name="admin_only")
admin_router.message.filter(AdminFilter())
admin_router.callback_query.filter(AdminFilter())


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
# Accounts — inline menu entry points
# ---------------------------------------------------------------------------

def _fmt_account_button_label(acc: dict) -> str:
    uname = acc.get("username")
    uname_part = f"@{uname}" if uname else "—"
    status = acc.get("status", "?")
    return f"{acc['account_id']} · {uname_part} · {status}"


async def _build_accounts_list_kb(manager: AccountManager) -> tuple[str, InlineKeyboardMarkup | None]:
    accounts = await manager.list_accounts()
    if not accounts:
        return "Нет зарегистрированных аккаунтов.", None
    rows = [
        [
            InlineKeyboardButton(
                text=_fmt_account_button_label(acc),
                callback_data=f"acc:open:{acc['account_id']}",
            )
        ]
        for acc in accounts
    ]
    return "Аккаунты:", InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.message(Command("accounts"))
async def cmd_accounts(message: Message, manager: AccountManager) -> None:
    text, kb = await _build_accounts_list_kb(manager)
    await message.answer(text, reply_markup=kb)


@admin_router.message(F.text == "Пользователи")
async def btn_users(message: Message, manager: AccountManager) -> None:
    await cmd_users(message, manager)


@admin_router.message(F.text == "Аккаунты")
async def btn_accounts(message: Message, manager: AccountManager) -> None:
    await cmd_accounts(message, manager)


# ---------------------------------------------------------------------------
# Account card rendering
# ---------------------------------------------------------------------------

async def _build_account_card(
    manager: AccountManager, account_id: str
) -> tuple[str, InlineKeyboardMarkup] | None:
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        return None
    total = await manager.get_total_send_count(account_id)

    uname = info.get("username")
    username_part = f"@{uname}" if uname else "—"
    tg_id = info.get("telegram_id") or "—"
    phone = info.get("phone") or "—"
    webhook = info.get("webhook_url") or "—"
    fi_text = "ВКЛ" if info["forward_incoming"] else "ВЫКЛ"
    fi_btn_label = "🔔 Пересылка: ВКЛ" if info["forward_incoming"] else "🔕 Пересылка: ВЫКЛ"

    text = (
        f"<b>Аккаунт {info['account_id']}</b>\n"
        f"Статус: {info['status']}\n"
        f"Telegram: {username_part} (id {tg_id})\n"
        f"Телефон: {phone}\n"
        "\n"
        f"Отправлено сегодня: {info['today_sent']} / {info['max_messages_per_day']}\n"
        f"Всего отправлено: {total}\n"
        f"Задержка: {info['min_delay_seconds']}–{info['max_delay_seconds']} сек\n"
        "\n"
        f"Пересылка входящих: {fi_text}\n"
        f"Вебхук: {webhook}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Лимиты", callback_data=f"acc:limits:{account_id}")],
        [InlineKeyboardButton(text=fi_btn_label, callback_data=f"acc:toggle:{account_id}")],
        [InlineKeyboardButton(text="🌐 Вебхук", callback_data=f"acc:wh:{account_id}")],
        [InlineKeyboardButton(text="📥 Входящие", callback_data=f"acc:inc:{account_id}:0")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"acc:del:{account_id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="acc:list")],
    ])
    return text, kb


async def _safe_edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None,
                     parse_mode: str | None = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass


async def _render_card_in_place(callback: CallbackQuery, manager: AccountManager,
                                 account_id: str) -> None:
    card = await _build_account_card(manager, account_id)
    if card is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        text, kb = await _build_accounts_list_kb(manager)
        await _safe_edit(callback, text, kb)
        return
    text, kb = card
    await _safe_edit(callback, text, kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: list & open
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data == "acc:list")
async def cb_list(callback: CallbackQuery, manager: AccountManager) -> None:
    text, kb = await _build_accounts_list_kb(manager)
    await _safe_edit(callback, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("acc:open:"))
async def cb_open(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    await _render_card_in_place(callback, manager, account_id)
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: toggle forward_incoming
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("acc:toggle:"))
async def cb_toggle_forward(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    new_value = not info["forward_incoming"]
    await manager.update_account(account_id, forward_incoming=new_value)
    await _render_card_in_place(callback, manager, account_id)
    await callback.answer("Пересылка ВКЛ" if new_value else "Пересылка ВЫКЛ")


# ---------------------------------------------------------------------------
# Callback: edit rate limits — start FSM
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("acc:limits:"))
async def cb_start_edit_limits(
    callback: CallbackQuery, manager: AccountManager, state: FSMContext
) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await state.set_state(AdminEdit.waiting_max_messages)
    await state.update_data(target_account_id=account_id)
    await callback.message.answer(
        f"Редактирование лимитов для {account_id}.\n\n"
        f"Сейчас max_messages_per_day = {info['max_messages_per_day']}.\n"
        "Отправьте новое целое число > 0, /skip чтобы оставить, /cancel чтобы выйти."
    )
    await callback.answer()


@admin_router.message(Command("skip"), StateFilter(AdminEdit.waiting_max_messages))
async def limits_skip_max(message: Message, state: FSMContext, manager: AccountManager) -> None:
    await _limits_ask_min_delay(message, state, manager)


@admin_router.message(StateFilter(AdminEdit.waiting_max_messages))
async def limits_set_max(message: Message, state: FSMContext, manager: AccountManager) -> None:
    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Нужно целое число > 0. Попробуйте ещё раз или /skip, /cancel.")
        return
    await state.update_data(new_max_messages=value)
    await _limits_ask_min_delay(message, state, manager)


async def _limits_ask_min_delay(message: Message, state: FSMContext, manager: AccountManager) -> None:
    data = await state.get_data()
    account_id = data["target_account_id"]
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await state.clear()
        await message.answer("Аккаунт исчез. Выход.")
        return
    await state.set_state(AdminEdit.waiting_min_delay)
    await message.answer(
        f"Сейчас min_delay_seconds = {info['min_delay_seconds']}.\n"
        "Отправьте новое целое число > 0, /skip или /cancel."
    )


@admin_router.message(Command("skip"), StateFilter(AdminEdit.waiting_min_delay))
async def limits_skip_min(message: Message, state: FSMContext, manager: AccountManager) -> None:
    await _limits_ask_max_delay(message, state, manager)


@admin_router.message(StateFilter(AdminEdit.waiting_min_delay))
async def limits_set_min(message: Message, state: FSMContext, manager: AccountManager) -> None:
    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Нужно целое число > 0. Попробуйте ещё раз или /skip, /cancel.")
        return
    await state.update_data(new_min_delay=value)
    await _limits_ask_max_delay(message, state, manager)


async def _limits_ask_max_delay(message: Message, state: FSMContext, manager: AccountManager) -> None:
    data = await state.get_data()
    account_id = data["target_account_id"]
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await state.clear()
        await message.answer("Аккаунт исчез. Выход.")
        return
    await state.set_state(AdminEdit.waiting_max_delay)
    await message.answer(
        f"Сейчас max_delay_seconds = {info['max_delay_seconds']}.\n"
        "Отправьте новое целое число > 0, /skip или /cancel.\n"
        "После этого шага изменения применятся."
    )


@admin_router.message(Command("skip"), StateFilter(AdminEdit.waiting_max_delay))
async def limits_skip_max_delay(message: Message, state: FSMContext, manager: AccountManager) -> None:
    await _limits_apply(message, state, manager)


@admin_router.message(StateFilter(AdminEdit.waiting_max_delay))
async def limits_set_max_delay(message: Message, state: FSMContext, manager: AccountManager) -> None:
    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Нужно целое число > 0. Попробуйте ещё раз или /skip, /cancel.")
        return
    await state.update_data(new_max_delay=value)
    await _limits_apply(message, state, manager)


async def _limits_apply(message: Message, state: FSMContext, manager: AccountManager) -> None:
    data = await state.get_data()
    account_id = data["target_account_id"]
    new_max = data.get("new_max_messages")
    new_min = data.get("new_min_delay")
    new_max_d = data.get("new_max_delay")

    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await state.clear()
        await message.answer("Аккаунт исчез. Выход.")
        return

    eff_min = new_min if new_min is not None else info["min_delay_seconds"]
    eff_max = new_max_d if new_max_d is not None else info["max_delay_seconds"]
    if eff_min > eff_max:
        await message.answer(
            f"Ошибка: min_delay ({eff_min}) не может быть больше max_delay ({eff_max}). "
            "Изменения отменены."
        )
        await state.clear()
        return

    kwargs: dict = {}
    if new_max is not None:
        kwargs["max_messages_per_day"] = new_max
    if new_min is not None:
        kwargs["min_delay_seconds"] = new_min
    if new_max_d is not None:
        kwargs["max_delay_seconds"] = new_max_d

    if not kwargs:
        await message.answer("Ничего не изменено.")
    else:
        await manager.update_account(account_id, **kwargs)
        await message.answer("Лимиты обновлены.")
    await state.clear()
    text, kb = (await _build_account_card(manager, account_id)) or (None, None)
    if text:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Callback: webhook submenu
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("acc:wh:"))
async def cb_webhook_menu(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    current = info.get("webhook_url") or "—"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Задать URL", callback_data=f"acc:wh_set:{account_id}")],
        [InlineKeyboardButton(text="Сбросить", callback_data=f"acc:wh_clr:{account_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"acc:open:{account_id}")],
    ])
    await _safe_edit(
        callback,
        f"<b>Вебхук аккаунта {account_id}</b>\n\nТекущий: {current}",
        kb,
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("acc:wh_set:"))
async def cb_start_set_webhook(
    callback: CallbackQuery, manager: AccountManager, state: FSMContext
) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await state.set_state(AdminEdit.waiting_webhook_url)
    await state.update_data(target_account_id=account_id)
    await callback.message.answer(
        "Отправьте новый webhook_url (http:// или https://), или /cancel для выхода."
    )
    await callback.answer()


@admin_router.message(StateFilter(AdminEdit.waiting_webhook_url))
async def webhook_set_value(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("URL должен начинаться с http:// или https://. Попробуйте снова или /cancel.")
        return
    data = await state.get_data()
    account_id = data["target_account_id"]
    try:
        await manager.update_account(account_id, webhook_url=url)
    except KeyError:
        await state.clear()
        await message.answer("Аккаунт исчез. Выход.")
        return
    await state.clear()
    await message.answer("Вебхук обновлён.")
    text, kb = (await _build_account_card(manager, account_id)) or (None, None)
    if text:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data.startswith("acc:wh_clr:"))
async def cb_clear_webhook(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        await manager.update_account(account_id, webhook_url=None)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    await _render_card_in_place(callback, manager, account_id)
    await callback.answer("Вебхук сброшен")


# ---------------------------------------------------------------------------
# Callback: delete with confirmation
# ---------------------------------------------------------------------------

@admin_router.callback_query(F.data.startswith("acc:del:"))
async def cb_ask_delete(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, удалить", callback_data=f"acc:del_ok:{account_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"acc:open:{account_id}")],
    ])
    await _safe_edit(
        callback,
        f"Точно удалить аккаунт <b>{account_id}</b>?\n"
        "Сессия и запись в БД будут удалены.",
        kb,
        parse_mode="HTML",
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("acc:del_ok:"))
async def cb_confirm_delete(callback: CallbackQuery, manager: AccountManager) -> None:
    account_id = callback.data.split(":", 2)[2]
    try:
        await manager.remove_account(account_id)
    except Exception as e:  # noqa: BLE001
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return
    text, kb = await _build_accounts_list_kb(manager)
    await _safe_edit(callback, f"Аккаунт {account_id} удалён.\n\n{text}", kb)
    await callback.answer("Удалено")


# ---------------------------------------------------------------------------
# Callback: incoming messages viewer
# ---------------------------------------------------------------------------

_INCOMING_PAGE_SIZE = 5


def _fmt_incoming(item) -> str:
    sender = item.get("sender_username") or item.get("sender_id") or "?"
    sender_label = f"@{sender}" if item.get("sender_username") else str(sender)
    text = item.get("message_text") or ""
    if len(text) > 120:
        text = text[:117] + "…"
    received = item.get("received_at", "")[:19].replace("T", " ")
    mark = "✅" if item.get("processed") else "🆕"
    return f"{mark} #{item['id']} {sender_label} · {received}\n{text}"


@admin_router.callback_query(F.data.startswith("acc:inc:"))
async def cb_incoming(callback: CallbackQuery, manager: AccountManager) -> None:
    parts = callback.data.split(":")
    # acc:inc:<account_id>:<offset>
    account_id = parts[2]
    try:
        offset = int(parts[3])
    except (IndexError, ValueError):
        offset = 0

    try:
        await manager.get_account_info(account_id)
    except KeyError:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    rows = await manager.get_recent_incoming(
        account_id=account_id,
        limit=offset + _INCOMING_PAGE_SIZE + 1,
        unprocessed_only=False,
    )
    page = rows[offset : offset + _INCOMING_PAGE_SIZE]
    has_next = len(rows) > offset + _INCOMING_PAGE_SIZE

    if not page:
        body = "Нет входящих сообщений." if offset == 0 else "Больше сообщений нет."
    else:
        body = "\n\n".join(_fmt_incoming(r) for r in page)

    header = f"<b>Входящие · {account_id}</b> (стр. {offset // _INCOMING_PAGE_SIZE + 1})"
    text = f"{header}\n\n{body}"

    buttons: list[list[InlineKeyboardButton]] = []
    for r in page:
        if not r.get("processed"):
            buttons.append([
                InlineKeyboardButton(
                    text=f"✅ Обработано #{r['id']}",
                    callback_data=f"acc:inc_done:{r['id']}:{account_id}:{offset}",
                )
            ])
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev = max(0, offset - _INCOMING_PAGE_SIZE)
        nav.append(InlineKeyboardButton(
            text="◀️", callback_data=f"acc:inc:{account_id}:{prev}",
        ))
    if has_next:
        nav.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"acc:inc:{account_id}:{offset + _INCOMING_PAGE_SIZE}",
        ))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"acc:open:{account_id}"),
    ])

    await _safe_edit(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("acc:inc_done:"))
async def cb_mark_processed(callback: CallbackQuery, manager: AccountManager) -> None:
    # acc:inc_done:<incoming_id>:<account_id>:<offset>
    parts = callback.data.split(":")
    try:
        incoming_id = int(parts[2])
        account_id = parts[3]
        offset = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Bad data", show_alert=True)
        return
    await manager.mark_processed(incoming_id)
    # Re-render same page
    callback.data = f"acc:inc:{account_id}:{offset}"
    await cb_incoming(callback, manager)


# ---------------------------------------------------------------------------
# /cancel for AdminEdit states
# ---------------------------------------------------------------------------

@admin_router.message(
    Command("cancel"),
    StateFilter(
        AdminEdit.waiting_max_messages,
        AdminEdit.waiting_min_delay,
        AdminEdit.waiting_max_delay,
        AdminEdit.waiting_webhook_url,
    ),
)
async def admin_cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")
