"""Interactive Telethon session authorization for a specific account.

Run: python -m app.auth_session --account-id <id>

Reads account credentials from the database and creates a .session file
that the server uses to authenticate with Telegram.
"""

import argparse
import asyncio
import os

import aiosqlite
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.config import get_settings


async def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize a Telegram account session")
    parser.add_argument("--account-id", required=True, help="Account ID to authorize")
    args = parser.parse_args()

    settings = get_settings()

    db = await aiosqlite.connect(settings.db_path)
    async with db.execute(
        "SELECT api_id, api_hash, session_name FROM accounts WHERE account_id = ?",
        (args.account_id,),
    ) as cursor:
        row = await cursor.fetchone()
    await db.close()

    if row is None:
        print(f"Account '{args.account_id}' not found in database.")
        return

    api_id, api_hash, session_name = row
    session_path = os.path.join(settings.sessions_dir, session_name)

    client = TelegramClient(
        session_path,
        api_id,
        api_hash,
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

    # Update account status in DB
    db = await aiosqlite.connect(settings.db_path)
    await db.execute(
        "UPDATE accounts SET status = 'authorized', telegram_id = ?, username = ?, phone = ? "
        "WHERE account_id = ?",
        (me.id, me.username, phone, args.account_id),
    )
    await db.commit()
    await db.close()

    await client.disconnect()
    print(f"Account '{args.account_id}' marked as authorized.")


if __name__ == "__main__":
    asyncio.run(main())
