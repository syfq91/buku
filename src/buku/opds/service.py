"""OPDS catalog service: maps the internal catalog onto OPDS feed models.

Phase 12: builds the root navigation feed, the ``/opds/books`` acquisition
feed with RFC 5005 pagination links, series/author navigation feeds and their
per-series / per-author acquisition sub-feeds, and the ``/opds/search``
acquisition feed backed by the Phase 8 FTS5 ``SearchService``.

Phase 16: the same feed shapes under the ``/opds/x4`` prefix, restricted to
books that can be represented by the ``x4`` profile. Acquisition links point
at the on-demand endpoint ``/opds/x4/books/{id}.epub`` instead of per-file
downloads; generation/caching stays in :class:`RepresentationService`.

Every feed entry carries acquisition links for the book's available formats,
cover/thumbnail artwork links, and an OPDS Progression 1.0 service link so
external readers can discover and sync reading state (Phase 13). Only pure
reads happen here — nothing ever writes to the media directory.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session, selectinload

from buku.models.book import Author, Book, BookAuthor, BookFile, Series
from buku.opds.models import (
    ACQUISITION_FEED_TYPE,
    KIND_ACQUISITION,
    NAVIGATION_FEED_TYPE,
    OPDS_PAGE_SIZE,
    PROGRESSION_TYPE,
    REL_ACQUISITION_OPEN_ACCESS,
    REL_IMAGE,
    REL_IMAGE_THUMBNAIL,
    REL_PROGRESSION,
    REL_SEARCH,
    REL_SELF,
    REL_START,
    REL_UP,
    OpdsAuthor,
    OpdsEntry,
    OpdsFeed,
    OpdsLink,
    cover_media_type,
    entity_id,
    feed_id,
    format_media_type,
)

X4_PREFIX = "/opds/x4"
X4_MEDIA_TYPE = "application/epub+zip"

_BOOK_LOADS = (
    selectinload(Book.author_links).selectinload(BookAuthor.author),
    selectinload(Book.series),
    selectinload(Book.identifiers),
    selectinload(Book.files),
)


def _as_utc(value: datetime) -> datetime:
    """Normalize a (possibly naive) SQLite datetime to an aware UTC instant."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _latest_updated(books: list[Book]) -> datetime:
    """Feed ``updated`` timestamp: newest book in the slice, or now."""
    if not books:
        return datetime.now(UTC)
    return max(_as_utc(book.updated_at) for book in books)


def _cover_href(base_url: str, cover_path: str) -> str | None:
    """Absolute ``/covers/`` href for a cached cover, or None when unusable."""
    name = Path(cover_path).name
    if not name:
        return None
    return f"{base_url}/covers/{quote(name)}"


class OpdsCatalogService:
    """Queries the catalog and builds OPDS feed presentation models."""

    # ------------------------------------------------------------------ #
    # Root / navigation feeds
    # ------------------------------------------------------------------ #
    def root(self, db: Session, base_url: str) -> OpdsFeed:
        """The OPDS Catalog Root: a navigation feed of catalog sections."""
        links = [
            OpdsLink("self", f"{base_url}/opds", NAVIGATION_FEED_TYPE),
            OpdsLink(REL_START, f"{base_url}/opds", NAVIGATION_FEED_TYPE),
            OpdsLink(
                REL_SEARCH,
                f"{base_url}/opds/opensearch.xml",
                "application/opensearchdescription+xml",
                title="Search",
            ),
        ]
        entries = [
            OpdsEntry(
                id=feed_id(base_url, "/opds/books"),
                title="Books",
                updated=datetime.now(UTC),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}/opds/books",
                        ACQUISITION_FEED_TYPE,
                        title="Browse all books",
                        count=self._count(db, Book),
                    )
                ],
            ),
            OpdsEntry(
                id=feed_id(base_url, "/opds/series"),
                title="Series",
                updated=datetime.now(UTC),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}/opds/series",
                        NAVIGATION_FEED_TYPE,
                        title="Browse by series",
                        count=self._count(db, Series),
                    )
                ],
            ),
            OpdsEntry(
                id=feed_id(base_url, "/opds/authors"),
                title="Authors",
                updated=datetime.now(UTC),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}/opds/authors",
                        NAVIGATION_FEED_TYPE,
                        title="Browse by author",
                        count=self._count(db, Author),
                    )
                ],
            ),
        ]
        return OpdsFeed(
            id=feed_id(base_url, "/opds"),
            title="buku Catalog",
            updated=datetime.now(UTC),
            kind="navigation",
            links=links,
            entries=entries,
        )

    def series_listing(self, db: Session, base_url: str) -> OpdsFeed:
        """Navigation feed listing every series with its book count."""
        rows = db.execute(
            select(Series, func.count(Book.id))
            .outerjoin(Book, Book.series_id == Series.id)
            .group_by(Series.id)
            .order_by(Series.name.asc())
        ).all()
        entries = [
            OpdsEntry(
                id=entity_id("series", series.id),
                title=series.name,
                updated=_as_utc(series.updated_at),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}/opds/series/{series.id}",
                        ACQUISITION_FEED_TYPE,
                        title=f"{series.name} books",
                        count=int(count_rows),
                    )
                ],
            )
            for series, count_rows in rows
        ]
        return OpdsFeed(
            id=feed_id(base_url, "/opds/series"),
            title="Series",
            updated=datetime.now(UTC),
            kind="navigation",
            links=self._navigation_links(base_url, "/opds/series"),
            entries=entries,
            total_results=len(entries),
        )

    def author_listing(self, db: Session, base_url: str) -> OpdsFeed:
        """Navigation feed listing every author with their book count."""
        rows = db.execute(
            select(Author, func.count(BookAuthor.book_id))
            .outerjoin(BookAuthor, BookAuthor.author_id == Author.id)
            .group_by(Author.id)
            .order_by(Author.sort_name.asc(), Author.name.asc())
        ).all()
        entries = [
            OpdsEntry(
                id=entity_id("author", author.id),
                title=author.name,
                updated=_as_utc(author.updated_at),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}/opds/authors/{author.id}",
                        ACQUISITION_FEED_TYPE,
                        title=f"Books by {author.name}",
                        count=int(count_rows),
                    )
                ],
            )
            for author, count_rows in rows
        ]
        return OpdsFeed(
            id=feed_id(base_url, "/opds/authors"),
            title="Authors",
            updated=datetime.now(UTC),
            kind="navigation",
            links=self._navigation_links(base_url, "/opds/authors"),
            entries=entries,
            total_results=len(entries),
        )

    # ------------------------------------------------------------------ #
    # Acquisition feeds
    # ------------------------------------------------------------------ #
    def books(
        self,
        db: Session,
        base_url: str,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed:
        """Complete acquisition feed of the catalog, paginated by title."""
        return self._books_feed(
            db,
            base_url,
            path="/opds/books",
            title="Books",
            stmt=select(Book).options(*_BOOK_LOADS),
            count_stmt=select(func.count()).select_from(Book),
            up="/opds",
            page=page,
            per_page=per_page,
        )

    def series_books(
        self,
        db: Session,
        base_url: str,
        series_id: int,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed | None:
        """Acquisition feed for one series, or None when the series is gone."""
        series = db.get(Series, series_id)
        if series is None:
            return None
        return self._books_feed(
            db,
            base_url,
            path=f"/opds/series/{series_id}",
            title=series.name,
            stmt=select(Book).where(Book.series_id == series_id).options(*_BOOK_LOADS),
            count_stmt=select(func.count()).select_from(Book).where(Book.series_id == series_id),
            up="/opds/series",
            page=page,
            per_page=per_page,
        )

    # ------------------------------------------------------------------ #
    # Phase 16 — On-demand X4 catalog
    # ------------------------------------------------------------------ #
    def x4_root(self, db: Session, base_url: str) -> OpdsFeed:
        """Navigation root of the X4 catalog (books, series, search)."""
        links = [
            OpdsLink("self", f"{base_url}{X4_PREFIX}", NAVIGATION_FEED_TYPE),
            OpdsLink(REL_START, f"{base_url}{X4_PREFIX}", NAVIGATION_FEED_TYPE),
            OpdsLink(
                REL_SEARCH,
                f"{base_url}{X4_PREFIX}/search",
                ACQUISITION_FEED_TYPE,
                title="Search",
            ),
        ]
        entries = [
            OpdsEntry(
                id=feed_id(base_url, f"{X4_PREFIX}/books"),
                title="Books",
                updated=datetime.now(UTC),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}{X4_PREFIX}/books",
                        ACQUISITION_FEED_TYPE,
                        title="Browse all books (X4)",
                        count=self._x4_count(db),
                    )
                ],
            ),
            OpdsEntry(
                id=feed_id(base_url, f"{X4_PREFIX}/series"),
                title="Series",
                updated=datetime.now(UTC),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}{X4_PREFIX}/series",
                        NAVIGATION_FEED_TYPE,
                        title="Browse by series (X4)",
                    )
                ],
            ),
        ]
        return OpdsFeed(
            id=feed_id(base_url, X4_PREFIX),
            title="buku Catalog (X4)",
            updated=datetime.now(UTC),
            kind="navigation",
            links=links,
            entries=entries,
        )

    def x4_books(
        self,
        db: Session,
        base_url: str,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed:
        """Acquisition feed of books representable by the ``x4`` profile."""
        return self._books_feed(
            db,
            base_url,
            path=f"{X4_PREFIX}/books",
            title="Books (X4)",
            stmt=self._x4_book_select(),
            count_stmt=self._x4_count_stmt(),
            up=X4_PREFIX,
            start=X4_PREFIX,
            profile="x4",
            page=page,
            per_page=per_page,
        )

    def x4_series_listing(self, db: Session, base_url: str) -> OpdsFeed:
        """Navigation feed of series that contain at least one X4-ready book."""
        rows = db.execute(
            select(Series, func.count(Book.id))
            .join(Book, Book.series_id == Series.id)
            .where(self._x4_ready_clause())
            .group_by(Series.id)
            .order_by(Series.name.asc())
        ).all()
        entries = [
            OpdsEntry(
                id=entity_id("series", series.id),
                title=series.name,
                updated=_as_utc(series.updated_at),
                links=[
                    OpdsLink(
                        "subsection",
                        f"{base_url}{X4_PREFIX}/series/{series.id}",
                        ACQUISITION_FEED_TYPE,
                        title=f"{series.name} books (X4)",
                        count=int(count_rows),
                    )
                ],
            )
            for series, count_rows in rows
        ]
        return OpdsFeed(
            id=feed_id(base_url, f"{X4_PREFIX}/series"),
            title="Series (X4)",
            updated=datetime.now(UTC),
            kind="navigation",
            links=self._navigation_links(base_url, f"{X4_PREFIX}/series", root=X4_PREFIX),
            entries=entries,
            total_results=len(entries),
        )

    def x4_series_books(
        self,
        db: Session,
        base_url: str,
        series_id: int,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed | None:
        """X4 acquisition feed for one series, or None when the series is gone."""
        series = db.get(Series, series_id)
        if series is None:
            return None
        return self._books_feed(
            db,
            base_url,
            path=f"{X4_PREFIX}/series/{series_id}",
            title=f"{series.name} (X4)",
            stmt=self._x4_book_select().where(Book.series_id == series_id),
            count_stmt=self._x4_count_stmt().where(Book.series_id == series_id),
            up=f"{X4_PREFIX}/series",
            start=X4_PREFIX,
            profile="x4",
            page=page,
            per_page=per_page,
        )

    def x4_search(
        self,
        db: Session,
        base_url: str,
        query: str,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed:
        """X4 acquisition feed of FTS5 search results (X4-ready books only)."""
        from buku.services.catalog import catalog_service
        from buku.services.search import search_service

        needle = query.strip()
        safe_page = max(1, page)
        offset = (safe_page - 1) * per_page
        results = search_service.search(db, needle, limit=per_page, offset=offset)
        books = catalog_service.books_by_ids(db, [item.book_id for item in results.items])
        entries = [
            self._book_entry(books[item.book_id], base_url, profile="x4")
            for item in results.items
            if item.book_id in books and self._book_x4_ready(books[item.book_id])
        ]
        params = {"q": needle} if needle else None
        links = self._pagination_links(
            base_url,
            f"{X4_PREFIX}/search",
            kind=ACQUISITION_FEED_TYPE,
            count=results.total,
            page=safe_page,
            per_page=per_page,
            up=X4_PREFIX,
            start=X4_PREFIX,
            params=params,
        )
        return OpdsFeed(
            id=feed_id(base_url, f"{X4_PREFIX}/search"),
            title=f"Search (X4): {needle}" if needle else "Search (X4)",
            updated=_latest_updated(list(books.values())) if books else datetime.now(UTC),
            kind=KIND_ACQUISITION,
            links=links,
            entries=entries,
            total_results=results.total,
            items_per_page=per_page,
        )

    def author_books(
        self,
        db: Session,
        base_url: str,
        author_id: int,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed | None:
        """Acquisition feed for one author, or None when the author is gone."""
        author = db.get(Author, author_id)
        if author is None:
            return None
        return self._books_feed(
            db,
            base_url,
            path=f"/opds/authors/{author_id}",
            title=author.name,
            stmt=select(Book)
            .join(BookAuthor, BookAuthor.book_id == Book.id)
            .where(BookAuthor.author_id == author_id)
            .options(*_BOOK_LOADS),
            count_stmt=(
                select(func.count())
                .select_from(BookAuthor)
                .where(BookAuthor.author_id == author_id)
            ),
            up="/opds/authors",
            page=page,
            per_page=per_page,
        )

    def search(
        self,
        db: Session,
        base_url: str,
        query: str,
        *,
        page: int = 1,
        per_page: int = OPDS_PAGE_SIZE,
    ) -> OpdsFeed:
        """Acquisition feed of FTS5 search results (Phase 8 powered)."""
        from buku.services.catalog import catalog_service
        from buku.services.search import search_service

        needle = query.strip()
        safe_page = max(1, page)
        offset = (safe_page - 1) * per_page
        results = search_service.search(db, needle, limit=per_page, offset=offset)
        books = catalog_service.books_by_ids(db, [item.book_id for item in results.items])
        entries = [
            self._book_entry(books[item.book_id], base_url)
            for item in results.items
            if item.book_id in books
        ]
        params = {"q": needle} if needle else None
        links = self._pagination_links(
            base_url,
            "/opds/search",
            kind=ACQUISITION_FEED_TYPE,
            count=results.total,
            page=safe_page,
            per_page=per_page,
            up="/opds",
            params=params,
        )
        return OpdsFeed(
            id=feed_id(base_url, "/opds/search"),
            title=f"Search: {needle}" if needle else "Search",
            updated=_latest_updated(list(books.values())) if books else datetime.now(UTC),
            kind=KIND_ACQUISITION,
            links=links,
            entries=entries,
            total_results=results.total,
            items_per_page=per_page,
        )

    # ------------------------------------------------------------------ #
    # Entry building
    # ------------------------------------------------------------------ #
    def _book_entry(self, book: Book, base_url: str, *, profile: str = "original") -> OpdsEntry:
        """Build a catalog entry for one logical book (all formats)."""
        authors = [
            OpdsAuthor(name=link.author.name)
            for link in book.author_links
            if link.author is not None
        ]
        identifiers: list[str] = []
        for ident in book.identifiers:
            value = ident.identifier_value.strip()
            if not value:
                continue
            if ident.identifier_type in ("isbn", "isbn_10"):
                identifiers.append(f"urn:isbn:{value}")
            else:
                identifiers.append(value)

        links: list[OpdsLink] = []
        cover_href = _cover_href(base_url, book.cover_path) if book.cover_path else None
        if cover_href is not None:
            media_type = cover_media_type(book.cover_path or cover_href)
            links.append(OpdsLink(REL_IMAGE, cover_href, media_type))
            links.append(OpdsLink(REL_IMAGE_THUMBNAIL, cover_href, media_type))
        links.extend(self._acquisition_links(book, base_url, profile=profile))
        links.append(
            OpdsLink(REL_PROGRESSION, f"{base_url}/opds/progression/{book.id}", PROGRESSION_TYPE)
        )

        series_name = book.series.name if book.series is not None else None
        return OpdsEntry(
            id=entity_id("book", book.id),
            title=book.title,
            updated=_as_utc(book.updated_at),
            authors=authors,
            language=book.language,
            issued=book.published_date,
            publisher=book.publisher,
            summary=book.description,
            content=book.description,
            series=series_name,
            series_index=book.series_index,
            identifiers=identifiers,
            links=links,
        )

    def _acquisition_links(
        self, book: Book, base_url: str, *, profile: str = "original"
    ) -> list[OpdsLink]:
        """Open-access acquisition link(s) for the requested profile.

        ``original`` yields one link per available (non-missing) format; ``x4``
        yields a single on-demand EPUB link when the book is representable.
        """
        if profile == "x4":
            if not self._book_x4_ready(book):
                return []
            return [
                OpdsLink(
                    REL_ACQUISITION_OPEN_ACCESS,
                    f"{base_url}{X4_PREFIX}/books/{book.id}.epub",
                    X4_MEDIA_TYPE,
                )
            ]
        links: list[OpdsLink] = []
        for file_row in book.files:
            if file_row.is_missing:
                continue
            media_type = format_media_type(file_row.file_format)
            if media_type is None:
                continue
            links.append(
                OpdsLink(
                    REL_ACQUISITION_OPEN_ACCESS,
                    f"{base_url}/opds/books/{book.id}/download/{file_row.id}",
                    media_type,
                )
            )
        return links

    @staticmethod
    def _x4_ready_clause() -> Any:
        """SQLAlchemy predicate: book has at least one present EPUB file."""
        return Book.files.any(and_(BookFile.file_format == "epub", BookFile.is_missing.is_(False)))

    @classmethod
    def _book_x4_ready(cls, book: Book) -> bool:
        """Whether the in-memory book has a present EPUB source for X4."""
        return any(
            file_row.file_format == "epub" and not file_row.is_missing for file_row in book.files
        )

    @classmethod
    def _x4_book_select(cls) -> Select[tuple[Book]]:
        return select(Book).where(cls._x4_ready_clause()).options(*_BOOK_LOADS)

    @classmethod
    def _x4_count_stmt(cls) -> Select[tuple[int]]:
        return select(func.count()).select_from(Book).where(cls._x4_ready_clause())

    def _x4_count(self, db: Session) -> int:
        return db.scalar(self._x4_count_stmt()) or 0

    # ------------------------------------------------------------------ #
    # Feed plumbing
    # ------------------------------------------------------------------ #
    def _books_feed(
        self,
        db: Session,
        base_url: str,
        *,
        path: str,
        title: str,
        stmt: Select[tuple[Book]],
        count_stmt: Select[tuple[int]],
        up: str,
        page: int,
        per_page: int,
        start: str = "/opds",
        profile: str = "original",
    ) -> OpdsFeed:
        """Paginate any filtered book query into an acquisition feed."""
        safe_page = max(1, page)
        total = db.scalar(count_stmt) or 0
        offset = (safe_page - 1) * per_page
        books = list(
            db.scalars(stmt.order_by(Book.title.asc()).limit(per_page).offset(offset)).all()
        )
        entries = [self._book_entry(book, base_url, profile=profile) for book in books]
        return OpdsFeed(
            id=feed_id(base_url, path),
            title=title,
            updated=_latest_updated(books),
            kind=KIND_ACQUISITION,
            links=self._pagination_links(
                base_url,
                path,
                kind=ACQUISITION_FEED_TYPE,
                count=total,
                page=safe_page,
                per_page=per_page,
                up=up,
                start=start,
            ),
            entries=entries,
            total_results=total,
            items_per_page=per_page,
        )

    def _navigation_links(self, base_url: str, path: str, *, root: str = "/opds") -> list[OpdsLink]:
        """Standard self/start/up links for a navigation feed."""
        return [
            OpdsLink(REL_SELF, f"{base_url}{path}", NAVIGATION_FEED_TYPE),
            OpdsLink(REL_START, f"{base_url}{root}", NAVIGATION_FEED_TYPE),
            OpdsLink(REL_UP, f"{base_url}{root}", NAVIGATION_FEED_TYPE),
        ]

    def _pagination_links(
        self,
        base_url: str,
        path: str,
        *,
        kind: str,
        count: int,
        page: int,
        per_page: int,
        up: str,
        start: str = "/opds",
        params: dict[str, str] | None = None,
    ) -> list[OpdsLink]:
        """RFC 5005 first/previous/next/last pagination links (plus self/start/up)."""
        total_pages = max(1, math.ceil(count / per_page))

        def href(target_page: int) -> str:
            query: dict[str, str] = {}
            if params:
                query.update(params)
            if target_page != 1:
                query["page"] = str(target_page)
            return f"{base_url}{path}" + (f"?{urlencode(query)}" if query else "")

        links = [
            OpdsLink(REL_SELF, href(page), kind),
            OpdsLink("first", href(1), kind),
        ]
        if page > 1:
            links.append(OpdsLink("previous", href(page - 1), kind))
        if page < total_pages:
            links.append(OpdsLink("next", href(page + 1), kind))
        links.append(OpdsLink("last", href(total_pages), kind))
        links.append(OpdsLink(REL_START, f"{base_url}{start}", NAVIGATION_FEED_TYPE))
        links.append(OpdsLink(REL_UP, f"{base_url}{up}", NAVIGATION_FEED_TYPE))
        return links

    def _count(self, db: Session, model: type[Any]) -> int:
        """Count rows of a table (used by the root navigation feed)."""
        return db.scalar(select(func.count()).select_from(model)) or 0


opds_service = OpdsCatalogService()


__all__ = ["OpdsCatalogService", "opds_service"]
