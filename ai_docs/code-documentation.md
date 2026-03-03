# Telethon-Evgenia: Code Documentation

> Telegram user-bot HTTP server for automated lead outreach via Telethon (MTProto).

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Module Reference](#module-reference)
  - [app/config.py — Configuration](#appconfigpy--configuration)
  - [app/auth.py — API Key Authentication](#appauthpy--api-key-authentication)
  - [app/telethon_client.py — Telegram Client](#apptelethon_clientpy--telegram-client)
  - [app/rate_limiter.py — Rate Limiting & Queue](#apprate_limiterpy--rate-limiting--queue)
  - [app/main.py — FastAPI Application](#appmainpy--fastapi-application)
  - [app/auth_session.py — Interactive Session Setup](#appauth_sessionpy--interactive-session-setup)
- [API Reference](#api-reference)
  - [POST /send](#post-send)
  - [GET /health](#get-health)
  - [POST /auth/send-code](#post-authsend-code)
  - [POST /auth/verify](#post-authverify)
  - [POST /auth/qr](#post-authqr)
  - [POST /auth/qr/wait](#post-authqrwait)
  - [POST /auth/qr/password](#post-authqrpassword)
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
  │  HTTP POST /send (with X-API-Key header)
  ▼
┌──────────────────────────────────────────────┐
│  FastAPI Server (app/main.py)                │
│                                              │
│  ┌─────────────┐    ┌──────────────────────┐ │
│  │ auth.py     │    │ config.py            │ │
│  │ API key     │    │ Settings from .env   │ │
│  │ verification│    └──────────────────────┘ │
│  └─────────────┘                             │
│         │                                    │
│         ▼                                    │
│  ┌──────────────────────────────────────┐    │
│  │ rate_limiter.py                      │    │
│  │                                      │    │
│  │  ┌────────────┐  ┌────────────────┐  │    │
│  │  │ Quota      │  │ asyncio.Queue  │  │    │
│  │  │ check      │  │ + Worker task  │  │    │
│  │  │ (SQLite)   │  │ (random delay) │  │    │
│  │  └────────────┘  └───────┬────────┘  │    │
│  │                          │           │    │
│  │  ┌───────────────────────┘           │    │
│  │  │  SQLite send_log (aiosqlite)      │    │
│  │  └──────────────────────────────────  │    │
│  └──────────────────────────────────────┘    │
│         │                                    │
│         ▼                                    │
│  ┌──────────────────────────────────────┐    │
│  │ telethon_client.py                   │    │
│  │ TelegramClient (MTProto user-bot)    │    │
│  └──────────────────────────────────────┘    │
│         │                                    │
└─────────┼────────────────────────────────────┘
          ▼
      Telegram
```

**Request flow:**

1. n8n sends `POST /send` with `X-API-Key` header
2. `auth.py` validates the API key
3. `rate_limiter` checks daily quota against SQLite `send_log`
4. Message is placed into `asyncio.Queue`
5. Background worker picks it up after a random delay (30–90s)
6. Worker re-checks quota, then calls `telethon_client.send_message()`
7. Result (message_id or error) is logged to SQLite and returned via `asyncio.Future`

**Lifecycle (startup / shutdown):**

```
startup:
  init_db()  →  start_client()  →  start_worker()

shutdown:
  stop_worker()  →  stop_client()  →  close_db()
```

---

## Module Reference

### `app/config.py` — Configuration

Loads all configuration from environment variables and `.env` file using Pydantic Settings.

#### `Settings` (class)

```python
class Settings(BaseSettings):
```

Pydantic v2 settings model. All fields map to environment variables (case-insensitive).

| Field | Type | Default | Env var | Description |
|-------|------|---------|---------|-------------|
| `telegram_api_id` | `int` | `0` | `TELEGRAM_API_ID` | Telegram API application ID from [my.telegram.org](https://my.telegram.org) |
| `telegram_api_hash` | `str` | `""` | `TELEGRAM_API_HASH` | Telegram API application hash |
| `telegram_session_name` | `str` | `"evgenia"` | `TELEGRAM_SESSION_NAME` | Name for the `.session` file |
| `host` | `str` | `"0.0.0.0"` | `HOST` | Server bind address |
| `port` | `int` | `8000` | `PORT` | Server bind port |
| `api_key` | `str` | `"change-me"` | `API_KEY` | Secret key for endpoint protection |
| `max_messages_per_day` | `int` | `25` | `MAX_MESSAGES_PER_DAY` | Hard daily message quota |
| `min_delay_seconds` | `int` | `30` | `MIN_DELAY_SECONDS` | Minimum random delay between sends (seconds) |
| `max_delay_seconds` | `int` | `90` | `MAX_DELAY_SECONDS` | Maximum random delay between sends (seconds) |
| `db_path` | `str` | `"data/send_log.db"` | `DB_PATH` | SQLite database file path |

#### `get_settings() -> Settings`

Cached factory (via `@lru_cache`) that returns the singleton `Settings` instance. Used as a FastAPI dependency.

---

### `app/auth.py` — API Key Authentication

Provides API key verification as a FastAPI dependency.

#### `verify_api_key(api_key, settings) -> str`

```python
async def verify_api_key(
    api_key: str = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> str
```

FastAPI dependency that reads the `X-API-Key` header and compares it to `settings.api_key`.

- **Returns:** the API key string on success
- **Raises:** `HTTPException(401)` with `"Invalid API key"` if mismatch
- **Header name:** `X-API-Key` (defined via `APIKeyHeader`)

---

### `app/telethon_client.py` — Telegram Client

Manages the Telethon `TelegramClient` lifecycle and provides message sending and session authorization. Uses a module-level `_client` variable (singleton pattern).

The client is initialized with device emulation parameters to reduce the risk of Telegram blocking the login:

```python
TelegramClient(
    session_name, api_id, api_hash,
    device_model="iPhone 17 Pro Max",
    system_version="26.2.1",
    app_version="12.5",
    lang_code="ru",
    system_lang_code="ru",
)
```

#### `start_client(settings: Settings) -> None`

Creates and connects the `TelegramClient` using credentials from `settings`. Logs whether the session is already authorized or requires login via one of the `/auth/*` endpoints.

#### `stop_client() -> None`

Disconnects the client and sets the module-level reference to `None`.

#### `get_me() -> dict | None`

Returns the authenticated user's info as `{"id": int, "username": str | None}`, or `None` if the client is not connected or an error occurs.

#### `send_message(recipient: str | int, message: str) -> int`

Sends a message to the given recipient via Telethon.

- **Parameters:**
  - `recipient` — `@username` string or numeric Telegram user ID
  - `message` — message text to send
- **Returns:** Telegram message ID (`int`)
- **Raises `RuntimeError`** in these cases:
  - Client not started
  - `PeerFloodError` — too many messages sent, rate limited by Telegram
  - `UserPrivacyRestrictedError` — recipient's privacy settings block the message
  - `FloodWaitError` — Telegram requires waiting N seconds

#### Phone Code Auth

##### `send_code(phone: str) -> str`

Sends a login code to the given phone number.

- **Returns:** `phone_code_hash` string (required for `verify_code`)

##### `verify_code(phone, code, phone_code_hash, password?) -> dict`

Signs in with the received code. If the account has 2FA enabled, `password` must be provided.

- **Returns:** `{"id": int, "username": str | None}`
- **Raises `RuntimeError("2FA password required")`** if 2FA is needed and `password` is not given

#### QR Login

##### `qr_login_start() -> str`

Starts a QR login session.

- **Returns:** `tg://login?token=...` URL — convert to a QR code image and scan with the Telegram mobile app

##### `qr_login_wait() -> dict`

Blocks up to 60 seconds waiting for the QR to be scanned.

- **Returns:** `{"id": int, "username": str | None}` on success
- **Raises `RuntimeError("2FA_REQUIRED")`** if the account has 2FA enabled (call `qr_login_2fa` next)

##### `qr_login_2fa(password: str) -> dict`

Submits the 2FA password after a successful QR scan.

- **Returns:** `{"id": int, "username": str | None}`

---

### `app/rate_limiter.py` — Rate Limiting & Queue

Implements daily quota enforcement, message queueing with random delays, and send logging via SQLite. This is the core anti-ban protection layer.

#### Database Functions

##### `init_db(db_path: str) -> None`

Opens an `aiosqlite` connection and creates the `send_log` table if it doesn't exist.

##### `close_db() -> None`

Closes the database connection.

##### `log_send(recipient, message, telegram_message_id, status, error?) -> None`

```python
async def log_send(
    recipient: str,
    message: str,
    telegram_message_id: int | None,
    status: str,
    error: str | None = None,
) -> None
```

Inserts a record into `send_log` with the current UTC timestamp.

| Parameter | Description |
|-----------|-------------|
| `recipient` | Recipient identifier (username or user_id as string) |
| `message` | Message text |
| `telegram_message_id` | Telegram's message ID on success, `None` on error |
| `status` | `"success"` or `"error"` |
| `error` | Error description (optional) |

##### `get_today_send_count() -> int`

Returns the number of successful sends (`status='success'`) since midnight UTC today.

##### `is_quota_available(max_per_day: int) -> bool`

Returns `True` if today's successful send count is below `max_per_day`.

#### Queue & Worker

##### Type Alias: `SendFn`

```python
SendFn = Callable[[str | int, str], Awaitable[int]]
```

Signature for the message-sending callable (matches `telethon_client.send_message`).

##### `_QueueItem` (dataclass)

```python
@dataclass
class _QueueItem:
    recipient: str | int
    message: str
    future: asyncio.Future[int]
```

Internal queue entry. The `future` is resolved with the Telegram message ID on success, or rejected with an exception on failure.

##### `start_worker(send_fn: SendFn, settings: Settings) -> None`

Creates a fresh `asyncio.Queue` and spawns the background worker as an `asyncio.Task`.

##### `stop_worker() -> None`

Cancels the worker task and clears the queue.

##### `enqueue_message(recipient: str | int, message: str) -> asyncio.Future[int]`

Adds a message to the queue and returns a `Future` that resolves when the message is actually sent (or fails).

##### Worker Loop (`_worker`)

The background worker loop:

1. Waits for an item from the queue
2. Sleeps for a random delay between `min_delay_seconds` and `max_delay_seconds`
3. Re-checks the daily quota
4. Calls `send_fn(recipient, message)`
5. Logs the result to SQLite
6. Resolves or rejects the item's `Future`

---

### `app/main.py` — FastAPI Application

The main application module. Defines the FastAPI app, lifespan, request/response models, and route handlers.

#### `lifespan(app: FastAPI)`

Async context manager for application lifecycle:

- **Startup:** `init_db()` → `start_client()` → `start_worker()`
- **Shutdown:** `stop_worker()` → `stop_client()` → `close_db()`

Also ensures the database directory exists (`os.makedirs`).

#### Routes

##### `post_send(body: SendRequest) -> SendResponse`

`POST /send` — Enqueue a message for sending.

1. Checks daily quota via `rate_limiter.is_quota_available()`
2. Converts numeric recipient strings to `int`
3. Enqueues the message and awaits the `Future`
4. Returns `{ok: true, message_id}` or `{ok: false, error}`

##### `get_health() -> HealthResponse`

`GET /health` — Check server and Telegram session status.

Returns authorized account info or `{authorized: false}`.

##### `post_auth_send_code(body: SendCodeRequest) -> SendCodeResponse`

`POST /auth/send-code` — Step 1 of phone-based login. Sends a Telegram login code to the given phone number.

##### `post_auth_verify(body: VerifyCodeRequest) -> VerifyCodeResponse`

`POST /auth/verify` — Step 2 of phone-based login. Verifies the received code and saves the session.

##### `post_auth_qr() -> QrResponse`

`POST /auth/qr` — Start a QR login. Returns the `tg://login?token=...` URL to display as a QR code.

##### `post_auth_qr_wait() -> QrWaitResponse`

`POST /auth/qr/wait` — Wait up to 60s for the QR to be scanned. If `need_2fa: true` is returned, call `/auth/qr/password` next.

##### `post_auth_qr_password(body: QrPasswordRequest) -> QrWaitResponse`

`POST /auth/qr/password` — Submit the 2FA password after a successful QR scan.

---

### `app/auth_session.py` — Interactive Session Setup

Standalone script for one-time Telegram session authorization from the command line.

```bash
python -m app.auth_session
```

Prompts for phone number, verification code, and optional 2FA password. Creates a `.session` file that the server uses for non-interactive authentication on subsequent starts.

Uses the same device emulation parameters as `telethon_client.py` (iPhone 17 Pro Max / iOS 26.2.1 / Telegram 12.5).

#### `main() -> None`

1. Connects to Telegram using credentials from `Settings`
2. Prompts for phone number and calls `send_code_request()` — stores `phone_code_hash`
3. Prompts for the code; retries on wrong code, re-requests on expired code
4. Handles 2FA (`SessionPasswordNeededError`) by prompting for the password
5. Prints the authorized account info and disconnects

---

## API Reference

### POST /send

Send a message to a Telegram user.

**Authentication:** `X-API-Key` header (required)

**Request Body:**

```json
{
  "recipient": "@username",
  "message": "Personalized message text"
}
```

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `recipient` | `string` | Yes | `min_length=1` | `@username` or numeric user ID as string |
| `message` | `string` | Yes | `min_length=1` | Message text to send |

**Success Response (200):**

```json
{
  "ok": true,
  "message_id": 12345
}
```

**Error Responses:**

| Status | Condition | Body |
|--------|-----------|------|
| 200 | Telethon send error (privacy, flood) | `{"ok": false, "error": "..."}` |
| 401 | Invalid or missing API key | `{"detail": "Invalid API key"}` |
| 422 | Validation error (empty fields) | Pydantic validation error |
| 429 | Daily quota exhausted | `{"detail": "Daily message quota exhausted"}` |

**Behavior Notes:**

- Numeric recipient strings (e.g. `"123456"`) are automatically converted to `int` for Telethon
- The message is queued, not sent immediately — there is a random delay of 30–90 seconds
- The HTTP response waits until the message is actually sent (or fails)
- Telethon-level errors (privacy, flood) return `200` with `ok: false`, not HTTP errors

---

### GET /health

Check server liveness and Telegram session status.

**Authentication:** `X-API-Key` header (required)

**Success Response (200):**

```json
{
  "status": "ok",
  "authorized": true,
  "account": "@username"
}
```

If the Telethon session is disconnected or not authorized:

```json
{
  "status": "ok",
  "authorized": false,
  "account": null
}
```

---

### POST /auth/send-code

Step 1 of phone-based session authorization. Sends a Telegram login code to the given phone number.

**Authentication:** `X-API-Key` header (required)

**Request Body:**

```json
{
  "phone": "+79991234567"
}
```

**Success Response (200):**

```json
{
  "ok": true,
  "phone_code_hash": "abc123..."
}
```

Save `phone_code_hash` — it is required for the next step.

**Error Response (200):**

```json
{
  "ok": false,
  "error": "..."
}
```

---

### POST /auth/verify

Step 2 of phone-based session authorization. Verifies the received code and saves the session.

**Authentication:** `X-API-Key` header (required)

**Request Body:**

```json
{
  "phone": "+79991234567",
  "code": "12345",
  "phone_code_hash": "abc123...",
  "password": "optional-2fa-password"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `phone` | `string` | Yes | Phone number used in `/auth/send-code` |
| `code` | `string` | Yes | Code received in Telegram app or SMS |
| `phone_code_hash` | `string` | Yes | Hash returned by `/auth/send-code` |
| `password` | `string` | No | 2FA password, if the account has it enabled |

**Success Response (200):**

```json
{
  "ok": true,
  "account": "@username"
}
```

**Error Response (200):**

```json
{
  "ok": false,
  "error": "2FA password required"
}
```

---

### POST /auth/qr

Start a QR-based session authorization. Returns a login URL to display as a QR code.

**Authentication:** `X-API-Key` header (required)

**Success Response (200):**

```json
{
  "ok": true,
  "url": "tg://login?token=..."
}
```

Convert the `url` to a QR code image (e.g. using `qrcode` library) and scan it with the Telegram mobile app. Then call `/auth/qr/wait`.

---

### POST /auth/qr/wait

Wait up to 60 seconds for the QR code to be scanned. Call after `/auth/qr`.

**Authentication:** `X-API-Key` header (required)

**Success Response (200) — scanned, no 2FA:**

```json
{
  "ok": true,
  "account": "@username",
  "need_2fa": false
}
```

**Response when 2FA is required (200):**

```json
{
  "ok": false,
  "need_2fa": true,
  "error": "2FA password required"
}
```

Call `/auth/qr/password` next.

---

### POST /auth/qr/password

Submit the 2FA password after a successful QR scan.

**Authentication:** `X-API-Key` header (required)

**Request Body:**

```json
{
  "password": "my-2fa-password"
}
```

**Success Response (200):**

```json
{
  "ok": true,
  "account": "@username"
}
```

---

## Data Models

### Request Models

#### `SendRequest`

```python
class SendRequest(BaseModel):
    recipient: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
```

#### `SendCodeRequest`

```python
class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=7)
```

#### `VerifyCodeRequest`

```python
class VerifyCodeRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str | None = None
```

#### `QrPasswordRequest`

```python
class QrPasswordRequest(BaseModel):
    password: str
```

### Response Models

#### `SendResponse`

```python
class SendResponse(BaseModel):
    ok: bool
    message_id: int | None = None
    error: str | None = None
```

#### `HealthResponse`

```python
class HealthResponse(BaseModel):
    status: str
    authorized: bool
    account: str | None = None
```

#### `SendCodeResponse`

```python
class SendCodeResponse(BaseModel):
    ok: bool
    phone_code_hash: str | None = None
    error: str | None = None
```

#### `VerifyCodeResponse`

```python
class VerifyCodeResponse(BaseModel):
    ok: bool
    account: str | None = None
    error: str | None = None
```

#### `QrResponse`

```python
class QrResponse(BaseModel):
    ok: bool
    url: str | None = None
    error: str | None = None
```

#### `QrWaitResponse`

```python
class QrWaitResponse(BaseModel):
    ok: bool
    account: str | None = None
    error: str | None = None
    need_2fa: bool = False
```

---

## Database Schema

SQLite database at `data/send_log.db` (configurable via `DB_PATH`).

### Table: `send_log`

```sql
CREATE TABLE IF NOT EXISTS send_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient           TEXT NOT NULL,
    message             TEXT NOT NULL,
    telegram_message_id INTEGER,
    status              TEXT NOT NULL,
    error               TEXT,
    sent_at             TEXT NOT NULL
)
```

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` | Auto-incrementing primary key |
| `recipient` | `TEXT` | Recipient identifier (username or user_id) |
| `message` | `TEXT` | Sent message text |
| `telegram_message_id` | `INTEGER` | Telegram's message ID (null on error) |
| `status` | `TEXT` | `"success"` or `"error"` |
| `error` | `TEXT` | Error description (null on success) |
| `sent_at` | `TEXT` | ISO 8601 UTC timestamp |

Quota enforcement queries this table: counts rows where `status='success'` and `sent_at >= today midnight UTC`.

---

## Configuration Reference

All configuration is loaded from environment variables / `.env` file.

See `.env.example` for a template:

```env
# Telegram API (get from https://my.telegram.org)
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION_NAME=evgenia

# Server
HOST=0.0.0.0
PORT=8000
API_KEY=change-me-to-a-secret-key

# Rate limits
MAX_MESSAGES_PER_DAY=25
MIN_DELAY_SECONDS=30
MAX_DELAY_SECONDS=90

# Database
DB_PATH=data/send_log.db
```

---

## Deployment

### Docker

**Build and run:**

```bash
docker compose up -d
```

**Prerequisites:**

1. Create `.env` file from `.env.example` with real credentials
2. Authorize Telethon session using one of two methods:

   **Option A — CLI script (recommended for first setup):**
   ```bash
   python -m app.auth_session
   ```
   Creates `<TELEGRAM_SESSION_NAME>.session` locally, then copy it to the server.

   **Option B — HTTP API (useful when server is already running but session expired):**
   ```bash
   # Step 1: send code
   curl -X POST http://host:8000/auth/send-code \
     -H "X-API-Key: <key>" \
     -H "Content-Type: application/json" \
     -d '{"phone": "+79991234567"}'

   # Step 2: verify code
   curl -X POST http://host:8000/auth/verify \
     -H "X-API-Key: <key>" \
     -H "Content-Type: application/json" \
     -d '{"phone": "+79991234567", "code": "12345", "phone_code_hash": "<hash from step 1>"}'
   ```

**docker-compose.yml** mounts:

- `./data:/app/data` — SQLite database persistence
- `./${TELEGRAM_SESSION_NAME}.session:/app/${TELEGRAM_SESSION_NAME}.session` — Telethon session file

The session file on the host must exist as a **file** (not a directory) before the container starts. If Docker created a directory at that path from a previous failed run, remove it first: `rm -rf <name>.session`.

**Health check:** polls `GET /health` every 30 seconds.

### Dockerfile

- Base image: `python:3.11-slim`
- Exposes port `8000`
- Runs: `uvicorn app.main:app --host 0.0.0.0 --port 8000`

---

## Testing

### Setup

Tests use `pytest` with `pytest-asyncio` (auto mode) and `httpx.AsyncClient` for HTTP testing.

```bash
pip install -r requirements.txt
pytest
```

### Test Configuration

`pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
```

### Test Structure

```
tests/
├── conftest.py          # Shared fixtures
├── test_send.py         # POST /send endpoint tests
├── test_health.py       # GET /health endpoint tests
└── test_rate_limiter.py # Rate limiter unit tests
```

### Key Fixtures (`conftest.py`)

| Fixture | Scope | Description |
|---------|-------|-------------|
| `test_settings` | function | `Settings` with zero delays, 5 msg/day quota, temp DB |
| `mock_send_message` | function | `AsyncMock(return_value=42)` replacing Telethon send |
| `db` | function | Initializes and tears down test SQLite database |
| `worker` | function | Starts and stops the rate limiter worker |
| `client` | function | Full `AsyncClient` with mocked Telethon and test settings |

The `client` fixture patches `telethon_client` module functions with mocks, overrides FastAPI dependencies, and manually initializes the database and worker (since `ASGITransport` doesn't run lifespan).

### Test Coverage

**`test_send.py`** (6 tests):
- Successful send → returns `{ok: true, message_id: 42}`
- Invalid API key → 401
- Empty message → 422 validation error
- Quota exhaustion (5 messages then rejection) → 429
- Telethon error (privacy) → `{ok: false, error: "..."}`
- Numeric recipient → converted to `int` for Telethon

**`test_health.py`** (4 tests):
- Valid key → returns authorized account info
- Invalid key → 401
- Missing key → 401/403
- Not authorized session → `{authorized: false}`

**`test_rate_limiter.py`** (5 tests):
- DB schema creation
- Log send records (counts only successful)
- Today-only count (old records excluded)
- Quota availability check
- Worker processes queue items and handles errors

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | `>=0.115,<1` | HTTP framework |
| `uvicorn[standard]` | `>=0.34,<1` | ASGI server |
| `telethon` | `>=1.37,<2` | Telegram MTProto client |
| `pydantic-settings` | `>=2.7,<3` | Environment config loading |
| `aiosqlite` | `>=0.21,<1` | Async SQLite |
| `python-dotenv` | `>=1.0,<2` | `.env` file loading |
| `pytest` | `>=8.0,<9` | Test framework (dev) |
| `pytest-asyncio` | `>=0.25,<1` | Async test support (dev) |
| `httpx` | `>=0.28,<1` | Async HTTP test client (dev) |
