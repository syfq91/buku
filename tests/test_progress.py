"""Phase 10 tests: central reading-progression service and REST surface.

Covers the canonical ``ProgressionService`` (create / read / update /
conflict resolution via modification timestamps), the Rule 3 invariants —
progress belongs strictly to ``(user_id, book_id)`` and user isolation — and
the ``/api/v1/progress`` transport layer that surfaces conflicts as HTTP 409.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import Book, Library
from buku.models.progress import ReadingProgress
from buku.services.auth import auth_service
from buku.services.progression import (
    ProgressionStatus,
    progression_service,
)

ProgressEnv = tuple[TestClient, sessionmaker[Session], str, str]


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_progress.db"
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
def progress_env(tmp_path: Path) -> Generator[ProgressEnv]:
    """Set up a migrated DB, two users, and an authenticated API client."""
    reset_engine()
    database_file = tmp_path / "progress_http.db"
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
        reader_token = client.post(
            "/api/v1/auth/login", json={"username": "reader", "password": "readerpass123"}
        ).json()["token"]
        admin_token = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "adminpass123"}
        ).json()["token"]
        yield client, factory_cls, reader_token, admin_token
    reset_engine()
    set_settings(None)


# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #
def make_book(session: Session, library_path: Path, *, title: str = "Dune") -> Book:
    """Create a minimal book row inside the given library directory."""
    library_path.mkdir(parents=True, exist_ok=True)
    library = session.scalar(select(Library).where(Library.path == str(library_path)))
    if library is None:
        library = Library(name="progress", path=str(library_path))
        session.add(library)
        session.flush()
    book = Book(library_id=library.id, title=title)
    session.add(book)
    session.flush()
    return book


def auth_headers(token: str) -> dict[str, str]:
    """Build an Authorization header from a session token."""
    return {"Authorization": f"Bearer {token}"}


def make_user(session: Session, *, username: str = "reader", is_admin: bool = False) -> int:
    """Create a user row and return its id."""
    return auth_service.create_user(
        session,
        username,
        "readerpass123",
        username.title(),
        is_admin=is_admin,
    ).id


def utc(iso: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp as an aware datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# Service: create / read / update
# --------------------------------------------------------------------------- #
def test_no_progress_returns_none(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()
        assert progression_service.get(db, user_id, book_id) is None
        assert progression_service.get_state(db, user_id, book_id) is None


def test_create_records_progress(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        timestamp = utc("2026-09-20T12:00:00+00:00")
        result = progression_service.update(
            db,
            user_id=user_id,
            book_id=book_id,
            progression=0.25,
            href="chapter03.xhtml",
            fragment="p42",
            title="Chapter 3",
            modified_at=timestamp,
            device_id="kobo-1",
            device_name="Kobo Clara",
        )
        assert result.status is ProgressionStatus.CREATED
        assert result.previous is None
        state = result.state
        assert state.user_id == user_id
        assert state.book_id == book_id
        assert state.progression == 0.25
        assert state.href == "chapter03.xhtml"
        assert state.fragment == "p42"
        assert state.title == "Chapter 3"
        assert state.device_id == "kobo-1"
        assert state.device_name == "Kobo Clara"
        assert state.modified_at == timestamp
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert stored.progression == 0.25
        assert stored.href == "chapter03.xhtml"
        assert stored.modified_at is not None


def test_create_sets_modified_at_for_unique_pair(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        progression_service.update(db, user_id, book_id, progression=0.1)
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(ReadingProgress)
            .where(ReadingProgress.user_id == user_id, ReadingProgress.book_id == book_id)
        )
        assert count == 1


def test_update_existing_applies_newer_timestamp(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        first = utc("2026-09-20T12:00:00+00:00")
        second = first + timedelta(minutes=5)
        progression_service.update(db, user_id, book_id, progression=0.25, modified_at=first)
        result = progression_service.update(
            db, user_id, book_id, progression=0.75, href="chapter09.xhtml", modified_at=second
        )
        assert result.status is ProgressionStatus.UPDATED
        assert result.state.progression == 0.75
        assert result.state.href == "chapter09.xhtml"
        assert result.state.modified_at == second
        assert result.previous is not None
        assert result.previous.progression == 0.25
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert stored.progression == 0.75
        assert stored.href == "chapter09.xhtml"


def test_update_equal_timestamp_is_applied(factory: sessionmaker[Session], tmp_path: Path) -> None:
    """Equal timestamps are not 'older', so the update is applied."""
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        timestamp = utc("2026-09-20T12:00:00+00:00")
        progression_service.update(db, user_id, book_id, progression=0.2, modified_at=timestamp)
        result = progression_service.update(
            db, user_id, book_id, progression=0.8, modified_at=timestamp
        )
        assert result.status is ProgressionStatus.UPDATED
        assert result.state.progression == 0.8
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert stored.progression == 0.8


# --------------------------------------------------------------------------- #
# Service: conflict resolution
# --------------------------------------------------------------------------- #
def test_update_older_timestamp_conflicts(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        later = utc("2026-09-20T12:00:00+00:00")
        progression_service.update(db, user_id, book_id, progression=0.9, modified_at=later)

        rejected = utc("2026-09-20T11:50:00+00:00")
        result = progression_service.update(
            db, user_id, book_id, progression=0.1, href="chapter02.xhtml", modified_at=rejected
        )
        assert result.status is ProgressionStatus.CONFLICT
        assert result.previous is not None
        assert result.previous.progression == 0.9
        assert result.state.progression == 0.1  # the rejected incoming value
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert stored.progression == 0.9  # stored state untouched
        assert stored.modified_at is not None


def test_naive_and_aware_timestamps_compare_as_utc(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Stored naive UTC (SQLite) must compare against aware client timestamps."""
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        # Apply with an aware timestamp: stored back as naive on SQLite.
        progression_service.update(
            db,
            user_id,
            book_id,
            progression=0.5,
            modified_at=utc("2026-09-20T10:00:00+00:00"),
        )
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        # Incoming with the same instant expressed in a different tz offset is
        # NOT older (it is equal), so it must be applied.
        result = progression_service.update(
            db,
            user_id,
            book_id,
            progression=0.6,
            modified_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC) - timedelta(hours=2),
        )
        assert result.status is ProgressionStatus.UPDATED

        # A genuinely older aware timestamp must conflict.
        older = result.state.modified_at - timedelta(seconds=5)
        conflict = progression_service.update(
            db, user_id, book_id, progression=0.1, modified_at=older
        )
        assert conflict.status is ProgressionStatus.CONFLICT


# --------------------------------------------------------------------------- #
# Service: validation & hygiene
# --------------------------------------------------------------------------- #
def test_progression_clamped_to_unit_range(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        progression_service.update(db, user_id, book_id, progression=1.5)
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert stored.progression == 1.0


def test_progression_rejects_non_finite(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        with pytest.raises(ValueError):
            progression_service.update(db, user_id, book_id, progression=float("nan"))
        with pytest.raises(ValueError):
            progression_service.update(db, user_id, book_id, progression=float("inf"))


def test_omitted_timestamp_uses_server_now(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        before = datetime.now(UTC)
        result = progression_service.update(db, user_id, book_id, progression=0.3)
        after = datetime.now(UTC)
        assert result.status is ProgressionStatus.CREATED
        assert before <= result.state.modified_at <= after


def test_long_href_is_truncated(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        progression_service.update(db, user_id, book_id, progression=0.2, href="x" * 900)
        db.commit()

    with factory() as db:
        stored = progression_service.get(db, user_id, book_id)
        assert stored is not None
        assert len(stored.href or "") == 500


# --------------------------------------------------------------------------- #
# Service: user isolation (Rule 3)
# --------------------------------------------------------------------------- #
def test_user_progress_is_isolated(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_a = make_user(db)
        user_b = make_user(db, username="other")
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        progression_service.update(db, user_id=user_a, book_id=book_id, progression=0.4)
        progression_service.update(db, user_id=user_b, book_id=book_id, progression=0.1)
        db.commit()

    with factory() as db:
        user_a_progress = progression_service.get(db, user_a, book_id)
        user_b_progress = progression_service.get(db, user_b, book_id)
        assert user_a_progress is not None and user_b_progress is not None
        assert user_a_progress.progression == 0.4
        assert user_b_progress.progression == 0.1
        assert user_a_progress.id != user_b_progress.id
        # Updating one user never affects the other.
        progression_service.update(db, user_id=user_a, book_id=book_id, progression=0.9)
        db.flush()
        assert progression_service.get(db, user_b, book_id).progression == 0.1  # type: ignore[union-attr]


def test_list_recent_orders_by_modified_at(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as db:
        user_id = make_user(db)
        first = make_book(db, tmp_path / "lib", title="Alpha")
        second = make_book(db, tmp_path / "lib", title="Beta")
        third = make_book(db, tmp_path / "lib", title="Gamma")
        base = utc("2026-09-19T08:00:00+00:00")
        progression_service.update(db, user_id, first.id, progression=0.1, modified_at=base)
        progression_service.update(
            db, user_id, second.id, progression=0.2, modified_at=base + timedelta(hours=1)
        )
        progression_service.update(
            db, user_id, third.id, progression=0.3, modified_at=base + timedelta(hours=2)
        )
        db.commit()

    with factory() as db:
        shelf = progression_service.list_recent(db, user_id, limit=2)
        assert len(shelf) == 2
        titles = [book.title for _, book in shelf]
        assert titles == ["Gamma", "Beta"]

        all_rows = progression_service.list_recent(db, user_id)
        assert len(all_rows) == 3
        assert [p.progression for p, _ in all_rows] == [0.3, 0.2, 0.1]


# --------------------------------------------------------------------------- #
# API: GET /api/v1/progress/{book_id}
# --------------------------------------------------------------------------- #
def test_get_progress_requires_auth(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = progress_env
    client.cookies.clear()
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()
    response = client.get(f"/api/v1/progress/{book_id}")
    assert response.status_code == 401


def test_get_progress_unknown_book_404(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, _, token, _ = progress_env
    response = client.get("/api/v1/progress/99999", headers=auth_headers(token))
    assert response.status_code == 404


def test_get_progress_without_record(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, token, _ = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()
    response = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(token))
    assert response.status_code == 200
    assert response.json()["progress"] is None


def test_get_progress_returns_record(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, token, _ = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        progression_service.update(
            db,
            1,
            book_id,
            progression=0.5,
            href="chapter03.xhtml",
            fragment="p42",
            modified_at=utc("2026-09-20T12:00:00+00:00"),
        )
        db.commit()

    response = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(token))
    assert response.status_code == 200
    progress = response.json()["progress"]
    assert progress is not None
    assert progress["book_id"] == book_id
    assert progress["progression"] == 0.5
    assert progress["href"] == "chapter03.xhtml"
    assert progress["fragment"] == "p42"


def test_get_progress_is_scoped_to_user(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, admin_token = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        # reader (user 1) records progress; admin (user 2) has none
        progression_service.update(db, 1, book_id, progression=0.5)
        db.commit()

    reader = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(reader_token))
    assert reader.status_code == 200
    assert reader.json()["progress"]["progression"] == 0.5

    admin = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(admin_token))
    assert admin.status_code == 200
    assert admin.json()["progress"] is None


# --------------------------------------------------------------------------- #
# API: PUT /api/v1/progress/{book_id}
# --------------------------------------------------------------------------- #
def test_put_progress_create_then_update(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, token, _ = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()

    first = client.put(
        f"/api/v1/progress/{book_id}",
        json={
            "progression": 0.25,
            "href": "chapter01.xhtml",
            "title": "Chapter 1",
            "modified_at": "2026-09-20T12:00:00Z",
            "device_id": "browser",
            "device_name": "Web Reader",
        },
        headers=auth_headers(token),
    )
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "created"
    assert body["progress"]["progression"] == 0.25
    assert body["progress"]["device_name"] == "Web Reader"

    second = client.put(
        f"/api/v1/progress/{book_id}",
        json={
            "progression": 0.6,
            "href": "chapter04.xhtml",
            "modified_at": "2026-09-20T12:30:00Z",
        },
        headers=auth_headers(token),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "updated"
    assert second.json()["progress"]["progression"] == 0.6

    fetched = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(token))
    assert fetched.json()["progress"]["progression"] == 0.6


def test_put_progress_older_timestamp_conflicts(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, factory_cls, token, _ = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()

    newer = client.put(
        f"/api/v1/progress/{book_id}",
        json={"progression": 0.9, "modified_at": "2026-09-20T12:00:00Z"},
        headers=auth_headers(token),
    )
    assert newer.status_code == 200

    stale = client.put(
        f"/api/v1/progress/{book_id}",
        json={"progression": 0.1, "modified_at": "2026-09-20T11:00:00Z"},
        headers=auth_headers(token),
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert "older" in detail["message"]
    assert detail["stored"]["progression"] == 0.9
    assert detail["incoming"]["progression"] == 0.1

    fetched = client.get(f"/api/v1/progress/{book_id}", headers=auth_headers(token))
    assert fetched.json()["progress"]["progression"] == 0.9


def test_put_progress_validation_rejects_out_of_range(
    progress_env: ProgressEnv, tmp_path: Path
) -> None:
    client, factory_cls, token, _ = progress_env
    with factory_cls() as db:
        book = make_book(db, tmp_path / "lib")
        book_id = book.id
        db.commit()

    too_high = client.put(
        f"/api/v1/progress/{book_id}",
        json={"progression": 1.5},
        headers=auth_headers(token),
    )
    assert too_high.status_code == 422

    too_low = client.put(
        f"/api/v1/progress/{book_id}",
        json={"progression": -0.1},
        headers=auth_headers(token),
    )
    assert too_low.status_code == 422


def test_put_progress_unknown_book_404(progress_env: ProgressEnv, tmp_path: Path) -> None:
    client, _, token, _ = progress_env
    response = client.put(
        "/api/v1/progress/99999",
        json={"progression": 0.5, "modified_at": "2026-09-20T12:00:00Z"},
        headers=auth_headers(token),
    )
    assert response.status_code == 404
