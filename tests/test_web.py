"""Phase 9 tests: the Jinja2 + HTMX web UI.

Covers every page surface introduced in Phase 9 — the login flow, dashboard,
catalog browse/detail/series/author pages, full-text search fragments, the
reader placeholder, personal shelves, settings, the admin console (users,
libraries, jobs, metadata review), static assets, cover serving, and the
read-only download path — verifying the thin-route contract that pages only
delegate to domain services and that downloads are confined to library roots.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from datetime import UTC, datetime
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
from buku.models import Author, Book, BookAuthor, BookFile, BookIdentifier, Library, Series, User
from buku.models.collection import Collection
from buku.models.job import Job
from buku.models.metadata import MetadataMatch as MetadataMatchRecord
from buku.models.progress import ReadingProgress
from buku.services.auth import auth_service
from buku.services.search import search_service

WebEnv = tuple[TestClient, sessionmaker[Session], str, str]


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_web.db"
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
def web_env(tmp_path: Path) -> Generator[WebEnv]:
    """Set up a migrated DB, reader + admin users, and an authenticated client."""
    reset_engine()
    database_file = tmp_path / "web_http.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url)
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory_cls = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        with factory_cls() as db:
            auth_service.create_user(db, "reader", "readerpass123", "Reader", is_admin=False)
            auth_service.create_user(db, "admin", "adminpass123", "Admin", is_admin=True)
        admin_token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"}
        ).json()["token"]
        reader_token = client.post(
            "/api/v1/auth/login", json={"username": "reader", "password": "readerpass123"}
        ).json()["token"]
        yield client, factory_cls, reader_token, admin_token
    reset_engine()
    set_settings(None)


@pytest.fixture
def admin_login(web_env: WebEnv) -> tuple[TestClient, str]:
    """Provide the client plus the admin session token for header-based requests."""
    client, _, _, admin_token = web_env
    return client, admin_token


def admin_headers(token: str) -> dict[str, str]:
    """Build an Authorization header for admin web-page requests."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
def make_book(
    session: Session,
    library_path: Path,
    *,
    title: str = "Dune",
    subtitle: str | None = None,
    authors: list[str] | None = None,
    series: str | None = None,
    series_index: float | None = None,
    description: str | None = None,
    publisher: str | None = None,
) -> Book:
    """Create a fully-related Book row (library, authors, series, ISBN)."""
    library_path.mkdir(parents=True, exist_ok=True)
    library = session.scalar(select(Library).where(Library.path == str(library_path)))
    if library is None:
        library = Library(name="web", path=str(library_path))
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
    session.add(
        BookIdentifier(book_id=book.id, identifier_type="isbn", identifier_value="9780441013593")
    )
    for name in authors or ["Frank Herbert"]:
        author = session.scalar(select(Author).where(Author.name == name))
        if author is None:
            author = Author(name=name, sort_name=name)
            session.add(author)
            session.flush()
        session.add(BookAuthor(book_id=book.id, author_id=author.id, role="author"))
    session.flush()
    return book


def attach_file(session: Session, book: Book, path: Path, *, fmt: str = "epub") -> BookFile:
    """Write a real file inside the library root and attach a BookFile row."""
    path.write_bytes(b"\x00\x01\x02test-bytes")
    row = BookFile(
        book_id=book.id,
        file_path=str(path),
        file_format=fmt,
        file_size_bytes=path.stat().st_size,
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        file_mtime=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def seed_candidate(
    session: Session,
    book_id: int,
    *,
    status: str = "pending",
) -> MetadataMatchRecord:
    """Insert a metadata_matches row exactly as enrichment persists them."""
    source = provenance_service.get_or_create_source(session, "google_books")
    item: dict[str, Any] = {
        "id": "abc123def",
        "volumeInfo": {
            "title": "Dune",
            "subtitle": "The Desert Planet",
            "authors": ["Frank Herbert"],
            "description": "A desert planet saga about spice.",
            "publisher": "Chilton Books",
            "publishedDate": "1965-08-01",
            "language": "en",
        },
    }
    parsed = GoogleBooksProvider._parse_item(item)
    assert parsed is not None
    payload = json.dumps(
        {
            "provider": "google_books",
            "external_id": "abc123def",
            "confidence": 0.95,
            "fields": sorted(parsed.sources.keys()),
            "raw": item,
        },
        default=str,
    )
    row = MetadataMatchRecord(
        book_id=book_id,
        source_id=source.id,
        external_id="abc123def",
        confidence_score=0.95,
        match_data=payload,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Session & landing
# --------------------------------------------------------------------------- #
def test_login_page_renders(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    client.cookies.clear()
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert 'id="login-form"' in response.text


def test_root_redirects_anonymous_user_to_login(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_root_redirects_signed_in_user_to_dashboard(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_dashboard_requires_auth(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    client.cookies.clear()
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_dashboard_shows_library_stats(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        make_book(db, tmp_path / "lib", title="Dune")
        make_book(db, tmp_path / "lib", title="Foundation", series="Foundation")
        db.commit()
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Your library" in response.text
    assert "<b>2</b><span>books" in response.text
    assert "Recent additions" in response.text


def test_dashboard_empty_state(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "bookserver scan" in response.text


# --------------------------------------------------------------------------- #
# Catalog pages
# --------------------------------------------------------------------------- #
def test_books_page_lists_and_filters(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune", authors=["Frank Herbert"])
        make_book(db, tmp_path / "lib", title="Neuromancer")
        search_service.index_book(db, dune)
        db.commit()

    response = client.get("/books")
    assert response.status_code == 200
    assert "Dune" in response.text
    assert "Neuromancer" in response.text

    filtered = client.get("/books", params={"q": "Dune"})
    assert filtered.status_code == 200
    assert "Dune" in filtered.text
    assert "Neuromancer" not in filtered.text


def test_books_page_pagination(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        for index in range(30):
            make_book(db, tmp_path / "lib", title=f"Volume {index:02d}")
        db.commit()
    response = client.get("/books")
    assert response.status_code == 200
    assert "Volume 00" in response.text
    assert "Volume 29" not in response.text
    assert "Page 1 of 2" in response.text
    assert "?page=2" in response.text


def test_book_detail_page_shows_metadata_and_formats(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(
            db,
            tmp_path / "lib",
            title="Dune",
            subtitle="The Desert Planet",
            authors=["Frank Herbert"],
            series="Dune",
            series_index=1,
            description="A desert planet saga.",
            publisher="Chilton Books",
        )
        attach_file(db, dune, tmp_path / "lib" / "dune.epub", fmt="epub")
        attach_file(db, dune, tmp_path / "lib" / "dune.pdf", fmt="pdf")
        book_id = dune.id
        db.commit()

    response = client.get(f"/books/{book_id}")
    assert response.status_code == 200
    assert "Dune" in response.text
    assert "The Desert Planet" in response.text
    assert "Frank Herbert" in response.text
    assert "desert planet saga" in response.text
    assert "/series/" in response.text
    assert "EPUB" in response.text
    assert "PDF" in response.text
    assert "/download/" in response.text
    assert "Not started yet" in response.text


def test_book_detail_shows_user_progress(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        reader = db.scalar(select(User).where(User.username == "reader"))
        assert reader is not None
        db.add(ReadingProgress(user_id=reader.id, book_id=dune.id, progression=0.5, title="Dune"))
        book_id = dune.id
        db.commit()

    response = client.get(f"/books/{book_id}")
    assert response.status_code == 200
    assert "50%" in response.text


def test_book_detail_404_page(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    response = client.get("/books/99999")
    assert response.status_code == 404
    assert "left the shelf" in response.text


def test_download_streams_file_within_library_root(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        file_row = attach_file(db, dune, tmp_path / "lib" / "dune.epub")
        db.commit()
        file_id = file_row.id
        book_id = dune.id

    response = client.get(f"/books/{book_id}/download/{file_id}")
    assert response.status_code == 200
    assert response.content == b"\x00\x01\x02test-bytes"
    assert 'filename="dune.epub"' in response.headers.get("content-disposition", "")


def test_download_missing_file_serves_410(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        path = tmp_path / "lib" / "gone.epub"
        path.write_bytes(b"data")
        file_row = attach_file(db, dune, path)
        file_row.is_missing = True
        db.commit()
        file_id = file_row.id
        book_id = dune.id

    response = client.get(f"/books/{book_id}/download/{file_id}")
    assert response.status_code == 410
    assert "disappeared" in response.text


def test_download_unknown_file_404(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        db.commit()

    response = client.get(f"/books/{dune.id}/download/9999")
    assert response.status_code == 404


def test_download_rejects_path_outside_library(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        outside = tmp_path / "elsewhere" / "evil.epub"
        outside.parent.mkdir()
        outside.write_bytes(b"evil")
        file_row = attach_file(db, dune, outside)
        db.commit()
        file_id = file_row.id

    response = client.get(f"/books/{dune.id}/download/{file_id}")
    assert response.status_code == 410


def test_series_page_lists_books(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        make_book(db, tmp_path / "lib", title="Dune", series="Dune Cycle")
        make_book(db, tmp_path / "lib", title="Dune Messiah", series="Dune Cycle")
        series_row = db.scalar(select(Series).where(Series.name == "Dune Cycle"))
        assert series_row is not None
        series_id = series_row.id
        db.commit()

    response = client.get(f"/series/{series_id}")
    assert response.status_code == 200
    assert "Dune Cycle" in response.text
    assert "Dune Messiah" in response.text


def test_author_page_lists_books(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        make_book(db, tmp_path / "lib", title="Dune", authors=["Frank Herbert"])
        make_book(db, tmp_path / "lib", title="Dune Messiah", authors=["Frank Herbert"])
        other = make_book(db, tmp_path / "lib", title="Other", authors=["Someone Else"])
        author_row = db.scalar(select(Author).where(Author.name == "Frank Herbert"))
        assert author_row is not None
        author_id = author_row.id
        other_id = other.id
        db.commit()

    response = client.get(f"/authors/{author_id}")
    assert response.status_code == 200
    assert "Dune" in response.text
    assert "Dune Messiah" in response.text
    assert f"/books/{other_id}" not in response.text


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search_results_fragment_returns_html(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune", authors=["Frank Herbert"])
        search_service.index_book(db, dune)
        db.commit()

    response = client.get("/search/results", params={"q": "Dun"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "1 result" in response.text
    assert "Dune" in response.text

    empty = client.get("/search/results", params={"q": ""})
    assert empty.status_code == 200
    assert "Type above to search" in empty.text


def test_search_results_requires_auth(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    client.cookies.clear()
    response = client.get("/search/results", params={"q": "Dune"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --------------------------------------------------------------------------- #
# Reader, shelves, settings
# --------------------------------------------------------------------------- #
def test_reader_page_without_epub_shows_empty_state(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        book_id = dune.id
        db.commit()

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    assert "has no EPUB to open in the browser" in response.text


def test_collections_page_lists_user_shelves(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    with factory_cls() as db:
        reader = db.scalar(select(User).where(User.username == "reader"))
        assert reader is not None
        db.add(Collection(user_id=reader.id, name="To read", description="favorites"))
        db.commit()

    response = client.get("/collections")
    assert response.status_code == 200
    assert "To read" in response.text


def test_settings_page_renders(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Change password" in response.text
    assert "Reader" in response.text


# --------------------------------------------------------------------------- #
# Static assets & covers
# --------------------------------------------------------------------------- #
def test_static_assets_served(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200
    assert htmx.headers["content-type"].startswith("text/javascript")
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]


def test_cached_cover_served(web_env: WebEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = web_env
    covers_dir = tmp_path / "cache" / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    with factory_cls() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        cover_path = covers_dir / "cover.png"
        cover_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        dune.cover_path = str(cover_path)
        db.commit()

    response = client.get("/books")
    assert response.status_code == 200
    assert "/covers/cover.png" in response.text

    cover = client.get("/covers/cover.png")
    assert cover.status_code == 200
    assert cover.content == b"\x89PNG\r\n\x1a\nfake"


# --------------------------------------------------------------------------- #
# Admin console
# --------------------------------------------------------------------------- #
def test_admin_pages_redirect_plain_users(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    for path in ("/admin/users", "/admin/libraries", "/admin/jobs", "/admin/metadata"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_admin_users_page(web_env: WebEnv, admin_login: tuple[TestClient, str]) -> None:
    client, token = admin_login
    response = client.get("/admin/users", headers=admin_headers(token))
    assert response.status_code == 200
    assert "reader" in response.text
    assert "admin" in response.text


def test_admin_libraries_page(
    web_env: WebEnv, admin_login: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, token = admin_login
    with web_env[1]() as db:
        make_book(db, tmp_path / "lib", title="Dune")
        db.commit()
    response = client.get("/admin/libraries", headers=admin_headers(token))
    assert response.status_code == 200
    assert str(tmp_path / "lib") in response.text


def test_admin_jobs_page(web_env: WebEnv, admin_login: tuple[TestClient, str]) -> None:
    client, token = admin_login
    with web_env[1]() as db:
        db.add(
            Job(
                id="00000000-0000-0000-0000-000000000001",
                type="scan_library",
                status="completed",
            )
        )
        db.commit()
    response = client.get("/admin/jobs", headers=admin_headers(token))
    assert response.status_code == 200
    assert "scan_library" in response.text


def test_admin_metadata_queue_lists_pending(
    web_env: WebEnv, admin_login: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, token = admin_login
    with web_env[1]() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        seed_candidate(db, dune.id)
        db.commit()

    response = client.get("/admin/metadata", headers=admin_headers(token))
    assert response.status_code == 200
    assert "Dune" in response.text
    assert "Pending" in response.text


# --------------------------------------------------------------------------- #
# Admin metadata review actions
# --------------------------------------------------------------------------- #
def test_review_book_page_and_apply_actions(
    web_env: WebEnv, admin_login: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, token = admin_login
    with web_env[1]() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        match = seed_candidate(db, dune.id)
        book_id = dune.id
        match_id = match.id
        db.commit()

    headers = admin_headers(token)

    page = client.get(f"/admin/metadata/books/{book_id}", headers=headers)
    assert page.status_code == 200
    assert "Apply missing fields" in page.text
    assert "Reject" in page.text
    assert 'id="review-body"' in page.text

    apply_fields_resp = client.post(
        f"/admin/metadata/books/{book_id}/matches/{match_id}/apply-fields",
        data={"fields": ["publisher"]},
        headers=headers,
    )
    assert apply_fields_resp.status_code == 200
    assert "Chilton Books" in apply_fields_resp.text

    with web_env[1]() as db:
        refresh = db.get(Book, book_id)
        assert refresh is not None
        assert refresh.publisher == "Chilton Books"

    apply_missing_resp = client.post(
        f"/admin/metadata/books/{book_id}/matches/{match_id}/apply-missing",
        headers=headers,
    )
    assert apply_missing_resp.status_code == 200
    assert "subtitle" in apply_missing_resp.text

    with web_env[1]() as db:
        refresh = db.get(Book, book_id)
        assert refresh is not None
        assert refresh.subtitle == "The Desert Planet"

    reject_resp = client.post(
        f"/admin/metadata/books/{book_id}/matches/{match_id}/reject",
        headers=headers,
    )
    assert reject_resp.status_code == 200
    assert "Candidate rejected" in reject_resp.text

    with web_env[1]() as db:
        row = db.get(MetadataMatchRecord, match_id)
        assert row is not None
        assert row.status == "rejected"


def test_review_manual_edit_persists(
    web_env: WebEnv, admin_login: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, token = admin_login
    with web_env[1]() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        book_id = dune.id
        db.commit()

    response = client.post(
        f"/admin/metadata/books/{book_id}",
        data={
            "title": "Dune Remastered",
            "subtitle": "Special Edition",
            "authors": "Frank Herbert, Brian Herbert",
            "series": "Dune Universe",
            "series_index": "1.5",
            "publisher": "Ace",
            "published_date": "2024-01-15",
            "language": "en",
            "description": "A revised edition.",
        },
        headers=admin_headers(token),
    )
    assert response.status_code == 200
    assert "Dune Remastered" in response.text

    with web_env[1]() as db:
        refresh = db.get(Book, book_id)
        assert refresh is not None
        assert refresh.title == "Dune Remastered"
        assert refresh.publisher == "Ace"
        assert refresh.series is not None
        assert refresh.series.name == "Dune Universe"
        assert len(refresh.author_links) == 2


def test_admin_review_actions_redirect_plain_users(
    web_env: WebEnv, admin_login: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, _, _, _ = web_env
    with web_env[1]() as db:
        dune = make_book(db, tmp_path / "lib", title="Dune")
        match = seed_candidate(db, dune.id)
        book_id = dune.id
        match_id = match.id
        db.commit()

    response = client.post(
        f"/admin/metadata/books/{book_id}/matches/{match_id}/apply-missing",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --------------------------------------------------------------------------- #
# Auth-free introspection
# --------------------------------------------------------------------------- #
def test_login_page_redirects_when_already_authed(web_env: WebEnv) -> None:
    client, _, _, _ = web_env
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
