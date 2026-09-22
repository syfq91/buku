"""CacheService: disposable artifact cache under ``/config/cache/`` (Phase 18).

The cache hierarchy (``covers/``, ``metadata/``, ``x4/``) is strictly derived
state: the database and the original media files remain the single source of
truth. This service never reads or writes ``/books`` and never mutates DB
rows — callers (scanner, representation service, admin console) own those.

Features:
- Disk usage statistics per subdirectory.
- Validation: orphan ``.tmp`` staging files, empty artifacts, optional
  ``.sha256`` sidecar checksum integrity, and symlink confinement.
- Manual invalidation (clear one subdirectory or the whole cache).
- Configurable maximum size with least-recently-used (mtime) eviction.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from buku.config import get_settings
from buku.scanner.hasher import compute_file_hash

logger = logging.getLogger("buku.services.cache")

CACHE_SUBDIRS: tuple[str, ...] = ("covers", "metadata", "x4")
CHECKSUM_SUFFIX = ".sha256"
TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class CacheDirStats:
    """Disk usage of one cache subdirectory."""

    name: str
    path: Path
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class CacheStats:
    """Aggregate disk usage across the whole disposable cache hierarchy."""

    root: Path
    dirs: tuple[CacheDirStats, ...]
    total_files: int
    total_bytes: int


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of a cache integrity pass."""

    checked: int = 0
    removed_tmp: int = 0
    removed_empty: int = 0
    removed_escaped: int = 0
    checksum_failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no escaping entries or checksum mismatches were found."""
        return self.removed_escaped == 0 and not self.checksum_failures


class CacheService:
    """Filesystem operations over the writable ``config_dir/cache`` tree."""

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def root(self) -> Path:
        """Resolved cache root (always under ``config_dir``, never media)."""
        return (get_settings().config_dir / "cache").resolve()

    def path_for(self, subdir: str) -> Path:
        """Absolute path of a known cache subdirectory (created if missing)."""
        name = self._validate_subdir(subdir)
        path = self.root() / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #
    def stats(self) -> CacheStats:
        """Disk usage for every known subdirectory and the cache overall."""
        root = self.root()
        dirs = tuple(self.dir_stats(name) for name in CACHE_SUBDIRS)
        return CacheStats(
            root=root,
            dirs=dirs,
            total_files=sum(d.file_count for d in dirs),
            total_bytes=sum(d.total_bytes for d in dirs),
        )

    def dir_stats(self, subdir: str) -> CacheDirStats:
        """Disk usage of a single cache subdirectory."""
        name = self._validate_subdir(subdir)
        path = self.root() / name
        file_count = 0
        total_bytes = 0
        if path.is_dir():
            for entry in self._iter_entries(path):
                if not self._is_regular_file(entry):
                    continue
                try:
                    total_bytes += entry.stat().st_size
                except OSError:
                    continue
                file_count += 1
        return CacheDirStats(name=name, path=path, file_count=file_count, total_bytes=total_bytes)

    # ------------------------------------------------------------------ #
    # Manual invalidation
    # ------------------------------------------------------------------ #
    def clear(self, subdir: str | None = None) -> int:
        """Delete cached files under *subdir*, or every subdirectory when None.

        Subdirectory shells are kept so static mounts (``/covers``) stay
        valid. Returns the number of filesystem entries removed.
        """
        targets = CACHE_SUBDIRS if subdir is None else (self._validate_subdir(subdir),)
        removed = 0
        for name in targets:
            directory = self.root() / name
            if not directory.is_dir():
                continue
            for entry in sorted(
                self._iter_entries(directory),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                if entry.is_dir() and not entry.is_symlink():
                    try:
                        entry.rmdir()
                    except OSError:
                        pass
                    continue
                try:
                    entry.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Failed to remove cache entry %s: %s", entry, exc)
        logger.info("Cache clear(%s) removed %d entr(y/ies)", subdir or "all", removed)
        return removed

    # ------------------------------------------------------------------ #
    # Validation & checksum integrity
    # ------------------------------------------------------------------ #
    def validate(self) -> ValidationReport:
        """Sweep the cache for orphan staging files and corruption.

        Removes ``.tmp`` staging leftovers, empty artifacts, and entries whose
        symlink target escapes the cache root. When a ``.sha256`` sidecar
        exists for an artifact, the artifact's digest is re-verified; a
        mismatch deletes both artifact and sidecar so callers regenerate.
        """
        root = self.root()
        checked = removed_tmp = removed_empty = removed_escaped = 0
        failures: list[str] = []

        for name in CACHE_SUBDIRS:
            directory = root / name
            if not directory.is_dir():
                continue
            for entry in self._iter_entries(directory):
                if entry.is_symlink():
                    if not self._confined(entry):
                        removed_escaped += self._safe_unlink(entry)
                    continue
                if not self._is_regular_file(entry):
                    continue
                if entry.name.endswith(TMP_SUFFIX):
                    removed_tmp += self._safe_unlink(entry)
                    continue
                if entry.name.endswith(CHECKSUM_SUFFIX):
                    continue

                checked += 1
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                if size == 0:
                    removed_empty += self._safe_unlink(entry)
                    continue

                sidecar = entry.with_name(entry.name + CHECKSUM_SUFFIX)
                if sidecar.is_file() and not sidecar.is_symlink():
                    if not self._sidecar_matches(entry, sidecar):
                        failures.append(str(entry))
                        self._safe_unlink(entry)
                        self._safe_unlink(sidecar)

        report = ValidationReport(
            checked=checked,
            removed_tmp=removed_tmp,
            removed_empty=removed_empty,
            removed_escaped=removed_escaped,
            checksum_failures=tuple(failures),
        )
        if not report.ok:
            logger.warning(
                "Cache validation found issues (escaped=%d, bad checksums=%d)",
                removed_escaped,
                len(failures),
            )
        return report

    def write_checksum(self, path: Path) -> Path | None:
        """Write a ``.sha256`` sidecar next to *path*; returns the sidecar."""
        try:
            digest = compute_file_hash(path)
        except OSError as exc:
            logger.warning("Cannot checksum %s: %s", path, exc)
            return None
        sidecar = path.with_name(path.name + CHECKSUM_SUFFIX)
        try:
            sidecar.write_text(digest + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot write checksum sidecar for %s: %s", path, exc)
            return None
        return sidecar

    def verify_checksum(self, path: Path, expected: str) -> bool:
        """Whether *path*'s SHA-256 digest equals *expected* (hex)."""
        try:
            actual = compute_file_hash(path)
        except OSError:
            return False
        return actual == expected.strip().lower()

    # ------------------------------------------------------------------ #
    # Size limits & eviction
    # ------------------------------------------------------------------ #
    def enforce_limit(self) -> int:
        """Evict LRU entries until the cache fits ``settings.cache_max_bytes``.

        A limit of ``0`` means unlimited and is a no-op (zero filesystem
        work), which is the default. Returns the number of artifacts removed.
        """
        limit = get_settings().cache_max_bytes
        if limit <= 0:
            return 0
        return self.evict(limit)

    def evict(self, max_bytes: int, subdir: str | None = None) -> int:
        """Delete least-recently-used artifacts until total size <= *max_bytes*.

        ``.sha256`` sidecars are freed together with their artifact. Returns
        the number of artifacts removed.
        """
        targets = CACHE_SUBDIRS if subdir is None else (self._validate_subdir(subdir),)
        candidates: list[tuple[float, int, Path]] = []
        for name in targets:
            directory = self.root() / name
            if not directory.is_dir():
                continue
            for entry in self._iter_entries(directory):
                if not self._is_regular_file(entry):
                    continue
                if entry.name.endswith(CHECKSUM_SUFFIX) or entry.name.endswith(TMP_SUFFIX):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                size = st.st_size
                sidecar = entry.with_name(entry.name + CHECKSUM_SUFFIX)
                if sidecar.is_file() and not sidecar.is_symlink():
                    try:
                        size += sidecar.stat().st_size
                    except OSError:
                        pass
                candidates.append((st.st_mtime, size, entry))

        total = sum(size for _, size, _ in candidates)
        if total <= max_bytes:
            return 0

        # Oldest mtime first: least-recently-used artifacts go first.
        candidates.sort()
        removed = 0
        freed = 0
        for _, size, entry in candidates:
            if total - freed <= max_bytes:
                break
            sidecar = entry.with_name(entry.name + CHECKSUM_SUFFIX)
            if sidecar.is_file() and not sidecar.is_symlink():
                try:
                    freed += sidecar.stat().st_size
                except OSError:
                    pass
                self._safe_unlink(sidecar)
            if self._safe_unlink(entry):
                freed += size
                removed += 1
        if removed:
            logger.info(
                "Cache evicted %d artifact(s), freed %d byte(s) (limit=%d)",
                removed,
                freed,
                max_bytes,
            )
        return removed

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_subdir(subdir: str) -> str:
        if subdir not in CACHE_SUBDIRS:
            raise ValueError(f"Unknown cache subdirectory: {subdir!r}")
        return subdir

    @staticmethod
    def _iter_entries(directory: Path) -> Iterator[Path]:
        """Walk *directory* without following directory symlinks.

        Symlinked directories are yielded once (so validation can inspect and
        remove escaping links) but never descended into — a link pointing at
        ``/etc`` must not expose outside files to unlink/stat via the cache.
        """
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            base = Path(dirpath)
            keep: list[str] = []
            for name in dirnames:
                child = base / name
                if child.is_symlink():
                    yield child
                else:
                    keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                yield base / name

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        return path.is_file() and not path.is_symlink()

    def _confined(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.root())
        except OSError:
            return False

    @staticmethod
    def _safe_unlink(path: Path) -> int:
        """Unlink *path* once, returning 1 on success and 0 on failure."""
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove cache entry %s: %s", path, exc)
            return 0
        return 1

    @staticmethod
    def _sidecar_matches(artifact: Path, sidecar: Path) -> bool:
        try:
            expected = sidecar.read_text(encoding="utf-8").strip().split()
        except OSError:
            return False
        if not expected:
            return False
        try:
            actual = compute_file_hash(artifact)
        except OSError:
            return False
        return actual == expected[0].lower()


cache_service = CacheService()


__all__ = [
    "CACHE_SUBDIRS",
    "CHECKSUM_SUFFIX",
    "TMP_SUFFIX",
    "CacheDirStats",
    "CacheService",
    "CacheStats",
    "ValidationReport",
    "cache_service",
]
