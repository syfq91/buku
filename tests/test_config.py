"""Tests for configuration parsing, precedence, and directory conventions."""

from pathlib import Path

import pytest

from buku.config import Settings, load_settings


def test_default_settings() -> None:
    """Verify default settings instantiation."""
    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.debug is False
    assert "sqlite:///" in settings.effective_database_url


def test_toml_config_loading(tmp_path: Path) -> None:
    """Verify loading configuration from TOML file with sections."""
    config_file = tmp_path / "test_config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
debug = true

[paths]
config_dir = "/tmp/buku_test_config"
books_dir = "/tmp/buku_test_books"

[database]
url = "sqlite:////tmp/custom.db"
"""
    )

    settings = load_settings(config_file=config_file)
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.debug is True
    assert settings.config_dir == Path("/tmp/buku_test_config")
    assert settings.books_dir == Path("/tmp/buku_test_books")
    assert settings.effective_database_url == "sqlite:////tmp/custom.db"


def test_env_var_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify environment variables override defaults and TOML."""
    config_file = tmp_path / "test_config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
"""
    )

    # Set BUKU_ env vars (takes precedence over TOML)
    monkeypatch.setenv("BUKU_PORT", "9090")
    monkeypatch.setenv("BUKU_HOST", "0.0.0.0")
    monkeypatch.setenv("BUKU_DEBUG", "1")

    settings = load_settings(config_file=config_file)
    assert settings.port == 9090
    assert settings.host == "0.0.0.0"
    assert settings.debug is True


def test_bookserver_env_var_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify BOOKSERVER_ prefix environment variables are supported."""
    monkeypatch.setenv("BOOKSERVER_PORT", "8888")
    monkeypatch.setenv("BOOKSERVER_HOST", "192.168.1.50")

    settings = load_settings()
    assert settings.port == 8888
    assert settings.host == "192.168.1.50"


def test_ensure_directories_creates_cache_but_never_books_dir(tmp_path: Path) -> None:
    """Verify ensure_directories creates config and cache, but never writes to books_dir."""
    cfg_dir = tmp_path / "cfg"
    books_dir = tmp_path / "non_existent_books"

    settings = Settings(config_dir=cfg_dir, books_dir=books_dir)
    settings.ensure_directories()

    # Config directory and cache subdirectories must exist
    assert cfg_dir.is_dir()
    assert (cfg_dir / "cache" / "covers").is_dir()
    assert (cfg_dir / "cache" / "metadata").is_dir()
    assert (cfg_dir / "cache" / "x4").is_dir()

    # Critical invariant: books_dir must NOT be created
    assert not books_dir.exists()
