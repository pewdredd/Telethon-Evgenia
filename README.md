# Telethon-Evgenia

Telegram user-bot HTTP server for automated lead outreach. Receives qualified leads via HTTP and sends personalized first-contact messages through a real Telegram account using Telethon (MTProto).

## Prerequisites

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)

## Setup

1. Clone and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create `.env` from template:

```bash
cp .env.example .env
# Edit .env with your Telegram API credentials and API key
```

3. Authorize your Telegram account (one-time):

```bash
python -m app.auth_session
```

4. Start the server:

```bash
uvicorn app.main:app
```

## API Reference

All endpoints require the `X-API-Key` header.

### POST /send

Send a message to a Telegram user.

```bash
curl -X POST http://localhost:8000/send \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"recipient": "@username", "message": "Hello!"}'
```

Response:

```json
{"ok": true, "message_id": 12345}
```

The request blocks for 30-90 seconds (random delay for ban protection) before returning the result.

### GET /health

```bash
curl http://localhost:8000/health -H "X-API-Key: your-api-key"
```

Response:

```json
{"status": "ok", "authorized": true, "account": "@username"}
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_API_ID` | — | Telegram API ID |
| `TELEGRAM_API_HASH` | — | Telegram API hash |
| `TELEGRAM_SESSION_NAME` | `evgenia` | Session file name |
| `HOST` | `0.0.0.0` | Server host |
| `PORT` | `8000` | Server port |
| `API_KEY` | `change-me` | API key for endpoint auth |
| `MAX_MESSAGES_PER_DAY` | `25` | Daily message quota |
| `MIN_DELAY_SECONDS` | `30` | Min delay between sends |
| `MAX_DELAY_SECONDS` | `90` | Max delay between sends |
| `DB_PATH` | `data/send_log.db` | SQLite database path |

## Rate Limiting

Messages are queued and sent one at a time with a random delay (30-90s by default). A hard daily quota (25 by default) prevents over-sending. Once reached, the server returns HTTP 429 until the next UTC day.

## Docker

```bash
# Authorize session first (outside Docker)
python -m app.auth_session

# Build and run
docker compose up --build
```

## Tests

```bash
pip install -r requirements.txt
pytest
```
