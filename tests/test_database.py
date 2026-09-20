"""Tests for SQLAlchemy domain models, relationships, and Alembic migrations."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from buku.cli import cli
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import (
    Author,
    Book,
    BookAuthor,
    BookFile,
    BookIdentifier,
    Collection,
    CollectionBook,
    Job,
    Library,
    MetadataMatch,
    MetadataProvenance,
    MetadataSource,
    ReadingProgress,
    Representation,
    Series,
    Session,
    User,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return isolated path for SQLite database test."""
    return tmp_path / "test_buku.db"


@pytest.fixture
def migrated_db(db_path: Path, tmp_path: Path) -> Generator[str]:
    """Run migrations on an isolated SQLite database and return its URL."""
    reset_engine()
    db_url = f"sqlite:///{db_path}"
    settings = Settings(config_dir=tmp_path, database_url=db_url)
    set_settings(settings)

    run_migrations(db_url)
    yield db_url

    reset_engine()
    set_settings(None)


def test_migrations_create_all_tables(migrated_db: str) -> None:
    """Verify that Alembic migrations create all core tables on a fresh database."""
    engine = get_engine(migrated_db)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        # Verify tables exist by performing select count queries
        assert session.scalar(select(User).limit(1)) is None
        assert session.scalar(select(Session).limit(1)) is None
        assert session.scalar(select(Library).limit(1)) is None
        assert session.scalar(select(Series).limit(1)) is None
        assert session.scalar(select(Book).limit(1)) is None
        assert session.scalar(select(BookFile).limit(1)) is None
        assert session.scalar(select(Author).limit(1)) is None
        assert session.scalar(select(BookAuthor).limit(1)) is None
        assert session.scalar(select(BookIdentifier).limit(1)) is None
        assert session.scalar(select(ReadingProgress).limit(1)) is None
        assert session.scalar(select(MetadataSource).limit(1)) is not None  # seeded
        assert session.scalar(select(MetadataMatch).limit(1)) is None
        assert session.scalar(select(MetadataProvenance).limit(1)) is None
        assert session.scalar(select(Representation).limit(1)) is None
        assert session.scalar(select(Job).limit(1)) is None
        assert session.scalar(select(Collection).limit(1)) is None
        assert session.scalar(select(CollectionBook).limit(1)) is None


def test_multiple_files_belong_to_one_logical_book(migrated_db: str) -> None:
    """Verify Cardinality Invariant: Multiple physical files belong to one logical book."""
    engine = get_engine(migrated_db)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        lib = Library(name="Main", path="/books")
        session.add(lib)
        session.flush()

        book = Book(
            library_id=lib.id,
            title="Dune",
            description="Classic sci-fi novel",
        )
        session.add(book)
        session.flush()

        # Add EPUB, PDF, and CBZ formats to the single logical book
        now = datetime.now(UTC)
        epub_file = BookFile(
            book_id=book.id,
            file_path="/books/Dune/dune.epub",
            file_format="epub",
            file_size_bytes=1024000,
            file_hash="hash_epub_123",
            file_mtime=now,
        )
        pdf_file = BookFile(
            book_id=book.id,
            file_path="/books/Dune/dune.pdf",
            file_format="pdf",
            file_size_bytes=2048000,
            file_hash="hash_pdf_456",
            file_mtime=now,
        )
        cbz_file = BookFile(
            book_id=book.id,
            file_path="/books/Dune/dune.cbz",
            file_format="cbz",
            file_size_bytes=5120000,
            file_hash="hash_cbz_789",
            file_mtime=now,
        )
        session.add_all([epub_file, pdf_file, cbz_file])
        session.commit()

        # Reload and assert relationships
        reloaded = session.get(Book, book.id)
        assert reloaded is not None
        assert len(reloaded.files) == 3
        formats = {f.file_format for f in reloaded.files}
        assert formats == {"epub", "pdf", "cbz"}


def test_user_isolated_reading_progress(migrated_db: str) -> None:
    """Verify Progress Invariant: Progress belongs to (user_id, book_id) with user isolation."""
    engine = get_engine(migrated_db)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        lib = Library(name="Main", path="/books")
        session.add(lib)
        session.flush()

        book = Book(library_id=lib.id, title="Neuromancer")
        user_a = User(
            username="alice",
            password_hash="argon2id$fakehash",
            display_name="Alice",
        )
        user_b = User(
            username="bob",
            password_hash="argon2id$fakehash",
            display_name="Bob",
        )
        session.add_all([book, user_a, user_b])
        session.flush()

        # Alice at 45% in chapter 3
        progress_a = ReadingProgress(
            user_id=user_a.id,
            book_id=book.id,
            progression=0.45,
            href="ch03.xhtml",
            fragment="p12",
        )
        # Bob at 90% in chapter 9
        progress_b = ReadingProgress(
            user_id=user_b.id,
            book_id=book.id,
            progression=0.90,
            href="ch09.xhtml",
            fragment="p40",
        )
        session.add_all([progress_a, progress_b])
        session.commit()

        # Query separately
        alice_prog = session.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_a.id, ReadingProgress.book_id == book.id
            )
        )
        bob_prog = session.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_b.id, ReadingProgress.book_id == book.id
            )
        )

        assert alice_prog is not None
        assert alice_prog.progression == 0.45
        assert bob_prog is not None
        assert bob_prog.progression == 0.90

        # Verify uniqueness on (user_id, book_id)
        duplicate_progress = ReadingProgress(
            user_id=user_a.id,
            book_id=book.id,
            progression=0.50,
        )
        session.add(duplicate_progress)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_database_persistence_across_restart(db_path: Path, migrated_db: str) -> None:
    """Verify data survives engine disposal and restart."""
    # Write initial data
    engine1 = get_engine(migrated_db)
    factory1 = get_session_factory(engine1)
    with factory1() as session:
        lib = Library(name="TestLib", path="/books")
        session.add(lib)
        session.flush()

        user = User(
            username="charlie",
            password_hash="hashedpass",
            display_name="Charlie",
        )
        book = Book(library_id=lib.id, title="Foundation")
        session.add_all([user, book])
        session.commit()
        book_id = book.id
        user_id = user.id

    # Reset and close engine
    reset_engine()

    # Re-open database with a new engine instance
    engine2 = get_engine(migrated_db)
    factory2 = get_session_factory(engine2)
    with factory2() as session:
        reloaded_book = session.get(Book, book_id)
        reloaded_user = session.get(User, user_id)
        assert reloaded_book is not None
        assert reloaded_book.title == "Foundation"
        assert reloaded_user is not None
        assert reloaded_user.username == "charlie"


def test_missing_file_preserves_book_and_progress(migrated_db: str) -> None:
    """Verify Rule 1: Marking a missing file does NOT destroy logical Book or progress."""
    engine = get_engine(migrated_db)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        lib = Library(name="Lib", path="/books")
        session.add(lib)
        session.flush()

        book = Book(library_id=lib.id, title="Snow Crash")
        user = User(username="reader", password_hash="pass", display_name="Reader")
        session.add_all([book, user])
        session.flush()

        book_file = BookFile(
            book_id=book.id,
            file_path="/books/SnowCrash.epub",
            file_format="epub",
            file_size_bytes=500000,
            file_hash="hash_snowcrash",
            file_mtime=datetime.now(UTC),
            is_missing=False,
        )
        progress = ReadingProgress(
            user_id=user.id,
            book_id=book.id,
            progression=0.75,
        )
        session.add_all([book_file, progress])
        session.commit()

        # Simulate scanner detecting missing file
        book_file.is_missing = True
        session.commit()

        # Verify book and user progress are still intact
        reloaded_book = session.get(Book, book.id)
        assert reloaded_book is not None
        assert reloaded_book.files[0].is_missing is True
        assert len(reloaded_book.reading_progress) == 1
        assert reloaded_book.reading_progress[0].progression == 0.75


def test_cli_migrate_command(tmp_path: Path) -> None:
    """Verify that 'bookserver migrate' executes Alembic migrations from scratch."""
    runner = CliRunner()
    cfg_dir = tmp_path / "cli_config"
    cfg_dir.mkdir()
    custom_db = cfg_dir / "migrated_cli.db"

    config_file = tmp_path / "test_config.toml"
    config_file.write_text(
        f"""
[paths]
config_dir = "{cfg_dir}"

[database]
url = "sqlite:///{custom_db}"
"""
    )

    result = runner.invoke(cli, ["migrate", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "Applying database migrations" in result.output
    assert "Database migrations applied successfully" in result.output

    # Verify that the database file was created
    assert custom_db.is_file()

    # Verify tables in the newly migrated DB
    engine = get_engine(f"sqlite:///{custom_db}")
    factory = get_session_factory(engine)
    with factory() as session:
        user_count = session.scalar(select(User).limit(1))
        assert user_count is None
    reset_engine()
