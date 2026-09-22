"""Phase 7 tests: admin metadata review & curation.

Covers the MetadataReviewService and the /api/v1/admin/metadata endpoints:
book → matches → review → apply missing / selected fields / manual edit /
reject, with the invariant that user-edited fields are never blind-overwritten.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.metadata.google_books import GoogleBooksProvider
from buku.metadata.provenance import provenance_service
from buku.models import Book, BookIdentifier, Library, User
from buku.models.metadata import MetadataMatch as MetadataMatchRecord
from buku.services.auth import auth_service
from buku.services.metadata_review import review_service


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_review.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url, jobs_enabled=False)
    set_settings(settings)
    run_migrations(url)
    yield url
    reset_engine()
    set_settings(None)


@pytest.fixture
def factory(db_url: str) -> sessionmaker[Session]:
    return get_session_factory(get_engine(db_url))


@pytest.fixture
def review_env(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session], str]]:
    """Set up a migrated DB, two users, and an authenticated admin client."""
    reset_engine()
    database_file = tmp_path / "review_http.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url, jobs_enabled=False)
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        with factory() as db:
            auth_service.create_user(db, "admin", "adminpass123", "Admin", is_admin=True)
            auth_service.create_user(db, "reader", "readerpass123", "Reader", is_admin=False)
        login = client.post("/login", json={"username": "admin", "password": "adminpass123"})
        assert login.status_code == 200
        token = login.json()["token"]
        yield client, factory, token
    reset_engine()
    set_settings(None)


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
def make_book(session: Session, *, title: str = "Dune", subtitle: str | None = None) -> Book:
    """Create a Book row (and its library) in the session."""
    library = session.scalar(select(Library).where(Library.path == "/tmp/review-lib"))
    if library is None:
        library = Library(name="review", path="/tmp/review-lib")
        session.add(library)
        session.flush()
    book = Book(library_id=library.id, title=title, subtitle=subtitle)
    session.add(book)
    session.flush()
    session.add(
        BookIdentifier(book_id=book.id, identifier_type="isbn", identifier_value="9780441013593")
    )
    session.flush()
    return book


def dune_volume(*, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """A Google Books item payload for Dune."""
    volume_info: dict[str, Any] = {
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


def seed_candidate(
    session: Session,
    book_id: int,
    *,
    external_id: str = "abc123def",
    confidence: float = 0.95,
    status: str = "pending",
    overrides: dict[str, Any] | None = None,
) -> MetadataMatchRecord:
    """Insert a metadata_matches row exactly as enrichment persists them."""
    source = provenance_service.get_or_create_source(session, "google_books")
    item = dune_volume(overrides=overrides)
    parsed = GoogleBooksProvider._parse_item(item)
    assert parsed is not None
    payload = json.dumps(
        {
            "provider": "google_books",
            "external_id": external_id,
            "confidence": confidence,
            "fields": sorted(parsed.sources.keys()),
            "raw": item,
        },
        default=str,
    )
    row = MetadataMatchRecord(
        book_id=book_id,
        source_id=source.id,
        external_id=external_id,
        confidence_score=confidence,
        match_data=payload,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Service tests
# --------------------------------------------------------------------------- #
def test_review_list_groups_books_with_pending_matches(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        first = make_book(db, title="Dune")
        seed_candidate(db, first.id)
        second = make_book(db, title="Foundation")
        seed_candidate(db, second.id, external_id="gb-foundation")
        db.commit()

    with factory() as db:
        total, items = review_service.list_review_items(db, status="pending")
        assert total == 2
        titles = {item.title for item in items}
        assert titles == {"Dune", "Foundation"}
        for item in items:
            assert item.pending == 1
            assert item.applied == 0


def test_review_list_filters_by_status(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        candidate.status = "rejected"
        db.commit()

    with factory() as db:
        total, items = review_service.list_review_items(db, status="rejected")
        assert total == 1
        assert items[0].book_id == book.id
        assert items[0].rejected == 1

        total_pending, _ = review_service.list_review_items(db, status="pending")
        assert total_pending == 0


def test_review_list_rejects_unknown_status(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        with pytest.raises(ValueError, match="Invalid match status"):
            review_service.list_review_items(db, status="banana")


def test_review_detail_shows_current_and_candidate_metadata(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="Extracted subtitle")
        provenance_service.mark(db, book.id, "subtitle", "embedded")
        seed_candidate(db, book.id)
        db.commit()

    with factory() as db:
        detail = review_service.get_book_review(db, book.id)
        assert detail.book.title == "Dune"
        assert detail.book.subtitle == "Extracted subtitle"
        assert detail.book.authors == []
        assert detail.provenance["subtitle"] == "embedded"
        assert len(detail.candidates) == 1
        candidate = detail.candidates[0]
        assert candidate.provider == "google_books"
        assert candidate.status == "pending"
        assert candidate.metadata is not None
        assert candidate.metadata.subtitle == "The Desert Planet"
        assert "subtitle" in candidate.offered_fields
        assert "description" in candidate.offered_fields


def test_apply_missing_fills_fields_but_skips_user_edits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        provenance_service.mark(db, book.id, "publisher", "user")
        db.commit()

    with factory() as db:
        result = review_service.apply_missing(db, book.id, candidate.id)
        db.commit()

    with factory() as db:
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.subtitle == "The Desert Planet"
        assert fresh.description is not None
        assert fresh.publisher is None  # user-edited → untouched
        assert [link.author.name for link in fresh.author_links] == ["Frank Herbert"]
        provenance = provenance_service.get_provenance(db, book.id)
        assert "subtitle" in result.fields_applied
        assert "publisher" not in result.fields_applied
        assert provenance["subtitle"] == "google_books"
        assert provenance["publisher"] == "user"
        # The applied match row is marked "applied".
        row = db.scalar(select(MetadataMatchRecord).where(MetadataMatchRecord.book_id == book.id))
        assert row is not None
        assert row.status == "applied"


def test_apply_missing_missing_book_and_match_raise(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        make_book(db, title="Dune")
        db.commit()
    with factory() as db:
        with pytest.raises(ValueError, match="Book not found"):
            review_service.apply_missing(db, 999, 1)
        with pytest.raises(ValueError, match="Metadata match not found"):
            review_service.apply_missing(db, 1, 999)


def test_apply_selected_fields_overwrites_current_values(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="Old subtitle")
        candidate = seed_candidate(db, book.id)
        db.commit()

    with factory() as db:
        result = review_service.apply_fields(db, book.id, candidate.id, ["subtitle", "publisher"])
        db.commit()
        assert result.fields_applied == ["subtitle", "publisher"]

        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.subtitle == "The Desert Planet"
        assert fresh.publisher == "Chilton Books"


def test_apply_selected_fields_never_overwrites_user_edits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="User kept title")
        candidate = seed_candidate(db, book.id)
        provenance_service.mark(db, book.id, "subtitle", "user")
        db.commit()

    with factory() as db:
        result = review_service.apply_fields(db, book.id, candidate.id, ["subtitle"])
        db.commit()

        assert result.fields_applied == []
        assert result.fields_skipped == ["subtitle"]
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.subtitle == "User kept title"


def test_apply_selected_fields_rejects_unknown_field(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        db.commit()

    with factory() as db:
        with pytest.raises(ValueError, match="Unknown metadata fields"):
            review_service.apply_fields(db, book.id, candidate.id, ["banana"])


def test_reject_match_marks_rejected(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        db.commit()

    with factory() as db:
        review_service.reject_match(db, book.id, candidate.id)
        db.commit()

    with factory() as db:
        row = db.get(MetadataMatchRecord, candidate.id)
        assert row is not None
        assert row.status == "rejected"


def test_update_metadata_marks_user_provenance(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="Auto subtitle")
        seed_candidate(db, book.id)
        db.commit()

    with factory() as db:
        result = review_service.update_metadata(
            db,
            book.id,
            {
                "title": "Dune (Revised)",
                "subtitle": "My manual subtitle",
                "author": None,  # ignored: unknown key
                "authors": ["Frank Herbert", "Brian Herbert"],
                "series": "Dune Saga",
            },
        )
        db.commit()
        assert result.fields_applied == [
            "subtitle",
            "title",
            "series",
            "authors",
        ]

        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.title == "Dune (Revised)"
        assert fresh.subtitle == "My manual subtitle"
        assert fresh.series is not None and fresh.series.name == "Dune Saga"
        assert [link.author.name for link in fresh.author_links] == [
            "Frank Herbert",
            "Brian Herbert",
        ]
        provenance = provenance_service.get_provenance(db, book.id)
        for field in ("title", "subtitle", "series", "authors"):
            assert provenance[field] == "user"


def test_update_metadata_rejects_empty_title(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        db.commit()

    with factory() as db:
        with pytest.raises(ValueError, match="Title must not be empty"):
            review_service.update_metadata(db, book.id, {"title": "   "})


def test_update_metadata_series_index_edit_preserves_series(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        book = make_book(db, title="Dune")
        review_service.update_metadata(db, book.id, {"series": "Dune Saga"})
        db.commit()

    # Editing only the index must not clear the series link.
    with factory() as db:
        result = review_service.update_metadata(db, book.id, {"series_index": 3.0})
        db.commit()
        assert result.fields_applied == ["series"]
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.series is not None and fresh.series.name == "Dune Saga"
        assert fresh.series_index == 3.0

    # An explicit empty series name clears the link.
    with factory() as db:
        review_service.update_metadata(db, book.id, {"series": ""})
        db.commit()
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.series_id is None


# --------------------------------------------------------------------------- #
# HTTP endpoint tests
# --------------------------------------------------------------------------- #
def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_review_endpoints_require_admin(
    review_env: tuple[TestClient, sessionmaker[Session], str],
) -> None:
    client, factory, _token = review_env

    # Clear the cookie jar left by the fixture's admin login.
    client.cookies.clear()
    assert client.get("/api/v1/admin/metadata/review").status_code == 401

    with factory() as db:
        reader = db.scalar(select(User).where(User.username == "reader"))
        assert reader is not None
    login = client.post("/login", json={"username": "reader", "password": "readerpass123"})
    reader_token = login.json()["token"]
    response = client.get("/api/v1/admin/metadata/review", headers=_auth_headers(reader_token))
    assert response.status_code == 403


def test_list_review_endpoint(review_env: tuple[TestClient, sessionmaker[Session], str]) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune")
        seed_candidate(db, book.id)
        db.commit()

    response = client.get("/api/v1/admin/metadata/review", headers=_auth_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["book_id"] == book.id
    assert body["items"][0]["title"] == "Dune"
    assert body["items"][0]["pending"] == 1

    bad_status = client.get(
        "/api/v1/admin/metadata/review?match_status=banana", headers=_auth_headers(token)
    )
    assert bad_status.status_code == 400


def test_book_review_endpoint(review_env: tuple[TestClient, sessionmaker[Session], str]) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="Extracted subtitle")
        seed_candidate(db, book.id)
        db.commit()

    response = client.get(f"/api/v1/admin/metadata/books/{book.id}", headers=_auth_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["book"]["title"] == "Dune"
    assert body["book"]["subtitle"] == "Extracted subtitle"
    assert body["candidates"][0]["provider"] == "google_books"
    assert body["candidates"][0]["metadata"]["subtitle"] == "The Desert Planet"
    assert "subtitle" in body["candidates"][0]["offered_fields"]

    missing = client.get("/api/v1/admin/metadata/books/999", headers=_auth_headers(token))
    assert missing.status_code == 404


def test_apply_missing_endpoint(review_env: tuple[TestClient, sessionmaker[Session], str]) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        db.commit()

    response = client.post(
        f"/api/v1/admin/metadata/books/{book.id}/matches/{candidate.id}/apply-missing",
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert "subtitle" in body["fields_applied"]
    assert body["message"] == "Applied all missing fields."

    with factory() as db:
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.subtitle == "The Desert Planet"
        row = db.get(MetadataMatchRecord, candidate.id)
        assert row is not None
        assert row.status == "applied"


def test_apply_fields_endpoint(review_env: tuple[TestClient, sessionmaker[Session], str]) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune", subtitle="Old subtitle")
        candidate = seed_candidate(db, book.id)
        db.commit()

    response = client.post(
        f"/api/v1/admin/metadata/books/{book.id}/matches/{candidate.id}/apply-fields",
        json={"fields": ["subtitle"]},
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["fields_applied"] == ["subtitle"]

    with factory() as db:
        fresh = db.get(Book, book.id)
        assert fresh is not None
        assert fresh.subtitle == "The Desert Planet"


def test_update_metadata_endpoint(
    review_env: tuple[TestClient, sessionmaker[Session], str],
) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune")
        db.commit()

    response = client.put(
        f"/api/v1/admin/metadata/books/{book.id}",
        json={"title": "Dune (Curated)", "subtitle": "Desert Planet"},
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    assert set(response.json()["fields_applied"]) == {"title", "subtitle"}

    with factory() as db:
        provenance = provenance_service.get_provenance(db, book.id)
        assert provenance["title"] == "user"
        assert provenance["subtitle"] == "user"


def test_reject_endpoint(review_env: tuple[TestClient, sessionmaker[Session], str]) -> None:
    client, factory, token = review_env
    with factory() as db:
        book = make_book(db, title="Dune")
        candidate = seed_candidate(db, book.id)
        db.commit()

    response = client.post(
        f"/api/v1/admin/metadata/books/{book.id}/matches/{candidate.id}/reject",
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Metadata match rejected."

    with factory() as db:
        row = db.get(MetadataMatchRecord, candidate.id)
        assert row is not None
        assert row.status == "rejected"


def test_mismatched_match_and_missing_book_404(
    review_env: tuple[TestClient, sessionmaker[Session], str],
) -> None:
    client, factory, token = review_env
    with factory() as db:
        first = make_book(db, title="Dune")
        second = make_book(db, title="Foundation")
        candidate = seed_candidate(db, first.id)
        db.commit()

    response = client.post(
        f"/api/v1/admin/metadata/books/{second.id}/matches/{candidate.id}/apply-missing",
        headers=_auth_headers(token),
    )
    assert response.status_code == 404
