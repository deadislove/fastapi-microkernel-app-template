import json
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "fastapi-microkernel-app-template"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-me-in-production-use-a-long-random-string"

    # JWT
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7

    # Database: defaults to SQLite for local dev; swap to asyncpg URL for Postgres
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # Plugin loading behavior:
    #   "fail_fast"    -> any plugin's register()/boot() failure aborts startup.
    #   "best_effort"  -> a failing plugin is isolated (marked FAILED, skipped);
    #                     the rest of the kernel still starts up.
    plugin_load_mode: Literal["fail_fast", "best_effort"] = "fail_fast"

    # Plugin activation: None (default) loads every discovered plugin; set to
    # a list to only load plugins whose directory name appears in it, so
    # the same codebase can enable different plugin sets per environment
    # without touching code or deleting directories.
    enabled_plugins: list[str] | None = None

    @field_validator("enabled_plugins", mode="before")
    @classmethod
    def parse_enabled_plugins(cls, v: str | list | None) -> list[str] | None:
        if isinstance(v, str):
            return json.loads(v)
        return v

    # Rate Limiting
    rate_limit_default: str = "100/minute"

    # CORS
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return json.loads(v)
        return v


# Single shared instance: import this everywhere instead of re-instantiating
settings = Settings()
