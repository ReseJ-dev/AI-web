"""Typed application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for local and deployed environments."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    database_url: str = Field(
        default="sqlite:///./data/app.db",
        validation_alias="DATABASE_URL",
    )
    source_policy_config_dir: Path = Field(
        default=Path("config"),
        validation_alias="SOURCE_POLICY_CONFIG_DIR",
    )
    brave_search_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="BRAVE_SEARCH_API_KEY",
    )
    search_result_retention_allowed: bool = Field(
        default=False,
        validation_alias="SEARCH_RESULT_RETENTION_ALLOWED",
    )
    search_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="SEARCH_TIMEOUT_SECONDS",
    )
    search_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="SEARCH_MAX_RETRIES",
    )
    search_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        validation_alias="SEARCH_BACKOFF_SECONDS",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide immutable view of environment settings."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache and reload environment values."""
    get_settings.cache_clear()
    return get_settings()
