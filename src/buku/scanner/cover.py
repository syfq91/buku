"""Cover image caching helpers.

Covers are derived, disposable artifacts. They are written **only** under the
writable configuration directory (``<config>/cache/covers/``) and never touch
the read-only media library, honoring AGENTS.md Rule 1.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("buku.scanner.cover")

# Signature -> normalized file extension
_MAGIC_BYTES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

# WebP: RIFF....WEBP
_WEBP_MAGIC_HEAD = b"RIFF"
_WEBP_MAGIC_TAIL = b"WEBP"


def guess_image_extension(data: bytes) -> str | None:
    """Detect an image format from its magic bytes; returns ''.ext or None."""
    for magic, ext in _MAGIC_BYTES:
        if data.startswith(magic):
            return f".{ext}"
    if len(data) >= 12 and data.startswith(_WEBP_MAGIC_HEAD) and data[8:12] == _WEBP_MAGIC_TAIL:
        return ".webp"
    return None


def cache_cover(cover: bytes, book_id: int, covers_dir: Path) -> str | None:
    """Persist cover bytes to the cache directory and return its path string.

    Returns ``None`` when the payload is not a recognizable image or the write
    fails, so callers can degrade gracefully.
    """
    extension = guess_image_extension(cover)
    if extension is None:
        logger.warning("Cover for book %s is not a recognizable image; skipping.", book_id)
        return None

    covers_dir.mkdir(parents=True, exist_ok=True)
    target = covers_dir / f"{book_id}{extension}"
    try:
        target.write_bytes(cover)
    except OSError as exc:
        logger.warning("Failed to write cover for book %s: %s", book_id, exc)
        return None
    return str(target)


__all__ = ["cache_cover", "guess_image_extension"]
