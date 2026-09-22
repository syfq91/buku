"""Configuration management for the buku digital book server.

Supports layered configuration:
1. Hardcoded defaults
2. TOML configuration file (/config/config.toml, ./config.toml, or custom path)
3. Environment variables (prefixed with BUKU_ or BOOKSERVER_)
4. Runtime / CLI flag overrides
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_default_config_dir() -> Path:
    """Return default configuration directory, preferring /config in Docker."""
    docker_dir = Path("/config")
    if docker_dir.is_dir():
        return docker_dir
    return Path("./config").resolve()


def get_default_books_dir() -> Path:
    """Return default books library directory, preferring /books in Docker."""
    docker_dir = Path("/books")
    if docker_dir.is_dir():
        return docker_dir
    return Path("./books").resolve()


def find_default_config_file() -> Path | None:
    """Locate config file from environment or standard paths."""
    env_config = os.environ.get("BUKU_CONFIG") or os.environ.get("BOOKSERVER_CONFIG")
    if env_config:
        p = Path(env_config)
        if p.is_file():
            return p.resolve()

    candidates = [
        Path("/config/config.toml"),
        Path("./config/config.toml").resolve(),
        Path("./config.toml").resolve(),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_toml_config(path: Path) -> dict[str, Any]:
    """Parse a TOML configuration file into a flat dictionary suitable for Settings."""
    if not path.is_file():
        return {}

    with path.open("rb") as f:
        data = tomllib.load(f)

    flat: dict[str, Any] = {}

    # Extract top-level keys
    for k, v in data.items():
        if not isinstance(v, dict):
            flat[k] = v

    # Extract sectioned keys if present: [server], [paths], [database]
    if "server" in data and isinstance(data["server"], dict):
        for k, v in data["server"].items():
            flat[k] = v

    if "paths" in data and isinstance(data["paths"], dict):
        if "config_dir" in data["paths"]:
            flat["config_dir"] = data["paths"]["config_dir"]
        if "books_dir" in data["paths"]:
            flat["books_dir"] = data["paths"]["books_dir"]

    if "database" in data and isinstance(data["database"], dict):
        if "url" in data["database"]:
            flat["database_url"] = data["database"]["url"]

    return flat


class Settings(BaseSettings):
    """Application settings with precedence: CLI/kwargs > Env vars > TOML > Defaults."""

    model_config = SettingsConfigDict(
        env_prefix="BUKU_",
        extra="ignore",
    )

    # Server options
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False

    # Path conventions
    config_dir: Path = Field(default_factory=get_default_config_dir)
    books_dir: Path = Field(default_factory=get_default_books_dir)

    # Database connection URL (defaults to sqlite in config_dir)
    database_url: str | None = None

    # Google Books metadata enrichment (optional API key)
    google_books_api_key: str | None = None

    # Phase 17 background job worker
    jobs_enabled: bool = True
    jobs_poll_interval: float = 1.0

    @property
    def effective_database_url(self) -> str:
        """Return explicitly configured database URL or SQLite DB inside config_dir."""
        if self.database_url:
            return self.database_url
        db_path = self.config_dir.resolve() / "buku.db"
        return f"sqlite:///{db_path}"

    def ensure_directories(self) -> None:
        """Ensure config and internal cache directories exist.

        NOTE: This must NEVER attempt to create or write into books_dir,
        respecting the read-only media directory contract.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = self.config_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "covers").mkdir(parents=True, exist_ok=True)
        (cache_dir / "metadata").mkdir(parents=True, exist_ok=True)
        (cache_dir / "x4").mkdir(parents=True, exist_ok=True)


def load_settings(
    config_file: Path | str | None = None,
    **overrides: Any,
) -> Settings:
    """Load settings by merging TOML configuration, environment variables, and overrides."""
    # Find config file
    cfg_path = Path(config_file) if config_file else find_default_config_file()
    toml_data: dict[str, Any] = {}
    if cfg_path and cfg_path.is_file():
        toml_data = load_toml_config(cfg_path)

    # Check BOOKSERVER_ env vars as fallback aliases
    bookserver_env_overrides: dict[str, Any] = {}
    env_mapping = {
        "BOOKSERVER_HOST": "host",
        "BOOKSERVER_PORT": "port",
        "BOOKSERVER_DEBUG": "debug",
        "BOOKSERVER_CONFIG_DIR": "config_dir",
        "BOOKSERVER_BOOKS_DIR": "books_dir",
        "BOOKSERVER_DATABASE_URL": "database_url",
        "BOOKSERVER_GOOGLE_BOOKS_API_KEY": "google_books_api_key",
    }
    for env_var, setting_key in env_mapping.items():
        val = os.environ.get(env_var)
        if val is not None:
            if setting_key == "port":
                try:
                    bookserver_env_overrides[setting_key] = int(val)
                except ValueError:
                    pass
            elif setting_key == "debug":
                bookserver_env_overrides[setting_key] = val.lower() in ("true", "1", "yes")
            elif setting_key in ("config_dir", "books_dir"):
                bookserver_env_overrides[setting_key] = Path(val)
            else:
                bookserver_env_overrides[setting_key] = val

    # Precedence: Defaults < TOML < BOOKSERVER_ env < BUKU_ env < explicit overrides
    merged: dict[str, Any] = dict(toml_data)

    # BOOKSERVER_ overrides TOML
    merged.update(bookserver_env_overrides)

    # If BUKU_ env var is present, remove from merged so Pydantic reads env var with priority
    for key in list(merged.keys()):
        if f"BUKU_{key.upper()}" in os.environ:
            del merged[key]

    # Explicit overrides take highest precedence
    clean_overrides = {k: v for k, v in overrides.items() if v is not None}
    merged.update(clean_overrides)

    return Settings(**merged)


_cached_settings: Settings | None = None


def get_settings() -> Settings:
    """Return globally cached settings instance or initialize from environment/defaults."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = load_settings()
    return _cached_settings


def set_settings(settings: Settings | None) -> None:
    """Update or clear globally cached settings (primarily for testing)."""
    global _cached_settings
    _cached_settings = settings
