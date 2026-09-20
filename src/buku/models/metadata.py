"""Metadata provenance and candidate match models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from buku.models.base import Base, utc_now

if TYPE_CHECKING:
    from buku.models.book import Book


class MetadataSource(Base):
    """Metadata provenance source (embedded, google_books, user, filename)."""

    __tablename__ = "metadata_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    matches: Mapped[list[MetadataMatch]] = relationship("MetadataMatch", back_populates="source")
    provenance_entries: Mapped[list[MetadataProvenance]] = relationship(
        "MetadataProvenance", back_populates="source", cascade="all, delete-orphan"
    )


class MetadataProvenance(Base):
    """Per-field provenance for a book's metadata attributes.

    Each (book, field) tracks which source populated it so automated enrichment
    and rescans can never overwrite user-edited values.
    """

    __tablename__ = "metadata_provenance"
    __table_args__ = (UniqueConstraint("book_id", "field_name", name="uq_book_field_provenance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("metadata_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    book: Mapped[Book] = relationship("Book", back_populates="metadata_provenance")
    source: Mapped[MetadataSource] = relationship(
        "MetadataSource", back_populates="provenance_entries"
    )


class MetadataMatch(Base):
    """Candidate metadata matches retrieved from external providers for review."""

    __tablename__ = "metadata_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("metadata_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    match_data: Mapped[str] = mapped_column(Text, nullable=False)  # JSON payload
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # pending, applied, rejected
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    book: Mapped[Book] = relationship("Book", back_populates="metadata_matches")
    source: Mapped[MetadataSource] = relationship("MetadataSource", back_populates="matches")
