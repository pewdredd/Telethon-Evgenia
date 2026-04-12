"""FSM state groups for the Telegram bot."""

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()
