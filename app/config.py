from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram API
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_name: str = "evgenia"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str = "change-me"

    # Rate limits
    max_messages_per_day: int = 25
    min_delay_seconds: int = 30
    max_delay_seconds: int = 90

    # Database
    db_path: str = "data/send_log.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
