# Telethon-Evgenia

Мульти-аккаунтный HTTP-сервер на базе Telethon для автоматической отправки первых сообщений лидам в Telegram. Получает квалифицированных лидов от n8n и пишет им от имени реальных аккаунтов через MTProto. Поддерживает любое количество аккаунтов одновременно.

## Зачем юзербот, а не обычный бот

Обычный бот не может написать первым — пользователь должен сам нажать /start. Юзербот (Telethon) выглядит как обычный человек и может инициировать переписку с любым пользователем.

## Требования

- Python 3.11+
- Telegram API credentials с [my.telegram.org](https://my.telegram.org) (для каждого аккаунта свои)
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
# Заполнить API_KEY (единый ключ для защиты всех эндпоинтов)
```

**3. Запустить сервер:**

```bash
uvicorn app.main:app
```

**4. Подключить аккаунт** — см. раздел ниже.

## Подключение аккаунтов

Каждый клиент получает свой аккаунт с собственными `api_id`/`api_hash` с [my.telegram.org](https://my.telegram.org). Аккаунты можно добавлять/удалять на горячую, без перезапуска сервера.

### Шаг 1 — Создать аккаунт

```bash
curl -X POST http://localhost:8000/accounts \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "client-1",
    "api_id": 12345,
    "api_hash": "0123456789abcdef...",
    "phone": "+79991234567"
  }'
```

Можно сразу задать индивидуальные лимиты:

```json
{
  "account_id": "client-1",
  "api_id": 12345,
  "api_hash": "0123456789abcdef...",
  "max_messages_per_day": 15,
  "min_delay_seconds": 60,
  "max_delay_seconds": 120
}
```

Если лимиты не указаны — берутся из `.env` (по умолчанию 25/30/90).

По умолчанию каждый аккаунт слушает входящие от своих лидов и пересылает их в `INCOMING_WEBHOOK_URL`. Если клиент хочет отвечать вручную (без n8n-обработки), можно отключить пересылку прямо при создании:

```json
{
  "account_id": "client-1",
  "api_id": 12345,
  "api_hash": "...",
  "forward_incoming": false
}
```

Статус нового аккаунта: `pending`. Он появится в системе, но отправлять сообщения не сможет, пока не пройдёт авторизацию.

### Шаг 2 — Авторизовать аккаунт

Три варианта на выбор:

#### Вариант A: через код из Telegram (HTTP API)

```bash
# 1. Запросить код
curl -X POST http://localhost:8000/accounts/client-1/auth/send-code \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"phone": "+79991234567"}'
# → {"ok": true, "phone_code_hash": "abc123..."}

# 2. Подтвердить код (+ password если есть 2FA)
curl -X POST http://localhost:8000/accounts/client-1/auth/verify \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "phone": "+79991234567",
    "code": "12345",
    "phone_code_hash": "abc123..."
  }'
# → {"ok": true, "account": "@username"}
```

#### Вариант B: через QR-код (HTTP API)

```bash
# 1. Получить ссылку
curl -X POST http://localhost:8000/accounts/client-1/auth/qr \
  -H "X-API-Key: your-api-key"
# → {"ok": true, "url": "tg://login?token=..."}
# Показать url как QR-код, отсканировать в Telegram

# 2. Ждать сканирования (до 60 секунд)
curl -X POST http://localhost:8000/accounts/client-1/auth/qr/wait \
  -H "X-API-Key: your-api-key"
# → {"ok": true, "account": "@username"}

# Если вернулось need_2fa: true — отправить пароль:
curl -X POST http://localhost:8000/accounts/client-1/auth/qr/password \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"password": "my-2fa-password"}'
```

#### Вариант C: через CLI (для локальной настройки)

```bash
python -m app.auth_session --account-id client-1
```

Интерактивно запросит номер телефона, код и пароль 2FA. Удобно при первой настройке.

### Шаг 3 — Проверить

```bash
# Статус конкретного аккаунта
curl http://localhost:8000/accounts/client-1/health \
  -H "X-API-Key: your-api-key"
# → {"status": "ok", "authorized": true, "account": "@username"}

# Статус всех аккаунтов
curl http://localhost:8000/health -H "X-API-Key: your-api-key"
```

После авторизации статус меняется на `authorized`, автоматически запускается воркер отправки и слушатель входящих.

## Управление аккаунтами

```bash
# Список всех аккаунтов
curl http://localhost:8000/accounts -H "X-API-Key: your-api-key"

# Детали аккаунта
curl http://localhost:8000/accounts/client-1 -H "X-API-Key: your-api-key"

# Изменить лимиты
curl -X PATCH http://localhost:8000/accounts/client-1 \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"max_messages_per_day": 50}'

# Отключить пересылку входящих (клиент сам отвечает вручную)
curl -X PATCH http://localhost:8000/accounts/client-1 \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"forward_incoming": false}'
# Эффект мгновенный — listener снимается с живого клиента, рестарт не нужен.
# Включить обратно: {"forward_incoming": true}

# Удалить аккаунт (останавливает клиент и удаляет из БД)
curl -X DELETE http://localhost:8000/accounts/client-1 \
  -H "X-API-Key: your-api-key"
```

## API

Все эндпоинты требуют заголовок `X-API-Key`.

### POST /accounts/{id}/send — отправить сообщение

```bash
curl -X POST http://localhost:8000/accounts/client-1/send \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"recipient": "@username", "message": "Привет!"}'
```

Запрос блокируется на 30–90 секунд (случайная задержка для защиты от бана), затем возвращает результат.

```json
{"ok": true, "message_id": 12345}
```

`recipient` — `@username` или числовой Telegram ID в виде строки.

### GET /accounts/{id}/incoming — входящие от лидов

```bash
curl "http://localhost:8000/accounts/client-1/incoming?limit=10&unprocessed_only=true" \
  -H "X-API-Key: your-api-key"
```

```json
{
  "ok": true,
  "messages": [
    {
      "id": 1,
      "account_id": "client-1",
      "sender_id": "123456",
      "sender_username": "lead_user",
      "message_text": "Привет, интересно!",
      "telegram_message_id": 789,
      "chat_id": 123456,
      "received_at": "2026-03-04T12:00:00+00:00",
      "processed": false
    }
  ],
  "count": 1
}
```

Listener автоматически перехватывает ответы от лидов, которым уже писали (по `send_log`). Если задан `INCOMING_WEBHOOK_URL` — входящие также отправляются POST-запросом на указанный URL (для n8n).

Listener можно отключить per-account флагом `forward_incoming = false` (см. раздел «Управление аккаунтами»). В этом случае сервер не подписывается на `NewMessage` для аккаунта, ничего не пишет в `incoming_log` и не дёргает webhook — клиент видит входящие только в своём Telegram и отвечает сам.

### GET /incoming — входящие по всем аккаунтам

```bash
curl "http://localhost:8000/incoming?limit=50" -H "X-API-Key: your-api-key"
```

### GET /health — статус сервера

```bash
curl http://localhost:8000/health -H "X-API-Key: your-api-key"
```

```json
{
  "status": "ok",
  "accounts": [
    {"account_id": "client-1", "status": "authorized", "username": "user1", "today_sent": 5},
    {"account_id": "client-2", "status": "pending", "username": null, "today_sent": 0}
  ]
}
```

## Docker

```bash
docker compose up -d
```

`docker-compose.yml` монтирует `./data:/app/data` — в этой папке хранятся SQLite база и файлы сессий (`data/sessions/`).

## Конфигурация

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `API_KEY` | `change-me` | Ключ для защиты эндпоинтов |
| `HOST` | `0.0.0.0` | Адрес сервера |
| `PORT` | `8000` | Порт сервера |
| `MAX_MESSAGES_PER_DAY` | `25` | Лимит по умолчанию для новых аккаунтов |
| `MIN_DELAY_SECONDS` | `30` | Мин. задержка по умолчанию |
| `MAX_DELAY_SECONDS` | `90` | Макс. задержка по умолчанию |
| `DB_PATH` | `data/send_log.db` | Путь к SQLite базе |
| `SESSIONS_DIR` | `data/sessions` | Папка для файлов сессий Telethon |
| `INCOMING_WEBHOOK_URL` | _(пусто)_ | URL для пересылки входящих (n8n webhook) |

Telegram API credentials (`api_id`, `api_hash`) теперь задаются при создании каждого аккаунта через API, а не в `.env`.

## Защита от бана

- Каждый аккаунт имеет свою очередь — сообщения отправляются по одному
- Случайная задержка 30–90 секунд между отправками (настраивается для каждого аккаунта)
- Жёсткий суточный лимит (по умолчанию 25) — после достижения сервер возвращает HTTP 429 до следующего дня по UTC
- Все отправки логируются в SQLite (`data/send_log.db`)
- Listener перехватывает ответы только от лидов из `send_log`

## Telegram-бот для клиентов

Помимо HTTP API, в проект встроен Telegram-бот (aiogram 3) для самостоятельной регистрации клиентов. Клиент общается с ботом напрямую — вводит `api_id`, `api_hash`, телефон и код подтверждения.

### Настройка

В `.env` добавить:

```env
BOT_TOKEN=123456:ABC-...        # токен бота от @BotFather
BOT_ADMINS=123456789,987654321  # Telegram ID администраторов (через запятую)
```

Бот запускается автоматически вместе с сервером (если `BOT_TOKEN` задан).

### Команды для клиентов

| Команда | Описание |
|---------|----------|
| `/start` | Начать регистрацию (или показать статус + кнопка перерегистрации) |
| `/status` | Статус аккаунта: авторизация, телефон, отправлено сегодня/лимит, всего |
| `/cancel` | Отменить регистрацию |
| `/back` | Вернуться на предыдущий шаг |

### Команды для администраторов

| Команда | Описание |
|---------|----------|
| `/add_user <telegram_id>` | Добавить пользователя в whitelist |
| `/remove_user <telegram_id>` | Удалить из whitelist |
| `/users` | Список пользователей |
| `/accounts` | Список всех аккаунтов |

Доступ к боту ограничен whitelist-ом — сначала админ добавляет пользователя через `/add_user`.

## Тесты

```bash
pytest
```
