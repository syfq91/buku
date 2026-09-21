"""RepresentationService: on-demand alternative book representations.

Coordinates the generic profile registry (:mod:`buku.represent`) with the
``representations`` table and the writable cache under ``/config/cache/``.

Rule 6 invariant:
    Generated artefacts (e.g. the ``x4`` profile) are cached under
    ``/config/cache/{profile}/`` and keyed by
    ``f(book_id, source_hash, profile, optimizer_version)`` so they
    regenerate exactly when the source, profile, or optimizer changes.
    Original media files are never written, edited, or deleted.

The identity ``original`` profile resolves straight to the book's own files
and is never materialized in the cache or the database.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from buku.config import get_settings
from buku.models.book import Book
from buku.models.representation import Representation
from buku.represent import GenerationResult, RepresentationError, get_profile
from buku.represent.original import OriginalProfile

logger = logging.getLogger("buku.services.representation")


class RepresentationService:
    """Read, generate, and invalidate cached book representations."""

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, db: Session, book_id: int, profile: str) -> Representation | None:
        """Return the ready representation row for a book+profile, or None.

        A row is only "ready" when its cache file still exists on disk; stale
        rows (deleted/moved cache files) behave as absent so callers
        regenerate instead of serving a broken artifact.
        """
        get_profile(profile)  # validates the name early
        rows = db.scalars(
            select(Representation).where(
                Representation.book_id == book_id, Representation.profile == profile
            )
        ).all()
        for row in rows:
            if self.path(row) is not None:
                return row
        return None

    def exists(self, db: Session, book_id: int, profile: str) -> bool:
        """Whether *book_id* has a usable representation in *profile*.

        The identity ``original`` profile counts as present whenever the
        logical book has at least one real attached file; generated profiles
        count as present only while their cached file exists.
        """
        prof = get_profile(profile)
        if isinstance(prof, OriginalProfile):
            book = db.get(Book, book_id)
            return book is not None and prof.exists(book)
        return self.get(db, book_id, profile) is not None

    def path(self, row: Representation) -> Path | None:
        """Resolve a representation row to its cache file (confinement-checked).

        Returns ``None`` when the stored path escapes the cache root or points
        at a file that no longer exists. Path traversal defense: a tampered
        ``cache_path`` can never be served or deleted outside the cache.
        """
        candidate = Path(row.cache_path).resolve()
        if not candidate.is_relative_to(self._cache_root()):
            logger.warning(
                "Representation cache path escapes the cache root; refusing: %s",
                row.cache_path,
            )
            return None
        return candidate if candidate.is_file() else None

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def generate(self, db: Session, book_id: int, profile: str) -> Representation:
        """Ensure an up-to-date representation exists and return its row.

        Renders into ``/config/cache/{profile}/`` keyed by
        ``f(book_id, source_hash, profile, optimizer_version)`` and re-runs
        the generator only when the source hash or optimizer version changed.
        Generation is staged to a ``.tmp`` sibling and atomically renamed so
        concurrent readers never observe a partially written artifact.
        """
        prof = get_profile(profile)
        book = db.get(Book, book_id)
        if book is None:
            raise RepresentationError(f"Book {book_id} does not exist.")

        sources = prof.source_files(book)
        if not sources:
            raise RepresentationError(
                f"Book {book_id} has no {prof.format} file to build the '{profile}' profile from."
            )
        source = sources[0]
        version = prof.optimizer_version()

        cache_dir = self._cache_dir(profile)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / self._cache_key(
            book.id, source.file_hash, profile, version, prof.format
        )

        existing = self.get(db, book_id, profile)
        if (
            existing is not None
            and existing.source_hash == source.file_hash
            and existing.optimizer_version == version
        ):
            return existing

        # Purge stale rows for this book+profile so the unique key
        # (book_id, profile, source_hash) can never collide on reuse and
        # repaired rows don't accumulate behind the served one.
        for stale in db.scalars(
            select(Representation).where(
                Representation.book_id == book.id, Representation.profile == profile
            )
        ).all():
            if stale is not existing:
                db.delete(stale)
        # Emit the deletes before any insert/update so the unique key
        # (book_id, profile, source_hash) never collides inside one flush.
        db.flush()

        if not target.exists():
            staged = target.with_name(target.name + ".tmp")
            try:
                result: GenerationResult = prof.generate(source, staged)
                staged.replace(target)
            finally:
                staged.unlink(missing_ok=True)
        else:
            # Crash between file write and row commit: repair the row only.
            result = GenerationResult(
                format=prof.format,
                source_hash=source.file_hash,
                optimizer_version=version,
                file_size_bytes=target.stat().st_size,
            )

        if existing is None:
            existing = Representation(
                book_id=book.id,
                profile=prof.name,
                format=result.format,
                source_hash=result.source_hash,
                optimizer_version=result.optimizer_version,
                cache_path=str(target),
                file_size_bytes=result.file_size_bytes,
            )
            db.add(existing)
        else:
            # The row is being re-pointed at a new cache file: retire the old one.
            old_path = self.path(existing)
            if old_path is not None and Path(existing.cache_path) != target:
                old_path.unlink(missing_ok=True)
            existing.format = result.format
            existing.source_hash = result.source_hash
            existing.optimizer_version = result.optimizer_version
            existing.cache_path = str(target)
            existing.file_size_bytes = result.file_size_bytes
        db.commit()
        db.refresh(existing)
        return existing

    def invalidate(self, db: Session, book_id: int, profile: str) -> bool:
        """Delete the profile's cached rows and cache files for *book_id*."""
        get_profile(profile)
        rows = db.scalars(
            select(Representation).where(
                Representation.book_id == book_id, Representation.profile == profile
            )
        ).all()
        if not rows:
            return False
        for row in rows:
            candidate = Path(row.cache_path).resolve()
            if candidate.is_relative_to(self._cache_root()):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to remove representation cache file: %s", exc)
            db.delete(row)
        db.commit()
        return True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _cache_root(self) -> Path:
        """Writable cache root shipped with the configuration directory."""
        return (get_settings().config_dir / "cache").resolve()

    def _cache_dir(self, profile: str) -> Path:
        return self._cache_root() / profile

    @staticmethod
    def _cache_key(book_id: int, source_hash: str, profile: str, version: str, fmt: str) -> str:
        """Deterministic cache file name: f(book_id, source_hash, profile, version)."""
        digest = hashlib.sha256(
            f"{book_id}:{source_hash}:{profile}:{version}".encode()
        ).hexdigest()[:16]
        return f"{profile}-{book_id}-{digest}.{fmt}"


representation_service = RepresentationService()


__all__ = ["RepresentationService", "representation_service"]
