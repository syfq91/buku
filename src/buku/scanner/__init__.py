"""Read-only library scanner package for buku."""

from buku.scanner.handlers import (
    BookMetadata,
    CBZFormatHandler,
    EPUBFormatHandler,
    FormatHandler,
    PDFFormatHandler,
    get_default_handlers,
    get_handler_for_file,
)
from buku.scanner.hasher import compute_file_hash
from buku.scanner.scanner import LibraryScanner, ScanStatistics

__all__ = [
    "BookMetadata",
    "CBZFormatHandler",
    "EPUBFormatHandler",
    "FormatHandler",
    "LibraryScanner",
    "PDFFormatHandler",
    "ScanStatistics",
    "compute_file_hash",
    "get_default_handlers",
    "get_handler_for_file",
]
