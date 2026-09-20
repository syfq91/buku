"""Phase 6 tests: metadata providers, matching, and enrichment.

Enrichment tests use a fake provider (no network); the Google Books parsing is
exercised through an injected ``http_get``. These tests mirror the scanner test
harness: an isolated SQLite database with migrations applied.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.metadata import (
    DEFAULT_MIN_CONFIDENCE,
    SOURCE_GOOGLE_BOOKS,
    GoogleBooksProvider,
    MetadataEnrichmentService,
    MetadataMatch,
    MetadataProvider,
    MetadataQuery,
    normalize_isbn,
    score_match,
)
from buku.metadata.enrichment import process_metadata_jobs
from buku.metadata.google_books import google_books_query_strings
from buku.metadata.provenance import provenance_service
from buku.models import Book, BookIdentifier, Job, Library, Series
from buku.models.metadata import MetadataMatch as MetadataMatchRecord
from buku.models.metadata import MetadataSource


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_metadata.db"
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


def make_book(
    session: Session,
    *,
    title: str,
    subtitle: str | None = None,
    isbn: str | None = None,
    library_path: str = "/tmp/test-lib",
    publisher: str | None = None,
) -> Book:
    """Create a Book row (with optional ISBN identifier) in the session."""
    library = session.scalar(select(Library).where(Library.path == library_path))
    if library is None:
        library = Library(name="test", path=library_path)
        session.add(library)
        session.flush()
    book = Book(
        library_id=library.id,
        title=title,
        subtitle=subtitle,
        publisher=publisher,
    )
    session.add(book)
    session.flush()
    if isbn:
        session.add(BookIdentifier(book_id=book.id, identifier_type="isbn", identifier_value=isbn))
    session.flush()
    return book


class FakeProvider(MetadataProvider):
    """Provider returning canned matches, exercising no network."""

    name = "fake"

    def __init__(self, matches: list[MetadataMatch]) -> None:
        self.matches = matches

    def search(self, query: MetadataQuery) -> list[MetadataMatch]:
        return list(self.matches)


def sample_match(
    *,
    overrides: dict[str, Any] | None = None,
    isbn: str = "9780441013593",
    confidence: float | None = None,
) -> MetadataMatch:
    """Build a canonical Google-Books-like match for Dune."""
    volume_info = {
        "title": "Dune",
        "subtitle": "The Desert Planet",
        "authors": ["Frank Herbert"],
        "description": "A desert planet saga about spice.",
        "publisher": "Chilton Books",
        "publishedDate": "1965-08-01",
        "language": "en",
        "pageCount": 412,
        "industryIdentifiers": [
            {"type": "ISBN_13", "identifier": isbn},
            {"type": "ISBN_10", "identifier": "0441013597"},
        ],
    }
    if overrides:
        volume_info.update(overrides)
    item = {"id": "abc123def", "volumeInfo": volume_info}
    provider = GoogleBooksProvider(http_get=lambda url, timeout: b"")
    parsed = provider._parse_item(item)
    assert parsed is not None
    if confidence is None:
        confidence = 0.95
    return MetadataMatch(
        provider="google_books",
        external_id="abc123def",
        confidence=confidence,
        metadata=parsed,
        raw=item,
    )


# --------------------------------------------------------------------------- #
# Matching priority flow & confidence scoring
# --------------------------------------------------------------------------- #


def test_query_strings_priority_order() -> None:
    query = MetadataQuery(
        title="Dune",
        authors=["Frank Herbert"],
        identifiers={"isbn": "9780441013593"},
    )
    strings = google_books_query_strings(query)
    assert strings[0] == "isbn:9780441013593"
    assert "isbn:9780441013593 intitle:" in strings[1]
    assert 'intitle:"Dune" inauthor:"Frank Herbert"' in strings[2]
    assert 'intitle:"Dune"' in strings[3]

    assert google_books_query_strings(MetadataQuery(identifiers={"isbn": "x"})) == ["isbn:x"]
    assert google_books_query_strings(MetadataQuery(title="Solo")) == ['intitle:"Solo"']


def test_normalize_isbn() -> None:
    assert normalize_isbn("978-0-4410-1359-3") == "9780441013593"
    assert normalize_isbn(None) is None


def test_score_match_isbn_confirmed_and_conflicting() -> None:
    query = MetadataQuery(identifiers={"isbn": "9780441013593"})
    confirmed = sample_match()
    assert score_match(query, confirmed.metadata) == pytest.approx(0.95)

    conflicting = sample_match(
        overrides={"industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781234567897"}]}
    )
    assert score_match(query, conflicting.metadata) < 0.2


def test_score_match_title_author_vs_title_only() -> None:
    query = MetadataQuery(title="Dune", authors=["Frank Herbert"])
    match = sample_match(confidence=0.0)
    assert score_match(query, match.metadata) == pytest.approx(0.85)

    bare = MetadataQuery(title="Dune")
    assert score_match(bare, match.metadata) == pytest.approx(0.60)
    assert DEFAULT_MIN_CONFIDENCE > 0.60  # bare title never auto-applies


# --------------------------------------------------------------------------- #
# GoogleBooksProvider parsing
# --------------------------------------------------------------------------- #


def test_google_books_isbn_search_and_parse(db_url: str, factory: sessionmaker[Session]) -> None:
    captured: dict[str, str] = {}
    payload = json.dumps({"totalItems": 1, "items": [sample_match(confidence=0.95).raw]})
    provider = GoogleBooksProvider(
        http_get=lambda url, timeout: captured.update(url=url) or payload.encode()
    )

    query = MetadataQuery(identifiers={"isbn": "9780441013593"})
    matches = provider.search(query)

    assert "q=isbn%3A9780441013593" in captured["url"]
    assert len(matches) == 1
    match = matches[0]
    assert match.provider == "google_books"
    assert match.external_id == "abc123def"
    assert match.metadata.title == "Dune"
    assert match.metadata.authors == ["Frank Herbert"]
    assert match.metadata.identifiers["isbn"] == "9780441013593"
    assert match.metadata.sources["title"] == SOURCE_GOOGLE_BOOKS
    assert match.confidence == pytest.approx(0.95)


def test_google_books_falls_through_to_next_tier_when_empty(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    def fake_http(url: str, timeout: float) -> bytes:
        query_param = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        if query_param.startswith("isbn:") and "intitle" not in query_param:
            return b'{"totalItems": 0}'
        return json.dumps({"totalItems": 1, "items": [sample_match(confidence=0.95).raw]}).encode()

    provider = GoogleBooksProvider(http_get=fake_http)
    matches = provider.search(
        MetadataQuery(
            title="Dune",
            authors=["Frank Herbert"],
            identifiers={"isbn": "9780441013593"},
        )
    )
    assert len(matches) == 1
    assert matches[0].metadata.title == "Dune"


def test_google_books_network_error_degrades_to_empty(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    def failing_http(url: str, timeout: float) -> bytes:
        raise OSError("connection refused")

    provider = GoogleBooksProvider(http_get=failing_http)
    assert provider.search(MetadataQuery(title="Lost Book")) == []


# --------------------------------------------------------------------------- #
# Enrichment service
# --------------------------------------------------------------------------- #


def test_enrichment_populates_missing_fields(db_url: str, factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", isbn="9780441013593", library_path="/tmp/lib-a")
        book_id = book.id
        db.commit()

    service = MetadataEnrichmentService(providers=[FakeProvider([sample_match()])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert result.accepted is not None
    applied = set(result.fields_applied)
    assert applied >= {
        "subtitle",
        "description",
        "publisher",
        "published_date",
        "language",
        "authors",
    }

    with factory() as db:
        loaded = db.get(Book, book_id)
        assert loaded is not None
        assert loaded.subtitle == "The Desert Planet"
        assert loaded.description == "A desert planet saga about spice."
        assert loaded.publisher == "Chilton Books"
        assert loaded.published_date == "1965-08-01"
        assert loaded.language == "en"

        author_names = [link.author.name for link in loaded.author_links]
        assert author_names == ["Frank Herbert"]

        provenance = provenance_service.get_provenance(db, book_id)
        assert provenance["subtitle"] == SOURCE_GOOGLE_BOOKS
        assert provenance["publisher"] == SOURCE_GOOGLE_BOOKS

        record = db.scalar(
            select(MetadataMatchRecord).where(MetadataMatchRecord.book_id == book_id)
        )
        assert record is not None
        assert record.status == "applied"
        assert record.external_id == "abc123def"
        source = db.get(MetadataSource, record.source_id)
        assert source is not None and source.name == SOURCE_GOOGLE_BOOKS


def test_enrichment_never_overwrites_user_edits(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    with factory() as db:
        provenance_service.ensure_sources(db)
        book = make_book(
            db,
            title="Dune",
            isbn="9780441013593",
            subtitle="My Edited Title",
        )
        provenance_service.mark(db, book.id, "subtitle", "user")
        provenance_service.mark(db, book.id, "publisher", "user")
        book_id = book.id
        db.commit()

    good_match = sample_match()  # offers subtitle/publisher, different values
    service = MetadataEnrichmentService(providers=[FakeProvider([good_match])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert "subtitle" not in result.fields_applied
    assert "publisher" not in result.fields_applied
    assert "description" in result.fields_applied

    with factory() as db:
        loaded = db.get(Book, book_id)
        assert loaded is not None
        assert loaded.subtitle == "My Edited Title"  # user edit untouched
        assert loaded.publisher is None  # user-provenanced, still missing -> untouched
        assert loaded.description == "A desert planet saga about spice."


def test_enrichment_never_overwrites_existing_embedded_fields(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    with factory() as db:
        book = make_book(
            db,
            title="Dune",
            isbn="9780441013593",
            subtitle="Known Subtitle",
            publisher="Original Publisher",
        )
        provenance_service.mark_many(db, book.id, {"subtitle": "embedded", "publisher": "embedded"})
        book_id = book.id
        db.commit()

    service = MetadataEnrichmentService(providers=[FakeProvider([sample_match()])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert "subtitle" not in result.fields_applied
    assert "publisher" not in result.fields_applied

    with factory() as db:
        loaded = db.get(Book, book_id)
        assert loaded is not None
        assert loaded.subtitle == "Known Subtitle"
        assert loaded.publisher == "Original Publisher"


def test_enrichment_rejects_low_confidence_match(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    with factory() as db:
        book = make_book(db, title="Some Book", isbn="9780441013593", library_path="/tmp/lib-c")
        book_id = book.id
        db.commit()

    weak = sample_match(confidence=0.05)  # below threshold
    service = MetadataEnrichmentService(providers=[FakeProvider([weak])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert result.accepted is None
    assert result.fields_applied == []
    with factory() as db:
        loaded = db.get(Book, book_id)
        assert loaded is not None
        assert loaded.subtitle is None  # nothing applied
        record = db.scalar(
            select(MetadataMatchRecord).where(MetadataMatchRecord.book_id == book_id)
        )
        assert record is not None
        assert record.status == "pending"


def test_enrichment_applies_series_when_missing(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    with factory() as db:
        book = make_book(db, title="Foundation", isbn="9780553293357", library_path="/tmp/lib-d")
        book_id = book.id
        db.commit()

    match = sample_match(
        overrides={
            "title": "Foundation",
            "authors": ["Isaac Asimov"],
            "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780553293357"}],
        }
    )
    match.metadata.series = "Foundation Series"
    match.metadata.series_index = 1.0
    match.metadata.sources["series"] = SOURCE_GOOGLE_BOOKS

    service = MetadataEnrichmentService(providers=[FakeProvider([match])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert "series" in result.fields_applied
    with factory() as db:
        loaded = db.get(Book, book_id)
        assert loaded is not None
        series = db.get(Series, loaded.series_id)
        assert series is not None
        assert series.name == "Foundation Series"
        assert loaded.series_index == 1.0


def test_enrichment_missing_book_raises(db_url: str, factory: sessionmaker[Session]) -> None:
    service = MetadataEnrichmentService(providers=[])
    with factory() as db:
        with pytest.raises(ValueError, match="not found"):
            service.enrich_book(db, 99999)


# --------------------------------------------------------------------------- #
# Job drain
# --------------------------------------------------------------------------- #


def test_process_metadata_jobs_drains_queued(db_url: str, factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", isbn="9780441013593", library_path="/tmp/lib-e")
        book_id = book.id
        db.add(
            Job(
                id="job-1",
                type="metadata_lookup",
                status="queued",
                payload=json.dumps({"book_id": book_id}),
                attempts=0,
                max_attempts=3,
            )
        )
        db.commit()

    service = MetadataEnrichmentService(providers=[FakeProvider([sample_match()])])
    with factory() as db:
        stats = process_metadata_jobs(db, service=service, limit=10)
        db.commit()

    assert stats.jobs_seen == 1
    assert stats.jobs_completed == 1
    assert stats.jobs_failed == 0
    assert stats.books_enriched == 1
    assert stats.fields_applied > 0

    with factory() as db:
        job = db.get(Job, "job-1")
        assert job is not None
        assert job.status == "completed"
        assert job.attempts == 1


def test_process_metadata_jobs_marks_failed_after_max_attempts(
    db_url: str, factory: sessionmaker[Session]
) -> None:
    with factory() as db:
        db.add(
            Job(
                id="job-bad",
                type="metadata_lookup",
                status="queued",
                payload='{"book_id": 424242}',
                attempts=2,
                max_attempts=3,
            )
        )
        db.commit()

    service = MetadataEnrichmentService(providers=[])
    with factory() as db:
        stats = process_metadata_jobs(db, service=service, limit=10)
        db.commit()

    assert stats.jobs_failed == 1
    with factory() as db:
        job = db.get(Job, "job-bad")
        assert job is not None
        assert job.status == "failed"
        assert "not found" in (job.error or "")
