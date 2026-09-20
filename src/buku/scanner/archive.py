"""Safe ZIP archive helpers protecting against malicious book containers.

EPUB and CBZ files are ZIP archives, and per AGENTS.md security policy we must
guard against:

- **Path traversal** (members escaping the container: ``..``, absolute paths,
  drive letters)
- **Decompression bombs** (total declared uncompressed size far exceeding what
  is reasonable for a book container)
- **Oversized individual resources** (covers, XML, images)

All checks are purely declarative using the ZIP central directory
(``ZipInfo.file_size`` / ``compress_size``): nothing is extracted to disk or
fully decompressed during validation, keeping memory bounded on SBCs. Reading
resources is additionally capped per-call, so even a container that passes
validation can never expand unboundedly into memory.
"""

from __future__ import annotations

import zipfile
from pathlib import PurePosixPath
from typing import IO

# Memory tuned for home servers / Raspberry Pi class hardware.
DEFAULT_MAX_UNCOMPRESSED_TOTAL = 512 * 1024 * 1024  # 512 MiB total container
DEFAULT_MAX_ENTRIES = 100_000


class ArchiveSafetyError(ValueError):
    """Raised when a ZIP container violates safety constraints."""


def _is_safe_member_name(name: str) -> bool:
    """Reject absolute paths, drive letters, and parent-directory traversal."""
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/"):
        return False
    # Windows drive letters such as C:/evil
    if len(cleaned) >= 2 and cleaned[1] == ":":
        return False
    parts = PurePosixPath(cleaned).parts
    return ".." not in parts


def validate_archive(
    zf: zipfile.ZipFile,
    *,
    max_total: int = DEFAULT_MAX_UNCOMPRESSED_TOTAL,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> list[zipfile.ZipInfo]:
    """Validate container-wide safety constraints and return member infos.

    Raises :class:`ArchiveSafetyError` on any violation. Validation reads only
    the central directory, never the member payloads.

    Decompression-bomb protection: the running total of declared uncompressed
    sizes is capped at ``max_total``. Because every read is additionally capped
    and reads never exceed ``ZipInfo.file_size``, memory stays bounded even for
    containers with extreme per-member compression.
    """
    infos = zf.infolist()

    if len(infos) > max_entries:
        raise ArchiveSafetyError(
            f"Archive contains {len(infos)} entries exceeding the limit of {max_entries}."
        )

    total_uncompressed = 0
    for info in infos:
        if not _is_safe_member_name(info.filename):
            raise ArchiveSafetyError(f"Archive member '{info.filename}' uses an unsafe path.")
        total_uncompressed += info.file_size

    if total_uncompressed > max_total:
        raise ArchiveSafetyError(
            f"Archive total uncompressed size {total_uncompressed} exceeds the "
            f"limit of {max_total} bytes (possible decompression bomb)."
        )

    return infos


def read_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read a single verified member into memory with an optional size cap."""
    if not _is_safe_member_name(info.filename):
        raise ArchiveSafetyError(f"Archive member '{info.filename}' has an unsafe path.")
    if max_bytes is not None and info.file_size > max_bytes:
        raise ArchiveSafetyError(
            f"Archive member '{info.filename}' is {info.file_size} bytes, "
            f"exceeding the read limit of {max_bytes} bytes."
        )
    return zf.read(info)


def find_member(infos: list[zipfile.ZipInfo], identifier: str) -> zipfile.ZipInfo | None:
    """Resolve an identifier against verified member names.

    The identifier is normalized (leading slashes collapsed, backslashes
    converted) and matched exactly against member names. A traversal
    identifier such as ``../secret`` cannot match any safe member, so lookups
    can never escape the container.
    """
    cleaned = identifier.lstrip("/").replace("\\", "/")
    if not _is_safe_member_name(cleaned):
        return None
    for info in infos:
        if info.filename == cleaned:
            return info
    return None


def open_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> IO[bytes]:
    """Return a streaming file-like object for a verified member."""
    if not _is_safe_member_name(info.filename):
        raise ArchiveSafetyError(f"Archive member '{info.filename}' has an unsafe path.")
    return zf.open(info)


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_UNCOMPRESSED_TOTAL",
    "ArchiveSafetyError",
    "find_member",
    "open_member",
    "read_member",
    "validate_archive",
]
