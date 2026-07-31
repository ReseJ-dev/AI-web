"""Static safeguards for the Docker development environment."""

from pathlib import Path
from typing import Any, cast

import yaml

from app.core.settings import Settings

PROJECT_ROOT = Path(__file__).parents[2]


def _mapping(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _compose() -> dict[str, Any]:
    payload = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text())
    return _mapping(payload)


def test_compose_defines_healthy_persistent_stack() -> None:
    compose = _compose()
    services = _mapping(compose["services"])

    assert set(services) == {"api", "streamlit", "postgres"}
    assert "postgres-data" in _mapping(compose["volumes"])
    for service_name in services:
        assert "healthcheck" in _mapping(services[service_name])

    postgres = _mapping(services["postgres"])
    assert "postgres-data:/var/lib/postgresql/data" in postgres["volumes"]

    api_dependencies = _mapping(_mapping(services["api"])["depends_on"])
    assert _mapping(api_dependencies["postgres"])["condition"] == "service_healthy"
    ui_dependencies = _mapping(_mapping(services["streamlit"])["depends_on"])
    assert _mapping(ui_dependencies["api"])["condition"] == "service_healthy"


def test_compose_requires_runtime_database_credentials() -> None:
    services = _mapping(_compose()["services"])
    postgres_environment = _mapping(_mapping(services["postgres"])["environment"])
    api_environment = _mapping(_mapping(services["api"])["environment"])
    ui_environment = _mapping(_mapping(services["streamlit"])["environment"])

    assert postgres_environment["POSTGRES_DB"].startswith("${POSTGRES_DB:?")
    assert postgres_environment["POSTGRES_USER"].startswith("${POSTGRES_USER:?")
    assert postgres_environment["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert api_environment["DATABASE_URL"].startswith("${COMPOSE_DATABASE_URL:?")
    assert "POSTGRES_PASSWORD" not in api_environment
    assert (
        not {
            "BRAVE_SEARCH_API_KEY",
            "TAVILY_API_KEY",
            "LLM_API_KEY",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
        }
        & ui_environment.keys()
    )
    backend_variables = {
        str(field.validation_alias or name)
        for name, field in Settings.model_fields.items()
        if str(field.validation_alias or name) != "UI_API_BASE_URL"
    }
    assert backend_variables <= api_environment.keys()
    assert "./.secrets:/run/secrets:ro" in _mapping(services["api"])["volumes"]
    assert (
        ui_environment["UI_API_ACCESS_TOKEN"]
        == "${UI_API_ACCESS_TOKEN:-${API_ACCESS_TOKEN:-}}"
    )


def test_images_use_python_312_non_root_users_and_health_checks() -> None:
    for filename in ("api.Dockerfile", "streamlit.Dockerfile"):
        dockerfile = (PROJECT_ROOT / "docker" / filename).read_text()
        assert dockerfile.startswith("FROM python:3.12-slim")
        assert "COPY config ./config" in dockerfile
        assert "\nUSER app\n" in dockerfile
        assert "\nHEALTHCHECK " in dockerfile

    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert ".secrets/" in dockerignore
