"""Interactive Telethon session authorization.

Run: python -m app.auth_session

Creates a .session file that the server uses to authenticate
with Telegram without interactive prompts.
"""

import asyncio

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(
        settings.telegram_session_name,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        device_model="iPhone 17 Pro Max",
        system_version="26.2.1",
        app_version="12.5",
        lang_code="ru",
        system_lang_code="ru",
    )

    await client.connect()

    phone = input("Phone number (e.g. +79991234567): ").strip()

    result = await client.send_code_request(phone)
    phone_code_hash = result.phone_code_hash
    print("Code sent. Check Telegram app (or SMS).")

    while True:
        code = input("Enter the code: ").strip()
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            break
        except PhoneCodeInvalidError:
            print("Wrong code, try again.")
        except PhoneCodeExpiredError:
            print("Code expired. Requesting a new one...")
            result = await client.send_code_request(phone)
            phone_code_hash = result.phone_code_hash
        except SessionPasswordNeededError:
            password = input("2FA password: ").strip()
            await client.sign_in(password=password)
            break

    me = await client.get_me()
    print(f"Authorized as: {me.first_name} (@{me.username}), id={me.id}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
