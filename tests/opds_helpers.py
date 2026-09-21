"""Shared seed/parse helpers for the OPDS test suites (Phases 12-13).

Builds realistic catalog rows (books with files, authors, series, identifiers,
covers) and small XML helpers for assertions against OPDS Atom feeds. Kept out
of ``conftest.py`` because it is pure helpers, not fixtures.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from buku.models import Author, Book, BookAuthor, BookFile, BookIdentifier, Library, Series
from tests.fixtures import build_cbz, build_epub, build_pdf, tiny_png_data

ATOM_NS = "http://www.w3.org/2005/Atom"
OPEN_SEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"


def auth_headers(token: str) -> dict[str, str]:
    """Build an Authorization header from a session token."""
    return {"Authorization": f"Bearer {token}"}


def basic_auth(username: str, password: str) -> dict[str, str]:
    """Build an HTTP Basic Authorization header."""
    import base64

    raw = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {raw}"}


def build_library(db: Session, root: Path, *, name: str = "opds") -> Library:
    """Create (or reuse) a library rooted inside the given test directory."""
    path = root / "lib"
    path.mkdir(parents=True, exist_ok=True)
    library = db.scalar(select(Library).where(Library.path == str(path)))
    if library is None:
        library = Library(name=name, path=str(path))
        db.add(library)
        db.flush()
    return library


def seed_book(
    db: Session,
    root: Path,
    *,
    title: str = "Dune",
    subtitle: str | None = None,
    description: str | None = None,
    publisher: str | None = None,
    published_date: str | None = "1965-08-01",
    language: str | None = "en",
    authors: Sequence[str] = ("Frank Herbert",),
    series: str | None = "Dune Chronicles",
    series_index: float | None = 1.0,
    isbn: str | None = "9780441013593",
    formats: Sequence[tuple[str, str]] = (("epub", "dune.epub"),),
    has_cover: bool = True,
) -> Book:
    """Create a fully-related Book with real files, authors, series, and ISBN.

    File rows point inside the library root so ``CatalogService.file_download_path``
    accepts them; every file is written to disk with well-formed content for its
    format. The returned book is flush-only; the caller decides when to commit.
    """
    library = build_library(db, root)
    series_row = None
    if series:
        series_row = db.scalar(select(Series).where(Series.name == series))
        if series_row is None:
            series_row = Series(name=series)
            db.add(series_row)
            db.flush()

    book = Book(
        library_id=library.id,
        series_id=series_row.id if series_row is not None else None,
        series_index=series_index,
        title=title,
        subtitle=subtitle,
        description=description,
        publisher=publisher,
        published_date=published_date,
        language=language,
        cover_path=str(Path(library.path) / "cover.png") if has_cover else None,
    )
    db.add(book)
    db.flush()

    if has_cover:
        (Path(library.path) / "cover.png").write_bytes(tiny_png_data())

    if isbn:
        db.add(BookIdentifier(book_id=book.id, identifier_type="isbn", identifier_value=isbn))

    for name in authors:
        author = db.scalar(select(Author).where(Author.name == name))
        if author is None:
            author = Author(name=name, sort_name=name)
            db.add(author)
            db.flush()
        db.add(BookAuthor(book_id=book.id, author_id=author.id, role="author"))

    for fmt, filename in formats:
        attach_file_row(db, book, Path(library.path) / filename, fmt=fmt)

    db.flush()
    return book


def attach_file_row(db: Session, book: Book, path: Path, *, fmt: str) -> BookFile:
    """Write a well-formed file of the given format and attach a BookFile row."""
    if fmt == "epub":
        build_epub(path, title=book.title, authors=[link.author.name for link in book.author_links])
    elif fmt == "pdf":
        build_pdf(path, title=book.title)
    elif fmt == "cbz":
        build_cbz(path)
    else:  # pragma: no cover - defensive
        path.write_bytes(f"fixture-{fmt}".encode())
    row = BookFile(
        book_id=book.id,
        file_path=str(path),
        file_format=fmt,
        file_size_bytes=path.stat().st_size,
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        file_mtime=datetime.now(UTC),
        is_missing=False,
    )
    db.add(row)
    db.flush()
    return row


def parse_feed(payload: bytes) -> ET.Element:
    """Parse an OPDS Atom feed body into its ElementTree root."""
    return ET.fromstring(payload)


def feed_links(feed: ET.Element) -> dict[str, list[dict[str, str]]]:
    """Map rel -> list of {href, type, title} for a feed's root links."""
    result: dict[str, list[dict[str, str]]] = {}
    for link in feed.iter(f"{{{ATOM_NS}}}link"):
        attrs = {key: value for key, value in link.attrib.items()}
        result.setdefault(attrs.get("rel", ""), []).append(attrs)
    return result


def entry_links(feed: ET.Element, entry_index: int = 0) -> dict[str, list[dict[str, str]]]:
    """Map rel -> list of {href, type, title} for one entry's links."""
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    result: dict[str, list[dict[str, str]]] = {}
    for link in entries[entry_index].iter(f"{{{ATOM_NS}}}link"):
        attrs = {key: value for key, value in link.attrib.items()}
        result.setdefault(attrs.get("rel", ""), []).append(attrs)
    return result


def utc(iso: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp as an aware datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))
