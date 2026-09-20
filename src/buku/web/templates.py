"""Jinja2 template infrastructure for the Phase 9 web UI.

Package templates and static assets live next to this module so the build
never needs to locate them at runtime. ``cover_url`` and ``format_badges``
are registered as template globals used throughout the pages.
"""

from __future__ import annotations

from pathlib import Path

from starlette.templating import Jinja2Templates

from buku.metadata.models import SOURCE_DESCRIPTIONS, BookMetadata
from buku.models.book import Book

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


_FIELD_LABELS: dict[str, str] = {
    "title": "Title",
    "subtitle": "Subtitle",
    "authors": "Authors",
    "series": "Series",
    "series_index": "Series number",
    "description": "Description",
    "publisher": "Publisher",
    "published_date": "Published",
    "language": "Language",
    "identifiers": "Identifiers",
}


def field_label(field: str) -> str:
    """Human label for a metadata field name."""
    return _FIELD_LABELS.get(field, field.replace("_", " ").title())


def show_field(meta: BookMetadata, field: str) -> str:
    """Render a candidate metadata value as a display string."""
    value = getattr(meta, field, None)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return "" if value is None else str(value)


def source_label(source: str) -> str:
    """Human description of a provenance source key."""
    return SOURCE_DESCRIPTIONS.get(source, source)


def cover_url(book: Book) -> str:
    """Render the browser URL for a book's cached cover (config cache)."""
    if not book.cover_path:
        return ""
    return "/covers/" + Path(book.cover_path).name


def format_badges(book: Book) -> list[str]:
    """Distinct format labels (EPUB, PDF, CBZ) for a book."""
    return sorted({file_row.file_format.upper() for file_row in book.files})


def author_names(book: Book) -> str:
    """Comma-joined author names for a book (display order)."""
    authors = [link.author for link in book.author_links if link.author is not None]
    authors.sort(key=lambda author: author.sort_name or author.name)
    return ", ".join(author.name for author in authors)


templates.env.globals["cover_url"] = cover_url
templates.env.globals["format_badges"] = format_badges
templates.env.globals["author_names"] = author_names
templates.env.globals["field_label"] = field_label
templates.env.globals["show_field"] = show_field
templates.env.globals["source_label"] = source_label


__all__ = ["STATIC_DIR", "TEMPLATES_DIR", "templates"]
