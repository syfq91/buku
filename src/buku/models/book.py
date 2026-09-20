"""Book, BookFile, Series, Author, and Identifier models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    from buku.models.collection import CollectionBook
    from buku.models.library import Library
    from buku.models.metadata import MetadataMatch
    from buku.models.progress import ReadingProgress
    from buku.models.representation import Representation


class Series(Base):
    """Book series grouping entity."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    books: Mapped[list[Book]] = relationship("Book", back_populates="series")


class Author(Base):
    """Author, editor, or contributor entity."""

    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    sort_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    book_links: Mapped[list[BookAuthor]] = relationship(
        "BookAuthor", back_populates="author", cascade="all, delete-orphan"
    )


class BookAuthor(Base):
    """Many-to-many relationship between books and authors with roles."""

    __tablename__ = "book_authors"

    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), primary_key=True
    )
    author_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), primary_key=True, default="author")

    book: Mapped[Book] = relationship("Book", back_populates="author_links")
    author: Mapped[Author] = relationship("Author", back_populates="book_links")


class BookIdentifier(Base):
    """External standard identifiers (ISBN-13, ISBN-10, ASIN, Google Books)."""

    __tablename__ = "book_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "book_id", "identifier_type", "identifier_value", name="uq_book_identifier"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identifier_type: Mapped[str] = mapped_column(String(50), nullable=False)
    identifier_value: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    book: Mapped[Book] = relationship("Book", back_populates="identifiers")


class Book(Base):
    """Logical book entity. Multiple formats (EPUB, PDF, CBZ) link to this book."""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    series_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True
    )
    series_index: Mapped[float | None] = mapped_column(Float, nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    subtitle: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_date: Mapped[str | None] = mapped_column(String(50), nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cover_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    library: Mapped[Library] = relationship("Library", back_populates="books")
    series: Mapped[Series | None] = relationship("Series", back_populates="books")
    files: Mapped[list[BookFile]] = relationship(
        "BookFile", back_populates="book", cascade="all, delete-orphan"
    )
    author_links: Mapped[list[BookAuthor]] = relationship(
        "BookAuthor", back_populates="book", cascade="all, delete-orphan"
    )
    identifiers: Mapped[list[BookIdentifier]] = relationship(
        "BookIdentifier", back_populates="book", cascade="all, delete-orphan"
    )
    reading_progress: Mapped[list[ReadingProgress]] = relationship(
        "ReadingProgress", back_populates="book", cascade="all, delete-orphan"
    )
    metadata_matches: Mapped[list[MetadataMatch]] = relationship(
        "MetadataMatch", back_populates="book", cascade="all, delete-orphan"
    )
    representations: Mapped[list[Representation]] = relationship(
        "Representation", back_populates="book", cascade="all, delete-orphan"
    )
    collection_links: Mapped[list[CollectionBook]] = relationship(
        "CollectionBook", back_populates="book", cascade="all, delete-orphan"
    )


class BookFile(Base):
    """Physical file representation associated with a logical book."""

    __tablename__ = "book_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_path: Mapped[str] = mapped_column(String(1000), unique=True, nullable=False)
    file_format: Mapped[str] = mapped_column(String(20), nullable=False)  # epub, pdf, cbz
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # sha256
    file_mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    book: Mapped[Book] = relationship("Book", back_populates="files")
