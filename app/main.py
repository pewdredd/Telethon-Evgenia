import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import rate_limiter, telethon_client
from app.auth import verify_api_key
from app.config import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    await rate_limiter.init_db(settings.db_path)
    await telethon_client.start_client(settings)
    rate_limiter.start_worker(telethon_client.send_message, settings)
    yield
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


# --- Routes ---

@app.post("/send", response_model=SendResponse)
async def post_send(
    body: SendRequest,
    settings: Settings = Depends(get_settings),
    _api_key: str = Depends(verify_api_key),
) -> SendResponse:
    if not await rate_limiter.is_quota_available(settings.max_messages_per_day):
        raise HTTPException(status_code=429, detail="Daily message quota exhausted")

    # Convert numeric recipient strings to int for Telethon
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
