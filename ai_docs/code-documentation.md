# Telethon-Evgenia: Code Documentation

> Multi-account Telegram user-bot HTTP server for automated lead outreach via Telethon (MTProto).

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Module Reference](#module-reference)
  - [app/config.py — Configuration](#appconfigpy--configuration)
  - [app/auth.py — API Key Authentication](#appauthpy--api-key-authentication)
  - [app/telethon_client.py — Telegram Client Factory](#apptelethon_clientpy--telegram-client-factory)
  - [app/account_manager.py — Account Manager](#appaccount_managerpy--account-manager)
  - [app/listener.py — Incoming Message Listener](#applistenerpy--incoming-message-listener)
  - [app/main.py — FastAPI Application](#appmainpy--fastapi-application)
  - [app/auth_session.py — Interactive Session Setup](#appauth_sessionpy--interactive-session-setup)
- [API Reference](#api-reference)
  - [Account Management](#account-management)
  - [Per-Account Operations](#per-account-operations)
  - [Aggregated Endpoints](#aggregated-endpoints)
- [Data Models](#data-models)
- [Database Schema](#database-schema)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
- [Testing](#testing)

---

## Architecture Overview

```
n8n (external orchestrator)
  │
  │  HTTP POST /accounts/{id}/send (with X-API-Key header)
  ▼
┌──────────────────────────────────────────────────────┐
│  FastAPI Server (app/main.py)                        │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────┐         │
│  │ auth.py     │    │ config.py            │         │
│  │ API key     │    │ Settings from .env   │         │
│  │ verification│    └──────────────────────┘         │
│  └─────────────┘                                     │
│         │                                            │
│         ▼                                            │
│  ┌────────────────────────────────────────────────┐  │
│  │ account_manager.py                             │  │
│  │                                                │  │
│  │  AccountState (per account):                   │  │
│  │  ┌──────────────┐  ┌────────────────┐          │  │
│  │  │ TelegramClient│  │ asyncio.Queue  │          │  │
│  │  │ (from factory)│  │ + Worker task  │          │  │
│  │  └──────────────┘  │ (random delay) │          │  │
│  │                    └────────────────┘          │  │
│  │                                                │  │
│  │  Shared SQLite DB (aiosqlite):                 │  │
│  │  - accounts table (CRUD)                       │  │
│  │  - send_log (per-account quota + logging)      │  │
│  │  - incoming_log (per-account incoming msgs)    │  │
│  └────────────────────────────────────────────────┘  │
│         │                                            │
│         ▼                                            │
│  ┌────────────────────────────────────────────────┐  │
│  │ telethon_client.py (pure factory functions)    │  │
│  │ create_client() / send_message() / get_me()    │  │
│  └────────────────────────────────────────────────┘  │
│         │                                            │
│         │  ┌──────────────────────────────┐          │
│         │  │ listener.py                  │          │
│         │  │ register_listener() per acct │          │
│         │  │ → incoming_log (via manager) │          │
│         │  │ → webhook forwarding         │          │
│         │  └──────────────────────────────┘          │
│         │                                            │
└─────────┼────────────────────────────────────────────┘
          ▼
      Telegram
```

**Request flow (per account):**

1. n8n sends `POST /accounts/{id}/send` with `X-API-Key` header
2. `auth.py` validates the API key
3. `AccountManager` checks daily quota for that account
4. Message is placed into the account's `asyncio.Queue`
5. Background worker picks it up after a random delay (30–90s)
6. Worker re-checks quota, then calls `telethon_client.send_message(client, ...)`
7. Result is logged to SQLite (scoped by `account_id`) and returned via `asyncio.Future`

**Lifecycle (startup / shutdown):**

```
startup:
  AccountManager.init_db()  →  AccountManager.load_all()
    (for each authorized account: create_client() → connect → start worker → register listener)

shutdown:
  AccountManager.shutdown_all()
    (for each account: cancel worker → remove listener → disconnect client → close DB)
```

---

## Module Reference

### `app/config.py` — Configuration

Loads configuration from environment variables and `.env` file using Pydantic Settings.

#### `Settings` (class)

| Field | Type | Default | Env var | Description |
|-------|------|---------|---------|-------------|
| `host` | `str` | `"0.0.0.0"` | `HOST` | Server bind address |
| `port` | `int` | `8000` | `PORT` | Server bind port |
| `api_key` | `str` | `"change-me"` | `API_KEY` | Secret key for endpoint protection |
| `max_messages_per_day` | `int` | `25` | `MAX_MESSAGES_PER_DAY` | Default daily quota for new accounts |
| `min_delay_seconds` | `int` | `30` | `MIN_DELAY_SECONDS` | Default min delay for new accounts |
| `max_delay_seconds` | `int` | `90` | `MAX_DELAY_SECONDS` | Default max delay for new accounts |
| `db_path` | `str` | `"data/send_log.db"` | `DB_PATH` | SQLite database file path |
| `sessions_dir` | `str` | `"data/sessions"` | `SESSIONS_DIR` | Directory for Telethon session files |
| `incoming_webhook_url` | `str` | `""` | `INCOMING_WEBHOOK_URL` | Webhook URL for incoming messages |

#### `get_settings() -> Settings`

Cached factory (via `@lru_cache`) returning the singleton `Settings` instance.

---

### `app/auth.py` — API Key Authentication

Unchanged. Single API key protects all endpoints via `X-API-Key` header.

---

### `app/telethon_client.py` — Telegram Client Factory

Pure functions — no module-level state. Each function takes a `TelegramClient` as parameter.

#### `create_client(api_id, api_hash, session_path) -> TelegramClient`

Factory that creates a `TelegramClient` with hardcoded device emulation params (iPhone 17 Pro Max).

#### `get_me(client) -> dict | None`

Returns `{"id": int, "username": str | None}` or `None`.

#### `send_message(client, recipient, message) -> int`

Sends a message and returns the Telegram message ID. Raises `RuntimeError` for Telethon errors.

#### Auth Functions

All take `client` as first parameter:
- `send_code(client, phone) -> str`
- `verify_code(client, phone, code, hash, password?) -> dict`
- `qr_login_start(client) -> (qr_login, url)`
- `qr_login_wait(qr_login) -> dict`
- `qr_login_2fa(client, password) -> dict`

---

### `app/account_manager.py` — Account Manager

Central module managing all per-account state. Holds a single shared `aiosqlite.Connection`.

#### `AccountState` (dataclass)

```python
@dataclass
class AccountState:
    account_id: str
    client: TelegramClient
    queue: asyncio.Queue
    worker_task: asyncio.Task | None
    max_messages_per_day: int
    min_delay_seconds: int
    max_delay_seconds: int
    qr_login: object | None
    listener_cleanup: Callable | None
```

#### `AccountManager` (class)

Key methods:

| Method | Description |
|--------|-------------|
| `init_db()` | Create SQLite tables (accounts, send_log, incoming_log) |
| `load_all()` | Load authorized accounts from DB, start clients/workers/listeners |
| `add_account(...)` | Create account in DB + start client |
| `remove_account(id)` | Stop client + delete from DB |
| `update_account(id, ...)` | Update rate limits (DB + in-memory) |
| `get_account(id)` | Returns `AccountState` or raises `KeyError` |
| `get_account_info(id)` | Returns account dict from DB with `today_sent` |
| `list_accounts()` | Returns all accounts with details |
| `mark_authorized(id, telegram_id, username)` | Update status, start worker/listener |
| `enqueue_message(id, recipient, msg)` | Add to account's queue, return Future |
| `log_send(...)` | Insert into send_log |
| `get_today_send_count(id)` | Count successful sends today |
| `is_quota_available(id, max)` | Check quota |
| `log_incoming(...)` | Insert into incoming_log |
| `get_recent_incoming(...)` | Query incoming_log (optional account filter) |
| `mark_processed(id)` | Update processed flag |
| `is_known_recipient(id, sender_id, username)` | Check send_log for matches |
| `shutdown_all()` | Clean teardown of everything |

Session files are stored at `{sessions_dir}/{session_name}.session`.

---

### `app/listener.py` — Incoming Message Listener

Parameterized functions — no module-level state (except shared httpx client).

#### `register_listener(account_id, client, manager, webhook_url) -> cleanup_fn`

Registers a `NewMessage` event handler on the client. Returns a callable that removes the handler.

The handler:
1. Checks if sender is a known recipient via `manager.is_known_recipient()`
2. Logs to incoming_log via `manager.log_incoming()`
3. Forwards to webhook (if configured)

---

### `app/main.py` — FastAPI Application

Defines the app, lifespan, Pydantic models, and all route handlers.

#### Lifespan

- **Startup:** Creates `AccountManager`, calls `init_db()` + `load_all()`, stores in `app.state`
- **Shutdown:** Calls `manager.shutdown_all()`

#### Routes

See [API Reference](#api-reference) below.

---

### `app/auth_session.py` — Interactive Session Setup

CLI script for authorizing a Telegram account from the command line.

```bash
python -m app.auth_session --account-id <id>
```

Reads account credentials from the database, prompts for phone/code/2FA, creates the `.session` file, and updates the account status to `authorized`.

---

## API Reference

### Account Management

#### POST /accounts

Create a new account.

**Request Body:**
```json
{
  "account_id": "client-1",
  "api_id": 12345,
  "api_hash": "abc123...",
  "phone": "+79991234567",
  "max_messages_per_day": 25,
  "min_delay_seconds": 30,
  "max_delay_seconds": 90
}
```

Only `account_id`, `api_id`, and `api_hash` are required. Rate limits default to server settings.

#### GET /accounts

List all accounts with statuses and today's sent count.

#### GET /accounts/{account_id}

Get details for a single account.

#### PATCH /accounts/{account_id}

Update rate limits.

```json
{
  "max_messages_per_day": 50
}
```

#### DELETE /accounts/{account_id}

Stop the account's client/worker and delete it from the database.

---

### Per-Account Operations

All endpoints below require `account_id` path parameter.

#### POST /accounts/{account_id}/send

Enqueue a message for sending. Same behavior as the old `POST /send`, scoped by account.

#### GET /accounts/{account_id}/health

Check auth status of a specific account.

#### GET /accounts/{account_id}/incoming

Retrieve incoming messages for this account. Query params: `limit`, `unprocessed_only`.

#### POST /accounts/{account_id}/incoming/{id}/processed

Mark an incoming message as processed.

#### Auth Endpoints

- `POST /accounts/{account_id}/auth/send-code` — send login code
- `POST /accounts/{account_id}/auth/verify` — verify code + 2FA
- `POST /accounts/{account_id}/auth/qr` — start QR login
- `POST /accounts/{account_id}/auth/qr/wait` — wait for QR scan
- `POST /accounts/{account_id}/auth/qr/password` — submit 2FA after QR

---

### Aggregated Endpoints

#### GET /health

Server status + all accounts summary.

```json
{
  "status": "ok",
  "accounts": [
    {"account_id": "client-1", "status": "authorized", "username": "user1", "today_sent": 5},
    {"account_id": "client-2", "status": "pending", "username": null, "today_sent": 0}
  ]
}
```

#### GET /incoming

Incoming messages across all accounts. Query params: `limit`, `unprocessed_only`.

---

## Database Schema

SQLite database at `data/send_log.db` (configurable via `DB_PATH`).

### Table: `accounts`

```sql
CREATE TABLE accounts (
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
);
```

### Table: `send_log`

```sql
CREATE TABLE send_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT NOT NULL,
    recipient           TEXT NOT NULL,
    message             TEXT NOT NULL,
    telegram_message_id INTEGER,
    status              TEXT NOT NULL,
    error               TEXT,
    sent_at             TEXT NOT NULL
);
CREATE INDEX idx_send_log_account_date ON send_log(account_id, sent_at);
```

### Table: `incoming_log`

```sql
CREATE TABLE incoming_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    sender_username     TEXT,
    message_text        TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    received_at         TEXT NOT NULL,
    processed           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_incoming_log_account ON incoming_log(account_id);
```

---

## Configuration Reference

```env
# Server
HOST=0.0.0.0
PORT=8000
API_KEY=change-me-to-a-secret-key

# Default rate limits (for new accounts)
MAX_MESSAGES_PER_DAY=25
MIN_DELAY_SECONDS=30
MAX_DELAY_SECONDS=90

# Database
DB_PATH=data/send_log.db

# Sessions directory
SESSIONS_DIR=data/sessions

# Listener (optional)
INCOMING_WEBHOOK_URL=
```

---

## Deployment

### Docker

```bash
docker compose up -d
```

**Prerequisites:**

1. Create `.env` file from `.env.example`
2. Create accounts via `POST /accounts` API
3. Authorize each account via auth endpoints or CLI:
   ```bash
   python -m app.auth_session --account-id <id>
   ```

**docker-compose.yml** mounts `./data:/app/data` for SQLite DB and session file persistence.

---

## Testing

```bash
pip install -r requirements.txt
pytest
```

### Test Structure

```
tests/
├── conftest.py          # Shared fixtures (AccountManager-based)
├── test_accounts.py     # Account CRUD endpoint tests
├── test_send.py         # POST /accounts/{id}/send tests
├── test_health.py       # Health endpoint tests
├── test_rate_limiter.py # Rate limiting / quota unit tests
└── test_listener.py     # Incoming message logging tests
```

34 tests covering account CRUD, sending, health checks, rate limiting, quota enforcement, incoming message logging, and recipient matching.
