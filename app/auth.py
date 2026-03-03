"""API key authentication for protecting HTTP endpoints.

All endpoints require an ``X-API-Key`` header whose value must match
the ``API_KEY`` environment variable.
"""

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

_api_key_header = APIKeyHeader(name="X-API-Key")


async def verify_api_key(
    api_key: str = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate the X-API-Key header against the configured secret.

    Args:
        api_key: Value from the ``X-API-Key`` request header.
        settings: Application settings (injected).

    Returns:
        The API key string on success.

    Raises:
        HTTPException: 401 if the key does not match ``settings.api_key``.
    """
    if api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key
