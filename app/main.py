"""FastAPI application entry point.

Defines the app, lifespan (startup/shutdown), Pydantic request/response
models, and HTTP endpoints: ``POST /send``, ``GET /health``,
``POST /auth/send-code``, ``POST /auth/verify``.
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import listener, rate_limiter, telethon_client
from app.auth import verify_api_key
from app.config import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    await rate_limiter.init_db(settings.db_path)
    await telethon_client.start_client(settings)
    rate_limiter.start_worker(telethon_client.send_message, settings)
    await listener.init_db(settings.db_path)
    await listener.start_listener(telethon_client.get_client(), settings)
    yield
    await listener.stop_listener()
    await listener.close_db()
    await rate_limiter.stop_worker()
    await telethon_client.stop_client()
    await rate_limiter.close_db()


app = FastAPI(title="Telethon-Evgenia", lifespan=lifespan)


# --- Models ---

class SendRequest(BaseModel):
    recipient: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class SendResponse(BaseModel):
    ok: bool
    message_id: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    authorized: bool
    account: str | None = None


class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=7)


class SendCodeResponse(BaseModel):
    ok: bool
    phone_code_hash: str | None = None
    error: str | None = None


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str | None = None


class VerifyCodeResponse(BaseModel):
    ok: bool
    account: str | None = None
    error: str | None = None


class IncomingMessageResponse(BaseModel):
    id: int
    sender_id: str
    sender_username: str | None = None
    message_text: str
    telegram_message_id: int
    chat_id: int
    received_at: str
    processed: bool


class IncomingListResponse(BaseModel):
    ok: bool
    messages: list[IncomingMessageResponse]
    count: int


# --- Routes ---

@app.post("/send", response_model=SendResponse)
async def post_send(
    body: SendRequest,
    settings: Settings = Depends(get_settings),
    _api_key: str = Depends(verify_api_key),
) -> SendResponse:
    if not await rate_limiter.is_quota_available(settings.max_messages_per_day):
        raise HTTPException(status_code=429, detail="Daily message quota exhausted")

    recipient: str | int = body.recipient
    if body.recipient.isdigit():
        recipient = int(body.recipient)

    future = await rate_limiter.enqueue_message(recipient, body.message)
    try:
        message_id = await future
        return SendResponse(ok=True, message_id=message_id)
    except Exception as exc:
        return SendResponse(ok=False, error=str(exc))


@app.get("/health", response_model=HealthResponse)
async def get_health(
    _api_key: str = Depends(verify_api_key),
) -> HealthResponse:
    me = await telethon_client.get_me()
    if me is None:
        return HealthResponse(status="ok", authorized=False)
    username = f"@{me['username']}" if me.get("username") else str(me["id"])
    return HealthResponse(status="ok", authorized=True, account=username)


@app.get("/incoming", response_model=IncomingListResponse)
async def get_incoming(
    limit: int = 50,
    unprocessed_only: bool = False,
    _api_key: str = Depends(verify_api_key),
) -> IncomingListResponse:
    messages = await listener.get_recent_incoming(limit=limit, unprocessed_only=unprocessed_only)
    return IncomingListResponse(ok=True, messages=messages, count=len(messages))


@app.post("/incoming/{incoming_id}/processed")
async def post_mark_processed(
    incoming_id: int,
    _api_key: str = Depends(verify_api_key),
) -> dict:
    try:
        await listener.mark_processed(incoming_id)
        return {"ok": True}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/auth/send-code", response_model=SendCodeResponse)
async def post_auth_send_code(
    body: SendCodeRequest,
    _api_key: str = Depends(verify_api_key),
) -> SendCodeResponse:
    """Step 1: send a Telegram login code to the given phone number."""
    try:
        phone_code_hash = await telethon_client.send_code(body.phone)
        return SendCodeResponse(ok=True, phone_code_hash=phone_code_hash)
    except Exception as exc:
        return SendCodeResponse(ok=False, error=str(exc))


@app.post("/auth/verify", response_model=VerifyCodeResponse)
async def post_auth_verify(
    body: VerifyCodeRequest,
    _api_key: str = Depends(verify_api_key),
) -> VerifyCodeResponse:
    """Step 2: verify the code received from Telegram and save the session."""
    try:
        me = await telethon_client.verify_code(
            body.phone, body.code, body.phone_code_hash, body.password
        )
        account = f"@{me['username']}" if me.get("username") else str(me["id"])
        return VerifyCodeResponse(ok=True, account=account)
    except Exception as exc:
        return VerifyCodeResponse(ok=False, error=str(exc))


class QrResponse(BaseModel):
    ok: bool
    url: str | None = None
    error: str | None = None


class QrWaitResponse(BaseModel):
    ok: bool
    account: str | None = None
    error: str | None = None
    need_2fa: bool = False


class QrPasswordRequest(BaseModel):
    password: str


@app.post("/auth/qr", response_model=QrResponse)
async def post_auth_qr(
    _api_key: str = Depends(verify_api_key),
) -> QrResponse:
    """Generate a QR login URL. Convert to QR code and scan with Telegram app."""
    try:
        url = await telethon_client.qr_login_start()
        return QrResponse(ok=True, url=url)
    except Exception as exc:
        return QrResponse(ok=False, error=str(exc))


@app.post("/auth/qr/wait", response_model=QrWaitResponse)
async def post_auth_qr_wait(
    _api_key: str = Depends(verify_api_key),
) -> QrWaitResponse:
    """Wait up to 60s for the QR to be scanned. Call after /auth/qr."""
    try:
        me = await telethon_client.qr_login_wait()
        account = f"@{me['username']}" if me.get("username") else str(me["id"])
        return QrWaitResponse(ok=True, account=account)
    except RuntimeError as exc:
        if "2FA_REQUIRED" in str(exc):
            return QrWaitResponse(ok=False, need_2fa=True, error="2FA password required")
        return QrWaitResponse(ok=False, error=str(exc))


@app.post("/auth/qr/password", response_model=QrWaitResponse)
async def post_auth_qr_password(
    body: QrPasswordRequest,
    _api_key: str = Depends(verify_api_key),
) -> QrWaitResponse:
    """Submit 2FA password after QR scan if need_2fa was returned."""
    try:
        me = await telethon_client.qr_login_2fa(body.password)
        account = f"@{me['username']}" if me.get("username") else str(me["id"])
        return QrWaitResponse(ok=True, account=account)
    except Exception as exc:
        return QrWaitResponse(ok=False, error=str(exc))
