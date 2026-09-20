"""Tests for the FastAPI application routes and lifecycle."""

from fastapi.testclient import TestClient

from buku.app import create_app
from buku.config import Settings


def test_health_endpoint(client: TestClient) -> None:
    """Verify health endpoint responds with 200 and valid JSON."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["app"] == "buku"
    assert "version" in data


def test_root_endpoint(client: TestClient) -> None:
    """Verify root endpoint responds with application info."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "buku" in data["message"].lower()
    assert "docs_url" in data


def test_app_debug_setting(test_settings: Settings) -> None:
    """Verify debug settings are passed through to FastAPI."""
    test_settings.debug = True
    app = create_app(test_settings)
    assert app.debug is True
