"""Metadata provenance and candidate match models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
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
