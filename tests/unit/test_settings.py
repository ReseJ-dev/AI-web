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
    monkeypatch.setenv("CRAWLER_REQUEST_DELAY_SECONDS", "0.25")
    monkeypatch.setenv("CRAWLER_MAX_RESPONSE_BYTES", "500000")
    monkeypatch.setenv("CRAWLER_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("CRAWLER_MAX_RETRY_AFTER_SECONDS", "30")

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
    assert settings.crawler_request_delay_seconds == 0.25
    assert settings.crawler_max_response_bytes == 500_000
    assert settings.crawler_timeout_seconds == 8
    assert settings.crawler_max_retry_after_seconds == 30
