"""Shared metadata field-application logic.

Used by both the automated enrichment pipeline (Phase 6) and the admin
metadata review & curation flow (Phase 7) so the two paths can never disagree
about what "apply" means, which fields exist, or which fields are protected.

Invariants enforced here (AGENTS.md Rule 3 & Phase 6/7 acceptance criteria):

- ``user_safe=True`` (the default) skips any field carrying ``user``
  provenance — automation and bulk review actions can never blind-overwrite
  a manual edit.
- ``missing_only=True`` only fills fields that are currently null/empty on
  the book; existing values — with any provenance — are left untouched.
- Applied fields are re-recorded under ``provenance_source`` so the audit
  trail always reflects where a value came from.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from buku.metadata.models import BookMetadata, populated_fields
from buku.metadata.provenance import provenance_service
from buku.models.base import utc_now
from buku.models.book import Author, Book, BookAuthor, BookIdentifier, Series

# Fields that can be applied from a candidate match. ``page_count`` is trackable
# metadata but is not a persisted Book column, so it is not applicable here.
APPLICABLE_FIELDS = frozenset(
    {
        "title",
        "subtitle",
        "description",
        "publisher",
        "published_date",
        "language",
        "series",
        "authors",
        "identifiers",
    }
)


def apply_fields(
    db: Session,
    book: Book,
    meta: BookMetadata,
    *,
    fields: Iterable[str] | None = None,
    missing_only: bool,
    user_safe: bool = True,
    provenance_source: str,
) -> list[str]:
    """Apply metadata from a candidate to a book; return the applied field names.

    - ``fields`` restricts application to the named subset (``None`` = all).
    - ``missing_only``: when True only empty fields are filled; when False the
      book's current values are replaced.
    - ``user_safe``: when True, fields with ``user`` provenance are skipped.
    - ``provenance_source``: source recorded for every applied field.
    """
    candidate_fields = populated_fields(meta)
    selected = set(fields) if fields is not None else None
    unknown = selected - APPLICABLE_FIELDS if selected else set()
    if unknown:
        raise ValueError(f"Unknown metadata fields: {sorted(unknown)}")

    user_fields = provenance_service.user_edited_fields(db, book.id) if user_safe else set()
    applied: list[str] = []

    def consider(field: str) -> bool:
        if selected is not None and field not in selected:
            return False
        if field in user_fields:
            return False
        return field in candidate_fields

    if consider("title") and (not missing_only or not book.title):
        book.title = meta.title
        applied.append("title")
    if consider("subtitle") and (not missing_only or book.subtitle is None):
        book.subtitle = meta.subtitle
        applied.append("subtitle")
    if consider("description") and (not missing_only or book.description is None):
        book.description = meta.description
        applied.append("description")
    if consider("publisher") and (not missing_only or book.publisher is None):
        book.publisher = meta.publisher
        applied.append("publisher")
    if consider("published_date") and (not missing_only or book.published_date is None):
        book.published_date = meta.published_date
        applied.append("published_date")
    if consider("language") and (not missing_only or book.language is None):
        book.language = meta.language
        applied.append("language")
    if consider("series") and (not missing_only or book.series_id is None):
        _apply_series(db, book, meta.series, meta.series_index, replace=not missing_only)
        applied.append("series")
    if consider("authors") and (not missing_only or not book.author_links):
        _apply_authors(db, book, meta.authors, replace=not missing_only)
        applied.append("authors")
    if consider("identifiers") and (
        not missing_only or _identifiers_missing(db, book, meta.identifiers)
    ):
        _apply_identifiers(db, book, meta.identifiers)
        applied.append("identifiers")

    if applied:
        book.updated_at = utc_now()
        provenance_service.mark_many(db, book.id, {f: provenance_source for f in applied})
    return applied


def _apply_series(
    db: Session,
    book: Book,
    series_name: str | None,
    series_index: float | None,
    *,
    replace: bool,
) -> None:
    """Link the book to the named series (creating it when needed)."""
    name = (series_name or "").strip()
    if not name:
        return
    series = db.scalar(select(Series).where(Series.name == name))
    if series is None:
        series = Series(name=name)
        db.add(series)
        db.flush()
    if replace or book.series_id is None:
        book.series_id = series.id
        book.series_index = series_index


def _apply_authors(db: Session, book: Book, author_names: list[str], *, replace: bool) -> None:
    """Attach authors by name, replacing existing links when ``replace`` is set."""
    if replace:
        for link in list(book.author_links):
            db.delete(link)
        book.author_links.clear()

    existing = {link.author.name for link in book.author_links}
    for name in author_names:
        cleaned = name.strip()
        if not cleaned or cleaned in existing:
            continue
        author = db.scalar(select(Author).where(Author.name == cleaned))
        if author is None:
            author = Author(name=cleaned)
            db.add(author)
            db.flush()
        book.author_links.append(BookAuthor(book_id=book.id, author_id=author.id, role="author"))


def _apply_identifiers(db: Session, book: Book, identifiers: dict[str, str]) -> None:
    """Add candidate identifiers that are not already present (always additive)."""
    existing = {(i.identifier_type, i.identifier_value) for i in book.identifiers}
    for identifier_type, value in identifiers.items():
        cleaned = value.strip()
        if not cleaned or (identifier_type, cleaned) in existing:
            continue
        db.add(
            BookIdentifier(
                book_id=book.id,
                identifier_type=identifier_type,
                identifier_value=cleaned,
            )
        )


def _identifiers_missing(db: Session, book: Book, identifiers: dict[str, str]) -> bool:
    """True when the book lacks at least one identifier type the candidate offers."""
    if not identifiers:
        return False
    existing_types = {i.identifier_type for i in book.identifiers}
    return any(identifier_type not in existing_types for identifier_type in identifiers)


__all__ = ["APPLICABLE_FIELDS", "apply_fields"]
