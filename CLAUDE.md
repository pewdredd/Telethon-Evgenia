# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telethon-Evgenia is a Telegram user-bot HTTP server for automated lead outreach. It receives qualified leads via HTTP from an n8n orchestrator and sends personalized first-contact messages through a real Telegram account using Telethon (MTProto).

The full specification lives in `ai_docs/project-core.md` (in Russian).

## Architecture

```
n8n (external orchestrator)
  → HTTP POST /send
    → FastAPI server (this project)
      → Rate limiter (queue + daily quota + random delays)
        → Telethon client (MTProto user-bot)
          → Telegram
```

Key design constraint: this uses a **user-bot** (Telethon), not the Bot API, because the use case requires initiating conversations with users who haven't interacted first. This brings ban risk, so rate limiting and delay randomization are critical.

## Tech Stack

- Python 3.11+, FastAPI, Telethon, uvicorn
- Configuration via `.env` (see `ai_docs/project-core.md` for all env vars)
- Deployment via Docker

## Planned Project Structure

```
app/
├── main.py              # FastAPI app, lifespan, route definitions
├── telethon_client.py   # Telethon session management & message sending
├── rate_limiter.py      # Daily quota, message queue, random delays (30-90s)
├── listener.py          # Incoming message listener (lead replies)
├── config.py            # .env loading via Pydantic settings
└── auth.py              # API key verification (protects endpoints from unauthorized access)
```

## API Endpoints

- **POST /send** — send message to a recipient (by `@username` or numeric `user_id`). Returns `{ ok, message_id }` or `{ ok: false, error }`.
- **GET /health** — server liveness + Telethon session status.
- **GET /incoming** — retrieve logged incoming messages from known leads. Query params: `limit`, `unprocessed_only`.

All endpoints are protected by API key auth.

## Rate Limiting Defaults

- `MAX_MESSAGES_PER_DAY = 25`
- `MIN_DELAY_SECONDS = 30`, `MAX_DELAY_SECONDS = 90` (random delay between sends)
- Hard daily cutoff — server refuses sends after quota is reached until next day

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
