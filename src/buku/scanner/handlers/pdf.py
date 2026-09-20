"""PDF format handler built on pypdf (BSD-3-Clause, pure Python)."""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from buku.scanner.handlers.base import BookMetadata, FormatHandler, parse_filename_metadata

logger = logging.getLogger("buku.scanner.pdf")

MAX_PDF_BYTES = 512 * 1024 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _normalize_date_value(value: object) -> str | None:
    """Normalize a pypdf date to an ISO date string (YYYY-MM-DD or YYYY)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        match = _DATE_RE.match(text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        match = re.match(r"^(\d{4})$", text)
        if match:
            return text
        return text
    return None


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
                return f.read(5) == b"%PDF-"
        except OSError:
            return False

    def extract_metadata(self, path: Path) -> BookMetadata:
        meta = parse_filename_metadata(path)
        # Unknown/corrupt documents degrade to zero pages rather than None.
        meta.page_count = 0

        try:
            reader = PdfReader(str(path), strict=False)
        except (
            PdfReadError,
            OSError,
            ValueError,
            NotImplementedError,
            RecursionError,
            AttributeError,
        ) as exc:
            # Fall back to filename-derived metadata rather than failing the scan.
            logger.warning("PDF metadata extraction degraded for '%s': %s", path, exc)
            return meta

        info = reader.metadata
        if info is not None:
            if info.title:
                meta.title = info.title.strip()
            if info.author:
                meta.authors = [info.author.strip()]
            if info.subject:
                meta.description = info.subject.strip()
            try:
                normalised = _normalize_date_value(getattr(info, "creation_date", None))
            except ValueError, AttributeError, TypeError:
                normalised = None
            if normalised:
                meta.published_date = normalised

        try:
            meta.page_count = len(reader.pages)
        except PdfReadError, NotImplementedError, RecursionError, ValueError, AttributeError:
            meta.page_count = 0

        try:
            root = reader.trailer.get("/Root")
            root_obj = root.get_object() if root is not None else None
            lang = root_obj.get("/Lang") if root_obj is not None else None
            if isinstance(lang, str) and lang:
                meta.language = lang
        except AttributeError, TypeError, ValueError:
            pass

        return meta

    def extract_cover(self, path: Path) -> bytes | None:
        """Extract the largest embedded image from the first page as a cover.

        pypdf's high-level ``PageObject.images`` requires Pillow; instead we walk
        the page's ``/Resources /XObject`` table and read raw stream bytes, which
        keeps the dependency footprint lean (JPEG data passes through unchanged,
        FlateDecode is decompressed with zlib).
        """
        try:
            reader = PdfReader(str(path), strict=False)
            if not reader.pages:
                return None
            resources = reader.pages[0].get("/Resources")
            if resources is None:
                return None
            resources = resources.get_object() if hasattr(resources, "get_object") else resources
            if not isinstance(resources, dict):
                return None
            xobjects = resources.get("/XObject")
            if xobjects is None:
                return None
            xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
            if not isinstance(xobjects, dict):
                return None

            best: bytes | None = None
            for value in xobjects.values():
                obj = value.get_object() if hasattr(value, "get_object") else value
                if not isinstance(obj, dict):
                    continue
                if str(obj.get("/Subtype")) != "/Image":
                    continue
                if not hasattr(obj, "get_data"):
                    continue
                try:
                    raw = bytes(obj.get_data())
                except Exception:
                    continue
                if not raw or len(raw) > MAX_COVER_BYTES:
                    continue
                if best is None or len(raw) > len(best):
                    best = raw

            return best
        except (
            PdfReadError,
            OSError,
            ValueError,
            NotImplementedError,
            RecursionError,
            AttributeError,
        ) as exc:
            logger.warning("Failed to extract cover from '%s': %s", path, exc)
            return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        """Return the document as an in-memory stream.

        PDFs are single-file documents rather than containers. ``identifier``
        accepts the file name, ``"pdf"``, or ``"*"``.
        """
        if identifier not in {path.name, "pdf", "*"}:
            raise KeyError(f"Resource '{identifier}' not found in PDF.")
        if path.stat().st_size > MAX_PDF_BYTES:
            raise ValueError(f"PDF at '{path}' exceeds the read limit of {MAX_PDF_BYTES} bytes.")
        return io.BytesIO(path.read_bytes())


__all__ = ["PDFFormatHandler"]
