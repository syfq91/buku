"""X4 EPUB optimizer (Phase 14 scaffold → Phase 15 pipeline).

Phase 14 ships an identity optimizer: it re-wraps the source EPUB with a
valid ``mimetype``-first, stored layout so the representation layer has a real,
verifiable generation pipeline to cache. Phase 15 replaces the body of
``optimize`` with the full e-ink optimization (image downscaling/grayscale
quantization, font stripping, CSS ``@font-face`` cleanup) while preserving
every structural element the locator invariant depends on.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from buku.scanner.archive import validate_archive


@dataclass(frozen=True)
class OptimizerResult:
    """Bookkeeping produced by one optimization run."""

    file_size_bytes: int


class X4Optimizer:
    """Produce a structurally sound ``x4`` profile EPUB from a source EPUB."""

    version = "1"

    def optimize(self, source_path: Path, target_path: Path) -> OptimizerResult:
        """Re-wrap *source_path* into *target_path* (never touching media)."""
        with zipfile.ZipFile(source_path, "r") as src:
            infos = validate_archive(src)
            with zipfile.ZipFile(target_path, "w") as dst:
                for info in infos:
                    name = info.filename
                    compress_type = (
                        zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
                    )
                    dst.writestr(name, src.read(info), compress_type=compress_type)
        return OptimizerResult(file_size_bytes=target_path.stat().st_size)


__all__ = ["OptimizerResult", "X4Optimizer"]
