"""SQLite FTS5 full-text search service for the buku catalog.

Phase 8: powers ``GET /api/v1/search`` without any external search engine.
The index is a *stored* FTS5 virtual table (``books_fts``) where ``rowid``
always equals ``books.id``. It is not an external-content table because
several indexed fields — ``authors``, ``series``, ``isbn`` — live in join
tables rather than on ``books`` itself, so application code keeps the index
in sync with the catalog:

- the scanner indexes every book it creates,
- enrichment re-indexes after applying provider fields,
- the Phase 7 review service re-indexes after curation actions,
- ``bookserver reindex`` rebuilds the whole index,
- the Phase 8 migration creates the table and backfills existing books.

``subjects`` and ``tags`` are reserved index columns: the ``Book`` model does
not persist them yet, so they index as empty strings until the metadata model
grows to support them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from buku.models.book import Book, BookAuthor

FTS_TABLE = "books_fts"

# Alphanumeric runs (Unicode-aware). The unicode61 tokenizer ignores
# punctuation anyway, so stripping it here also normalizes queries such as
# ISBN "978-0-441-01359-3" into a single clean token.
_TOKENIZER = re.compile(r"\w+", re.UNICODE)


@dataclass
class SearchResultItem:
    """A single catalog entry surfaced by a search query."""

    book_id: int
    title: str
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    series: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    cover_path: str | None = None
    rank: float | None = None


@dataclass
class SearchResults:
    """Paged results for a search query."""

    query: str
    total: int
    limit: int
    offset: int
    items: list[SearchResultItem] = field(default_factory=list)


def _tokenize(query: str) -> list[str]:
    """Split a raw query into normalized lowercase search terms."""
    return [token.lower() for token in _TOKENIZER.findall(query)]


def _build_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression from a raw user query.

    Every term is quoted as a phrase and given a trailing ``*`` prefix
    wildcard so the search behaves like search-as-you-type (``dun`` matches
    ``Dune``). Terms are AND-combined. Returns ``None`` when the query has no
    usable terms.
    """
    terms = _tokenize(query)
    if not terms:
        return None
    return " AND ".join(f'"{term}"*' for term in terms)


class SearchService:
    """Maintains and queries the ``books_fts`` FTS5 index."""

    # ------------------------------------------------------------------ #
    # Index maintenance
    # ------------------------------------------------------------------ #
    def index_book(self, db: Session, book: Book) -> bool:
        """Upsert (or insert) the FTS row for a book.

        FTS5 tables treat ``rowid`` as a unique key, so re-inserting an
        existing row raises a constraint error; ``INSERT OR REPLACE`` (delete
        + insert) is required for re-indexing an updated book. The caller's
        unflushed changes are flushed first so relationship reads observe the
        latest state. Returns ``True`` when the row was written.
        """
        db.flush()
        values = self._index_values(db, book)
        db.execute(
            text(
                f"INSERT OR REPLACE INTO {FTS_TABLE}("
                "rowid, title, subtitle, authors, series, description, "
                "publisher, subjects, isbn, tags"
                ") VALUES ("
                ":rowid, :title, :subtitle, :authors, :series, :description, "
                ":publisher, :subjects, :isbn, :tags"
                ")"
            ),
            values,
        )
        return True

    def remove_book(self, db: Session, book_id: int) -> None:
        """Remove a book's row from the index (used when a book is deleted)."""
        db.execute(
            text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :rowid"),
            {"rowid": book_id},
        )

    def rebuild(self, db: Session) -> int:
        """Drop every index row and re-index the full catalog. Returns count."""
        db.execute(text(f"DELETE FROM {FTS_TABLE}"))
        books = db.scalars(
            select(Book)
            .options(
                selectinload(Book.author_links).selectinload(BookAuthor.author),
                selectinload(Book.series),
                selectinload(Book.identifiers),
            )
            .order_by(Book.id)
        ).all()
        for book in books:
            self.index_book(db, book)
        return len(books)

    # ------------------------------------------------------------------ #
    # Querying
    # ------------------------------------------------------------------ #
    def search(
        self,
        db: Session,
        query: str,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResults:
        """Run an FTS5 query, ranked by bm25 relevance, with pagination."""
        match_expr = _build_match_query(query)
        if match_expr is None:
            return SearchResults(query=query, total=0, limit=limit, offset=offset)

        total = (
            db.scalar(
                text(f"SELECT count(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :q"),
                {"q": match_expr},
            )
            or 0
        )

        rows = db.execute(
            text(
                f"SELECT rowid, rank FROM {FTS_TABLE} "
                f"WHERE {FTS_TABLE} MATCH :q "
                "ORDER BY rank LIMIT :limit OFFSET :offset"
            ),
            {"q": match_expr, "limit": limit, "offset": offset},
        ).all()

        book_ids = [cast(int, row[0]) for row in rows]
        rank_by_id = {cast(int, row[0]): cast(float, row[1]) for row in rows}
        if not book_ids:
            return SearchResults(query=query, total=total, limit=limit, offset=offset)

        books = db.scalars(
            select(Book)
            .options(
                selectinload(Book.author_links).selectinload(BookAuthor.author),
                selectinload(Book.series),
                selectinload(Book.identifiers),
            )
            .where(Book.id.in_(book_ids))
        ).all()
        by_id = {book.id: book for book in books}

        items = [
            self._result_item(by_id[book_id], rank_by_id.get(book_id))
            for book_id in book_ids
            if book_id in by_id
        ]
        return SearchResults(
            query=query,
            total=total,
            limit=limit,
            offset=offset,
            items=items,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _index_values(self, db: Session, book: Book) -> dict[str, object]:
        """Compute the indexed strings for a book (avoids N+1 via loaders)."""
        del db  # relationships are loaded eagerly or lazily by the caller
        authors = ", ".join(
            link.author.name for link in book.author_links if link.author is not None
        )
        series = book.series.name if book.series is not None else ""
        isbn_values = ", ".join(
            identifier.identifier_value
            for identifier in book.identifiers
            if identifier.identifier_type in ("isbn", "isbn_10")
        )
        return {
            "rowid": book.id,
            "title": book.title or "",
            "subtitle": book.subtitle or "",
            "authors": authors,
            "series": series,
            "description": book.description or "",
            "publisher": book.publisher or "",
            "subjects": "",
            "isbn": isbn_values,
            "tags": "",
        }

    def _result_item(self, book: Book, rank: float | None) -> SearchResultItem:
        """Build a result item from a book ORM object."""
        return SearchResultItem(
            book_id=book.id,
            title=book.title,
            subtitle=book.subtitle,
            authors=[link.author.name for link in book.author_links if link.author],
            series=book.series.name if book.series is not None else None,
            description=book.description,
            publisher=book.publisher,
            published_date=book.published_date,
            language=book.language,
            cover_path=book.cover_path,
            rank=rank,
        )


search_service = SearchService()
