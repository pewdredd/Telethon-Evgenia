"""FastAPI application entry point.

Multi-account Telegram user-bot HTTP server. All operations are scoped
by account_id. Account management, auth, sending, and incoming message
retrieval are all handled through per-account endpoints.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import telethon_client
from app.account_manager import AccountManager
from app.auth import verify_api_key
from app.config import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    manager = AccountManager(settings)
    await manager.init_db()
    await manager.load_all()
    app.state.manager = manager
    yield
    await manager.shutdown_all()


app = FastAPI(title="Telethon-Evgenia", lifespan=lifespan)


def get_manager() -> AccountManager:
    return app.state.manager


# --- Models ---

class CreateAccountRequest(BaseModel):
    account_id: str = Field(..., min_length=1)
    api_id: int
    api_hash: str = Field(..., min_length=1)
    phone: str | None = None
    max_messages_per_day: int | None = None
    min_delay_seconds: int | None = None
    max_delay_seconds: int | None = None
    forward_incoming: bool | None = None


class UpdateAccountRequest(BaseModel):
    max_messages_per_day: int | None = None
    min_delay_seconds: int | None = None
    max_delay_seconds: int | None = None
    forward_incoming: bool | None = None


class SendRequest(BaseModel):
    recipient: str | int
    message: str = Field(..., min_length=1)


class SendResponse(BaseModel):
    ok: bool
    message_id: int | None = None
    user_id: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    accounts: list[dict] | None = None


class AccountHealthResponse(BaseModel):
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
    account_id: str
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


# --- Account management routes ---

@app.post("/accounts")
async def create_account(
    body: CreateAccountRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    try:
        result = await manager.add_account(
            account_id=body.account_id,
            api_id=body.api_id,
            api_hash=body.api_hash,
            phone=body.phone,
            max_messages_per_day=body.max_messages_per_day,
            min_delay_seconds=body.min_delay_seconds,
            max_delay_seconds=body.max_delay_seconds,
            forward_incoming=body.forward_incoming,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/accounts")
async def list_accounts(
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    accounts = await manager.list_accounts()
    return {"ok": True, "accounts": accounts, "count": len(accounts)}


@app.get("/accounts/{account_id}")
async def get_account(
    account_id: str,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    try:
        info = await manager.get_account_info(account_id)
        return {"ok": True, **info}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")


@app.patch("/accounts/{account_id}")
async def update_account(
    account_id: str,
    body: UpdateAccountRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    try:
        info = await manager.update_account(
            account_id,
            max_messages_per_day=body.max_messages_per_day,
            min_delay_seconds=body.min_delay_seconds,
            max_delay_seconds=body.max_delay_seconds,
            forward_incoming=body.forward_incoming,
        )
        return {"ok": True, **info}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")


@app.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    try:
        await manager.get_account_info(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    await manager.remove_account(account_id)
    return {"ok": True}


# --- Per-account send ---

@app.post("/accounts/{account_id}/send", response_model=SendResponse)
async def post_send(
    account_id: str,
    body: SendRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> SendResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    if not await manager.is_quota_available(account_id, state.max_messages_per_day):
        raise HTTPException(status_code=429, detail="Daily message quota exhausted")

    recipient: str | int = body.recipient
    if isinstance(body.recipient, str) and body.recipient.isdigit():
        recipient = int(body.recipient)

    future = await manager.enqueue_message(account_id, recipient, body.message)
    try:
        message_id, user_id = await future
        return SendResponse(ok=True, message_id=message_id, user_id=user_id)
    except Exception as exc:
        return SendResponse(ok=False, error=str(exc))


# --- Per-account health ---

@app.get("/accounts/{account_id}/health", response_model=AccountHealthResponse)
async def get_account_health(
    account_id: str,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> AccountHealthResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    me = await telethon_client.get_me(state.client)
    if me is None:
        return AccountHealthResponse(status="ok", authorized=False)
    username = f"@{me['username']}" if me.get("username") else str(me["id"])
    return AccountHealthResponse(status="ok", authorized=True, account=username)


# --- Per-account incoming ---

@app.get("/accounts/{account_id}/incoming", response_model=IncomingListResponse)
async def get_account_incoming(
    account_id: str,
    limit: int = 50,
    unprocessed_only: bool = False,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> IncomingListResponse:
    try:
        manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    messages = await manager.get_recent_incoming(
        account_id=account_id, limit=limit, unprocessed_only=unprocessed_only
    )
    return IncomingListResponse(ok=True, messages=messages, count=len(messages))


@app.post("/accounts/{account_id}/incoming/{incoming_id}/processed")
async def post_mark_processed(
    account_id: str,
    incoming_id: int,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> dict:
    try:
        manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    await manager.mark_processed(incoming_id)
    return {"ok": True}


# --- Per-account auth ---

@app.post("/accounts/{account_id}/auth/send-code", response_model=SendCodeResponse)
async def post_auth_send_code(
    account_id: str,
    body: SendCodeRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> SendCodeResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    try:
        phone_code_hash = await telethon_client.send_code(state.client, body.phone)
        return SendCodeResponse(ok=True, phone_code_hash=phone_code_hash)
    except Exception as exc:
        return SendCodeResponse(ok=False, error=str(exc))


@app.post("/accounts/{account_id}/auth/verify", response_model=VerifyCodeResponse)
async def post_auth_verify(
    account_id: str,
    body: VerifyCodeRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> VerifyCodeResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    try:
        me = await telethon_client.verify_code(
            state.client, body.phone, body.code, body.phone_code_hash, body.password
        )
        account_str = f"@{me['username']}" if me.get("username") else str(me["id"])
        await manager.mark_authorized(account_id, me["id"], me.get("username"))
        return VerifyCodeResponse(ok=True, account=account_str)
    except Exception as exc:
        return VerifyCodeResponse(ok=False, error=str(exc))


@app.post("/accounts/{account_id}/auth/qr", response_model=QrResponse)
async def post_auth_qr(
    account_id: str,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> QrResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    try:
        qr_login, url = await telethon_client.qr_login_start(state.client)
        state.qr_login = qr_login
        return QrResponse(ok=True, url=url)
    except Exception as exc:
        return QrResponse(ok=False, error=str(exc))


@app.post("/accounts/{account_id}/auth/qr/wait", response_model=QrWaitResponse)
async def post_auth_qr_wait(
    account_id: str,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> QrWaitResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    if state.qr_login is None:
        return QrWaitResponse(ok=False, error="No active QR login. Call /auth/qr first.")
    try:
        me = await telethon_client.qr_login_wait(state.qr_login)
        state.qr_login = None
        account_str = f"@{me['username']}" if me.get("username") else str(me["id"])
        await manager.mark_authorized(account_id, me["id"], me.get("username"))
        return QrWaitResponse(ok=True, account=account_str)
    except RuntimeError as exc:
        if "2FA_REQUIRED" in str(exc):
            return QrWaitResponse(ok=False, need_2fa=True, error="2FA password required")
        return QrWaitResponse(ok=False, error=str(exc))


@app.post("/accounts/{account_id}/auth/qr/password", response_model=QrWaitResponse)
async def post_auth_qr_password(
    account_id: str,
    body: QrPasswordRequest,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> QrWaitResponse:
    try:
        state = manager.get_account(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    try:
        me = await telethon_client.qr_login_2fa(state.client, body.password)
        account_str = f"@{me['username']}" if me.get("username") else str(me["id"])
        await manager.mark_authorized(account_id, me["id"], me.get("username"))
        return QrWaitResponse(ok=True, account=account_str)
    except Exception as exc:
        return QrWaitResponse(ok=False, error=str(exc))


# --- Aggregated routes ---

@app.get("/health", response_model=HealthResponse)
async def get_health(
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> HealthResponse:
    accounts = await manager.list_accounts()
    summaries = []
    for acc in accounts:
        summaries.append({
            "account_id": acc["account_id"],
            "status": acc["status"],
            "username": acc.get("username"),
            "today_sent": acc.get("today_sent", 0),
        })
    return HealthResponse(status="ok", accounts=summaries)


@app.get("/incoming", response_model=IncomingListResponse)
async def get_incoming(
    limit: int = 50,
    unprocessed_only: bool = False,
    _api_key: str = Depends(verify_api_key),
    manager: AccountManager = Depends(get_manager),
) -> IncomingListResponse:
    messages = await manager.get_recent_incoming(limit=limit, unprocessed_only=unprocessed_only)
    return IncomingListResponse(ok=True, messages=messages, count=len(messages))
