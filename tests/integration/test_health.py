"""Tests for the API health endpoint."""

from fastapi.testclient import TestClient

from app.api.main import app


def test_health_endpoint() -> None:
    """The health endpoint reports an operational API."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
