"""Metadata enrichment service (Phase 6 acceptance criteria).

Drives the matching priority flow against the configured providers, persists
candidate matches for later review (Phase 7 UI), and auto-applies the best
high-confidence match to fill *missing* fields — never user-edited ones.

Application rules (AGENTS.md Rule 4 & Phase 6 IMPORTANT):
- Only genuinely missing fields (null/empty) are populated.
- Fields marked ``user`` in provenance are never touched by automation.
- Applied fields are re-recorded with ``google_books`` provenance.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from buku.metadata.matching import DEFAULT_MIN_CONFIDENCE, build_query
from buku.metadata.models import (
    SOURCE_GOOGLE_BOOKS,
    MetadataMatch,
    MetadataQuery,
    populated_fields,
)
from buku.metadata.provenance import provenance_service
from buku.metadata.provider import MetadataProvider, get_default_providers
from buku.models.base import utc_now
from buku.models.book import Author, Book, BookAuthor, BookIdentifier, Series
from buku.models.metadata import MetadataMatch as MetadataMatchRecord

logger = logging.getLogger("buku.metadata.enrichment")

# Candidate match rows stored per book per pass, capped to avoid unbounded growth.
MAX_CANDIDATES_PER_BOOK = 10


@dataclass
class EnrichmentResult:
    """Outcome of one enrichment pass for a single book."""

    book_id: int
    query: MetadataQuery
    candidates: list[MetadataMatch] = field(default_factory=list)
    accepted: MetadataMatch | None = None
    fields_applied: list[str] = field(default_factory=list)


class MetadataEnrichmentService:
    """Search external providers and apply missing metadata safely."""

    def __init__(
        self,
        *,
        providers: list[MetadataProvider] | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self.providers = providers if providers is not None else get_default_providers()
        self.min_confidence = min_confidence
        self.provenance = provenance_service

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def enrich_book(self, db: Session, book_id: int) -> EnrichmentResult:
        """Find candidates for a book and apply the best accepted match."""
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"Book with id={book_id} not found.")

        self.provenance.ensure_sources(db)
        query = build_query(book)
        result = EnrichmentResult(book_id=book_id, query=query)
        if not query.has_content:
            return result

        candidates = self._collect_candidates(db, book.id, query)
        result.candidates = candidates
        candidates.sort(key=lambda m: m.confidence, reverse=True)

        accepted = next((m for m in candidates if m.confidence >= self.min_confidence), None)
        if accepted is None:
            return result

        result.accepted = accepted
        result.fields_applied = self._apply_candidate(db, book, accepted)
        if result.fields_applied:
            self.provenance.mark_many(
                db, book.id, {field: SOURCE_GOOGLE_BOOKS for field in result.fields_applied}
            )
        # Record the accepted candidate regardless so the Phase 7 review UI can
        # audit (and re-apply) the automatic decision.
        self._record_acceptance(db, book.id, accepted)
        db.flush()
        return result

    # ------------------------------------------------------------------ #
    # Candidate discovery & persistence
    # ------------------------------------------------------------------ #
    def _collect_candidates(
        self, db: Session, book_id: int, query: MetadataQuery
    ) -> list[MetadataMatch]:
        """Run every provider and upsert candidate rows for later review."""
        candidates: list[MetadataMatch] = []
        for provider in self.providers:
            try:
                provider_matches = provider.search(query)
            except Exception as exc:
                logger.warning("Provider '%s' failed for query: %s", provider.name, exc)
                continue
            candidates.extend(provider_matches)

        for match in candidates[:MAX_CANDIDATES_PER_BOOK]:
            self._upsert_match_row(db, book_id, match)
        db.flush()
        return candidates

    def _upsert_match_row(self, db: Session, book_id: int, match: MetadataMatch) -> None:
        """Insert-or-update a metadata_matches row keyed by (book, source, external_id)."""
        source = self.provenance.get_or_create_source(db, SOURCE_GOOGLE_BOOKS)
        row = db.scalar(
            select(MetadataMatchRecord).where(
                MetadataMatchRecord.book_id == book_id,
                MetadataMatchRecord.source_id == source.id,
                MetadataMatchRecord.external_id == match.external_id,
            )
        )
        payload = json.dumps(
            {
                "provider": match.provider,
                "external_id": match.external_id,
                "confidence": match.confidence,
                "fields": sorted(match.metadata.sources.keys()),
                "raw": match.raw,
            },
            default=str,
        )
        if row is None:
            db.add(
                MetadataMatchRecord(
                    book_id=book_id,
                    source_id=source.id,
                    external_id=match.external_id,
                    confidence_score=match.confidence,
                    match_data=payload,
                    status="pending",
                )
            )
        else:
            row.confidence_score = match.confidence
            row.match_data = payload
            row.updated_at = utc_now()

    # ------------------------------------------------------------------ #
    # Field application (never overwrites user edits)
    # ------------------------------------------------------------------ #
    def _apply_candidate(self, db: Session, book: Book, match: MetadataMatch) -> list[str]:
        """Apply missing, non-user-edited fields from the accepted match."""
        meta = match.metadata
        user_fields = self.provenance.user_edited_fields(db, book.id)
        candidate_fields = populated_fields(meta)
        applied: list[str] = []

        if (
            book.subtitle is None
            and "subtitle" in candidate_fields
            and "subtitle" not in user_fields
        ):
            book.subtitle = meta.subtitle
            applied.append("subtitle")
        if (
            book.description is None
            and "description" in candidate_fields
            and "description" not in user_fields
        ):
            book.description = meta.description
            applied.append("description")
        if (
            book.publisher is None
            and "publisher" in candidate_fields
            and "publisher" not in user_fields
        ):
            book.publisher = meta.publisher
            applied.append("publisher")
        if "published_date" in candidate_fields and "published_date" not in user_fields:
            if book.published_date is None:
                book.published_date = meta.published_date
                applied.append("published_date")
        if "language" in candidate_fields and "language" not in user_fields:
            if book.language is None:
                book.language = meta.language
                applied.append("language")
        if "series" in candidate_fields and "series" not in user_fields:
            if self._series_missing(book):
                self._apply_series(db, book, meta.series, meta.series_index)
                applied.append("series")
        if "authors" in candidate_fields and "authors" not in user_fields:
            if not book.author_links:
                self._apply_authors(db, book, meta.authors)
                applied.append("authors")
        if "identifiers" in candidate_fields and "identifiers" not in user_fields:
            isbn = meta.identifiers.get("isbn")
            if isbn and not self._has_identifier(db, book.id, "isbn"):
                db.add(
                    BookIdentifier(
                        book_id=book.id,
                        identifier_type="isbn",
                        identifier_value=isbn.strip(),
                    )
                )
                applied.append("identifiers")

        if applied:
            book.updated_at = utc_now()
        return applied

    # ------------------------------------------------------------------ #
    # Field-level helpers
    # ------------------------------------------------------------------ #
    def _series_missing(self, book: Book) -> bool:
        return book.series_id is None

    def _apply_series(
        self,
        db: Session,
        book: Book,
        series_name: str | None,
        series_index: float | None,
    ) -> None:
        name = (series_name or "").strip()
        if not name:
            return
        series = db.scalar(select(Series).where(Series.name == name))
        if series is None:
            series = Series(name=name)
            db.add(series)
            db.flush()
        book.series_id = series.id
        book.series_index = series_index

    def _apply_authors(self, db: Session, book: Book, author_names: list[str]) -> None:
        for name in author_names:
            cleaned = name.strip()
            if not cleaned:
                continue
            author = db.scalar(select(Author).where(Author.name == cleaned))
            if author is None:
                author = Author(name=cleaned)
                db.add(author)
                db.flush()
            db.add(BookAuthor(book_id=book.id, author_id=author.id, role="author"))

    def _has_identifier(self, db: Session, book_id: int, identifier_type: str) -> bool:
        return (
            db.scalar(
                select(BookIdentifier.id).where(
                    BookIdentifier.book_id == book_id,
                    BookIdentifier.identifier_type == identifier_type,
                )
            )
            is not None
        )

    # ------------------------------------------------------------------ #
    # Acceptance bookkeeping
    # ------------------------------------------------------------------ #
    def _record_acceptance(self, db: Session, book_id: int, match: MetadataMatch) -> None:
        """Mark candidate rows for the accepted match as ``applied``."""
        source = self.provenance.get_or_create_source(db, SOURCE_GOOGLE_BOOKS)
        rows = db.scalars(
            select(MetadataMatchRecord).where(
                MetadataMatchRecord.book_id == book_id,
                MetadataMatchRecord.source_id == source.id,
                MetadataMatchRecord.external_id == match.external_id,
            )
        ).all()
        for row in rows:
            if row.status != "applied":
                row.status = "applied"
                row.updated_at = utc_now()


__all__ = [
    "EnrichmentResult",
    "EnrichmentRunStats",
    "MAX_CANDIDATES_PER_BOOK",
    "MetadataEnrichmentService",
    "process_metadata_jobs",
]


@dataclass
class EnrichmentRunStats:
    """Aggregated metrics from draining ``metadata_lookup`` jobs."""

    jobs_seen: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    books_enriched: int = 0
    fields_applied: int = 0


def process_metadata_jobs(
    db: Session,
    *,
    service: MetadataEnrichmentService | None = None,
    limit: int = 50,
) -> EnrichmentRunStats:
    """Synchronously drain queued ``metadata_lookup`` jobs (Phase 17 worker starter).

    Marks each job ``running``, enriches the referenced book, then completes or
    fails it using the Job model's attempt/retry fields. Callers are expected to
    commit the session afterwards.
    """
    from buku.models.job import Job

    active_service = service or MetadataEnrichmentService()
    stats = EnrichmentRunStats()
    jobs = db.scalars(
        select(Job)
        .where(Job.type == "metadata_lookup", Job.status == "queued")
        .order_by(Job.created_at)
        .limit(max(limit, 1))
    ).all()
    stats.jobs_seen = len(jobs)

    for job in jobs:
        job.status = "running"
        job.started_at = utc_now()
        job.attempts = (job.attempts or 0) + 1
        try:
            payload = json.loads(job.payload or "{}")
            book_id = payload.get("book_id")
            if not isinstance(book_id, int):
                raise ValueError("metadata_lookup payload missing numeric 'book_id'.")
            result = active_service.enrich_book(db, book_id)
            job.status = "completed"
            job.error = None
            job.finished_at = utc_now()
            stats.jobs_completed += 1
            stats.books_enriched += 1
            stats.fields_applied += len(result.fields_applied)
        except Exception as exc:
            job.error = str(exc)[:2000]
            job.finished_at = utc_now()
            if (job.attempts or 0) >= (job.max_attempts or 3):
                job.status = "failed"
            else:
                job.status = "queued"  # retried on the next drain pass
            stats.jobs_failed += 1
            logger.warning("metadata_lookup job %s failed: %s", job.id, exc)

    db.flush()
    return stats
