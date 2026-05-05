"""Application configuration loaded from environment variables and .env file.

Uses Pydantic Settings to map environment variables to typed fields.
See .env.example for a complete template.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings populated from environment variables.

    All fields map to uppercase env vars (e.g. ``api_key`` ← ``API_KEY``).
    A ``.env`` file in the project root is loaded automatically.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str = "change-me"

    # Default rate limits (used when creating new accounts)
    max_messages_per_day: int = 25
    min_delay_seconds: int = 30
    max_delay_seconds: int = 90

    # Database
    db_path: str = "data/send_log.db"

    # Sessions directory
    sessions_dir: str = "data/sessions"

    # Listener
    incoming_webhook_url: str = ""

    # Proxy for Telegram connections (e.g. http://user:pass@host:port)
    https_proxy: str = ""

    # Connection watchdog
    watchdog_interval_seconds: int = 30
    max_reconnect_attempts: int = 5
    reconnect_backoff_base_seconds: int = 5

    # Telegram bot for client self-registration
    bot_token: str = ""
    bot_admins: list[int] = []

    @field_validator("bot_admins", mode="before")
    @classmethod
    def _parse_bot_admins(cls, v: object) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    """Return the cached singleton Settings instance.

    Used as a FastAPI dependency via ``Depends(get_settings)``.
    """
    return Settings()
