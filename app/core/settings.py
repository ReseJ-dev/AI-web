"""Typed application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide immutable view of environment settings."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache and reload environment values."""
    get_settings.cache_clear()
    return get_settings()
