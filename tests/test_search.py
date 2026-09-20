"""Phase 8 tests: SQLite FTS5 full-text search.

Covers the SearchService index lifecycle (index_book / remove_book / rebuild),
query ranking and pagination, the ``/api/v1/search`` endpoint (auth + results),
the ``/search`` web UI placeholder page, and the index-sync hooks wired into
the scanner, metadata enrichment, and the Phase 7 review flow — proving that
search stays correct without any external search engine.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.metadata.enrichment import MetadataEnrichmentService
from buku.metadata.google_books import GoogleBooksProvider
from buku.metadata.models import BookMetadata, MetadataMatch, MetadataQuery
from buku.metadata.provenance import provenance_service
from buku.metadata.provider import MetadataProvider
from buku.models import Author, Book, BookAuthor, BookIdentifier, Library, Series
from buku.models.metadata import MetadataMatch as MetadataMatchRecord
from buku.scanner import LibraryScanner
from buku.services.auth import auth_service
from buku.services.metadata_review import review_service
from buku.services.search import search_service
from tests.fixtures import build_epub

SearchEnv = tuple[TestClient, sessionmaker[Session], str]


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_search.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url)
    set_settings(settings)
    run_migrations(url)
    yield url
    reset_engine()
    set_settings(None)


@pytest.fixture
def factory(db_url: str) -> sessionmaker[Session]:
    """Provide a session factory bound to the migrated test database."""
    return get_session_factory(get_engine(db_url))


@pytest.fixture
def search_env(tmp_path: Path) -> Generator[SearchEnv]:
    """Set up a migrated DB, a user, and an authenticated API client."""
    reset_engine()
    database_file = tmp_path / "search_http.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url)
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory_cls = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        with factory_cls() as db:
            auth_service.create_user(db, "reader", "readerpass123", "Reader", is_admin=False)
        login = client.post("/login", json={"username": "reader", "password": "readerpass123"})
        assert login.status_code == 200
        token = login.json()["token"]
        yield client, factory_cls, token
    reset_engine()
    set_settings(None)


def auth_headers(token: str) -> dict[str, str]:
    """Build an Authorization header from a session token."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
def make_book(
    session: Session,
    *,
    title: str = "Dune",
    subtitle: str | None = None,
    authors: list[str] | None = None,
    series: str | None = None,
    series_index: float | None = None,
    isbn: str | None = None,
    description: str | None = None,
    publisher: str | None = None,
) -> Book:
    """Create a fully-related Book row (library, authors, series, ISBN)."""
    library = session.scalar(select(Library).where(Library.path == "/tmp/search-lib"))
    if library is None:
        library = Library(name="search", path="/tmp/search-lib")
        session.add(library)
        session.flush()

    series_id = None
    if series:
        series_row = session.scalar(select(Series).where(Series.name == series))
        if series_row is None:
            series_row = Series(name=series)
            session.add(series_row)
            session.flush()
        series_id = series_row.id

    book = Book(
        library_id=library.id,
        title=title,
        subtitle=subtitle,
        description=description,
        publisher=publisher,
        series_id=series_id,
        series_index=series_index,
    )
    session.add(book)
    session.flush()

    for name in authors or []:
        author = session.scalar(select(Author).where(Author.name == name))
        if author is None:
            author = Author(name=name)
            session.add(author)
            session.flush()
        session.add(BookAuthor(book_id=book.id, author_id=author.id, role="author"))

    if isbn:
        session.add(BookIdentifier(book_id=book.id, identifier_type="isbn", identifier_value=isbn))
    session.flush()
    return book


def dune_metadata() -> BookMetadata:
    """A canonical Google-Books-like metadata payload for Dune."""
    return BookMetadata(
        title="Dune",
        subtitle="The Desert Planet",
        authors=["Frank Herbert"],
        description="A desert planet saga about spice.",
        publisher="Chilton Books",
        published_date="1965-08-01",
        language="en",
        identifiers={"isbn": "9780441013593"},
    )


class FakeProvider(MetadataProvider):
    """Provider returning canned matches, exercising no network."""

    name = "fake"

    def __init__(self, matches: list[MetadataMatch]) -> None:
        self.matches = matches

    def search(self, query: MetadataQuery) -> list[MetadataMatch]:
        return list(self.matches)


def dune_volume(*, overrides: dict[str, object] | None = None) -> dict[str, object]:
    """A Google Books item payload for Dune."""
    volume_info: dict[str, object] = {
        "title": "Dune",
        "subtitle": "The Desert Planet",
        "authors": ["Frank Herbert"],
        "description": "A desert planet saga about spice.",
        "publisher": "Chilton Books",
        "publishedDate": "1965-08-01",
        "language": "en",
        "pageCount": 412,
        "industryIdentifiers": [
            {"type": "ISBN_13", "identifier": "9780441013593"},
            {"type": "ISBN_10", "identifier": "0441013597"},
        ],
    }
    if overrides:
        volume_info.update(overrides)
    return {"id": "abc123def", "volumeInfo": volume_info}


def seed_match(session: Session, book_id: int) -> MetadataMatchRecord:
    """Insert a pending metadata_matches row exactly as enrichment persists them."""
    source = provenance_service.get_or_create_source(session, "google_books")
    item = dune_volume()
    parsed = GoogleBooksProvider._parse_item(item)
    assert parsed is not None
    payload = json.dumps(
        {
            "provider": "google_books",
            "external_id": "abc123def",
            "confidence": 0.99,
            "fields": sorted(parsed.sources.keys()),
            "raw": item,
        },
        default=str,
    )
    row = MetadataMatchRecord(
        book_id=book_id,
        source_id=source.id,
        external_id="abc123def",
        confidence_score=0.99,
        match_data=payload,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Service: index lifecycle & querying
# --------------------------------------------------------------------------- #
def test_index_book_and_search_by_title(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        book_id = book.id
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        results = search_service.search(db, "Dune")
        assert results.total == 1
        assert results.query == "Dune"
        assert results.items[0].book_id == book_id
        assert results.items[0].title == "Dune"
        assert results.items[0].rank is not None


def test_search_prefix_matching(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        results = search_service.search(db, "dun")
        assert results.total == 1
        assert results.items[0].book_id == book.id


def test_search_by_author_series_isbn_subtitle_publisher(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(
            db,
            title="Dune",
            subtitle="The Desert Planet",
            authors=["Frank Herbert"],
            series="Dune Saga",
            isbn="9780441013593",
            publisher="Chilton Books",
            description="A desert planet saga about spice.",
        )
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        assert search_service.search(db, "Herbert").total == 1
        assert search_service.search(db, "Dune Saga").total == 1
        assert search_service.search(db, "9780441013593").total == 1
        assert search_service.search(db, "Desert Planet").total == 1
        assert search_service.search(db, "Chilton").total == 1
        assert search_service.search(db, "spice").total == 1
        result = search_service.search(db, "Herbert").items[0]
        assert result.authors == ["Frank Herbert"]
        assert result.series == "Dune Saga"


def test_search_multiple_terms_are_and_combined(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        dune = make_book(db, title="Dune", authors=["Frank Herbert"])
        search_service.index_book(db, dune)
        other = make_book(db, title="Dune Field Guide", authors=["Anita Smith"])
        search_service.index_book(db, other)
        db.commit()

    with factory() as db:
        results = search_service.search(db, "Dune Herbert")
        assert results.total == 1
        assert results.items[0].book_id == dune.id


def test_search_pagination(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book_ids = []
        for index in range(5):
            book = make_book(db, title=f"Volume {index}", publisher="Common Publisher")
            book_ids.append(book.id)
            search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        first = search_service.search(db, "Common Publisher", limit=2, offset=0)
        assert first.total == 5
        assert [item.book_id for item in first.items] == book_ids[:2]

        second = search_service.search(db, "Common Publisher", limit=2, offset=2)
        assert [item.book_id for item in second.items] == book_ids[2:4]

        last = search_service.search(db, "Common Publisher", limit=2, offset=4)
        assert [item.book_id for item in last.items] == book_ids[4:]


def test_blank_query_returns_nothing(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        empty = search_service.search(db, "")
        assert empty.total == 0
        assert empty.items == []
        whitespace = search_service.search(db, "   ")
        assert whitespace.total == 0
        punctuation = search_service.search(db, "!!! ???")
        assert punctuation.total == 0


def test_search_no_results(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        results = search_service.search(db, "zzzzzznope")
        assert results.total == 0
        assert results.items == []


def test_rebuild_reindexes_whole_catalog(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        search_service.index_book(db, book)
        other = make_book(db, title="Foundation", authors=["Isaac Asimov"])
        search_service.index_book(db, other)
        db.commit()

    # Bypass the sync hook: mutate metadata directly, then rebuild.
    with factory() as db:
        loaded = db.get(Book, other.id)
        assert loaded is not None
        loaded.title = "Foundation Series Rewritten"
        db.commit()

    with factory() as db:
        assert search_service.search(db, "Foundation Series Rewritten").total == 0

    with factory() as db:
        count = search_service.rebuild(db)
        assert count == 2
        db.commit()

    with factory() as db:
        results = search_service.search(db, "Foundation Series Rewritten")
        assert results.total == 1
        assert search_service.search(db, "Dune").total == 1


def test_remove_book_drops_index_row(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        book_id = book.id
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        assert search_service.search(db, "Dune").total == 1
        search_service.remove_book(db, book_id)
        db.commit()

    with factory() as db:
        assert search_service.search(db, "Dune").total == 0


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
def test_search_endpoint_requires_auth(search_env: SearchEnv) -> None:
    client, _factory, _token = search_env
    client.cookies.clear()
    response = client.get("/api/v1/search", params={"q": "Dune"})
    assert response.status_code == 401


def test_search_endpoint_returns_books(search_env: SearchEnv) -> None:
    client, factory, token = search_env
    with factory() as db:
        book = make_book(db, title="Dune", authors=["Frank Herbert"], isbn="9780441013593")
        search_service.index_book(db, book)
        db.commit()

    response = client.get("/api/v1/search", params={"q": "Dune"}, headers=auth_headers(token))
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "Dune"
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["book_id"] == book.id
    assert item["title"] == "Dune"
    assert item["authors"] == ["Frank Herbert"]


def test_search_endpoint_pagination(search_env: SearchEnv) -> None:
    client, factory, token = search_env
    with factory() as db:
        for index in range(3):
            book = make_book(db, title=f"Topic {index}")
            search_service.index_book(db, book)
        db.commit()

    first = client.get(
        "/api/v1/search",
        params={"q": "Topic", "limit": 2, "offset": 0},
        headers=auth_headers(token),
    )
    assert first.status_code == 200
    assert first.json()["total"] == 3
    assert len(first.json()["items"]) == 2

    second = client.get(
        "/api/v1/search",
        params={"q": "Topic", "limit": 2, "offset": 2},
        headers=auth_headers(token),
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1


def test_search_endpoint_rejects_oversized_limit(search_env: SearchEnv) -> None:
    client, _factory, token = search_env
    response = client.get(
        "/api/v1/search", params={"q": "Dune", "limit": 101}, headers=auth_headers(token)
    )
    assert response.status_code == 422


def test_search_page_requires_auth(search_env: SearchEnv) -> None:
    client, _factory, _token = search_env
    client.cookies.clear()
    response = client.get("/search", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_search_page_renders_html(search_env: SearchEnv) -> None:
    client, _factory, token = search_env
    response = client.get("/search", headers=auth_headers(token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Search" in response.text
    assert "/search/results" in response.text


# --------------------------------------------------------------------------- #
# Index sync hooks across the catalog lifecycle
# --------------------------------------------------------------------------- #
def test_scanner_indexes_new_book(factory: sessionmaker[Session], tmp_path: Path) -> None:
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(
        library_dir / "Frank Herbert - Dune.epub",
        title="Dune",
        authors=["Frank Herbert"],
        description="A desert planet saga about spice.",
    )

    scanner = LibraryScanner(factory)
    stats = scanner.scan_library(library_dir)
    assert stats.files_added == 1

    with factory() as db:
        db.scalars(select(Book)).one()
        assert search_service.search(db, "Dune").total == 1
        assert search_service.search(db, "Herbert").total == 1
        assert search_service.search(db, "spice").total == 1
        assert search_service.search(db, "zzz").total == 0


def test_enrichment_reindexes_applied_fields(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", isbn="9780441013593")
        book_id = book.id
        db.commit()

    match = MetadataMatch(
        provider="fake",
        external_id="fake-1",
        confidence=0.99,
        metadata=dune_metadata(),
        raw={},
    )
    service = MetadataEnrichmentService(providers=[FakeProvider([match])])
    with factory() as db:
        result = service.enrich_book(db, book_id)
        db.commit()

    assert result.accepted is not None
    assert "subtitle" in result.fields_applied

    with factory() as db:
        results = search_service.search(db, "The Desert Planet")
        assert results.total == 1
        assert results.items[0].book_id == book_id
        assert search_service.search(db, "Chilton").total == 1
        assert search_service.search(db, "spice").total == 1


def test_review_apply_missing_reindexes(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        book_id = book.id
        candidate = seed_match(db, book_id)
        db.commit()

    with factory() as db:
        result = review_service.apply_missing(db, book_id, candidate.id)
        assert "publisher" in result.fields_applied

    with factory() as db:
        assert search_service.search(db, "Chilton Books").total == 1
        assert search_service.search(db, "The Desert Planet").total == 1


def test_review_manual_edit_reindexes(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        book_id = book.id
        search_service.index_book(db, book)
        db.commit()

    with factory() as db:
        review_service.update_metadata(
            db, book_id, {"title": "Sandworm Chronicles", "publisher": "Ace Books"}
        )

    with factory() as db:
        results = search_service.search(db, "Sandworm")
        assert results.total == 1
        assert results.items[0].title == "Sandworm Chronicles"
        assert search_service.search(db, "Ace").total == 1
        # The re-index replaced the old title; "Dune" no longer matches.
        assert search_service.search(db, "Dune").total == 0
