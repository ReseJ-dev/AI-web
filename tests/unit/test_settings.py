"""Tests for environment-backed application settings."""

from pathlib import Path

import pytest

from app.core.settings import Settings


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override local development defaults."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SOURCE_POLICY_CONFIG_DIR", "/tmp/policy-config")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-secret")
    monkeypatch.setenv("SEARCH_RESULT_RETENTION_ALLOWED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "test-company-model")
    monkeypatch.setenv("LLM_API_URL", "https://llm.example/extract")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("LLM_RESPONSE_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_MAX_INPUT_CHARS", "40000")
    monkeypatch.setenv("CRAWLER_REQUEST_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("CRAWLER_MAX_RESPONSE_BYTES", "500000")
    monkeypatch.setenv("CRAWLER_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("CRAWLER_MAX_RETRY_AFTER_SECONDS", "30")
    monkeypatch.setenv("OPENCORPORATES_API_KEY", "registry-secret")
    monkeypatch.setenv("OPENCORPORATES_LICENSED_DATA_USE_ALLOWED", "true")
    monkeypatch.setenv("OPENCORPORATES_MAX_RETRIES", "2")
    monkeypatch.setenv("WIKIDATA_ENABLED", "true")
    monkeypatch.setenv("WIKIDATA_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("GEONAMES_USERNAME", "geo-user")
    monkeypatch.setenv("GEONAMES_CACHE_TTL_SECONDS", "7200")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/tmp/google-service.json")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_SHEETS_CREATE_ALLOWED", "true")
    monkeypatch.setenv("GOOGLE_SHEETS_MAX_RETRIES", "4")
    monkeypatch.setenv("UI_API_BASE_URL", "http://api.internal:8080")

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.source_policy_config_dir == Path("/tmp/policy-config")
    assert settings.brave_search_api_key is not None
    assert settings.brave_search_api_key.get_secret_value() == "test-secret"
    assert settings.search_result_retention_allowed is True
    assert settings.llm_provider == "fake"
    assert settings.llm_model == "test-company-model"
    assert settings.llm_api_url == "https://llm.example/extract"
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "llm-secret"
    assert settings.llm_response_max_retries == 1
    assert settings.llm_max_input_chars == 40_000
    assert settings.crawler_request_delay_seconds == 0.25
    assert settings.crawler_max_response_bytes == 500_000
    assert settings.crawler_timeout_seconds == 8
    assert settings.crawler_max_retry_after_seconds == 30
    assert settings.opencorporates_api_key is not None
    assert settings.opencorporates_api_key.get_secret_value() == "registry-secret"
    assert settings.opencorporates_licensed_data_use_allowed is True
    assert settings.opencorporates_max_retries == 2
    assert settings.wikidata_enabled is True
    assert settings.wikidata_timeout_seconds == 12
    assert settings.geonames_username is not None
    assert settings.geonames_username.get_secret_value() == "geo-user"
    assert settings.geonames_cache_ttl_seconds == 7_200
    assert settings.google_service_account_file == Path("/tmp/google-service.json")
    assert settings.google_sheets_spreadsheet_id == "sheet-id"
    assert settings.google_sheets_create_allowed is True
    assert settings.google_sheets_max_retries == 4
    assert settings.ui_api_base_url == "http://api.internal:8080"


def test_blank_optional_google_settings_are_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied environment template does not turn blank credentials into paths."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")

    settings = Settings(_env_file=None)

    assert settings.google_service_account_file is None
    assert settings.google_service_account_json is None
    assert settings.google_sheets_spreadsheet_id is None
