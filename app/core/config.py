from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    app_env: str = "development"
    app_version: str = "0.3.0"
    git_sha: str = "local"
    secret_key: str = Field(min_length=32)
    database_url: str
    redis_url: str
    telegram_bot_token: str = ""
    telegram_bot_username: str = "DialogSpyBot"
    telegram_webhook_secret: str = Field(min_length=8)
    public_base_url: str = "http://localhost:8000"
    mini_app_url: str = "http://localhost:8000/app"
    admin_url: str = "http://localhost:8000/admin"
    telegram_admin_ids: tuple[int, ...] = ()
    media_root: Path = Path("/data/media")
    media_signing_ttl_seconds: int = 300
    init_data_max_age_seconds: int = 600
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    cors_origins: tuple[str, ...] = ()

    @field_validator("telegram_admin_ids", "cors_origins", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            values = [item.strip() for item in value.split(",") if item.strip()]
            return tuple(int(item) for item in values) if values and values[0].isdigit() else tuple(values)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
