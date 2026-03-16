"""Central per-account state management.

Manages multiple Telegram accounts, each with their own TelegramClient,
message queue, worker task, and rate limits. All database operations
(accounts, send_log, incoming_log) are handled here with a single
shared aiosqlite connection.
"""

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypedDict

import aiosqlite

from app import telethon_client
from app.config import Settings

logger = logging.getLogger(__name__)


class IncomingMessage(TypedDict):
    id: int
    account_id: str
    sender_id: str
    sender_username: str | None
    message_text: str
    telegram_message_id: int
    chat_id: int
    received_at: str
    processed: bool


@dataclass
class _QueueItem:
    recipient: str | int
    message: str
    future: asyncio.Future[int]


@dataclass
class AccountState:
    account_id: str
    client: object  # TelegramClient
    queue: asyncio.Queue[_QueueItem]
    worker_task: asyncio.Task | None
    max_messages_per_day: int
    min_delay_seconds: int
    max_delay_seconds: int
    qr_login: object | None = None
    listener_cleanup: object | None = None


_CREATE_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id          TEXT PRIMARY KEY,
    api_id              INTEGER NOT NULL,
    api_hash            TEXT NOT NULL,
    phone               TEXT,
    telegram_id         INTEGER,
    username            TEXT,
    session_name        TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending',
    max_messages_per_day INTEGER NOT NULL DEFAULT 25,
    min_delay_seconds   INTEGER NOT NULL DEFAULT 30,
    max_delay_seconds   INTEGER NOT NULL DEFAULT 90,
    created_at          TEXT NOT NULL
)
"""

_CREATE_SEND_LOG = """
CREATE TABLE IF NOT EXISTS send_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT NOT NULL,
    recipient           TEXT NOT NULL,
    message             TEXT NOT NULL,
    telegram_message_id INTEGER,
    status              TEXT NOT NULL,
    error               TEXT,
    sent_at             TEXT NOT NULL
)
"""

_CREATE_SEND_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_send_log_account_date ON send_log(account_id, sent_at)
"""

_CREATE_INCOMING_LOG = """
CREATE TABLE IF NOT EXISTS incoming_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    sender_username     TEXT,
    message_text        TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    received_at         TEXT NOT NULL,
    processed           INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INCOMING_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_incoming_log_account ON incoming_log(account_id)
"""


class AccountManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._accounts: dict[str, AccountState] = {}
        self._db: aiosqlite.Connection | None = None
        self._http_client = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized")
        return self._db

    async def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._settings.db_path) or ".", exist_ok=True)
        os.makedirs(self._settings.sessions_dir, exist_ok=True)
        self._db = await aiosqlite.connect(self._settings.db_path)
        await self._db.execute(_CREATE_ACCOUNTS)
        await self._db.execute(_CREATE_SEND_LOG)
        await self._db.execute(_CREATE_SEND_LOG_INDEX)
        await self._db.execute(_CREATE_INCOMING_LOG)
        await self._db.execute(_CREATE_INCOMING_LOG_INDEX)
        await self._db.commit()

    async def load_all(self) -> None:
        """Load all authorized accounts from DB and start their clients/workers."""
        async with self.db.execute(
            "SELECT account_id, api_id, api_hash, session_name, "
            "max_messages_per_day, min_delay_seconds, max_delay_seconds "
            "FROM accounts WHERE status = 'authorized'"
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d = row
            try:
                await self._start_account(
                    account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d
                )
                logger.info("Loaded account %s", account_id)
            except Exception:
                logger.exception("Failed to load account %s", account_id)

    async def _start_account(
        self,
        account_id: str,
        api_id: int,
        api_hash: str,
        session_name: str,
        max_messages_per_day: int,
        min_delay_seconds: int,
        max_delay_seconds: int,
    ) -> AccountState:
        session_path = os.path.join(self._settings.sessions_dir, session_name)
        client = telethon_client.create_client(api_id, api_hash, session_path)
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info("Account %s authorized as %s (@%s)", account_id, me.first_name, me.username)
        else:
            logger.warning("Account %s not yet authorized", account_id)

        queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        state = AccountState(
            account_id=account_id,
            client=client,
            queue=queue,
            worker_task=None,
            max_messages_per_day=max_messages_per_day,
            min_delay_seconds=min_delay_seconds,
            max_delay_seconds=max_delay_seconds,
        )

        # Start worker only if authorized
        if await client.is_user_authorized():
            state.worker_task = asyncio.create_task(
                self._worker(state), name=f"worker-{account_id}"
            )
            # Register listener
            from app.listener import register_listener
            state.listener_cleanup = register_listener(
                account_id, client, self, self._settings.incoming_webhook_url
            )

        self._accounts[account_id] = state
        return state

    async def _stop_account(self, account_id: str) -> None:
        state = self._accounts.pop(account_id, None)
        if state is None:
            return
        if state.worker_task is not None:
            state.worker_task.cancel()
            try:
                await state.worker_task
            except asyncio.CancelledError:
                pass
        if state.listener_cleanup is not None:
            state.listener_cleanup()
        if hasattr(state.client, 'disconnect'):
            await state.client.disconnect()

    # --- Account CRUD ---

    async def add_account(
        self,
        account_id: str,
        api_id: int,
        api_hash: str,
        phone: str | None = None,
        max_messages_per_day: int | None = None,
        min_delay_seconds: int | None = None,
        max_delay_seconds: int | None = None,
    ) -> dict:
        max_mpd = max_messages_per_day if max_messages_per_day is not None else self._settings.max_messages_per_day
        min_d = min_delay_seconds if min_delay_seconds is not None else self._settings.min_delay_seconds
        max_d = max_delay_seconds if max_delay_seconds is not None else self._settings.max_delay_seconds

        session_name = f"session_{account_id}"
        now = datetime.now(UTC).isoformat()

        await self.db.execute(
            "INSERT INTO accounts "
            "(account_id, api_id, api_hash, phone, session_name, status, "
            "max_messages_per_day, min_delay_seconds, max_delay_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (account_id, api_id, api_hash, phone, session_name, max_mpd, min_d, max_d, now),
        )
        await self.db.commit()

        # Start the client (pending status — not authorized yet, but ready for auth)
        state = await self._start_account(
            account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d
        )

        return {
            "account_id": account_id,
            "status": "pending",
            "session_name": session_name,
            "max_messages_per_day": max_mpd,
            "min_delay_seconds": min_d,
            "max_delay_seconds": max_d,
            "created_at": now,
        }

    async def remove_account(self, account_id: str) -> None:
        await self._stop_account(account_id)
        await self.db.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
        await self.db.commit()

    async def update_account(
        self,
        account_id: str,
        max_messages_per_day: int | None = None,
        min_delay_seconds: int | None = None,
        max_delay_seconds: int | None = None,
    ) -> dict:
        # Fetch current
        async with self.db.execute(
            "SELECT account_id FROM accounts WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Account {account_id} not found")

        updates = []
        params = []
        if max_messages_per_day is not None:
            updates.append("max_messages_per_day = ?")
            params.append(max_messages_per_day)
        if min_delay_seconds is not None:
            updates.append("min_delay_seconds = ?")
            params.append(min_delay_seconds)
        if max_delay_seconds is not None:
            updates.append("max_delay_seconds = ?")
            params.append(max_delay_seconds)

        if updates:
            params.append(account_id)
            await self.db.execute(
                f"UPDATE accounts SET {', '.join(updates)} WHERE account_id = ?",
                params,
            )
            await self.db.commit()

            # Update in-memory state
            state = self._accounts.get(account_id)
            if state:
                if max_messages_per_day is not None:
                    state.max_messages_per_day = max_messages_per_day
                if min_delay_seconds is not None:
                    state.min_delay_seconds = min_delay_seconds
                if max_delay_seconds is not None:
                    state.max_delay_seconds = max_delay_seconds

        return await self.get_account_info(account_id)

    async def get_account_info(self, account_id: str) -> dict:
        async with self.db.execute(
            "SELECT account_id, api_id, phone, telegram_id, username, session_name, "
            "status, max_messages_per_day, min_delay_seconds, max_delay_seconds, created_at "
            "FROM accounts WHERE account_id = ?",
            (account_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Account {account_id} not found")

        today_sent = await self.get_today_send_count(account_id)
        return {
            "account_id": row[0],
            "api_id": row[1],
            "phone": row[2],
            "telegram_id": row[3],
            "username": row[4],
            "session_name": row[5],
            "status": row[6],
            "max_messages_per_day": row[7],
            "min_delay_seconds": row[8],
            "max_delay_seconds": row[9],
            "created_at": row[10],
            "today_sent": today_sent,
        }

    async def list_accounts(self) -> list[dict]:
        async with self.db.execute(
            "SELECT account_id FROM accounts ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append(await self.get_account_info(row[0]))
        return results

    def get_account(self, account_id: str) -> AccountState:
        state = self._accounts.get(account_id)
        if state is None:
            raise KeyError(f"Account {account_id} not found or not started")
        return state

    # --- Auth status update ---

    async def mark_authorized(self, account_id: str, telegram_id: int, username: str | None) -> None:
        """Update account status to 'authorized' and start worker/listener."""
        await self.db.execute(
            "UPDATE accounts SET status = 'authorized', telegram_id = ?, username = ? "
            "WHERE account_id = ?",
            (telegram_id, username, account_id),
        )
        await self.db.commit()

        state = self._accounts.get(account_id)
        if state and state.worker_task is None:
            state.worker_task = asyncio.create_task(
                self._worker(state), name=f"worker-{account_id}"
            )
            from app.listener import register_listener
            state.listener_cleanup = register_listener(
                account_id, state.client, self, self._settings.incoming_webhook_url
            )

    # --- Worker loop ---

    async def _worker(self, state: AccountState) -> None:
        while True:
            item = await state.queue.get()
            try:
                delay = random.uniform(state.min_delay_seconds, state.max_delay_seconds)
                await asyncio.sleep(delay)

                if not await self.is_quota_available(state.account_id, state.max_messages_per_day):
                    item.future.set_exception(RuntimeError("Daily message quota exhausted"))
                    await self.log_send(
                        state.account_id, str(item.recipient), item.message,
                        None, "error", "quota_exhausted"
                    )
                    continue

                message_id = await telethon_client.send_message(
                    state.client, item.recipient, item.message
                )
                await self.log_send(
                    state.account_id, str(item.recipient), item.message,
                    message_id, "success"
                )
                item.future.set_result(message_id)
            except Exception as exc:
                error_msg = str(exc)
                await self.log_send(
                    state.account_id, str(item.recipient), item.message,
                    None, "error", error_msg
                )
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                state.queue.task_done()

    async def enqueue_message(self, account_id: str, recipient: str | int, message: str) -> asyncio.Future[int]:
        state = self.get_account(account_id)
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        await state.queue.put(_QueueItem(recipient=recipient, message=message, future=future))
        return future

    # --- Send log ---

    async def log_send(
        self,
        account_id: str,
        recipient: str,
        message: str,
        telegram_message_id: int | None,
        status: str,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO send_log "
            "(account_id, recipient, message, telegram_message_id, status, error, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, recipient, message, telegram_message_id, status, error,
             datetime.now(UTC).isoformat()),
        )
        await self.db.commit()

    async def get_today_send_count(self, account_id: str) -> int:
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with self.db.execute(
            "SELECT COUNT(*) FROM send_log "
            "WHERE account_id = ? AND status = 'success' AND sent_at >= ?",
            (account_id, today_start),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def is_quota_available(self, account_id: str, max_per_day: int) -> bool:
        return await self.get_today_send_count(account_id) < max_per_day

    # --- Incoming log ---

    async def log_incoming(
        self,
        account_id: str,
        sender_id: str,
        sender_username: str | None,
        message_text: str,
        telegram_message_id: int,
        chat_id: int,
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO incoming_log "
            "(account_id, sender_id, sender_username, message_text, "
            "telegram_message_id, chat_id, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, sender_id, sender_username, message_text,
             telegram_message_id, chat_id, datetime.now(UTC).isoformat()),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_recent_incoming(
        self,
        account_id: str | None = None,
        limit: int = 50,
        unprocessed_only: bool = False,
    ) -> list[IncomingMessage]:
        conditions = []
        params: list = []
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)
        if unprocessed_only:
            conditions.append("processed = 0")

        query = (
            "SELECT id, account_id, sender_id, sender_username, message_text, "
            "telegram_message_id, chat_id, received_at, processed FROM incoming_log"
        )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [
            IncomingMessage(
                id=row[0],
                account_id=row[1],
                sender_id=row[2],
                sender_username=row[3],
                message_text=row[4],
                telegram_message_id=row[5],
                chat_id=row[6],
                received_at=row[7],
                processed=bool(row[8]),
            )
            for row in rows
        ]

    async def mark_processed(self, incoming_id: int) -> None:
        await self.db.execute(
            "UPDATE incoming_log SET processed = 1 WHERE id = ?",
            (incoming_id,),
        )
        await self.db.commit()

    # --- Recipient matching ---

    async def is_known_recipient(
        self, account_id: str, sender_id: int, sender_username: str | None
    ) -> bool:
        try:
            conditions = ["recipient = ?"]
            params: list[str] = [str(sender_id)]
            if sender_username:
                clean = sender_username.lstrip("@")
                conditions.append("recipient = ?")
                params.append(f"@{clean}")
                conditions.append("recipient = ?")
                params.append(clean)
            where = " OR ".join(conditions)
            async with self.db.execute(
                f"SELECT COUNT(*) FROM send_log "
                f"WHERE account_id = ? AND ({where}) AND status = 'success'",
                [account_id, *params],
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0] if row else 0) > 0
        except Exception:
            logger.exception("Error checking known recipient")
            return False

    # --- Shutdown ---

    async def shutdown_all(self) -> None:
        for account_id in list(self._accounts):
            await self._stop_account(account_id)
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        if self._db is not None:
            await self._db.close()
            self._db = None
