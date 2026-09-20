"""Pytest fixtures for buku tests."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from buku.app import create_app
from buku.config import Settings, set_settings


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Provide an isolated temporary configuration directory."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def tmp_books_dir(tmp_path: Path) -> Path:
    """Provide an isolated temporary books directory."""
    books = tmp_path / "books"
    books.mkdir(parents=True, exist_ok=True)
    return books


@pytest.fixture
def test_settings(tmp_config_dir: Path, tmp_books_dir: Path) -> Generator[Settings]:
    """Provide a Settings instance pointing to isolated test directories."""
    settings = Settings(
        host="127.0.0.1",
        port=8080,
        debug=True,
        config_dir=tmp_config_dir,
        books_dir=tmp_books_dir,
    )
    set_settings(settings)
    yield settings
    set_settings(None)


@pytest.fixture
def client(test_settings: Settings) -> Generator[TestClient]:
    """Provide a FastAPI TestClient configured with test settings."""
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c
