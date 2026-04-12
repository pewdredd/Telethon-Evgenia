"""Registration FSM handlers — client self-registration with error handling."""

import logging
import re

from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
)

from app.account_manager import AccountManager
from app.bot.states import Registration
from app import telethon_client

logger = logging.getLogger(__name__)

router = Router(name="registration")

MAX_CODE_ATTEMPTS = 3
MAX_2FA_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# /start — entry point for non-admin users (admins handled by admin.py)
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start_registration(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    # Link telegram_id to a user previously added by username
    await manager.resolve_bot_user(message.from_user.id, message.from_user.username)

    account_id = str(message.from_user.id)

    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        info = None

    if info and info.get("status") == "authorized":
        phone = info.get("phone") or "—"
        masked = mask_phone(phone) if phone != "—" else "—"
        username = info.get("username") or "—"
        today = info.get("today_sent", 0)
        limit = info.get("max_messages_per_day", 25)
        total = await manager.get_total_send_count(account_id)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Перерегистрировать аккаунт",
                callback_data="reregister",
            )]
        ])
        await message.answer(
            f"Ваш аккаунт подключён.\n\n"
            f"Авторизация: {info['status']}\n"
            f"Телефон: {masked}\n"
            f"Username: @{username}\n"
            f"Отправлено сегодня: {today}/{limit}\n"
            f"Всего отправлено: {total}",
            reply_markup=keyboard,
        )
        return

    if info and info.get("status") == "pending":
        await manager.remove_account(account_id)
        await state.clear()

    await _prompt_api_id(message, state)


# ---------------------------------------------------------------------------
# /status — account status for registered users
# ---------------------------------------------------------------------------

@router.message(Command("status"))
async def cmd_status(message: Message, manager: AccountManager) -> None:
    account_id = str(message.from_user.id)
    try:
        info = await manager.get_account_info(account_id)
    except KeyError:
        await message.answer(
            "Аккаунт не найден.\n"
            "Используйте /start для регистрации."
        )
        return

    phone = info.get("phone") or "—"
    masked = mask_phone(phone) if phone != "—" else "—"
    today = info.get("today_sent", 0)
    limit = info.get("max_messages_per_day", 25)
    total = await manager.get_total_send_count(account_id)

    await message.answer(
        f"Статус аккаунта:\n\n"
        f"Авторизация: {info.get('status', 'unknown')}\n"
        f"Телефон: {masked}\n"
        f"Отправлено сегодня: {today}/{limit}\n"
        f"Всего отправлено: {total}"
    )


# ---------------------------------------------------------------------------
# Re-register callback — inline button from /start
# ---------------------------------------------------------------------------

@router.callback_query(lambda c: c.data == "reregister")
async def on_reregister(
    callback: CallbackQuery, state: FSMContext, manager: AccountManager
) -> None:
    account_id = str(callback.from_user.id)
    try:
        await manager.remove_account(account_id)
    except Exception:
        pass

    await callback.answer()
    await callback.message.answer("Аккаунт удалён. Начинаем регистрацию заново.")
    await _prompt_api_id(callback.message, state)


# ---------------------------------------------------------------------------
# /cancel — abort registration at any point
# ---------------------------------------------------------------------------

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext, manager: AccountManager) -> None:
    await _cleanup_account(state, manager)
    await state.clear()
    await message.answer("Регистрация отменена.")


# ---------------------------------------------------------------------------
# /back — go to previous step
# ---------------------------------------------------------------------------

@router.message(Command("back"), StateFilter("*"))
async def cmd_back(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    current = await state.get_state()

    if current == Registration.waiting_api_id:
        await _cleanup_account(state, manager)
        await state.clear()
        await message.answer("Регистрация отменена.")
        return

    if current == Registration.waiting_api_hash:
        await message.answer("Введите ваш api_id (числовой):")
        await state.set_state(Registration.waiting_api_id)
        return

    if current == Registration.waiting_phone:
        await message.answer("Введите ваш api_hash:")
        await state.set_state(Registration.waiting_api_hash)
        return

    if current in (Registration.waiting_code, Registration.waiting_2fa):
        await _cleanup_account(state, manager)
        await message.answer(
            "Возврат к вводу номера телефона.\n"
            "Отправьте номер телефона (в формате +79001234567):"
        )
        await state.set_state(Registration.waiting_phone)
        return

    await message.answer("Нечего отменять.")


# ---------------------------------------------------------------------------
# Step 1: api_id
# ---------------------------------------------------------------------------

@router.message(Registration.waiting_api_id)
async def process_api_id(message: Message, state: FSMContext) -> None:
    try:
        api_id = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("api_id должен быть числом. Попробуйте ещё раз:")
        return

    await state.update_data(api_id=api_id)
    await message.answer("Отлично! Теперь отправьте ваш api_hash:")
    await state.set_state(Registration.waiting_api_hash)


# ---------------------------------------------------------------------------
# Step 2: api_hash
# ---------------------------------------------------------------------------

@router.message(Registration.waiting_api_hash)
async def process_api_hash(message: Message, state: FSMContext) -> None:
    api_hash = (message.text or "").strip()
    if len(api_hash) < 10:
        await message.answer(
            "api_hash слишком короткий (минимум 10 символов). Попробуйте ещё раз:"
        )
        return

    await state.update_data(api_hash=api_hash)
    await message.answer("Теперь отправьте номер телефона (в формате +79001234567):")
    await state.set_state(Registration.waiting_phone)


# ---------------------------------------------------------------------------
# Step 3: phone → add_account + send_code
# ---------------------------------------------------------------------------

@router.message(Registration.waiting_phone)
async def process_phone(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    phone = (message.text or "").strip()
    if not re.match(r"^\+\d{7,15}$", phone):
        await message.answer(
            "Неверный формат номера. Отправьте в формате +79001234567:"
        )
        return

    data = await state.get_data()
    api_id = data["api_id"]
    api_hash = data["api_hash"]
    account_id = str(message.from_user.id)

    try:
        await manager.add_account(account_id, api_id, api_hash, phone=phone)
    except ValueError:
        try:
            await manager.remove_account(account_id)
        except Exception:
            pass
        await manager.add_account(account_id, api_id, api_hash, phone=phone)

    try:
        client = manager.get_account(account_id).client
        phone_code_hash = await telethon_client.send_code(client, phone)
    except ApiIdInvalidError:
        await _cleanup_account_by_id(account_id, manager)
        await message.answer(
            "Неверный api_id или api_hash. Начните регистрацию заново.\n\n"
            "Введите ваш api_id (числовой):"
        )
        await state.set_state(Registration.waiting_api_id)
        return
    except PhoneNumberInvalidError:
        await _cleanup_account_by_id(account_id, manager)
        await message.answer(
            "Неверный номер телефона. Попробуйте ещё раз:"
        )
        return
    except FloodWaitError as e:
        await _cleanup_account_by_id(account_id, manager)
        await message.answer(
            f"Telegram требует подождать {e.seconds} секунд перед повторной попыткой.\n"
            "Начните регистрацию позже.\n\n"
            "Введите ваш api_id (числовой):"
        )
        await state.set_state(Registration.waiting_api_id)
        return
    except Exception as e:
        logger.error("send_code failed for %s: %s", account_id, e)
        await _cleanup_account_by_id(account_id, manager)
        await message.answer(
            f"Ошибка отправки кода: {e}\n"
            "Начните регистрацию заново.\n\n"
            "Введите ваш api_id (числовой):"
        )
        await state.set_state(Registration.waiting_api_id)
        return

    await state.update_data(
        phone=phone,
        account_id=account_id,
        phone_code_hash=phone_code_hash,
        code_attempts=MAX_CODE_ATTEMPTS,
    )
    await message.answer("Код отправлен! Введите код из Telegram:")
    await state.set_state(Registration.waiting_code)


# ---------------------------------------------------------------------------
# Step 4: code → verify_code
# ---------------------------------------------------------------------------

@router.message(Registration.waiting_code)
async def process_code(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите код:")
        return

    data = await state.get_data()
    account_id = data["account_id"]
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    attempts = data.get("code_attempts", MAX_CODE_ATTEMPTS)

    client = manager.get_account(account_id).client

    try:
        result = await telethon_client.verify_code(
            client, phone, code, phone_code_hash
        )
    except RuntimeError as e:
        if "2FA" in str(e):
            await state.update_data(code=code, tfa_attempts=MAX_2FA_ATTEMPTS)
            await message.answer(
                "У вас включена двухфакторная аутентификация.\n"
                "Введите пароль 2FA:"
            )
            await state.set_state(Registration.waiting_2fa)
            return
        await _handle_code_error(message, state, manager, attempts, str(e))
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        label = "Неверный код." if isinstance(e, PhoneCodeInvalidError) else "Код истёк."
        await _handle_code_error(message, state, manager, attempts, label)
        return
    except Exception as e:
        await _handle_code_error(message, state, manager, attempts, str(e))
        return

    await _complete_registration(message, state, manager, account_id, result)


# ---------------------------------------------------------------------------
# Step 5: 2FA password
# ---------------------------------------------------------------------------

@router.message(Registration.waiting_2fa)
async def process_2fa(
    message: Message, state: FSMContext, manager: AccountManager
) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Введите пароль 2FA:")
        return

    data = await state.get_data()
    account_id = data["account_id"]
    phone = data["phone"]
    code = data["code"]
    phone_code_hash = data["phone_code_hash"]
    attempts = data.get("tfa_attempts", MAX_2FA_ATTEMPTS)

    client = manager.get_account(account_id).client

    try:
        result = await telethon_client.verify_code(
            client, phone, code, phone_code_hash, password=password
        )
    except PasswordHashInvalidError:
        await _handle_2fa_error(message, state, manager, attempts, "Неверный пароль.")
        return
    except Exception as e:
        await _handle_2fa_error(message, state, manager, attempts, str(e))
        return

    await _complete_registration(message, state, manager, account_id, result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mask_phone(phone: str) -> str:
    """Mask middle digits: '+79991234567' -> '+7999***4567'."""
    if len(phone) <= 8:
        return phone
    return phone[:len(phone) - 7] + "***" + phone[-4:]


async def _prompt_api_id(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Добро пожаловать! Я помогу подключить ваш Telegram-аккаунт.\n\n"
        "Для начала вам понадобятся api_id и api_hash.\n"
        "Получить их можно на сайте my.telegram.org:\n\n"
        "1. Перейдите на https://my.telegram.org\n"
        "2. Войдите с вашим номером телефона\n"
        "3. Откройте «API development tools»\n"
        "4. Создайте приложение (если ещё нет)\n"
        "5. Скопируйте api_id и api_hash\n\n"
        "Отправьте ваш api_id (числовой):\n\n"
        "Для отмены — /cancel, для возврата — /back"
    )
    await state.set_state(Registration.waiting_api_id)


async def _cleanup_account(state: FSMContext, manager: AccountManager) -> None:
    data = await state.get_data()
    account_id = data.get("account_id")
    if account_id:
        try:
            await manager.remove_account(account_id)
        except Exception:
            pass


async def _cleanup_account_by_id(account_id: str, manager: AccountManager) -> None:
    try:
        await manager.remove_account(account_id)
    except Exception:
        pass


async def _handle_code_error(
    message: Message,
    state: FSMContext,
    manager: AccountManager,
    attempts: int,
    error_text: str,
) -> None:
    attempts -= 1
    if attempts <= 0:
        await _cleanup_account(state, manager)
        await state.update_data(account_id=None)
        await message.answer(
            f"{error_text}\n"
            "Попытки исчерпаны. Возврат к вводу номера телефона.\n\n"
            "Отправьте номер телефона (в формате +79001234567):"
        )
        await state.set_state(Registration.waiting_phone)
        return

    await state.update_data(code_attempts=attempts)
    await message.answer(
        f"{error_text}\n"
        f"Осталось попыток: {attempts}. Введите код ещё раз:"
    )


async def _handle_2fa_error(
    message: Message,
    state: FSMContext,
    manager: AccountManager,
    attempts: int,
    error_text: str,
) -> None:
    attempts -= 1
    if attempts <= 0:
        await _cleanup_account(state, manager)
        await state.update_data(account_id=None)
        await message.answer(
            f"{error_text}\n"
            "Попытки исчерпаны. Возврат к вводу номера телефона.\n\n"
            "Отправьте номер телефона (в формате +79001234567):"
        )
        await state.set_state(Registration.waiting_phone)
        return

    await state.update_data(tfa_attempts=attempts)
    await message.answer(
        f"{error_text}\n"
        f"Осталось попыток: {attempts}. Введите пароль ещё раз:"
    )


async def _complete_registration(
    message: Message,
    state: FSMContext,
    manager: AccountManager,
    account_id: str,
    result: dict,
) -> None:
    await manager.mark_authorized(account_id, result["id"], result.get("username"))

    info = await manager.get_account_info(account_id)
    username = info.get("username") or "—"
    limit = info.get("max_messages_per_day", 25)

    await message.answer(
        f"Аккаунт успешно подключён!\n\n"
        f"Username: @{username}\n"
        f"Лимит сообщений: {limit}/день\n"
        f"Статус: authorized"
    )
    await state.clear()
