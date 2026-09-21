"""The ``x4`` e-ink EPUB profile (Phase 14).

Renders a book's EPUB source into the lightweight X4 representation produced
by :class:`buku.represent.optimizer.X4Optimizer`. Progress and locators stay
logical (they belong to the :class:`Book`, never to a profile), so the X4
rendering only needs to preserve the source's internal structure — which the
generator guarantees (AGENTS.md Rule 6).
"""

from __future__ import annotations

from pathlib import Path

from buku.models.book import Book, BookFile
from buku.represent.base import GenerationResult, RepresentationProfile
from buku.represent.optimizer import OptimizerResult, X4Optimizer


class X4Profile(RepresentationProfile):
    """On-demand, e-ink-optimized EPUB rendering of a logical book."""

    name = "x4"
    format = "epub"

    def __init__(self, optimizer: X4Optimizer | None = None) -> None:
        self._optimizer = optimizer or X4Optimizer()

    def source_files(self, book: Book) -> list[BookFile]:
        """EPUB files of the book that are still present in the library."""
        return [
            file_row
            for file_row in book.files
            if file_row.file_format == "epub" and not file_row.is_missing
        ]

    def optimizer_version(self) -> str:
        return self._optimizer.version

    def generate(self, source: BookFile, target: Path) -> GenerationResult:
        result: OptimizerResult = self._optimizer.optimize(Path(source.file_path), target)
        return GenerationResult(
            format=self.format,
            source_hash=source.file_hash,
            optimizer_version=self._optimizer.version,
            file_size_bytes=result.file_size_bytes,
        )


__all__ = ["X4Profile"]
