from telethon import TelegramClient
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError

from app.config import Settings

_client: TelegramClient | None = None


async def start_client(settings: Settings) -> None:
    global _client
    _client = TelegramClient(
        settings.telegram_session_name,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError(
            "Telethon session is not authorized. "
            "Run 'python -m app.auth_session' first."
        )


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


async def send_message(recipient: str | int, message: str) -> int:
    if _client is None:
        raise RuntimeError("Telethon client is not started")

    try:
        result = await _client.send_message(recipient, message)
        return result.id
    except PeerFloodError:
        raise RuntimeError(
            "Telegram PeerFloodError: too many messages sent, try again later"
        )
    except UserPrivacyRestrictedError:
        raise RuntimeError(
            "User privacy settings prevent sending messages"
        )
    except FloodWaitError as e:
        raise RuntimeError(
            f"Telegram FloodWaitError: must wait {e.seconds} seconds"
        )
