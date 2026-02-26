from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

_api_key_header = APIKeyHeader(name="X-API-Key")


async def verify_api_key(
    api_key: str = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    if api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
