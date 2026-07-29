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

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.source_policy_config_dir == Path("/tmp/policy-config")
    assert settings.brave_search_api_key is not None
    assert settings.brave_search_api_key.get_secret_value() == "test-secret"
    assert settings.search_result_retention_allowed is True
