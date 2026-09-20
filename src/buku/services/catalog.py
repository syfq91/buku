"""Catalog browsing & media-serving queries for the web UI (Phase 9).

Read-only views over the catalog: book browsing with sorting and pagination,
recent additions, dashboard statistics, series/author detail, user shelves,
reading progress lookup, and safe download-path resolution. Every query here
is a pure read — nothing in this module ever writes to the media directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from buku.models.book import Author, Book, BookAuthor, BookFile, Series
from buku.models.collection import Collection, CollectionBook
from buku.models.progress import ReadingProgress

_BOOK_LOADS = (
    selectinload(Book.author_links).selectinload(BookAuthor.author),
    selectinload(Book.series),
    selectinload(Book.identifiers),
    selectinload(Book.files),
)


@dataclass
class CatalogPage:
    """A paged slice of books with pagination metadata."""

    items: list[Book]
    total: int
    offset: int = 0
    limit: int = 24

    @property
    def page(self) -> int:
        return (self.offset // self.limit) + 1 if self.limit else 1

    @property
    def pages(self) -> int:
        if not self.limit:
            return 1
        return max(1, (self.total + self.limit - 1) // self.limit)

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + self.limit < self.total


@dataclass
class DashboardStats:
    """Library-level counts for the dashboard."""

    books: int = 0
    series: int = 0
    authors: int = 0
    formats: dict[str, int] = field(default_factory=dict)


class CatalogService:
    """Read-only catalog queries backing the web pages."""

    # ------------------------------------------------------------------ #
    # Books
    # ------------------------------------------------------------------ #
    def list_books(
        self,
        db: Session,
        *,
        query: str | None = None,
        sort: str = "title",
        limit: int = 24,
        offset: int = 0,
    ) -> CatalogPage:
        """Return a paged, optionally filtered and sorted book slice."""
        stmt = select(Book).options(*_BOOK_LOADS)
        count_stmt = select(func.count()).select_from(Book)
        needle = query.strip() if query else ""
        if needle:
            pattern = f"%{needle}%"
            cond = Book.title.ilike(pattern) | Book.subtitle.ilike(pattern)
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        total = db.scalar(count_stmt) or 0
        order = Book.title.asc() if sort == "title" else Book.created_at.desc()
        books = db.scalars(stmt.order_by(order).limit(limit).offset(offset)).all()
        return CatalogPage(items=list(books), total=total, limit=limit, offset=offset)

    def get_book(self, db: Session, book_id: int) -> Book | None:
        """Load a single book with its relational fields populated."""
        return db.scalars(select(Book).where(Book.id == book_id).options(*_BOOK_LOADS)).first()

    def recent_books(self, db: Session, limit: int = 12) -> list[Book]:
        """Return the most recently added books."""
        return list(
            db.scalars(
                select(Book).options(*_BOOK_LOADS).order_by(Book.created_at.desc()).limit(limit)
            ).all()
        )

    def dashboard_stats(self, db: Session) -> DashboardStats:
        """Compute library-level counts for the dashboard."""
        stats = DashboardStats(
            books=db.scalar(select(func.count()).select_from(Book)) or 0,
            series=db.scalar(select(func.count()).select_from(Series)) or 0,
            authors=db.scalar(select(func.count()).select_from(Author)) or 0,
        )
        rows = db.execute(
            select(BookFile.file_format, func.count()).group_by(BookFile.file_format)
        ).all()
        stats.formats = {row[0]: row[1] for row in rows}
        return stats

    # ------------------------------------------------------------------ #
    # Series & authors
    # ------------------------------------------------------------------ #
    def get_series(self, db: Session, series_id: int) -> Series | None:
        """Load a series entity with its books."""
        return db.scalars(
            select(Series)
            .where(Series.id == series_id)
            .options(selectinload(Series.books).options(*_BOOK_LOADS))
        ).first()

    def get_author(self, db: Session, author_id: int) -> Author | None:
        """Load an author entity with their book links."""
        return db.scalars(
            select(Author)
            .where(Author.id == author_id)
            .options(
                selectinload(Author.book_links).selectinload(BookAuthor.book).options(*_BOOK_LOADS)
            )
        ).first()

    # ------------------------------------------------------------------ #
    # Shelves & progress
    # ------------------------------------------------------------------ #
    def list_collections(self, db: Session, user_id: int) -> list[Collection]:
        """Return the user's collections with their shelf contents."""
        return list(
            db.scalars(
                select(Collection)
                .where(Collection.user_id == user_id)
                .options(selectinload(Collection.books).selectinload(CollectionBook.book))
                .order_by(Collection.name.asc())
            ).all()
        )

    def progress_for_user(self, db: Session, user_id: int, book_id: int) -> ReadingProgress | None:
        """Return the user's reading progress for a book, if any."""
        return db.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_id, ReadingProgress.book_id == book_id
            )
        )

    def reading_shelf(
        self, db: Session, user_id: int, limit: int = 8
    ) -> list[tuple[ReadingProgress, Book]]:
        """Return the user's most recently touched reading progress entries."""
        rows = list(
            db.scalars(
                select(ReadingProgress)
                .where(ReadingProgress.user_id == user_id)
                .order_by(ReadingProgress.modified_at.desc())
                .limit(limit)
                .options(selectinload(ReadingProgress.book).options(*_BOOK_LOADS))
            ).all()
        )
        return [(row, row.book) for row in rows if row.book is not None]

    def books_by_ids(self, db: Session, book_ids: list[int]) -> dict[int, Book]:
        """Fetch books by id (used to hydrate full-text search results)."""
        if not book_ids:
            return {}
        rows = db.scalars(select(Book).where(Book.id.in_(book_ids)).options(*_BOOK_LOADS)).all()
        return {book.id: book for book in rows}

    # ------------------------------------------------------------------ #
    # Media serving
    # ------------------------------------------------------------------ #
    def resolve_file(self, db: Session, book_id: int, file_id: int) -> BookFile | None:
        """Return a file belonging to the book, or None if it is not attached.

        Missing files are returned too so the caller can distinguish "this
        file never belonged to the book" (404) from "the file has gone
        missing" (410).
        """
        book = self.get_book(db, book_id)
        if book is None:
            return None
        for file_row in book.files:
            if file_row.id == file_id:
                return file_row
        return None

    def file_download_path(self, file_row: BookFile) -> Path | None:
        """Resolve the on-disk path for a file, confined to its library root.

        Defense-in-depth: even though file paths originate from the scanner,
        the resolved path must sit inside the owning library root or we refuse
        to serve it.
        """
        path = Path(file_row.file_path).resolve()
        if file_row.book is None or file_row.book.library is None:
            return None
        root = Path(file_row.book.library.path).resolve()
        if not path.is_relative_to(root):
            return None
        return path


catalog_service = CatalogService()
