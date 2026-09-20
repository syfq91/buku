"""Alternative book representation (e.g. X4 e-ink EPUB) model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from buku.models.base import Base, utc_now

if TYPE_CHECKING:
    from buku.models.book import Book


class Representation(Base):
    """Cached alternative format representation (e.g. X4 profile)."""

    __tablename__ = "representations"
    __table_args__ = (
        UniqueConstraint("book_id", "profile", "source_hash", name="uq_book_representation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. x4
    format: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g. epub
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    optimizer_version: Mapped[str] = mapped_column(String(50), nullable=False)
    cache_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    book: Mapped[Book] = relationship("Book", back_populates="representations")
