"""User personal collections and bookshelves models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from buku.models.base import Base, utc_now

if TYPE_CHECKING:
    from buku.models.book import Book
    from buku.models.user import User


class Collection(Base):
    """User-created collection / shelf."""

    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_collection_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="collections")
    books: Mapped[list[CollectionBook]] = relationship(
        "CollectionBook", back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionBook(Base):
    """Association entity linking collections and books with order index."""

    __tablename__ = "collection_books"

    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    collection: Mapped[Collection] = relationship("Collection", back_populates="books")
    book: Mapped[Book] = relationship("Book", back_populates="collection_links")
