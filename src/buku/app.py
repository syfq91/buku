"""FastAPI application factory and core routes for buku."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from buku.config import Settings, get_settings

logger = logging.getLogger("buku")
__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Handle application startup and shutdown events."""
    settings: Settings = getattr(app.state, "settings", get_settings())

    # Ensure configuration directories exist (read-only books_dir is untouched)
    settings.ensure_directories()

    # Initialize database engine
    from buku.db import get_engine

    engine = get_engine(settings.effective_database_url)

    logger.info("Starting buku v%s", __version__)
    logger.info("Config directory: %s", settings.config_dir)
    logger.info("Books directory: %s", settings.books_dir)
    logger.info("Database URL: %s", settings.effective_database_url)

    yield

    engine.dispose()
    logger.info("Stopping buku")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure an instance of the FastAPI application."""
    active_settings = settings or get_settings()

    app = FastAPI(
        title="buku",
        description="Lightweight, self-hosted digital book server",
        version=__version__,
        lifespan=lifespan,
        debug=active_settings.debug,
    )

    # Attach settings to application state
    app.state.settings = active_settings

    # Include routers
    from buku.api import admin_metadata_router, auth_router, search_router

    app.include_router(auth_router)
    app.include_router(admin_metadata_router)
    app.include_router(search_router)

    @app.get("/health", tags=["System"])
    async def health() -> dict[str, Any]:
        """Health check endpoint."""
        return {
            "status": "ok",
            "app": "buku",
            "version": __version__,
        }

    @app.get("/", tags=["System"])
    async def root() -> dict[str, Any]:
        """Root status and navigation info."""
        return {
            "message": "Welcome to buku - Lightweight Digital Book Server",
            "version": __version__,
            "docs_url": "/docs",
        }

    return app
