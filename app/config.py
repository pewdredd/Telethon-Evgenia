"""Application configuration loaded from environment variables and .env file.

Uses Pydantic Settings to map environment variables to typed fields.
See .env.example for a complete template.
"""

from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    """Return the cached singleton Settings instance.

    Used as a FastAPI dependency via ``Depends(get_settings)``.
    """
    return Settings()
