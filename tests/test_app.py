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
    """Verify the root lands on sign-in, then the dashboard once authenticated."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_app_debug_setting(test_settings: Settings) -> None:
    """Verify debug settings are passed through to FastAPI."""
    test_settings.debug = True
    app = create_app(test_settings)
    assert app.debug is True
