"""Streaming file hashing utilities for memory-constrained environments."""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_CHUNK_SIZE = 65536  # 64 KB chunks for memory efficiency on SBCs (Raspberry Pi, etc.)


def compute_file_hash(path: Path | str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Compute SHA-256 hex digest of a file in streaming chunks.

    Never loads the entire file into memory, keeping RAM usage strictly bounded.
    """
    file_path = Path(path)
    hasher = hashlib.sha256()

    with file_path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)

    return hasher.hexdigest()
