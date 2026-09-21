"""The ``original`` identity profile (Phase 14).

The original profile is the book's intrinsic representation — its own real
media files. It never produces any cached artifact; :meth:`exists` answers
whether the logical book currently has at least one attached, non-missing
file, and :meth:`generate` is deliberately unsupported (there is nothing to
render).
"""

from __future__ import annotations

from pathlib import Path

from buku.models.book import Book, BookFile
from buku.represent.base import GenerationResult, ProfileNotSupportedError, RepresentationProfile


class OriginalProfile(RepresentationProfile):
    """The book's natural, identity representation."""

    name = "original"
    format = "original"

    def source_files(self, book: Book) -> list[BookFile]:
        return [file_row for file_row in book.files if not file_row.is_missing]

    def generate(self, source: BookFile, target: Path) -> GenerationResult:
        raise ProfileNotSupportedError(
            "The 'original' profile is the book's intrinsic files and is never generated."
        )


__all__ = ["OriginalProfile"]
