"""Typed application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    """Prefer repository config, then fall back to wheel-packaged defaults."""
    repository_config = Path("config")
    packaged_config = Path(__file__).resolve().parents[1] / "config"
    return repository_config if repository_config.is_dir() else packaged_config


class Settings(BaseSettings):
    """Runtime configuration for local and deployed environments."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        populate_by_name=True,
    )

    app_env: str = Field(default="development", validation_alias="APP_ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    database_url: str = Field(
        default="sqlite:///./data/app.db",
        validation_alias="DATABASE_URL",
    )
    source_policy_config_dir: Path = Field(
        default_factory=_default_config_dir,
        validation_alias="SOURCE_POLICY_CONFIG_DIR",
    )
    api_access_token: SecretStr | None = Field(
        default=None,
        validation_alias="API_ACCESS_TOKEN",
    )
    brave_search_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="BRAVE_SEARCH_API_KEY",
    )
    search_provider: str = Field(
        default="tavily",
        min_length=1,
        max_length=50,
        validation_alias="SEARCH_PROVIDER",
    )
    tavily_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="TAVILY_API_KEY",
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
    project_user_agent: str = Field(
        default="AIWebResearchAgent/0.1",
        min_length=1,
        max_length=500,
        validation_alias="PROJECT_USER_AGENT",
    )
    robots_cache_ttl_seconds: float = Field(
        default=3_600,
        ge=0,
        le=86_400,
        validation_alias="ROBOTS_CACHE_TTL_SECONDS",
    )
    robots_strict_mode: bool = Field(
        default=True,
        validation_alias="ROBOTS_STRICT_MODE",
    )
    compliance_http_timeout_seconds: float = Field(
        default=10,
        gt=0,
        validation_alias="COMPLIANCE_HTTP_TIMEOUT_SECONDS",
    )
    terms_max_documents: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="TERMS_MAX_DOCUMENTS",
    )
    html_content_max_chars: int = Field(
        default=20_000,
        ge=1_000,
        le=200_000,
        validation_alias="HTML_CONTENT_MAX_CHARS",
    )
    llm_provider: str = Field(
        default="disabled",
        min_length=1,
        max_length=100,
        validation_alias="LLM_PROVIDER",
    )
    llm_model: str = Field(
        default="not-configured",
        min_length=1,
        max_length=200,
        validation_alias="LLM_MODEL",
    )
    llm_api_url: str | None = Field(
        default=None,
        validation_alias="LLM_API_URL",
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="LLM_API_KEY",
    )
    llm_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        validation_alias="LLM_TIMEOUT_SECONDS",
    )
    llm_http_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias="LLM_HTTP_MAX_RETRIES",
    )
    llm_response_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias="LLM_RESPONSE_MAX_RETRIES",
    )
    llm_retry_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        validation_alias="LLM_RETRY_BACKOFF_SECONDS",
    )
    llm_max_input_chars: int = Field(
        default=60_000,
        ge=1_000,
        le=200_000,
        validation_alias="LLM_MAX_INPUT_CHARS",
    )
    research_search_budget: int = Field(
        default=20,
        ge=1,
        le=50,
        validation_alias="RESEARCH_SEARCH_BUDGET",
    )
    research_search_page_size: int = Field(
        default=20,
        ge=1,
        le=20,
        validation_alias="RESEARCH_SEARCH_PAGE_SIZE",
    )
    research_crawl_page_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias="RESEARCH_CRAWL_PAGE_LIMIT",
    )
    crawler_request_delay_seconds: float = Field(
        default=1.0,
        ge=0,
        le=60,
        validation_alias="CRAWLER_REQUEST_DELAY_SECONDS",
    )
    crawler_max_response_bytes: int = Field(
        default=2_000_000,
        ge=1_024,
        le=5_000_000,
        validation_alias="CRAWLER_MAX_RESPONSE_BYTES",
    )
    crawler_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=120,
        validation_alias="CRAWLER_TIMEOUT_SECONDS",
    )
    crawler_max_retry_after_seconds: float = Field(
        default=120.0,
        ge=0,
        le=3_600,
        validation_alias="CRAWLER_MAX_RETRY_AFTER_SECONDS",
    )
    opencorporates_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="OPENCORPORATES_API_KEY",
    )
    opencorporates_licensed_data_use_allowed: bool = Field(
        default=False,
        validation_alias="OPENCORPORATES_LICENSED_DATA_USE_ALLOWED",
    )
    opencorporates_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        validation_alias="OPENCORPORATES_TIMEOUT_SECONDS",
    )
    opencorporates_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="OPENCORPORATES_MAX_RETRIES",
    )
    opencorporates_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        validation_alias="OPENCORPORATES_BACKOFF_SECONDS",
    )
    opencorporates_max_retry_after_seconds: float = Field(
        default=60.0,
        ge=0,
        le=3_600,
        validation_alias="OPENCORPORATES_MAX_RETRY_AFTER_SECONDS",
    )
    wikidata_enabled: bool = Field(
        default=False,
        validation_alias="WIKIDATA_ENABLED",
    )
    wikidata_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=120,
        validation_alias="WIKIDATA_TIMEOUT_SECONDS",
    )
    wikidata_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="WIKIDATA_MAX_RETRIES",
    )
    wikidata_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        validation_alias="WIKIDATA_BACKOFF_SECONDS",
    )
    geonames_username: SecretStr | None = Field(
        default=None,
        validation_alias="GEONAMES_USERNAME",
    )
    geonames_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        validation_alias="GEONAMES_TIMEOUT_SECONDS",
    )
    geonames_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="GEONAMES_MAX_RETRIES",
    )
    geonames_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        validation_alias="GEONAMES_BACKOFF_SECONDS",
    )
    geonames_cache_ttl_seconds: float = Field(
        default=86_400,
        ge=60,
        le=2_592_000,
        validation_alias="GEONAMES_CACHE_TTL_SECONDS",
    )
    google_service_account_file: Path | None = Field(
        default=None,
        validation_alias="GOOGLE_SERVICE_ACCOUNT_FILE",
    )
    google_service_account_json: SecretStr | None = Field(
        default=None,
        validation_alias="GOOGLE_SERVICE_ACCOUNT_JSON",
    )
    google_sheets_spreadsheet_id: str | None = Field(
        default=None,
        validation_alias="GOOGLE_SHEETS_SPREADSHEET_ID",
    )
    google_sheets_create_allowed: bool = Field(
        default=False,
        validation_alias="GOOGLE_SHEETS_CREATE_ALLOWED",
    )
    google_sheets_title: str = Field(
        default="AI Web Research Results",
        min_length=1,
        max_length=200,
        validation_alias="GOOGLE_SHEETS_TITLE",
    )
    google_sheets_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        le=120,
        validation_alias="GOOGLE_SHEETS_TIMEOUT_SECONDS",
    )
    google_sheets_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="GOOGLE_SHEETS_MAX_RETRIES",
    )
    google_sheets_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        le=30,
        validation_alias="GOOGLE_SHEETS_BACKOFF_SECONDS",
    )
    ui_api_base_url: str = Field(
        default="http://localhost:8000",
        min_length=1,
        max_length=2_048,
        validation_alias="UI_API_BASE_URL",
    )

    @field_validator(
        "google_service_account_file",
        "google_sheets_spreadsheet_id",
        mode="before",
    )
    @classmethod
    def blank_google_values_are_unset(cls, value: object) -> object:
        """Treat blank optional Google settings as absent."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("google_service_account_json", mode="before")
    @classmethod
    def blank_google_secret_is_unset(cls, value: object) -> object:
        """Avoid treating an empty credential environment variable as a key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "api_access_token",
        "brave_search_api_key",
        "tavily_api_key",
        "llm_api_key",
        mode="before",
    )
    @classmethod
    def blank_api_secrets_are_unset(cls, value: object) -> object:
        """Treat blank API credential variables as unconfigured."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def credentials_require_tls(self) -> "Settings":
        """Require safe transport and API protection around configured credentials."""
        if (
            self.llm_api_key is not None
            and self.llm_api_url is not None
            and urlsplit(self.llm_api_url).scheme.casefold() != "https"
        ):
            raise ValueError("LLM_API_URL must use HTTPS when LLM_API_KEY is set")
        paid_or_mutating_credentials = (
            self.brave_search_api_key is not None
            or self.tavily_api_key is not None
            or self.google_service_account_file is not None
            or self.google_service_account_json is not None
        )
        if paid_or_mutating_credentials and self.api_access_token is None:
            raise ValueError(
                "API_ACCESS_TOKEN is required when search or Google Sheets "
                "credentials are configured"
            )
        return self


class UiSettings(BaseSettings):
    """Minimal dashboard configuration that cannot load backend provider secrets."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        populate_by_name=True,
    )

    api_base_url: str = Field(
        default="http://localhost:8000",
        min_length=1,
        max_length=2_048,
        validation_alias="UI_API_BASE_URL",
    )
    api_access_token: SecretStr | None = Field(
        default=None,
        validation_alias="UI_API_ACCESS_TOKEN",
    )

    @field_validator("api_access_token", mode="before")
    @classmethod
    def blank_token_is_unset(cls, value: object) -> object:
        """Treat an empty dashboard token as absent."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide immutable view of environment settings."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache and reload environment values."""
    get_settings.cache_clear()
    return get_settings()


@lru_cache(maxsize=1)
def get_ui_settings() -> UiSettings:
    """Return dashboard-only settings without constructing backend settings."""
    return UiSettings()
