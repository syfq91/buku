"""Abstract base class and data containers for book format handlers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


@dataclass
class BookMetadata:
    """Standardized metadata extracted from book files."""

    title: str
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    page_count: int | None = None


def parse_filename_metadata(path: Path) -> BookMetadata:
    """Infer basic title and author from filename conventions.

    Recognizes formats such as:
    - 'Author - Title.ext'
    - 'Title.ext'
    """
    stem = path.stem.strip()
    if " - " in stem:
        parts = stem.split(" - ", 1)
        author = parts[0].strip()
        title = parts[1].strip()
        authors = [author] if author else []
    else:
        title = stem
        authors = []

    return BookMetadata(
        title=title or path.name,
        authors=authors,
    )


class FormatHandler(ABC):
    """Abstract interface for format-specific parsing and extraction."""

    @property
    @abstractmethod
    def format_name(self) -> str:
        """Normalized format identifier (e.g. 'epub', 'cbz', 'pdf')."""
        ...

    @abstractmethod
    def detect(self, path: Path) -> bool:
        """Check if file matches this format via extension and/or file signatures."""
        ...

    @abstractmethod
    def extract_metadata(self, path: Path) -> BookMetadata:
        """Extract embedded or inferred metadata from file."""
        ...

    @abstractmethod
    def extract_cover(self, path: Path) -> bytes | None:
        """Extract cover image bytes if present."""
        ...

    @abstractmethod
    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        """Retrieve a specific content stream / resource from the archive."""
        ...
