"""Tests for the read-only library scanner (Phase 4)."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import Book, BookFile, Job, Library, ReadingProgress, User
from buku.scanner import LibraryScanner
from tests.fixtures import TINY_JPEG, build_cbz, build_epub, build_pdf, tiny_png_data


@pytest.fixture
def db_url(tmp_path: Path) -> Generator[str]:
    """Provide an isolated SQLite database with migrations applied."""
    reset_engine()
    database_file = tmp_path / "test_scanner.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url)
    set_settings(settings)

    run_migrations(url)
    yield url

    reset_engine()
    set_settings(None)


def test_scan_discovers_and_adds_books(db_url: str, tmp_path: Path) -> None:
    """Verify that scanner recursively discovers and registers EPUB, CBZ, and PDF files."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()

    build_epub(library_dir / "Frank Herbert - Dune.epub", title="Dune", authors=["Frank Herbert"])
    build_cbz(library_dir / "Comics" / "Spider-Man.cbz", count=4)
    build_pdf(library_dir / "Docs" / "Python Guide.pdf")
    # Non-book and hidden files that should be ignored
    (library_dir / "notes.txt").write_text("ignore me")
    (library_dir / ".hidden_book.epub").write_bytes(b"PK\x03\x04hidden")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    stats = scanner.scan_library(library_dir)
    assert stats.files_discovered == 3
    assert stats.files_added == 3
    assert stats.files_unchanged == 0
    assert stats.files_missing == 0
    assert len(stats.errors) == 0

    with factory() as session:
        books = session.scalars(select(Book)).all()
        assert len(books) == 3

        titles = {b.title for b in books}
        assert "Dune" in titles
        assert "Spider-Man" in titles
        assert "Python Guide" in titles

        files = session.scalars(select(BookFile)).all()
        assert len(files) == 3
        formats = {f.file_format for f in files}
        assert formats == {"epub", "cbz", "pdf"}
        assert all(f.is_missing is False for f in files)
        assert all(len(f.file_hash) == 64 for f in files)


def test_scanning_twice_without_changes_skips_reprocessing(db_url: str, tmp_path: Path) -> None:
    """Verify Acceptance Criterion 1: Second scan performs essentially no expensive reprocessing.

    Unchanged files must skip file hashing and database record modifications completely.
    """
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(library_dir / "Book One.epub", title="Book One")
    build_pdf(library_dir / "Book Two.pdf")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    # Initial scan
    stats1 = scanner.scan_library(library_dir)
    assert stats1.files_discovered == 2
    assert stats1.files_added == 2
    assert stats1.files_unchanged == 0

    # Second scan: file hashing must be skipped completely for unchanged files
    with patch("buku.scanner.scanner.compute_file_hash") as mock_hash:
        stats2 = scanner.scan_library(library_dir)
        assert stats2.files_discovered == 2
        assert stats2.files_added == 0
        assert stats2.files_updated == 0
        assert stats2.files_unchanged == 2
        assert stats2.files_missing == 0
        # Hashing was called 0 times because size and mtime matched
        mock_hash.assert_not_called()


def test_read_only_media_directory_enforcement(db_url: str, tmp_path: Path) -> None:
    """Verify Acceptance Criterion 2: Scanner functions properly when library is read-only (:ro)."""
    library_dir = tmp_path / "ro_books"
    library_dir.mkdir()
    sub_dir = library_dir / "SciFi"
    sub_dir.mkdir()

    f1 = build_epub(sub_dir / "Foundation.epub", title="Foundation")
    f2 = build_pdf(sub_dir / "Manual.pdf")

    # Set read-only permissions on files and directories (simulating :ro mount)
    os.chmod(f1, 0o444)
    os.chmod(f2, 0o444)
    os.chmod(sub_dir, 0o555)
    os.chmod(library_dir, 0o555)

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    try:
        stats = scanner.scan_library(library_dir)
        assert stats.files_discovered == 2
        assert stats.files_added == 2
        assert len(stats.errors) == 0

        # Verify DB records were created normally
        with factory() as session:
            books = session.scalars(select(Book)).all()
            assert len(books) == 2
    finally:
        # Restore write permissions for tmp_path teardown
        os.chmod(library_dir, 0o777)
        os.chmod(sub_dir, 0o777)
        os.chmod(f1, 0o666)
        os.chmod(f2, 0o666)


def test_multi_format_book_grouping_same_directory(db_url: str, tmp_path: Path) -> None:
    """Verify Rule 2 Invariant: Multiple formats of a book in same folder link to one Book."""
    library_dir = tmp_path / "books"
    book_folder = library_dir / "Frank Herbert" / "Dune"
    book_folder.mkdir(parents=True)

    build_epub(book_folder / "Dune.epub", title="Dune", chapter_text="epub version")
    build_pdf(book_folder / "Dune.pdf")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    stats = scanner.scan_library(library_dir)
    assert stats.files_discovered == 2
    assert stats.files_added == 2

    with factory() as session:
        books = session.scalars(select(Book)).all()
        # Must be exactly 1 logical book with 2 physical files
        assert len(books) == 1
        book = books[0]
        assert book.title == "Dune"
        assert len(book.files) == 2
        formats = {f.file_format for f in book.files}
        assert formats == {"epub", "pdf"}


def test_modified_file_detected_and_updated(db_url: str, tmp_path: Path) -> None:
    """Verify that modifying a file's content updates its hash and size in the database."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    book_file = build_epub(
        library_dir / "Book.epub", title="Book", chapter_text="version 1 content"
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    scanner.scan_library(library_dir)

    with factory() as session:
        record = session.scalar(select(BookFile))
        assert record is not None
        initial_hash = record.file_hash
        initial_size = record.file_size_bytes

    # Modify file content and advance mtime
    build_epub(book_file, title="Book", chapter_text="version 2 with much longer text content")
    new_mtime = datetime.now(UTC).timestamp() + 5
    os.utime(book_file, (new_mtime, new_mtime))

    stats2 = scanner.scan_library(library_dir)
    assert stats2.files_updated == 1
    assert stats2.files_added == 0
    assert stats2.files_unchanged == 0

    with factory() as session:
        updated_record = session.scalar(select(BookFile))
        assert updated_record is not None
        assert updated_record.file_hash != initial_hash
        assert updated_record.file_size_bytes != initial_size


def test_deleted_file_marks_missing_preserving_progress(db_url: str, tmp_path: Path) -> None:
    """Verify Rule 1 & 3: Deleted file is marked is_missing; progress is NOT deleted."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    book_path = build_epub(library_dir / "Neuromancer.epub", title="Neuromancer")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    scanner.scan_library(library_dir)

    with factory() as session:
        book = session.scalar(select(Book))
        assert book is not None
        user = User(username="reader1", password_hash="hash", display_name="Reader")
        session.add(user)
        session.flush()

        progress = ReadingProgress(
            user_id=user.id,
            book_id=book.id,
            progression=0.65,
            href="ch05.xhtml",
        )
        session.add(progress)
        session.commit()
        book_id = book.id
        user_id = user.id

    # Delete the physical file
    book_path.unlink()

    stats2 = scanner.scan_library(library_dir)
    assert stats2.files_missing == 1
    assert stats2.files_discovered == 0

    with factory() as session:
        # Book and progress must remain intact
        reloaded_book = session.get(Book, book_id)
        assert reloaded_book is not None
        assert len(reloaded_book.files) == 1
        assert reloaded_book.files[0].is_missing is True

        user_progress = session.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_id, ReadingProgress.book_id == book_id
            )
        )
        assert user_progress is not None
        assert user_progress.progression == 0.65


def test_restored_file_clears_missing_status(db_url: str, tmp_path: Path) -> None:
    """Verify that restoring a previously missing file resets is_missing to False."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    book_path = build_epub(
        library_dir / "Restored.epub", title="Restored", chapter_text="stable content"
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    scanner.scan_library(library_dir)

    # Delete file
    book_path.unlink()
    scanner.scan_library(library_dir)

    with factory() as session:
        f = session.scalar(select(BookFile))
        assert f is not None
        assert f.is_missing is True

    # Re-create file at same path
    build_epub(book_path, title="Restored", chapter_text="stable content")
    stats = scanner.scan_library(library_dir)
    assert stats.files_updated == 1

    with factory() as session:
        reloaded = session.scalar(select(BookFile))
        assert reloaded is not None
        assert reloaded.is_missing is False


def test_moved_renamed_file_detected_by_hash(db_url: str, tmp_path: Path) -> None:
    """Verify that moving/renaming a file updates its path via content hash matching."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    old_file = build_epub(
        library_dir / "Original.epub", title="Original", chapter_text="unique novel content"
    )

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    scanner.scan_library(library_dir)

    with factory() as session:
        initial_file = session.scalar(select(BookFile))
        assert initial_file is not None
        file_id = initial_file.id
        book_id = initial_file.book_id

    # Rename file (old path disappears, new path appears with same content hash)
    new_file = library_dir / "Renamed.epub"
    old_file.rename(new_file)

    stats = scanner.scan_library(library_dir)
    assert stats.files_moved == 1
    assert stats.files_added == 0
    assert stats.files_missing == 0

    with factory() as session:
        updated = session.get(BookFile, file_id)
        assert updated is not None
        assert updated.book_id == book_id
        assert updated.file_path == str(new_file.resolve())
        assert updated.is_missing is False


def test_metadata_lookup_job_enqueued_on_new_book(db_url: str, tmp_path: Path) -> None:
    """Verify that newly indexed books enqueue a metadata_lookup task in the jobs table."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(library_dir / "JobTest.epub", title="JobTest")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    scanner.scan_library(library_dir)

    with factory() as session:
        jobs = session.scalars(select(Job)).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.type == "metadata_lookup"
        assert job.status == "queued"
        assert "JobTest" in job.payload or "book_id" in job.payload


def test_scan_all_libraries(db_url: str, tmp_path: Path) -> None:
    """Verify scan_all_libraries scans all registered libraries and aggregates statistics."""
    lib1_dir = tmp_path / "lib1"
    lib2_dir = tmp_path / "lib2"
    lib1_dir.mkdir()
    lib2_dir.mkdir()

    build_epub(lib1_dir / "BookA.epub", title="BookA")
    build_pdf(lib2_dir / "BookB.pdf")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)

    with factory() as session:
        l1 = Library(name="Library 1", path=str(lib1_dir.resolve()))
        l2 = Library(name="Library 2", path=str(lib2_dir.resolve()))
        session.add_all([l1, l2])
        session.commit()

    scanner = LibraryScanner(factory)
    stats = scanner.scan_all_libraries()

    assert stats.files_discovered == 2
    assert stats.files_added == 2
    assert stats.duration_seconds >= 0.0

    with factory() as session:
        books = session.scalars(select(Book)).all()
        assert len(books) == 2


def test_corrupt_file_skipped_gracefully(db_url: str, tmp_path: Path) -> None:
    """Verify scanner handles corrupt or unreadable files gracefully without crashing."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    build_epub(library_dir / "Good.epub", title="Good")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    # Simulate an error computing hash for one file
    with patch("buku.scanner.scanner.compute_file_hash", side_effect=OSError("I/O Error")):
        stats = scanner.scan_library(library_dir)
        assert stats.files_discovered == 1
        assert stats.files_added == 0
        assert len(stats.errors) == 1
        assert "I/O Error" in stats.errors[0]


def test_corrupt_epub_zip_skipped_gracefully(db_url: str, tmp_path: Path) -> None:
    """Phase 5 acceptance: malformed containers fail gracefully, never crash the scanner."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()
    # PK magic but garbage after the header: not a valid ZIP container.
    corrupt = library_dir / "Corrupt.epub"
    corrupt.write_bytes(b"PK\x03\x04this is not a real zip archive")
    build_epub(library_dir / "Healthy.epub", title="Healthy")

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)

    stats = scanner.scan_library(library_dir)
    assert stats.files_discovered == 2
    assert stats.files_added == 1  # only the healthy EPUB is indexed
    assert len(stats.errors) == 1
    assert "Failed to extract metadata" in stats.errors[0]

    with factory() as session:
        books = session.scalars(select(Book)).all()
        assert len(books) == 1
        assert books[0].title == "Healthy"


def test_cover_cached_to_config_dir(db_url: str, tmp_path: Path) -> None:
    """Verify covers are extracted and cached under the config dir, never /books."""
    library_dir = tmp_path / "books"
    library_dir.mkdir()

    png_cover = tiny_png_data(width=2, height=2, rgb=(0, 128, 255))
    build_epub(library_dir / "Covered.epub", title="Covered", cover=png_cover)
    build_pdf(library_dir / "Document.pdf", title="Document", image=TINY_JPEG)

    before = sorted(p.name for p in library_dir.rglob("*"))

    engine = get_engine(db_url)
    factory = get_session_factory(engine)
    scanner = LibraryScanner(factory)
    stats = scanner.scan_library(library_dir)
    assert stats.files_added == 2
    assert len(stats.errors) == 0

    covers_dir = tmp_path / "cache" / "covers"
    with factory() as session:
        books = session.scalars(select(Book).order_by(Book.id)).all()
        assert len(books) == 2
        for book in books:
            assert book.cover_path is not None
            cover_file = Path(book.cover_path)
            assert cover_file.is_file()
            assert cover_file.parent == covers_dir

        by_title = {b.title: b for b in books}
        covered_book = by_title["Covered"]
        document_book = by_title["Document"]
        assert covered_book.cover_path is not None
        assert document_book.cover_path is not None
        epub_cover = Path(covered_book.cover_path)
        pdf_cover = Path(document_book.cover_path)
        assert epub_cover.suffix == ".png"
        assert epub_cover.read_bytes() == png_cover
        assert pdf_cover.suffix == ".jpg"
        assert pdf_cover.read_bytes() == TINY_JPEG

    # Rule 1: media directory contents are untouched.
    after = sorted(p.name for p in library_dir.rglob("*"))
    assert before == after
