"""Read-only library scanner service for discovering, indexing, and reconciling media files."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.config import get_settings
from buku.jobs.queue import enqueue_job
from buku.metadata.provenance import provenance_service
from buku.models.base import utc_now
from buku.models.book import Author, Book, BookAuthor, BookFile, BookIdentifier, Series
from buku.models.library import Library
from buku.scanner.cover import cache_cover
from buku.scanner.handlers import FormatHandler, get_default_handlers, get_handler_for_file
from buku.scanner.hasher import compute_file_hash
from buku.services.search import search_service

logger = logging.getLogger("buku.scanner")


@dataclass
class ScanStatistics:
    """Aggregated metrics collected during a library scan execution."""

    files_discovered: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_unchanged: int = 0
    files_moved: int = 0
    files_missing: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def merge(self, other: ScanStatistics) -> None:
        """Merge another ScanStatistics instance into this one."""
        self.files_discovered += other.files_discovered
        self.files_added += other.files_added
        self.files_updated += other.files_updated
        self.files_unchanged += other.files_unchanged
        self.files_moved += other.files_moved
        self.files_missing += other.files_missing
        self.errors.extend(other.errors)
        self.duration_seconds += other.duration_seconds


class LibraryScanner:
    """Read-only scanner for media libraries.

    Strict Invariants Honored:
    - Rule 1: Media directory is strictly read-only. Never writes or alters media files.
    - Rule 2: Logical Book vs Physical File. Multiple formats group into a single Book.
    - Missing files are marked with is_missing=True, never deleting user state.
    - Unchanged files are skipped with zero expensive rehashing.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: list[FormatHandler] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers or get_default_handlers()

    def get_or_create_library(self, db: Session, path: Path, name: str | None = None) -> Library:
        """Find an existing Library record by resolved path or create a new one."""
        resolved_path = str(path.resolve())
        library = db.scalar(select(Library).where(Library.path == resolved_path))
        if library is None:
            lib_name = name or path.name or "Default Library"
            library = Library(name=lib_name, path=resolved_path)
            db.add(library)
            db.commit()
            db.refresh(library)
            logger.info("Registered library '%s' at path %s", library.name, library.path)
        return library

    def scan_all_libraries(self) -> ScanStatistics:
        """Scan all registered libraries in the database."""
        total_stats = ScanStatistics()
        start_time = time.perf_counter()

        with self.session_factory() as db:
            libraries = db.scalars(select(Library)).all()
            if not libraries:
                default_books = get_settings().books_dir
                if default_books.exists():
                    lib = self.get_or_create_library(db, default_books)
                    libraries = [lib]

        for lib in libraries:
            lib_stats = self.scan_library(lib.id)
            total_stats.merge(lib_stats)

        total_stats.duration_seconds = round(time.perf_counter() - start_time, 3)
        return total_stats

    def scan_library(self, library_identifier: int | str | Path) -> ScanStatistics:
        """Perform a read-only scan of a specific library root.

        Accepts either an integer library ID or a filesystem Path/string.
        """
        start_time = time.perf_counter()
        stats = ScanStatistics()

        with self.session_factory() as db:
            # 1. Resolve Library entity
            if isinstance(library_identifier, int):
                library = db.get(Library, library_identifier)
                if library is None:
                    raise ValueError(f"Library with id={library_identifier} not found.")
            else:
                lib_path = Path(library_identifier).resolve()
                if not lib_path.is_dir():
                    raise FileNotFoundError(
                        f"Library path '{lib_path}' does not exist or is not a directory."
                    )
                library = self.get_or_create_library(db, lib_path)

            library_root = Path(library.path)
            if not library_root.is_dir():
                raise FileNotFoundError(f"Library directory '{library_root}' does not exist.")

            logger.info("Starting read-only scan on library '%s' [%s]", library.name, library_root)

            # 2. Discover files recursively (Strictly read-only)
            discovered_files: list[tuple[Path, FormatHandler]] = []
            seen_file_paths: set[str] = set()

            for root, dirs, files in os.walk(library_root, followlinks=False):
                # Exclude hidden directories in-place (e.g. .git, .stversions)
                dirs[:] = [d for d in dirs if not d.startswith(".")]

                for filename in files:
                    if filename.startswith("."):
                        continue

                    file_path = Path(root) / filename
                    handler = get_handler_for_file(file_path, self.handlers)
                    if handler is None:
                        continue

                    resolved_str = str(file_path.resolve())
                    seen_file_paths.add(resolved_str)
                    discovered_files.append((file_path, handler))

            stats.files_discovered = len(discovered_files)

            # 3. Fetch existing BookFiles for this library
            existing_files = db.scalars(
                select(BookFile)
                .join(Book, BookFile.book_id == Book.id)
                .where(Book.library_id == library.id)
            ).all()
            files_by_path: dict[str, BookFile] = {f.file_path: f for f in existing_files}
            files_by_hash: dict[str, list[BookFile]] = {}
            for f in existing_files:
                files_by_hash.setdefault(f.file_hash, []).append(f)

            # 4. Process discovered files
            for file_path, handler in discovered_files:
                resolved_str = str(file_path.resolve())

                try:
                    stat_info = file_path.stat()
                except OSError as e:
                    msg = f"Cannot stat file '{file_path}': {e}"
                    logger.warning(msg)
                    stats.errors.append(msg)
                    continue

                current_size = stat_info.st_size
                current_mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=UTC)
                current_mtime_ts = int(stat_info.st_mtime)

                existing_record = files_by_path.get(resolved_str)

                # Case 1: Unchanged file check (size and mtime identical, not missing)
                if existing_record is not None and not existing_record.is_missing:
                    rec_mtime = existing_record.file_mtime
                    if rec_mtime.tzinfo is None:
                        rec_mtime = rec_mtime.replace(tzinfo=UTC)
                    rec_mtime_ts = int(rec_mtime.timestamp())

                    if (
                        existing_record.file_size_bytes == current_size
                        and rec_mtime_ts == current_mtime_ts
                    ):
                        stats.files_unchanged += 1
                        continue

                # Case 2: File is new or changed - Compute SHA-256
                try:
                    file_hash = compute_file_hash(file_path)
                except OSError as e:
                    msg = f"Cannot compute hash for '{file_path}': {e}"
                    logger.warning(msg)
                    stats.errors.append(msg)
                    continue

                # Subcase 2a: Existing record exists at this path
                if existing_record is not None:
                    existing_record.file_size_bytes = current_size
                    existing_record.file_mtime = current_mtime
                    existing_record.file_hash = file_hash
                    existing_record.is_missing = False
                    existing_record.updated_at = utc_now()
                    # Refresh the derived cover when content changed; covers are
                    # disposable cache artifacts (never user-edited metadata).
                    self._cache_book_cover(existing_record.book, handler, file_path)
                    stats.files_updated += 1
                    continue

                # Subcase 2b: Moved or renamed file detection
                # Look for an existing record with the exact same content hash
                # whose old path is not in seen_file_paths and no longer exists on disk
                candidate_moved: BookFile | None = None
                for cand in files_by_hash.get(file_hash, []):
                    if (
                        cand.file_path != resolved_str
                        and cand.file_path not in seen_file_paths
                        and not Path(cand.file_path).exists()
                    ):
                        candidate_moved = cand
                        break

                if candidate_moved is not None:
                    logger.info(
                        "Detected moved/renamed file: '%s' -> '%s'",
                        candidate_moved.file_path,
                        resolved_str,
                    )
                    if candidate_moved.file_path in files_by_path:
                        del files_by_path[candidate_moved.file_path]

                    candidate_moved.file_path = resolved_str
                    candidate_moved.file_size_bytes = current_size
                    candidate_moved.file_mtime = current_mtime
                    candidate_moved.is_missing = False
                    candidate_moved.updated_at = utc_now()
                    files_by_path[resolved_str] = candidate_moved
                    stats.files_moved += 1
                    continue

                # Subcase 2c: Genuinely new file
                try:
                    meta = handler.extract_metadata(file_path)
                except Exception as e:
                    msg = f"Failed to extract metadata from '{file_path}': {e}"
                    logger.warning(msg)
                    stats.errors.append(msg)
                    continue

                # Cardinality Invariant (Rule 2): Multi-format grouping in same directory
                parent_dir_str = str(file_path.parent.resolve())
                book = self._find_or_create_book(
                    db=db,
                    library_id=library.id,
                    parent_dir=parent_dir_str,
                    title=meta.title,
                    meta=meta,
                )

                book_file = BookFile(
                    book_id=book.id,
                    file_path=resolved_str,
                    file_format=handler.format_name,
                    file_size_bytes=current_size,
                    file_hash=file_hash,
                    file_mtime=current_mtime,
                    is_missing=False,
                )
                db.add(book_file)
                db.flush()
                files_by_path[resolved_str] = book_file

                # Cache derived cover image (writes only under config cache)
                self._cache_book_cover(book, handler, file_path)

                # Queue background metadata enrichment job
                self._enqueue_metadata_job(db, book.id, library.id)
                stats.files_added += 1

            # 5. Missing File Reconciliation (Rule 1 Invariant)
            # Files in database not found in current filesystem scan are flagged is_missing
            for path_str, book_file in files_by_path.items():
                if path_str not in seen_file_paths and not book_file.is_missing:
                    logger.info("Marking missing file: %s", path_str)
                    book_file.is_missing = True
                    book_file.updated_at = utc_now()
                    stats.files_missing += 1

            db.commit()

        stats.duration_seconds = round(time.perf_counter() - start_time, 3)
        logger.info(
            "Scan complete for '%s' in %.3fs: discovered=%d, added=%d, updated=%d, "
            "unchanged=%d, moved=%d, missing=%d, errors=%d",
            library.name,
            stats.duration_seconds,
            stats.files_discovered,
            stats.files_added,
            stats.files_updated,
            stats.files_unchanged,
            stats.files_moved,
            stats.files_missing,
            len(stats.errors),
        )
        return stats

    def _find_or_create_book(
        self,
        db: Session,
        library_id: int,
        parent_dir: str,
        title: str,
        meta: Any,
    ) -> Book:
        """Find an existing Book in the same folder with matching title, or create a new Book."""
        # 1. Check if an existing Book in this library & folder already exists
        norm_title = title.strip().lower()
        candidates = (
            db.scalars(
                select(Book)
                .join(BookFile, Book.id == BookFile.book_id)
                .where(
                    Book.library_id == library_id,
                    BookFile.file_path.startswith(parent_dir),
                )
            )
            .unique()
            .all()
        )

        for cand in candidates:
            if cand.title.strip().lower() == norm_title:
                return cand

        # 2. Create new Book entity
        series_id: int | None = None
        if meta.series:
            series_name = meta.series.strip()
            series = db.scalar(select(Series).where(Series.name == series_name))
            if series is None:
                series = Series(name=series_name)
                db.add(series)
                db.flush()
            series_id = series.id

        book = Book(
            library_id=library_id,
            title=title.strip(),
            subtitle=meta.subtitle,
            description=meta.description,
            publisher=meta.publisher,
            published_date=meta.published_date,
            language=meta.language,
            series_id=series_id,
            series_index=meta.series_index,
        )
        db.add(book)
        db.flush()

        # 3. Associate authors
        for author_name in meta.authors:
            cleaned = author_name.strip()
            if not cleaned:
                continue
            author = db.scalar(select(Author).where(Author.name == cleaned))
            if author is None:
                author = Author(name=cleaned)
                db.add(author)
                db.flush()

            link = BookAuthor(book_id=book.id, author_id=author.id, role="author")
            db.add(link)

        # 4. Associate identifiers (e.g. ISBN)
        for id_type, id_val in meta.identifiers.items():
            cleaned_val = id_val.strip()
            if not cleaned_val:
                continue
            ident = BookIdentifier(
                book_id=book.id,
                identifier_type=id_type,
                identifier_value=cleaned_val,
            )
            db.add(ident)

        # 5. Record per-field provenance (embedded / filename fallback) so
        # automated enrichment never overwrites user-edited attributes later.
        if getattr(meta, "sources", None):
            provenance_service.mark_many(db, book.id, meta.sources)

        db.flush()
        # 6. Index the new book into the FTS5 full-text search index.
        search_service.index_book(db, book)
        return book

    def _cache_book_cover(
        self,
        book: Book,
        handler: FormatHandler,
        file_path: Path,
    ) -> None:
        """Extract and cache a cover image for a book (read-only contract).

        Covers are written only under the writable configuration cache
        (``<config>/cache/covers/``); the media directory is never touched.
        Failures degrade gracefully with a logged warning.
        """
        try:
            cover_bytes = handler.extract_cover(file_path)
        except Exception as exc:
            logger.warning("Cover extraction failed for '%s': %s", file_path, exc)
            return
        if not cover_bytes:
            return

        settings = get_settings()
        covers_dir = settings.config_dir / "cache" / "covers"
        cover_path = cache_cover(cover_bytes, book_id=book.id, covers_dir=covers_dir)
        if cover_path is not None:
            book.cover_path = cover_path

    def _enqueue_metadata_job(self, db: Session, book_id: int, library_id: int) -> None:
        """Queue a background metadata enrichment job."""
        enqueue_job(db, "metadata_lookup", {"book_id": book_id, "library_id": library_id})
