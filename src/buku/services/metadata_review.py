"""Admin metadata review & curation service (Phase 7).

Provides the backend operations behind the metadata review workflow:

    Book ──► Metadata Matches ──► Review ──► Apply Selected / Missing Fields

Every mutation reuses the shared :func:`buku.metadata.application.apply_fields`
logic so manual curation and auto-enrichment can never disagree, and fields
marked with ``user`` provenance are always protected from blind overwrites.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buku.metadata.application import apply_fields as apply_fields_shared
from buku.metadata.google_books import GoogleBooksProvider
from buku.metadata.models import SOURCE_USER, BookMetadata, populated_fields
from buku.metadata.provenance import provenance_service
from buku.models.base import utc_now
from buku.models.book import Author, Book, BookAuthor
from buku.models.metadata import MetadataMatch

logger = logging.getLogger("buku.services.metadata_review")

MATCH_STATUSES = ("pending", "applied", "rejected")


@dataclass
class BookMetadataView:
    """Current persisted metadata for a book, ready for side-by-side review."""

    title: str
    subtitle: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    authors: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_book(cls, book: Book) -> BookMetadataView:
        """Build a snapshot from the ORM Book, resolving lazy relationships."""
        return cls(
            title=book.title,
            subtitle=book.subtitle,
            description=book.description,
            publisher=book.publisher,
            published_date=book.published_date,
            language=book.language,
            series=book.series.name if book.series else None,
            series_index=book.series_index,
            authors=[link.author.name for link in book.author_links],
            identifiers={i.identifier_type: i.identifier_value for i in book.identifiers},
        )


@dataclass
class CandidateView:
    """A persisted match row with its parsed metadata for display/review."""

    id: int
    provider: str
    external_id: str
    confidence: float
    status: str
    offered_fields: list[str] = field(default_factory=list)
    metadata: BookMetadata | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ReviewItem:
    """One book in the review list, with its match status counts."""

    book_id: int
    title: str
    authors: list[str] = field(default_factory=list)
    pending: int = 0
    applied: int = 0
    rejected: int = 0
    updated_at: datetime | None = None


@dataclass
class BookReviewDetail:
    """Current metadata, provenance, and all candidate matches for one book."""

    book: BookMetadataView
    provenance: dict[str, str] = field(default_factory=dict)
    candidates: list[CandidateView] = field(default_factory=list)


@dataclass
class AppliedResult:
    """Outcome of a curation action."""

    fields_applied: list[str] = field(default_factory=list)
    fields_skipped: list[str] = field(default_factory=list)
    match_status: str = "applied"


class MetadataReviewService:
    """Review, apply, and reject metadata match candidates (Phase 7)."""

    # ------------------------------------------------------------------ #
    # Read paths
    # ------------------------------------------------------------------ #
    def list_review_items(
        self,
        db: Session,
        *,
        status: str = "pending",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[int, list[ReviewItem]]:
        """Page through books that have matches in the given status."""
        if status not in MATCH_STATUSES:
            raise ValueError(f"Invalid match status: {status!r}.")

        total = (
            db.scalar(
                select(func.count(func.distinct(MetadataMatch.book_id))).where(
                    MetadataMatch.status == status
                )
            )
            or 0
        )
        rows = db.execute(
            select(
                MetadataMatch.book_id,
                func.max(MetadataMatch.updated_at).label("latest"),
            )
            .where(MetadataMatch.status == status)
            .group_by(MetadataMatch.book_id)
            .order_by(func.max(MetadataMatch.updated_at).desc())
            .limit(limit)
            .offset(offset)
        ).all()

        items: list[ReviewItem] = []
        for row in rows:
            book = db.get(Book, row.book_id)
            if book is None:
                continue
            counts = self._status_counts(db, row.book_id)
            items.append(
                ReviewItem(
                    book_id=book.id,
                    title=book.title,
                    authors=[link.author.name for link in book.author_links],
                    pending=counts.get("pending", 0),
                    applied=counts.get("applied", 0),
                    rejected=counts.get("rejected", 0),
                    updated_at=row.latest,
                )
            )
        return total, items

    def get_book_review(self, db: Session, book_id: int) -> BookReviewDetail:
        """Return the current metadata, provenance, and candidates for a book."""
        book = self._get_book(db, book_id)
        matches = db.scalars(
            select(MetadataMatch)
            .where(MetadataMatch.book_id == book_id)
            .order_by(MetadataMatch.confidence_score.desc(), MetadataMatch.updated_at.desc())
        ).all()
        return BookReviewDetail(
            book=BookMetadataView.from_book(book),
            provenance=provenance_service.get_provenance(db, book_id),
            candidates=[self._candidate_view(match) for match in matches],
        )

    # ------------------------------------------------------------------ #
    # Curation actions
    # ------------------------------------------------------------------ #
    def apply_missing(self, db: Session, book_id: int, match_id: int) -> AppliedResult:
        """Apply every missing, non-user-edited field from a candidate."""
        book = self._get_book(db, book_id)
        match = self._get_match(db, book_id, match_id)
        candidate = self._candidate_view(match)
        if candidate.metadata is None:
            raise ValueError("Candidate metadata is unavailable for application.")
        applied = apply_fields_shared(
            db,
            book,
            candidate.metadata,
            missing_only=True,
            user_safe=True,
            provenance_source=candidate.provider,
        )
        self._set_match_status(match, "applied")
        db.commit()
        skipped = [field for field in candidate.offered_fields if field not in applied]
        return AppliedResult(fields_applied=applied, fields_skipped=skipped)

    def apply_fields(
        self, db: Session, book_id: int, match_id: int, fields: list[str]
    ) -> AppliedResult:
        """Apply a hand-picked subset of candidate fields (skips user edits)."""
        if not fields:
            raise ValueError("At least one field must be selected.")
        book = self._get_book(db, book_id)
        match = self._get_match(db, book_id, match_id)
        candidate = self._candidate_view(match)
        if candidate.metadata is None:
            raise ValueError("Candidate metadata is unavailable for application.")
        applied = apply_fields_shared(
            db,
            book,
            candidate.metadata,
            fields=fields,
            missing_only=False,
            user_safe=True,
            provenance_source=candidate.provider,
        )
        self._set_match_status(match, "applied")
        db.commit()
        skipped = [field for field in fields if field not in applied]
        return AppliedResult(fields_applied=applied, fields_skipped=skipped)

    def update_metadata(self, db: Session, book_id: int, edits: dict[str, Any]) -> AppliedResult:
        """Persist manual metadata edits, marking every touched field as ``user``.

        Manual edits deliberately overwrite current values — including values
        with embedded/filename provenance — and become protected from future
        automated enrichment and rescans.
        """
        book = self._get_book(db, book_id)
        applied: list[str] = []

        for edit_field, attr in (
            ("subtitle", "subtitle"),
            ("description", "description"),
            ("publisher", "publisher"),
            ("published_date", "published_date"),
            ("language", "language"),
        ):
            if edit_field not in edits:
                continue
            setattr(book, attr, edits[edit_field])
            applied.append(edit_field)

        if "title" in edits:
            title = str(edits["title"] or "").strip()
            if not title:
                raise ValueError("Title must not be empty.")
            book.title = title
            applied.append("title")

        if "series" in edits or "series_index" in edits:
            if "series" in edits:
                self._apply_series_edit(db, book, edits.get("series"))
            if "series_index" in edits:
                book.series_index = edits["series_index"]
            applied.append("series")

        if "authors" in edits:
            names = edits["authors"]
            if not isinstance(names, list):
                raise ValueError("'authors' must be a list of strings.")
            self._replace_authors(db, book, [str(n).strip() for n in names if str(n).strip()])
            applied.append("authors")

        if applied:
            book.updated_at = utc_now()
            provenance_service.mark_many(db, book.id, {field: SOURCE_USER for field in applied})
        db.commit()
        return AppliedResult(fields_applied=applied, match_status="edited")

    def reject_match(self, db: Session, book_id: int, match_id: int) -> None:
        """Mark a candidate as rejected; existing applied data is untouched."""
        match = self._get_match(db, book_id, match_id)
        self._set_match_status(match, "rejected")
        db.commit()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _status_counts(self, db: Session, book_id: int) -> dict[str, int]:
        rows = db.execute(
            select(MetadataMatch.status, func.count())
            .where(MetadataMatch.book_id == book_id)
            .group_by(MetadataMatch.status)
        ).all()
        return {status: count for status, count in rows}

    def _candidate_view(self, match: MetadataMatch) -> CandidateView:
        payload = self._match_payload(match.match_data)
        metadata = self._parse_match_metadata(payload)
        offered = payload.get("fields") or (
            sorted(populated_fields(metadata)) if metadata is not None else []
        )
        return CandidateView(
            id=match.id,
            provider=payload.get("provider", "unknown"),
            external_id=match.external_id,
            confidence=match.confidence_score,
            status=match.status,
            offered_fields=[str(field) for field in offered],
            metadata=metadata,
            created_at=match.created_at,
            updated_at=match.updated_at,
        )

    def _parse_match_metadata(self, payload: dict[str, Any]) -> BookMetadata | None:
        """Rebuild the candidate's BookMetadata from the stored raw payload."""
        if payload.get("provider") == "google_books" and isinstance(payload.get("raw"), dict):
            return GoogleBooksProvider._parse_item(payload["raw"])
        return None

    @staticmethod
    def _match_payload(match_data: str) -> dict[str, Any]:
        try:
            payload = json.loads(match_data or "{}")
            return payload if isinstance(payload, dict) else {}
        except ValueError, TypeError:
            return {}

    @staticmethod
    def _get_book(db: Session, book_id: int) -> Book:
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError("Book not found.")
        return book

    @staticmethod
    def _get_match(db: Session, book_id: int, match_id: int) -> MetadataMatch:
        match = db.get(MetadataMatch, match_id)
        if match is None or match.book_id != book_id:
            raise ValueError("Metadata match not found.")
        return match

    @staticmethod
    def _set_match_status(match: MetadataMatch, status: str) -> None:
        if match.status != status:
            match.status = status
            match.updated_at = utc_now()

    def _apply_series_edit(
        self,
        db: Session,
        book: Book,
        name: Any,
    ) -> None:
        from buku.models.book import Series

        cleaned = str(name or "").strip() if name is not None else ""
        if cleaned:
            series_row = db.scalar(select(Series).where(Series.name == cleaned))
            if series_row is None:
                series_row = Series(name=cleaned)
                db.add(series_row)
                db.flush()
            book.series_id = series_row.id
        else:
            book.series_id = None

    def _replace_authors(self, db: Session, book: Book, names: list[str]) -> None:
        for link in list(book.author_links):
            db.delete(link)
        book.author_links.clear()
        for name in names:
            if not name:
                continue
            author = db.scalar(select(Author).where(Author.name == name))
            if author is None:
                author = Author(name=name)
                db.add(author)
                db.flush()
            book.author_links.append(
                BookAuthor(book_id=book.id, author_id=author.id, role="author")
            )


review_service = MetadataReviewService()

__all__ = [
    "AppliedResult",
    "BookMetadataView",
    "BookReviewDetail",
    "CandidateView",
    "MetadataReviewService",
    "ReviewItem",
    "review_service",
]
