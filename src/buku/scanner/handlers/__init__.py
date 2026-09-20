"""Registry and lookup utilities for format handlers."""

from __future__ import annotations

from pathlib import Path

from buku.scanner.handlers.base import BookMetadata, FormatHandler, parse_filename_metadata
from buku.scanner.handlers.cbz import CBZFormatHandler
from buku.scanner.handlers.epub import EPUBFormatHandler
from buku.scanner.handlers.pdf import PDFFormatHandler

_DEFAULT_HANDLERS: list[FormatHandler] = [
    EPUBFormatHandler(),
    CBZFormatHandler(),
    PDFFormatHandler(),
]


def get_default_handlers() -> list[FormatHandler]:
    """Return default registered format handlers."""
    return list(_DEFAULT_HANDLERS)


def get_handler_for_file(
    path: Path,
    handlers: list[FormatHandler] | None = None,
) -> FormatHandler | None:
    """Find the first matching FormatHandler that detects the given file."""
    active_handlers = handlers or _DEFAULT_HANDLERS
    for handler in active_handlers:
        if handler.detect(path):
            return handler
    return None


__all__ = [
    "CBZFormatHandler",
    "EPUBFormatHandler",
    "FormatHandler",
    "PDFFormatHandler",
    "BookMetadata",
    "get_default_handlers",
    "get_handler_for_file",
    "parse_filename_metadata",
]
