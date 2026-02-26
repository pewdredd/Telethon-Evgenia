"""Interactive Telethon session authorization.

Run: python -m app.auth_session

Creates a .session file that the server uses to authenticate
with Telegram without interactive prompts.
"""

import asyncio

from telethon import TelegramClient

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(
        settings.telegram_session_name,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    # start() handles phone/code/password prompts interactively
    await client.start()
    me = await client.get_me()
    print(f"Authorized as: {me.first_name} (@{me.username}), id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
