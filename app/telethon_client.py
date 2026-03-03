"""Telethon (MTProto) client lifecycle and message sending.

Manages a singleton TelegramClient at module level. If the session is not
yet authorized, use POST /auth/qr to get a QR code login URL, or
POST /auth/send-code + POST /auth/verify for phone code login.
"""

import logging

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)

from app.config import Settings

logger = logging.getLogger(__name__)

_client: TelegramClient | None = None
_qr_login = None  # active QR login token


async def start_client(settings: Settings) -> None:
    global _client
    _client = TelegramClient(
        settings.telegram_session_name,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        device_model="iPhone 17 Pro Max",
        system_version="26.2.1",
        app_version="12.5",
        lang_code="ru",
        system_lang_code="ru",
    )
    await _client.connect()
    if await _client.is_user_authorized():
        me = await _client.get_me()
        logger.info("Telethon authorized as %s (@%s)", me.first_name, me.username)
    else:
        logger.warning("Telethon session not authorized. Use POST /auth/qr to log in.")


async def stop_client() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


async def get_me() -> dict | None:
    if _client is None or not _client.is_connected():
        return None
    try:
        me = await _client.get_me()
        if me is None:
            return None
        return {"id": me.id, "username": me.username}
    except Exception:
        return None


async def qr_login_start() -> str:
    """Start QR login. Returns the tg://login?token=... URL to show as QR."""
    global _qr_login
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    _qr_login = await _client.qr_login()
    return _qr_login.url


async def qr_login_wait() -> dict:
    """Wait for the user to scan the QR and return account info."""
    global _qr_login
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    if _qr_login is None:
        raise RuntimeError("No active QR login. Call /auth/qr first.")
    try:
        await _qr_login.wait(timeout=60)
    except SessionPasswordNeededError:
        raise RuntimeError("2FA_REQUIRED")
    me = await _client.get_me()
    _qr_login = None
    return {"id": me.id, "username": me.username}


async def qr_login_2fa(password: str) -> dict:
    """Submit 2FA password after QR scan."""
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    await _client.sign_in(password=password)
    me = await _client.get_me()
    return {"id": me.id, "username": me.username}


async def send_code(phone: str) -> str:
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    result = await _client.send_code_request(phone)
    return result.phone_code_hash


async def verify_code(
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
) -> dict:
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    try:
        await _client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("2FA password required")
        await _client.sign_in(password=password)
    me = await _client.get_me()
    return {"id": me.id, "username": me.username}


async def send_message(recipient: str | int, message: str) -> int:
    if _client is None:
        raise RuntimeError("Telethon client is not started")
    if not await _client.is_user_authorized():
        raise RuntimeError("Telethon session is not authorized")
    try:
        result = await _client.send_message(recipient, message)
        return result.id
    except PeerFloodError:
        raise RuntimeError("Telegram PeerFloodError: too many messages sent, try again later")
    except UserPrivacyRestrictedError:
        raise RuntimeError("User privacy settings prevent sending messages")
    except FloodWaitError as e:
        raise RuntimeError(f"Telegram FloodWaitError: must wait {e.seconds} seconds")
