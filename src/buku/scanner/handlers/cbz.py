"""CBZ (Comic Book ZIP) format handler using only the Python standard library."""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from buku.scanner.archive import (
    ArchiveSafetyError,
    find_member,
    read_member,
    validate_archive,
)
from buku.scanner.handlers.base import BookMetadata, FormatHandler, parse_filename_metadata

logger = logging.getLogger("buku.scanner.cbz")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
MAX_PAGE_BYTES = 100 * 1024 * 1024


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
                return f.read(4) == b"PK\x03\x04"
        except OSError:
            return False

    @staticmethod
    def _image_members(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
        """Return image members in reading order (sorted by member name)."""
        images = [
            info
            for info in infos
            if PurePosixPath(info.filename).suffix.lower() in IMAGE_EXTENSIONS
        ]
        images.sort(key=lambda info: info.filename)
        return images

    def extract_metadata(self, path: Path) -> BookMetadata:
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            page_count = len(self._image_members(zf, infos))
        meta = parse_filename_metadata(path)
        meta.page_count = page_count
        return meta

    def extract_cover(self, path: Path) -> bytes | None:
        try:
            with zipfile.ZipFile(path) as zf:
                infos = validate_archive(zf)
                images = self._image_members(zf, infos)
                if not images:
                    return None
                return read_member(zf, images[0], max_bytes=MAX_PAGE_BYTES)
        except (
            ArchiveSafetyError,
            zipfile.BadZipFile,
            KeyError,
            ValueError,
            OSError,
        ) as exc:
            logger.warning("Failed to extract cover from '%s': %s", path, exc)
            return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            info = find_member(infos, identifier)
            if info is None:
                raise KeyError(f"Page '{identifier}' not found in CBZ.")
            return io.BytesIO(read_member(zf, info, max_bytes=MAX_PAGE_BYTES))


__all__ = ["CBZFormatHandler"]
