"""Default format handlers for EPUB, CBZ, and PDF formats."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from buku.scanner.handlers.base import BookMetadata, FormatHandler


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


class EPUBFormatHandler(FormatHandler):
    """Handler for EPUB digital publications (.epub)."""

    @property
    def format_name(self) -> str:
        return "epub"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".epub":
            return False
        if not path.is_file():
            return False
        try:
            with path.open("rb") as f:
                header = f.read(4)
                return header.startswith(b"PK")
        except OSError:
            return False

    def extract_metadata(self, path: Path) -> BookMetadata:
        return parse_filename_metadata(path)

    def extract_cover(self, path: Path) -> bytes | None:
        return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        raise NotImplementedError("EPUB resource extraction implemented in Phase 5.")


class CBZFormatHandler(FormatHandler):
    """Handler for Comic Book Zip archives (.cbz)."""

    @property
    def format_name(self) -> str:
        return "cbz"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".cbz":
            return False
        if not path.is_file():
            return False
        try:
            with path.open("rb") as f:
                header = f.read(4)
                return header.startswith(b"PK")
        except OSError:
            return False

    def extract_metadata(self, path: Path) -> BookMetadata:
        return parse_filename_metadata(path)

    def extract_cover(self, path: Path) -> bytes | None:
        return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        raise NotImplementedError("CBZ resource extraction implemented in Phase 5.")


class PDFFormatHandler(FormatHandler):
    """Handler for Portable Document Format (.pdf)."""

    @property
    def format_name(self) -> str:
        return "pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        if not path.is_file():
            return False
        try:
            with path.open("rb") as f:
                header = f.read(5)
                return header.startswith(b"%PDF")
        except OSError:
            return False

    def extract_metadata(self, path: Path) -> BookMetadata:
        return parse_filename_metadata(path)

    def extract_cover(self, path: Path) -> bytes | None:
        return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        raise NotImplementedError("PDF resource extraction implemented in Phase 5.")
