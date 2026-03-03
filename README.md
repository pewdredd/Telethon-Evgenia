# Telethon-Evgenia

HTTP-сервер на базе Telethon для автоматической отправки первых сообщений лидам в Telegram. Получает квалифицированных лидов от n8n и пишет им от имени реального аккаунта через MTProto.

## Зачем юзербот, а не обычный бот

Обычный бот не может написать первым — пользователь должен сам нажать /start. Юзербот (Telethon) выглядит как обычный человек и может инициировать переписку с любым пользователем.

## Требования

- Python 3.11+
- Telegram API credentials с [my.telegram.org](https://my.telegram.org)
- Docker (для деплоя)

## Быстрый старт

**1. Установить зависимости:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Создать `.env`:**

```bash
cp .env.example .env
# Заполнить TELEGRAM_API_ID, TELEGRAM_API_HASH, API_KEY
```

**3. Авторизовать Telegram-аккаунт (один раз):**

```bash
python -m app.auth_session
```

Введёт номер телефона, код из Telegram и (если нужно) пароль 2FA. Создаёт файл `<TELEGRAM_SESSION_NAME>.session`.

**4. Запустить сервер:**

```bash
uvicorn app.main:app
```

## Docker

```bash
# Сначала авторизовать сессию локально
python -m app.auth_session

# Запустить контейнер
docker compose up -d
```

`docker-compose.yml` монтирует файл сессии и папку `data/` (SQLite) как volumes.

## API

Все эндпоинты требуют заголовок `X-API-Key`.

### POST /send — отправить сообщение

```bash
curl -X POST http://localhost:8000/send \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"recipient": "@username", "message": "Привет!"}'
```

Запрос блокируется на 30–90 секунд (случайная задержка для защиты от бана), затем возвращает результат.

```json
{"ok": true, "message_id": 12345}
```

`recipient` — `@username` или числовой Telegram ID в виде строки.

### GET /health — статус сервера

```bash
curl http://localhost:8000/health -H "X-API-Key: your-api-key"
```

```json
{"status": "ok", "authorized": true, "account": "@username"}
```

### Авторизация через HTTP API

Если сессия истекла и нужно переавторизоваться без доступа к серверу напрямую:

**Через код из Telegram:**

```bash
# Шаг 1 — запросить код
curl -X POST http://localhost:8000/auth/send-code \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"phone": "+79991234567"}'
# → {"ok": true, "phone_code_hash": "abc123..."}

# Шаг 2 — подтвердить код
curl -X POST http://localhost:8000/auth/verify \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"phone": "+79991234567", "code": "12345", "phone_code_hash": "abc123..."}'
# → {"ok": true, "account": "@username"}
```

**Через QR-код:**

```bash
# Получить ссылку для QR
curl -X POST http://localhost:8000/auth/qr -H "X-API-Key: your-api-key"
# → {"ok": true, "url": "tg://login?token=..."}

# Ждать сканирования (до 60 секунд)
curl -X POST http://localhost:8000/auth/qr/wait -H "X-API-Key: your-api-key"
# → {"ok": true, "account": "@username"}
# Если need_2fa: true — отправить пароль на /auth/qr/password
```

## Конфигурация

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `TELEGRAM_API_ID` | — | API ID с my.telegram.org |
| `TELEGRAM_API_HASH` | — | API Hash с my.telegram.org |
| `TELEGRAM_SESSION_NAME` | `evgenia` | Имя файла сессии |
| `HOST` | `0.0.0.0` | Адрес сервера |
| `PORT` | `8000` | Порт сервера |
| `API_KEY` | `change-me` | Ключ для защиты эндпоинтов |
| `MAX_MESSAGES_PER_DAY` | `25` | Лимит сообщений в день |
| `MIN_DELAY_SECONDS` | `30` | Минимальная задержка между отправками |
| `MAX_DELAY_SECONDS` | `90` | Максимальная задержка между отправками |
| `DB_PATH` | `data/send_log.db` | Путь к SQLite базе |

## Защита от бана

- Сообщения ставятся в очередь и отправляются по одному
- Случайная задержка 30–90 секунд между отправками
- Жёсткий суточный лимит (по умолчанию 25) — после достижения сервер возвращает HTTP 429 до следующего дня по UTC
- Все отправки логируются в SQLite (`data/send_log.db`)

## Тесты

```bash
pytest
```
