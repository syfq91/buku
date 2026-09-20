"""Command-line interface for the buku / bookserver digital book server."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import click
import uvicorn

from buku.config import load_settings, set_settings

__version__ = "0.1.0"


def setup_logging(debug: bool = False) -> None:
    """Configure standard logging format."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
@click.version_option(version=__version__, prog_name="bookserver")
def cli() -> None:
    """buku / bookserver: Lightweight digital book server."""
    pass


@cli.command()
@click.option(
    "--host",
    type=str,
    default=None,
    help="Host address to bind the server to [default: 0.0.0.0 or config].",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Port number to bind the server to [default: 8080 or config].",
)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable auto-reload for development.",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable debug mode and detailed logging.",
)
def serve(
    host: str | None,
    port: int | None,
    reload: bool,
    config_file: Path | None,
    debug: bool,
) -> None:
    """Start the book server web application."""
    overrides: dict[str, Any] = {}
    if host is not None:
        overrides["host"] = host
        os.environ["BUKU_HOST"] = host
    if port is not None:
        overrides["port"] = port
        os.environ["BUKU_PORT"] = str(port)
    if debug:
        overrides["debug"] = True
        os.environ["BUKU_DEBUG"] = "1"
    if config_file:
        os.environ["BUKU_CONFIG"] = str(config_file.resolve())

    settings = load_settings(config_file=config_file, **overrides)
    set_settings(settings)
    setup_logging(debug=settings.debug)

    # Ensure config directory exists before starting server
    settings.ensure_directories()

    click.echo(f"Starting bookserver on http://{settings.host}:{settings.port}")
    click.echo(f"  Configuration dir : {settings.config_dir}")
    click.echo(f"  Media library dir : {settings.books_dir} (Read-Only)")
    click.echo(f"  Database URL      : {settings.effective_database_url}")

    uvicorn.run(
        "buku.app:create_app",
        host=settings.host,
        port=settings.port,
        reload=reload,
        factory=True,
        log_level="debug" if settings.debug else "info",
    )


@cli.command()
@click.option(
    "--books-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to books media directory [default: /books or config].",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def scan(books_dir: Path | None, config_file: Path | None) -> None:
    """Scan library directories for new or modified books (Read-Only)."""
    overrides: dict[str, Any] = {}
    if books_dir is not None:
        overrides["books_dir"] = books_dir

    settings = load_settings(config_file=config_file, **overrides)
    set_settings(settings)
    setup_logging(debug=settings.debug)

    target_dir = settings.books_dir
    click.echo(f"Scanning library directory: {target_dir}")
    if not target_dir.exists():
        click.echo(
            f"Warning: Media directory '{target_dir}' does not exist.",
            err=True,
        )
        sys.exit(1)

    click.echo("Scanner initialized in read-only mode. Media files will never be modified.")

    from buku.db import get_engine, get_session_factory, run_migrations
    from buku.scanner import LibraryScanner

    settings.ensure_directories()
    run_migrations(settings.effective_database_url)
    engine = get_engine(settings.effective_database_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    try:
        if books_dir is not None:
            stats = scanner.scan_library(books_dir)
        else:
            stats = scanner.scan_all_libraries()
    except Exception as e:
        click.echo(f"Error during scan: {e}", err=True)
        sys.exit(1)

    click.echo("Scan Summary:")
    click.echo(f"  Discovered : {stats.files_discovered}")
    click.echo(f"  Added      : {stats.files_added}")
    click.echo(f"  Updated    : {stats.files_updated}")
    click.echo(f"  Unchanged  : {stats.files_unchanged}")
    click.echo(f"  Moved      : {stats.files_moved}")
    click.echo(f"  Missing    : {stats.files_missing}")
    click.echo(f"  Errors     : {len(stats.errors)}")
    click.echo(f"  Duration   : {stats.duration_seconds:.3f}s")

    if stats.errors:
        click.echo("Warnings / Errors encountered:")
        for err in stats.errors:
            click.echo(f"  - {err}", err=True)


@cli.command()
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def migrate(config_file: Path | None) -> None:
    """Run database schema migrations."""
    settings = load_settings(config_file=config_file)
    set_settings(settings)
    setup_logging(debug=settings.debug)

    settings.ensure_directories()
    click.echo(f"Target database: {settings.effective_database_url}")
    click.echo(f"Applying database migrations to: {settings.effective_database_url}")
    from buku.db import run_migrations

    run_migrations(settings.effective_database_url)
    click.echo("Database migrations applied successfully.")


@cli.command()
@click.option(
    "--book-id",
    type=int,
    default=None,
    help="Enrich a single book by ID (default: drain queued metadata_lookup jobs).",
)
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Maximum number of queued jobs to process in one invocation.",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def enrich(book_id: int | None, limit: int, config_file: Path | None) -> None:
    """Look up missing metadata from external providers (e.g. Google Books).

    Fills only fields that are missing and not user-edited. Without --book-id,
    drains queued ``metadata_lookup`` jobs produced by the scanner.
    """
    settings = load_settings(config_file=config_file)
    set_settings(settings)
    setup_logging(debug=settings.debug)

    settings.ensure_directories()
    from buku.db import get_engine, get_session_factory, run_migrations
    from buku.metadata.enrichment import MetadataEnrichmentService, process_metadata_jobs

    run_migrations(settings.effective_database_url)
    engine = get_engine(settings.effective_database_url)
    factory = get_session_factory(engine)
    service = MetadataEnrichmentService()

    if book_id is not None:
        with factory() as db:
            try:
                result = service.enrich_book(db, book_id)
                db.commit()
            except ValueError as e:
                click.echo(f"Error: {e}", err=True)
                sys.exit(1)
        click.echo(f"Enriched book id={book_id}")
        click.echo(f"  Candidates   : {len(result.candidates)}")
        click.echo(f"  Accepted     : {'yes' if result.accepted else 'no'}")
        click.echo(f"  Fields applied: {', '.join(sorted(result.fields_applied)) or '(none)'}")
        click.echo(f"  Confidence   : {result.accepted.confidence:.2f}" if result.accepted else "")
        return

    with factory() as db:
        stats = process_metadata_jobs(db, service=service, limit=limit)
        db.commit()
    click.echo("Metadata enrichment run summary:")
    click.echo(f"  Jobs seen     : {stats.jobs_seen}")
    click.echo(f"  Jobs completed: {stats.jobs_completed}")
    click.echo(f"  Jobs failed   : {stats.jobs_failed}")
    click.echo(f"  Books enriched: {stats.books_enriched}")
    click.echo(f"  Fields applied: {stats.fields_applied}")


@cli.command()
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def reindex(config_file: Path | None) -> None:
    """Rebuild the SQLite FTS5 full-text search index from the catalog."""
    settings = load_settings(config_file=config_file)
    set_settings(settings)
    setup_logging(debug=settings.debug)

    settings.ensure_directories()
    from buku.db import get_engine, get_session_factory, run_migrations
    from buku.services.search import search_service

    run_migrations(settings.effective_database_url)
    engine = get_engine(settings.effective_database_url)
    factory = get_session_factory(engine)

    with factory() as db:
        count = search_service.rebuild(db)
        db.commit()

    click.echo(f"Reindexed {count} books into the full-text search index.")


@cli.group()
def user() -> None:
    """Manage user accounts."""
    pass


@user.command("create")
@click.option("--username", "-u", required=True, help="Unique username.")
@click.option("--password", "-p", required=True, help="User password.")
@click.option("--display-name", "-d", default=None, help="Display name [default: username].")
@click.option("--admin", is_flag=True, default=False, help="Grant administrator privileges.")
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def user_create(
    username: str,
    password: str,
    display_name: str | None,
    admin: bool,
    config_file: Path | None,
) -> None:
    """Create a new user account."""
    settings = load_settings(config_file=config_file)
    set_settings(settings)
    from buku.db import get_engine, get_session_factory
    from buku.services.auth import auth_service

    engine = get_engine(settings.effective_database_url)
    factory = get_session_factory(engine)
    with factory() as db:
        try:
            u = auth_service.create_user(
                db,
                username=username,
                password=password,
                display_name=display_name or username,
                is_admin=admin,
            )
            click.echo(f"User '{u.username}' (id={u.id}, admin={u.is_admin}) created successfully.")
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


@user.command("list")
@click.option(
    "--config",
    "config_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to TOML configuration file.",
)
def user_list(config_file: Path | None) -> None:
    """List all registered users."""
    settings = load_settings(config_file=config_file)
    set_settings(settings)
    from sqlalchemy import select

    from buku.db import get_engine, get_session_factory
    from buku.models.user import User

    engine = get_engine(settings.effective_database_url)
    factory = get_session_factory(engine)
    with factory() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        if not users:
            click.echo("No users found.")
            return

        click.echo(f"{'ID':<4} {'Username':<20} {'Display Name':<20} {'Admin':<7} {'Active':<7}")
        click.echo("-" * 62)
        for u in users:
            admin_str = str(u.is_admin)
            active_str = str(u.is_active)
            click.echo(
                f"{u.id:<4} {u.username:<20} {u.display_name:<20} {admin_str:<7} {active_str:<7}"
            )


def main() -> None:
    """CLI entrypoint."""
    cli()


if __name__ == "__main__":
    main()
