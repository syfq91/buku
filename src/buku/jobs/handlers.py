"""Job type handlers executed by the Phase 17 background worker.

Handlers receive a session factory and a decoded payload dict. Heavy domain
imports stay inside each function to avoid circular imports and keep startup
cheap.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("buku.jobs.handlers")

JobHandler = Callable[[sessionmaker[Session], dict[str, Any]], None]


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Job payload missing numeric {key!r}.")
    return value


def handle_scan_library(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Run a read-only library scan (all libraries, or one by id)."""
    from buku.scanner import LibraryScanner

    scanner = LibraryScanner(factory)
    library_id = payload.get("library_id")
    if isinstance(library_id, int) and not isinstance(library_id, bool):
        scanner.scan_library(library_id)
    else:
        scanner.scan_all_libraries()


def handle_metadata_lookup(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Enrich a single book from external providers (never user edits)."""
    from buku.metadata.enrichment import MetadataEnrichmentService

    book_id = _require_int(payload, "book_id")
    service = MetadataEnrichmentService()
    with factory() as db:
        service.enrich_book(db, book_id)
        db.commit()


def handle_generate_x4(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Generate (or refresh) the cached e-ink X4 representation."""
    from buku.services.representation import representation_service

    book_id = _require_int(payload, "book_id")
    with factory() as db:
        representation_service.generate(db, book_id, "x4")


def _first_present_file(book: Any) -> Any:
    for file_row in book.files:
        if not file_row.is_missing:
            return file_row
    return None


def handle_generate_cover(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Re-extract and cache a cover image under ``/config/cache/covers/``."""
    from buku.config import get_settings
    from buku.models.book import Book
    from buku.scanner.cover import cache_cover
    from buku.scanner.handlers import get_default_handlers, get_handler_for_file

    book_id = _require_int(payload, "book_id")
    with factory() as db:
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"Book {book_id} does not exist.")
        file_row = _first_present_file(book)
        if file_row is None:
            raise ValueError(f"Book {book_id} has no present files to extract a cover from.")
        path = Path(file_row.file_path)
        handler = get_handler_for_file(path, get_default_handlers())
        if handler is None:
            raise ValueError(f"No format handler for '{path}'.")
        cover_bytes = handler.extract_cover(path)
        if not cover_bytes:
            raise ValueError(f"No cover embedded in '{path}'.")
        covers_dir = get_settings().config_dir / "cache" / "covers"
        cover_path = cache_cover(cover_bytes, book_id=book.id, covers_dir=covers_dir)
        if cover_path is None:
            raise ValueError(f"Failed to cache cover for book {book_id}.")
        book.cover_path = cover_path
        db.commit()


def handle_extract_metadata(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Re-run embedded metadata extraction (cover + non-user book fields)."""
    from buku.metadata.application import apply_fields
    from buku.metadata.models import SOURCE_EMBEDDED
    from buku.metadata.provenance import provenance_service
    from buku.models.book import Book
    from buku.scanner.handlers import get_default_handlers, get_handler_for_file

    book_id = _require_int(payload, "book_id")
    with factory() as db:
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"Book {book_id} does not exist.")
        file_row = _first_present_file(book)
        if file_row is None:
            raise ValueError(f"Book {book_id} has no present files to extract metadata from.")
        path = Path(file_row.file_path)
        handler = get_handler_for_file(path, get_default_handlers())
        if handler is None:
            raise ValueError(f"No format handler for '{path}'.")
        meta = handler.extract_metadata(path)
        provenance_service.ensure_sources(db)
        # Scanner metadata uses the shared BookMetadata shape with ``sources``.
        sources = getattr(meta, "sources", None) or {}
        if sources:
            from buku.metadata.models import BookMetadata as MetadataBookMetadata

            if isinstance(meta, MetadataBookMetadata):
                apply_fields(
                    db,
                    book,
                    meta,
                    missing_only=True,
                    user_safe=True,
                    provenance_source=SOURCE_EMBEDDED,
                )
        cover_bytes = handler.extract_cover(path)
        if cover_bytes:
            from buku.config import get_settings
            from buku.scanner.cover import cache_cover

            covers_dir = get_settings().config_dir / "cache" / "covers"
            cover_path = cache_cover(cover_bytes, book_id=book.id, covers_dir=covers_dir)
            if cover_path is not None:
                book.cover_path = cover_path
        db.commit()


HANDLERS: dict[str, JobHandler] = {
    "scan_library": handle_scan_library,
    "extract_metadata": handle_extract_metadata,
    "metadata_lookup": handle_metadata_lookup,
    "generate_cover": handle_generate_cover,
    "generate_x4": handle_generate_x4,
}


def decode_payload(raw: str | None) -> dict[str, Any]:
    """Parse a job's JSON payload, defaulting to an empty object."""
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Job payload must be a JSON object.")
    return data


__all__ = [
    "HANDLERS",
    "JobHandler",
    "decode_payload",
    "handle_extract_metadata",
    "handle_generate_cover",
    "handle_generate_x4",
    "handle_metadata_lookup",
    "handle_scan_library",
]
