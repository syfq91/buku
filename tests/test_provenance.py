"""Phase 6 tests: metadata provenance tracking.

Covers the ProvenanceService unit surface plus scanner integration proving the
acceptance criterion "existing user edits remain untouched during future
rescans".
"""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.metadata import (
    SOURCE_EMBEDDED,
    SOURCE_FILENAME,
    SOURCE_GOOGLE_BOOKS,
    SOURCE_USER,
    MetadataMatch,
    MetadataProvider,
    MetadataQuery,
)
from buku.metadata.enrichment import MetadataEnrichmentService
from buku.metadata.provenance import provenance_service
from buku.models import Book, Library
from buku.models.metadata import MetadataProvenance, MetadataSource
from buku.scanner import LibraryScanner
from buku.scanner.handlers import BookMetadata
from tests.fixtures import build_cbz, build_epub


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_provenance.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url)
    set_settings(settings)
    run_migrations(url)
    yield url
    reset_engine()
    set_settings(None)


@pytest.fixture
def factory(db_url: str) -> sessionmaker[Session]:
    return get_session_factory(get_engine(db_url))


# --------------------------------------------------------------------------- #
# ProvenanceService unit surface
# --------------------------------------------------------------------------- #


def test_ensure_sources_idempotent(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        provenance_service.ensure_sources(db)
        db.commit()
        first = {s.name for s in db.scalars(select(MetadataSource)).all()}
        assert first == {
            SOURCE_EMBEDDED,
            SOURCE_GOOGLE_BOOKS,
            SOURCE_USER,
            SOURCE_FILENAME,
        }
        provenance_service.ensure_sources(db)
        db.commit()
        second = {s.name for s in db.scalars(select(MetadataSource)).all()}
        assert second == first


def test_mark_and_get_provenance(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        library = Library(name="lib", path="/tmp/prov-lib")
        db.add(library)
        db.flush()
        book = Book(library_id=library.id, title="Test")
        db.add(book)
        db.flush()
        book_id = book.id

        provenance_service.mark(db, book_id, "subtitle", SOURCE_USER)
        provenance_service.mark(db, book_id, "publisher", SOURCE_EMBEDDED)
        db.commit()

        prov = provenance_service.get_provenance(db, book_id)
        assert prov == {"subtitle": SOURCE_USER, "publisher": SOURCE_EMBEDDED}
        assert provenance_service.user_edited_fields(db, book_id) == {"subtitle"}
        assert provenance_service.is_user_edited(db, book_id, "subtitle") is True
        assert provenance_service.is_user_edited(db, book_id, "publisher") is False

        # Upsert: moving subtitle from user to embedded must keep one row.
        provenance_service.mark(db, book_id, "subtitle", SOURCE_EMBEDDED)
        db.commit()
        rows = db.scalars(
            select(MetadataProvenance).where(MetadataProvenance.book_id == book_id)
        ).all()
        assert len(rows) == 2


# --------------------------------------------------------------------------- #
# Scanner integration: provenance recorded at index time
# --------------------------------------------------------------------------- #


def test_scanner_records_embedded_provenance_for_epub(db_url: str, tmp_path: Path) -> None:
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(
        library_dir / "Dune.epub",
        title="Dune",
        subtitle="The Desert Planet",
        authors=["Frank Herbert"],
        language="en",
        publisher="Chilton Books",
        published_date="1965-08-01",
        identifiers={"isbn": "urn:isbn:9780441013593"},
    )

    engine = get_engine(db_url)
    scanner = LibraryScanner(get_session_factory(engine))
    stats = scanner.scan_library(library_dir)
    assert stats.files_added == 1

    with get_session_factory(engine)() as db:
        book = db.scalar(select(Book))
        assert book is not None
        prov = provenance_service.get_provenance(db, book.id)
        assert prov["title"] == SOURCE_EMBEDDED
        assert prov["subtitle"] == SOURCE_EMBEDDED
        assert prov["authors"] == SOURCE_EMBEDDED
        assert prov["publisher"] == SOURCE_EMBEDDED
        assert prov["published_date"] == SOURCE_EMBEDDED
        assert prov["language"] == SOURCE_EMBEDDED
        assert prov["identifiers"] == SOURCE_EMBEDDED
        assert "user" not in prov.values()


def test_scanner_records_filename_provenance_for_cbz(db_url: str, tmp_path: Path) -> None:
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_cbz(library_dir / "Stan Lee - Spider-Man.cbz", count=2)

    engine = get_engine(db_url)
    scanner = LibraryScanner(get_session_factory(engine))
    stats = scanner.scan_library(library_dir)
    assert stats.files_added == 1

    with get_session_factory(engine)() as db:
        book = db.scalar(select(Book))
        assert book is not None
        prov = provenance_service.get_provenance(db, book.id)
        assert prov.get("title") == SOURCE_FILENAME
        assert prov.get("authors") == SOURCE_FILENAME


def test_epub_titleless_falls_back_to_filename_provenance(db_url: str, tmp_path: Path) -> None:
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(library_dir / "Ghost Story.epub", title=None)

    engine = get_engine(db_url)
    scanner = LibraryScanner(get_session_factory(engine))
    scanner.scan_library(library_dir)

    with get_session_factory(engine)() as db:
        book = db.scalar(select(Book))
        assert book is not None
        assert book.title == "Ghost Story"
        prov = provenance_service.get_provenance(db, book.id)
        assert prov.get("title") == SOURCE_FILENAME


# --------------------------------------------------------------------------- #
# Acceptance criterion: user edits survive future rescans
# --------------------------------------------------------------------------- #


def test_user_edit_survives_rescan_after_content_change(db_url: str, tmp_path: Path) -> None:
    """Phase 6 acceptance: a modified file rescan must not clobber user edits."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    book_file = build_epub(
        library_dir / "Dune.epub",
        title="Dune",
        publisher="Chilton Books",
        chapter_text="original",
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)
    scanner.scan_library(library_dir)

    with factory() as db:
        book = db.scalar(select(Book))
        assert book is not None
        book_id = book.id
        # A user fixes the publisher.
        provenance_service.mark(db, book_id, "publisher", SOURCE_USER)
        book.publisher = "User's Edition"
        db.commit()

    # Modify the file so the rescan re-processes it (new hash, new mtime).
    modified = build_epub(
        book_file,
        title="Dune",
        publisher="A New Embedded Publisher",
        chapter_text="totally different, much longer chapter body now",
    )
    new_mtime = datetime.now(UTC).timestamp() + 10
    os.utime(modified, (new_mtime, new_mtime))

    stats = scanner.scan_library(library_dir)
    assert stats.files_updated == 1

    with factory() as db:
        book = db.get(Book, book_id)
        assert book is not None
        assert book.publisher == "User's Edition"  # never clobbered
        prov = provenance_service.get_provenance(db, book_id)
        assert prov["publisher"] == SOURCE_USER


class ReplayProvider(MetadataProvider):
    """A stub provider replaying the given matches to the enrichment service."""

    name = "replay"

    def __init__(self, matches: list[MetadataMatch]) -> None:
        self.matches = matches

    def search(self, query: MetadataQuery) -> list[MetadataMatch]:
        return list(self.matches)


def test_user_edit_survives_repeated_enrichment(db_url: str, tmp_path: Path) -> None:
    """Enrichment must skip user-edited fields even when new candidates appear."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(
        library_dir / "Dune.epub",
        title="Dune",
        subtitle="The Desert Planet",
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)
    scanner.scan_library(library_dir)

    with factory() as db:
        book = db.scalar(select(Book))
        assert book is not None
        book_id = book.id
        provenance_service.mark(db, book_id, "subtitle", SOURCE_USER)
        book.subtitle = "User Subtitle"
        db.commit()

    candidate = BookMetadata(
        title="Dune",
        subtitle="Provider Subtitle",
        authors=["Frank Herbert"],
        sources={"title": SOURCE_GOOGLE_BOOKS, "subtitle": SOURCE_GOOGLE_BOOKS},
    )
    match = MetadataMatch(
        provider="replay",
        external_id="ext-1",
        confidence=0.95,
        metadata=candidate,
    )
    service = MetadataEnrichmentService(providers=[ReplayProvider([match])])

    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert "subtitle" not in result.fields_applied
    with factory() as db:
        book = db.get(Book, book_id)
        assert book is not None
        assert book.subtitle == "User Subtitle"
        assert provenance_service.get_provenance(db, book_id)["subtitle"] == SOURCE_USER
