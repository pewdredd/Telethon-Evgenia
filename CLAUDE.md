# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telethon-Evgenia is a multi-account Telegram user-bot HTTP server for automated lead outreach. It receives qualified leads via HTTP from an n8n orchestrator and sends personalized first-contact messages through real Telegram accounts using Telethon (MTProto). Multiple accounts can run simultaneously in one server instance.

The full specification lives in `ai_docs/project-core.md` (in Russian).

## Architecture

```
n8n (external orchestrator)
  → HTTP POST /accounts/{account_id}/send
    → FastAPI server (this project)
      → AccountManager (per-account state)
        → Rate limiter (queue + daily quota + random delays)
          → Telethon client (MTProto user-bot)
            → Telegram
```

Key design constraint: this uses **user-bots** (Telethon), not the Bot API, because the use case requires initiating conversations with users who haven't interacted first. This brings ban risk, so rate limiting and delay randomization are critical.

## Tech Stack

- Python 3.11+, FastAPI, Telethon, uvicorn
- SQLite via aiosqlite (single DB file: `data/send_log.db`)
- Configuration via `.env` (see `.env.example`)
- Deployment via Docker

## Project Structure

```
app/
├── main.py              # FastAPI app, lifespan, route definitions
├── account_manager.py   # Central per-account state (clients, queues, workers, DB ops)
├── telethon_client.py   # Pure factory functions for Telethon (no module-level state)
├── listener.py          # Incoming message listener (parameterized, no globals)
├── config.py            # .env loading via Pydantic settings
├── auth.py              # API key verification (X-API-Key header)
└── auth_session.py      # CLI script for interactive session authorization
```

## Database

Single SQLite file with three tables:
- `accounts` — registered Telegram accounts with credentials, status, rate limits
- `send_log` — all outgoing message attempts (scoped by `account_id`)
- `incoming_log` — incoming messages from known leads (scoped by `account_id`)

Session files stored in `data/sessions/`.

## API Endpoints

### Account Management
- **POST /accounts** — create account (api_id, api_hash, rate limits)
- **GET /accounts** — list all accounts + statuses + today_sent count
- **GET /accounts/{account_id}** — single account details
- **PATCH /accounts/{account_id}** — update rate limits
- **DELETE /accounts/{account_id}** — stop + delete account

### Per-Account Operations
- **POST /accounts/{account_id}/send** — enqueue message
- **GET /accounts/{account_id}/health** — auth status of this account
- **GET /accounts/{account_id}/incoming** — incoming messages for this account
- **POST /accounts/{account_id}/incoming/{id}/processed** — mark as processed
- **POST /accounts/{account_id}/auth/send-code** — send login code
- **POST /accounts/{account_id}/auth/verify** — verify code
- **POST /accounts/{account_id}/auth/qr** — generate QR login URL
- **POST /accounts/{account_id}/auth/qr/wait** — wait for QR scan
- **POST /accounts/{account_id}/auth/qr/password** — submit 2FA after QR

### Aggregated
- **GET /health** — server status + all accounts summary
- **GET /incoming** — incoming across all accounts

All endpoints protected by single `X-API-Key`.

## Rate Limiting Defaults

- `MAX_MESSAGES_PER_DAY = 25`
- `MIN_DELAY_SECONDS = 30`, `MAX_DELAY_SECONDS = 90` (random delay between sends)
- Hard daily cutoff — server refuses sends after quota is reached until next day
- Each account has independent rate limits (configurable at creation or via PATCH)

## Code Conventions

Defined in `.claude/fastapi/SKILL.md`. Key points:

- Functional style over classes where possible
- `async def` for I/O-bound operations, `def` for pure functions
- Type hints on all function signatures; Pydantic models for request/response validation
- Guard clauses and early returns for error handling
- Use FastAPI lifespan context managers (not `@app.on_event`)
- Use FastAPI dependency injection for shared state
- File/directory naming: lowercase with underscores

## Language

The project specification (`ai_docs/project-core.md`) is written in Russian. Code, comments, and API responses should be in English.
