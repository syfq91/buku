"""Metadata provenance persistence service (Phase 6).

Every imported book attribute tracks its source: ``embedded``, ``google_books``,
``user``, or ``filename``. Provenance is stored per (book, field) in the
``metadata_provenance`` table so automated jobs can distinguish weak fallbacks
from manual edits and can *never* overwrite user-edited fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from buku.metadata.models import SOURCE_DESCRIPTIONS, SOURCE_USER
from buku.models.base import utc_now
from buku.models.metadata import MetadataProvenance, MetadataSource


class ProvenanceService:
    """Read and record per-field metadata provenance for books."""

    def ensure_sources(self, db: Session) -> None:
        """Idempotently seed the metadata_sources registry."""

        for name, description in SOURCE_DESCRIPTIONS.items():
            self.get_or_create_source(db, name, description)
        db.flush()

    def get_or_create_source(
        self, db: Session, name: str, description: str | None = None
    ) -> MetadataSource:
        """Return the source row for ``name``, creating it if necessary."""
        source = db.scalar(select(MetadataSource).where(MetadataSource.name == name))
        if source is None:
            source = MetadataSource(name=name, description=description)
            db.add(source)
            db.flush()
        return source

    def mark(self, db: Session, book_id: int, field_name: str, source_name: str) -> None:
        """Upsert the provenance record for a single (book, field)."""
        source = self.get_or_create_source(db, source_name, SOURCE_DESCRIPTIONS.get(source_name))
        existing = db.scalar(
            select(MetadataProvenance).where(
                MetadataProvenance.book_id == book_id,
                MetadataProvenance.field_name == field_name,
            )
        )
        now: datetime = utc_now()
        if existing is not None:
            existing.source_id = source.id
            existing.updated_at = now
            return
        db.add(
            MetadataProvenance(
                book_id=book_id,
                field_name=field_name,
                source_id=source.id,
                updated_at=now,
            )
        )

    def mark_many(self, db: Session, book_id: int, field_sources: dict[str, str]) -> None:
        """Record provenance for a mapping of ``{field_name: source_name}``."""
        for field_name, source_name in field_sources.items():
            self.mark(db, book_id, field_name, source_name)

    def get_provenance(self, db: Session, book_id: int) -> dict[str, str]:
        """Return ``{field_name: source_name}`` for a book."""
        rows = db.scalars(
            select(MetadataProvenance).where(MetadataProvenance.book_id == book_id)
        ).all()
        return {row.field_name: row.source.name for row in rows}

    def user_edited_fields(self, db: Session, book_id: int) -> set[str]:
        """Return the set of fields a user has manually edited."""
        return {
            field
            for field, source in self.get_provenance(db, book_id).items()
            if source == SOURCE_USER
        }

    def is_user_edited(self, db: Session, book_id: int, field_name: str) -> bool:
        """True if the given field carries user provenance."""
        return self.get_provenance(db, book_id).get(field_name) == SOURCE_USER


provenance_service = ProvenanceService()

__all__ = ["ProvenanceService", "provenance_service"]
