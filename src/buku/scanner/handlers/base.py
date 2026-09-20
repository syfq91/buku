"""Abstract base class and data containers for book format handlers.

``BookMetadata`` lives in :mod:`buku.metadata.models` (the shared metadata
architecture data structure, Phase 6) and is re-exported here for scanner
convenience.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from buku.metadata.models import SOURCE_FILENAME, BookMetadata

__all__ = ["BookMetadata", "FormatHandler", "parse_filename_metadata"]


def parse_filename_metadata(path: Path) -> BookMetadata:
    """Infer basic title and author from filename conventions.

    Recognizes formats such as:
    - 'Author - Title.ext'
    - 'Title.ext'

    Fields populated here carry ``filename`` provenance so automated enrichment
    can treat them as weak fallbacks.
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

    sources: dict[str, str] = {}
    title = title or path.name
    if title:
        sources["title"] = SOURCE_FILENAME
    if authors:
        sources["authors"] = SOURCE_FILENAME

    return BookMetadata(
        title=title,
        authors=authors,
        sources=sources,
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
