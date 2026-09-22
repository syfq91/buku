"""Pytest fixtures for buku tests."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.services.auth import auth_service

OpdsEnv = tuple[TestClient, sessionmaker[Session], str, str]


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
        jobs_enabled=False,
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


@pytest.fixture
def opds_env(tmp_path: Path) -> Generator[OpdsEnv]:
    """Provide a migrated DB, two users, and an authenticated client for OPDS tests."""
    reset_engine()
    database_file = tmp_path / "opds.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url, jobs_enabled=False)
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory_cls = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        with factory_cls() as db:
            auth_service.create_user(db, "reader", "readerpass123", "Reader", is_admin=False)
            auth_service.create_user(db, "admin", "adminpass123", "Admin", is_admin=True)
        reader_token = client.post(
            "/api/v1/auth/login", json={"username": "reader", "password": "readerpass123"}
        ).json()["token"]
        admin_token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"}
        ).json()["token"]
        yield client, factory_cls, reader_token, admin_token
    reset_engine()
    set_settings(None)
