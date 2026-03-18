"""Telethon (MTProto) client factory and message sending.

Pure functions — no module-level state. Each function takes a TelegramClient
as parameter. Use ``create_client()`` to instantiate a new client.
"""

import logging

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)

logger = logging.getLogger(__name__)


def create_client(api_id: int, api_hash: str, session_path: str) -> TelegramClient:
    """Create a new TelegramClient with hardcoded device emulation params."""
    logging.getLogger("telethon").setLevel(logging.ERROR)
    return TelegramClient(
        session_path,
        api_id,
        api_hash,
        device_model="iPhone 17 Pro Max",
        system_version="26.2.1",
        app_version="12.5",
        lang_code="ru",
        system_lang_code="ru",
    )


async def get_me(client: TelegramClient) -> dict | None:
    """Return {"id": ..., "username": ...} or None if not authorized."""
    if not client.is_connected():
        return None
    try:
        me = await client.get_me()
        if me is None:
            return None
        return {"id": me.id, "username": me.username}
    except Exception:
        return None


async def send_message(client: TelegramClient, recipient: str | int, message: str) -> tuple[int, int]:
    """Send a message and return (message_id, user_id)."""
    if not await client.is_user_authorized():
        raise RuntimeError("Telethon session is not authorized")
    try:
        result = await client.send_message(recipient, message)
        user_id = result.peer_id.user_id
        return result.id, user_id
    except PeerFloodError:
        raise RuntimeError("Telegram PeerFloodError: too many messages sent, try again later")
    except UserPrivacyRestrictedError:
        raise RuntimeError("User privacy settings prevent sending messages")
    except FloodWaitError as e:
        raise RuntimeError(f"Telegram FloodWaitError: must wait {e.seconds} seconds")


async def send_code(client: TelegramClient, phone: str) -> str:
    """Send a Telegram login code and return the phone_code_hash."""
    result = await client.send_code_request(phone)
    return result.phone_code_hash


async def verify_code(
    client: TelegramClient,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str | None = None,
) -> dict:
    """Verify login code, handle 2FA, return account info."""
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("2FA password required")
        await client.sign_in(password=password)
    me = await client.get_me()
    return {"id": me.id, "username": me.username}


async def qr_login_start(client: TelegramClient):
    """Start QR login. Returns (qr_login_object, tg://login URL)."""
    qr_login = await client.qr_login()
    return qr_login, qr_login.url


async def qr_login_wait(qr_login) -> dict:
    """Wait for QR scan (up to 60s). Returns account info or raises for 2FA."""
    try:
        await qr_login.wait(timeout=60)
    except SessionPasswordNeededError:
        raise RuntimeError("2FA_REQUIRED")
    # qr_login.wait() resolves the client's auth, get_me from the client
    me = await qr_login._client.get_me()
    return {"id": me.id, "username": me.username}


async def qr_login_2fa(client: TelegramClient, password: str) -> dict:
    """Submit 2FA password after QR scan."""
    await client.sign_in(password=password)
    me = await client.get_me()
    return {"id": me.id, "username": me.username}
