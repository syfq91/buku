"""Phase 14 tests: the generic representation system.

Covers the profile registry (``original`` identity + ``x4`` generated
profile), and the ``RepresentationService`` read/generate/exists/invalidate
contract with its cache-key composition and cache-path confinement — verifying
that nothing ever writes outside ``/config/cache/`` and that generated EPUBs
are structurally sound `mimetype`-first containers.
"""

from __future__ import annotations

import zipfile
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import Book, Library
from buku.represent import ProfileNotSupportedError, RepresentationError, UnknownProfileError
from buku.services.representation import RepresentationService, representation_service
from tests.opds_helpers import seed_book

RepEnv = tuple[TestClient, sessionmaker[Session], Path]


@pytest.fixture
def rep_env(tmp_path: Path) -> Generator[RepEnv]:
    """Migrated DB, writable config cache, and a seeded library root."""
    reset_engine()
    url = f"sqlite:///{tmp_path}/representations.db"
    settings = Settings(
        config_dir=tmp_path, books_dir=tmp_path, database_url=url, jobs_enabled=False
    )
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory_cls = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        yield client, factory_cls, tmp_path
    reset_engine()
    set_settings(None)


def _service() -> RepresentationService:
    return representation_service


def _x4_files(factory: sessionmaker[Session], tmp_path: Path) -> int:
    """Seed one epub-only book and return its id."""
    with factory() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "dune.epub")])
        db.commit()
        return book.id


# --------------------------------------------------------------------------- #
# Profile registry
# --------------------------------------------------------------------------- #
def test_registered_profiles_include_original_and_x4() -> None:
    from buku.represent import get_profile, profile_names

    assert get_profile("original").name == "original"
    assert get_profile("x4").name == "x4"
    assert "x4" in profile_names()
    assert "original" in profile_names()


def test_unknown_profile_raises() -> None:
    from buku.represent import get_profile

    with pytest.raises(UnknownProfileError):
        get_profile("moon")


def test_original_profile_exists_with_real_files(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        assert representation_service.exists(db, book_id, "original") is True


def test_original_profile_missing_when_no_files(rep_env: RepEnv) -> None:
    _, factory, _ = rep_env
    with factory() as db:
        library = Library(name="empty", path=str(rep_env[2] / "empty"))
        db.add(library)
        db.flush()
        book = Book(library_id=library.id, title="No files")
        db.add(book)
        db.commit()
        book_id = book.id
    with factory() as db:
        assert representation_service.exists(db, book_id, "original") is False


def test_original_profile_not_generatable(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        with pytest.raises(ProfileNotSupportedError):
            representation_service.generate(db, book_id, "original")


# --------------------------------------------------------------------------- #
# Read contract
# --------------------------------------------------------------------------- #
def test_get_returns_none_before_generation(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        assert representation_service.get(db, book_id, "x4") is None
        assert representation_service.exists(db, book_id, "x4") is False


def test_generate_creates_row_and_cache_file(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)

    with factory() as db:
        row = representation_service.generate(db, book_id, "x4")
        assert row.book_id == book_id
        assert row.profile == "x4"
        assert row.format == "epub"
        assert row.optimizer_version
        assert row.source_hash
        assert row.file_size_bytes > 0

    cache_root = tmp_path / "cache" / "x4"
    assert cache_root.is_dir()
    files = list(cache_root.iterdir())
    assert len(files) == 1

    served = representation_service.path(row)
    assert served is not None
    assert served.resolve().is_relative_to(cache_root.resolve())
    assert served.read_bytes() == files[0].read_bytes()


def test_generated_x4_is_mimetype_first_stored_container(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        row = representation_service.generate(db, book_id, "x4")
        served = representation_service.path(row)
    assert served is not None
    with zipfile.ZipFile(served) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.read("mimetype") == b"application/epub+zip"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert "META-INF/container.xml" in names
        assert "OEBPS/content.opf" in names


def test_generate_is_idempotent(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        first = representation_service.generate(db, book_id, "x4")
        first_id = first.id
        first_file = representation_service.path(first)
        assert first_file is not None
        first_bytes = first_file.read_bytes()
        second = representation_service.generate(db, book_id, "x4")
        assert second.id == first_id
    # Idempotent runs keep a single row and a single cache file.
    cache_dir = tmp_path / "cache" / "x4"
    files = list(cache_dir.iterdir())
    assert len(files) == 1
    assert first_file.read_bytes() == first_bytes


def test_generate_regenerates_when_source_hash_changes(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    with factory() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "dune.epub")])
        db.commit()
        book_id = book.id
        row = representation_service.generate(db, book_id, "x4")
        old_key = Path(row.cache_path).name

        source = next(f for f in book.files if f.file_format == "epub")
        source.file_hash = "f" * 64  # simulate an edited source
        row = representation_service.generate(db, book_id, "x4")
        new_key = Path(row.cache_path).name

        assert new_key != old_key
        assert row.source_hash == "f" * 64
    files = list((tmp_path / "cache" / "x4").iterdir())
    assert len(files) == 1


def test_generate_requires_epub_source(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    with factory() as db:
        book = seed_book(db, tmp_path, formats=[("pdf", "dune.pdf")])
        db.commit()
        book_id = book.id
        with pytest.raises(RepresentationError):
            representation_service.generate(db, book_id, "x4")


def test_generate_unknown_book_raises(rep_env: RepEnv) -> None:
    _, factory, _ = rep_env
    with factory() as db:
        with pytest.raises(RepresentationError):
            representation_service.generate(db, 999999, "x4")


def test_generate_unknown_profile_raises(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        with pytest.raises(UnknownProfileError):
            representation_service.generate(db, book_id, "moon")


# --------------------------------------------------------------------------- #
# Invalidate contract
# --------------------------------------------------------------------------- #
def test_invalidate_removes_row_and_file(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        representation_service.generate(db, book_id, "x4")
        assert representation_service.exists(db, book_id, "x4") is True
        removed = representation_service.invalidate(db, book_id, "x4")
        assert removed is True
        assert representation_service.get(db, book_id, "x4") is None
        assert representation_service.exists(db, book_id, "x4") is False
    assert list((tmp_path / "cache" / "x4").iterdir()) == []


def test_invalidate_missing_profile_returns_false(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        assert representation_service.invalidate(db, book_id, "x4") is False


def test_invalidate_does_not_touch_other_books(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    with factory() as db:
        one = _x4_files(factory, tmp_path)
        db.commit()
    with factory() as db:
        two = seed_book(db, tmp_path, title="Other Dune", formats=[("epub", "other.epub")])
        db.commit()
        two_id = two.id
    with factory() as db:
        representation_service.generate(db, one, "x4")
        representation_service.generate(db, two_id, "x4")
        representation_service.invalidate(db, one, "x4")
        assert representation_service.exists(db, one, "x4") is False
        assert representation_service.exists(db, two_id, "x4") is True


# --------------------------------------------------------------------------- #
# Cache path confinement (Rule 1 / Rule 6 defense)
# --------------------------------------------------------------------------- #
def test_path_rejects_rows_outside_cache_root(rep_env: RepEnv) -> None:
    from buku.models.representation import Representation

    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    escape = tmp_path / "media" / "evil.epub"
    escape.parent.mkdir(parents=True, exist_ok=True)
    escape.write_bytes(b"not a real epub")
    with factory() as db:
        row = Representation(
            book_id=book_id,
            profile="x4",
            format="epub",
            source_hash="0" * 64,
            optimizer_version="1",
            cache_path=str(escape.resolve()),
            file_size_bytes=16,
        )
        db.add(row)
        db.commit()
        assert representation_service.path(row) is None


def test_invalidate_only_deletes_within_cache(rep_env: RepEnv) -> None:
    from buku.models.representation import Representation

    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    outside = tmp_path / "outside" / "victim.epub"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"keep me")
    with factory() as db:
        row = Representation(
            book_id=book_id,
            profile="x4",
            format="epub",
            source_hash="0" * 64,
            optimizer_version="1",
            cache_path=str(outside.resolve()),
            file_size_bytes=8,
        )
        db.add(row)
        db.commit()
        representation_service.invalidate(db, book_id, "x4")
    assert outside.is_file()  # untouched


def test_stray_cache_file_without_row_is_not_served(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    cache_dir = tmp_path / "cache" / "x4"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "orphan.epub").write_bytes(b"orphan")
    with factory() as db:
        # No DB row: even an existing cache file must not count as served.
        assert representation_service.get(db, book_id, "x4") is None
        row = representation_service.generate(db, book_id, "x4")
        served = representation_service.path(row)
        assert served is not None
        assert served.name != "orphan.epub"


def test_missing_file_treated_as_absent(rep_env: RepEnv) -> None:
    _, factory, tmp_path = rep_env
    book_id = _x4_files(factory, tmp_path)
    with factory() as db:
        row = representation_service.generate(db, book_id, "x4")
        served = representation_service.path(row)
        assert served is not None
        assert representation_service.exists(db, book_id, "x4") is True
    served.unlink()
    with factory() as db:
        assert representation_service.exists(db, book_id, "x4") is False
        assert representation_service.get(db, book_id, "x4") is None
        # Regenerating repairs the row+file pair.
        representation_service.generate(db, book_id, "x4")
        assert representation_service.exists(db, book_id, "x4") is True
