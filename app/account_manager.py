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

_UNSET: object = object()


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
    forward_incoming: bool = True
    webhook_url: str | None = None
    qr_login: object | None = None
    listener_cleanup: object | None = None
    watchdog_task: asyncio.Task | None = None


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
    forward_incoming    INTEGER NOT NULL DEFAULT 1,
    webhook_url         TEXT,
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

_CREATE_BOT_USERS = """
CREATE TABLE IF NOT EXISTS bot_users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER,
    username     TEXT,
    added_by     INTEGER NOT NULL,
    added_at     TEXT NOT NULL
)
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
        await self._db.execute(_CREATE_BOT_USERS)
        # Migration: add forward_incoming column to accounts if missing
        try:
            await self._db.execute(
                "ALTER TABLE accounts ADD COLUMN forward_incoming INTEGER NOT NULL DEFAULT 1"
            )
        except aiosqlite.OperationalError:
            pass
        # Migration: add webhook_url column to accounts if missing
        try:
            await self._db.execute("ALTER TABLE accounts ADD COLUMN webhook_url TEXT")
        except aiosqlite.OperationalError:
            pass
        # Migration: bot_users PK change — old schema had telegram_id as PK.
        async with self._db.execute("PRAGMA table_info(bot_users)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "id" not in columns:
            await self._db.execute("ALTER TABLE bot_users RENAME TO _bot_users_old")
            await self._db.execute(_CREATE_BOT_USERS)
            await self._db.execute(
                "INSERT INTO bot_users (telegram_id, username, added_by, added_at) "
                "SELECT telegram_id, NULL, added_by, added_at FROM _bot_users_old"
            )
            await self._db.execute("DROP TABLE _bot_users_old")
        else:
            # Migration: add username column if missing (fresh schema already has it)
            try:
                await self._db.execute("ALTER TABLE bot_users ADD COLUMN username TEXT")
            except aiosqlite.OperationalError:
                pass
        await self._db.commit()

    def _effective_webhook_url(self, state: AccountState) -> str | None:
        return state.webhook_url or self._settings.incoming_webhook_url

    async def load_all(self) -> None:
        """Load all authorized accounts from DB and start their clients/workers."""
        async with self.db.execute(
            "SELECT account_id, api_id, api_hash, session_name, "
            "max_messages_per_day, min_delay_seconds, max_delay_seconds, "
            "forward_incoming, webhook_url "
            "FROM accounts WHERE status = 'authorized'"
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            (account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d,
             fwd, webhook_url) = row
            try:
                await self._start_account(
                    account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d,
                    bool(fwd), webhook_url,
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
        forward_incoming: bool = True,
        webhook_url: str | None = None,
    ) -> AccountState:
        session_path = os.path.join(self._settings.sessions_dir, session_name)
        client = telethon_client.create_client(api_id, api_hash, session_path, self._settings.https_proxy)
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
            forward_incoming=forward_incoming,
            webhook_url=webhook_url,
        )

        # Start worker only if authorized
        if await client.is_user_authorized():
            state.worker_task = asyncio.create_task(
                self._worker(state), name=f"worker-{account_id}"
            )
            state.watchdog_task = asyncio.create_task(
                self._connection_watchdog(state), name=f"watchdog-{account_id}"
            )
            # Register listener only if forwarding is enabled
            if forward_incoming:
                from app.listener import register_listener
                state.listener_cleanup = register_listener(
                    account_id, client, self, self._effective_webhook_url(state)
                )

        self._accounts[account_id] = state
        return state

    async def _stop_account(self, account_id: str) -> None:
        state = self._accounts.pop(account_id, None)
        if state is None:
            return
        if state.watchdog_task is not None:
            state.watchdog_task.cancel()
            try:
                await state.watchdog_task
            except asyncio.CancelledError:
                pass
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
        forward_incoming: bool | None = None,
        webhook_url: str | None = None,
    ) -> dict:
        max_mpd = max_messages_per_day if max_messages_per_day is not None else self._settings.max_messages_per_day
        min_d = min_delay_seconds if min_delay_seconds is not None else self._settings.min_delay_seconds
        max_d = max_delay_seconds if max_delay_seconds is not None else self._settings.max_delay_seconds
        fwd = True if forward_incoming is None else forward_incoming

        session_name = f"session_{account_id}"
        now = datetime.now(UTC).isoformat()

        try:
            await self.db.execute(
                "INSERT INTO accounts "
                "(account_id, api_id, api_hash, phone, session_name, status, "
                "max_messages_per_day, min_delay_seconds, max_delay_seconds, "
                "forward_incoming, webhook_url, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (account_id, api_id, api_hash, phone, session_name, max_mpd, min_d, max_d,
                 1 if fwd else 0, webhook_url, now),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError:
            raise ValueError(f"Account '{account_id}' already exists")

        # Start the client (pending status — not authorized yet, but ready for auth)
        state = await self._start_account(
            account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d, fwd, webhook_url
        )

        return {
            "account_id": account_id,
            "status": "pending",
            "session_name": session_name,
            "max_messages_per_day": max_mpd,
            "min_delay_seconds": min_d,
            "max_delay_seconds": max_d,
            "forward_incoming": fwd,
            "webhook_url": webhook_url,
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
        forward_incoming: bool | None = None,
        webhook_url: str | None | object = _UNSET,
    ) -> dict:
        # Fetch current
        async with self.db.execute(
            "SELECT account_id FROM accounts WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Account {account_id} not found")

        updates = []
        params: list = []
        if max_messages_per_day is not None:
            updates.append("max_messages_per_day = ?")
            params.append(max_messages_per_day)
        if min_delay_seconds is not None:
            updates.append("min_delay_seconds = ?")
            params.append(min_delay_seconds)
        if max_delay_seconds is not None:
            updates.append("max_delay_seconds = ?")
            params.append(max_delay_seconds)
        if forward_incoming is not None:
            updates.append("forward_incoming = ?")
            params.append(1 if forward_incoming else 0)
        webhook_changed = False
        if webhook_url is not _UNSET:
            updates.append("webhook_url = ?")
            params.append(webhook_url)
            webhook_changed = True

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
                if webhook_changed:
                    state.webhook_url = webhook_url  # type: ignore[assignment]
                if forward_incoming is not None and forward_incoming != state.forward_incoming:
                    state.forward_incoming = forward_incoming
                    if forward_incoming:
                        # False → True: register listener if account is running
                        if state.worker_task is not None and state.listener_cleanup is None:
                            from app.listener import register_listener
                            state.listener_cleanup = register_listener(
                                account_id, state.client, self,
                                self._effective_webhook_url(state),
                            )
                    else:
                        # True → False: tear down listener if registered
                        if state.listener_cleanup is not None:
                            state.listener_cleanup()
                            state.listener_cleanup = None
                elif webhook_changed and state.listener_cleanup is not None:
                    # URL changed while listener already running — re-register with new URL
                    from app.listener import register_listener
                    state.listener_cleanup()
                    state.listener_cleanup = register_listener(
                        account_id, state.client, self,
                        self._effective_webhook_url(state),
                    )

        return await self.get_account_info(account_id)

    async def get_account_info(self, account_id: str) -> dict:
        async with self.db.execute(
            "SELECT account_id, api_id, phone, telegram_id, username, session_name, "
            "status, max_messages_per_day, min_delay_seconds, max_delay_seconds, "
            "forward_incoming, webhook_url, created_at "
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
            "forward_incoming": bool(row[10]),
            "webhook_url": row[11],
            "created_at": row[12],
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
            state.watchdog_task = asyncio.create_task(
                self._connection_watchdog(state), name=f"watchdog-{account_id}"
            )
            if state.forward_incoming:
                from app.listener import register_listener
                state.listener_cleanup = register_listener(
                    account_id, state.client, self, self._effective_webhook_url(state)
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

                if not state.client.is_connected():
                    try:
                        await state.client.connect()
                    except Exception as exc:
                        logger.warning(
                            "Inline reconnect for %s failed: %s", state.account_id, exc
                        )

                message_id, user_id = await telethon_client.send_message(
                    state.client, item.recipient, item.message
                )
                await self.log_send(
                    state.account_id, str(item.recipient), item.message,
                    message_id, "success"
                )
                item.future.set_result((message_id, user_id))
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

    async def enqueue_message(self, account_id: str, recipient: str | int, message: str) -> asyncio.Future[tuple[int, int]]:
        state = self.get_account(account_id)
        future: asyncio.Future[tuple[int, int]] = asyncio.get_running_loop().create_future()
        await state.queue.put(_QueueItem(recipient=recipient, message=message, future=future))
        return future

    # --- Connection watchdog ---

    async def _connection_watchdog(self, state: AccountState) -> None:
        """Periodically check client connectivity and reconnect on failure.

        Runs while the account is active. Cancelled by ``_stop_account``.
        """
        interval = self._settings.watchdog_interval_seconds
        max_attempts = self._settings.max_reconnect_attempts
        backoff_base = self._settings.reconnect_backoff_base_seconds
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if state.client.is_connected():
                continue
            logger.warning(
                "Account %s disconnected, attempting reconnect", state.account_id
            )
            connected = False
            for attempt in range(1, max_attempts + 1):
                try:
                    await state.client.connect()
                    connected = state.client.is_connected()
                    if connected:
                        logger.info(
                            "Account %s reconnected on attempt %d", state.account_id, attempt
                        )
                        break
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning(
                        "Reconnect attempt %d for %s failed: %s",
                        attempt, state.account_id, exc,
                    )
                try:
                    await asyncio.sleep(backoff_base * attempt)
                except asyncio.CancelledError:
                    return
            if not connected:
                logger.error(
                    "Account %s reconnect gave up after %d attempts; will retry next tick",
                    state.account_id, max_attempts,
                )

    # --- Restart ---

    async def restart_account(self, account_id: str) -> None:
        """Stop and re-start an account's client/worker using DB row.

        Useful for ops recovery without restarting the whole container.
        """
        async with self.db.execute(
            "SELECT api_id, api_hash, session_name, max_messages_per_day, "
            "min_delay_seconds, max_delay_seconds, forward_incoming, webhook_url "
            "FROM accounts WHERE account_id = ?",
            (account_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"Account {account_id} not found")
        api_id, api_hash, session_name, max_mpd, min_d, max_d, fwd, webhook_url = row
        await self._stop_account(account_id)
        await self._start_account(
            account_id, api_id, api_hash, session_name, max_mpd, min_d, max_d,
            bool(fwd), webhook_url,
        )

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

    async def get_total_send_count(self, account_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM send_log "
            "WHERE account_id = ? AND status = 'success'",
            (account_id,),
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

    # --- Bot users ---

    async def add_bot_user(
        self,
        added_by: int,
        telegram_id: int | None = None,
        username: str | None = None,
    ) -> None:
        if username:
            username = username.lstrip("@").lower()
        # Avoid duplicates: check existing entry by telegram_id or username
        if telegram_id:
            async with self.db.execute(
                "SELECT 1 FROM bot_users WHERE telegram_id = ?", [telegram_id]
            ) as cur:
                if await cur.fetchone():
                    return
        if username:
            async with self.db.execute(
                "SELECT 1 FROM bot_users WHERE username = ?", [username]
            ) as cur:
                if await cur.fetchone():
                    return
        await self.db.execute(
            "INSERT INTO bot_users (telegram_id, username, added_by, added_at) "
            "VALUES (?, ?, ?, ?)",
            [telegram_id, username, added_by, datetime.now(UTC).isoformat()],
        )
        await self.db.commit()

    async def remove_bot_user(self, identifier: str) -> bool:
        """Remove by numeric telegram_id or username."""
        try:
            tid = int(identifier)
            cursor = await self.db.execute(
                "DELETE FROM bot_users WHERE telegram_id = ?", [tid]
            )
        except ValueError:
            clean = identifier.lstrip("@").lower()
            cursor = await self.db.execute(
                "DELETE FROM bot_users WHERE username = ?", [clean]
            )
        await self.db.commit()
        return cursor.rowcount > 0

    async def list_bot_users(self) -> list[dict]:
        async with self.db.execute(
            "SELECT telegram_id, username, added_by, added_at FROM bot_users ORDER BY added_at"
        ) as cursor:
            return [
                {"telegram_id": r[0], "username": r[1], "added_by": r[2], "added_at": r[3]}
                for r in await cursor.fetchall()
            ]

    async def is_bot_user(self, telegram_id: int, username: str | None = None) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM bot_users WHERE telegram_id = ?", [telegram_id]
        ) as cursor:
            if await cursor.fetchone():
                return True
        if username:
            clean = username.lstrip("@").lower()
            async with self.db.execute(
                "SELECT 1 FROM bot_users WHERE username = ?", [clean]
            ) as cursor:
                return await cursor.fetchone() is not None
        return False

    async def resolve_bot_user(self, telegram_id: int, username: str | None) -> None:
        """Link telegram_id to a user previously added by username."""
        if not username:
            return
        clean = username.lstrip("@").lower()
        # Find entry with matching username but no telegram_id
        async with self.db.execute(
            "SELECT id FROM bot_users WHERE username = ? AND telegram_id IS NULL",
            [clean],
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await self.db.execute(
                "UPDATE bot_users SET telegram_id = ? WHERE id = ?",
                [telegram_id, row[0]],
            )
            await self.db.commit()

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
