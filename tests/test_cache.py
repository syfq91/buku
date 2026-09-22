"""Phase 18 tests: disposable cache validation, invalidation, limits, eviction.

Covers ``CacheService`` over ``config_dir/cache/{covers,metadata,x4}``: stats,
manual clear, checksum integrity, LRU eviction, configurable max size (TOML +
env), confinement (never touches ``books_dir``), and the ``cache_maintenance``
job handler.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from pydantic import ValidationError

from buku.config import Settings, load_settings, set_settings
from buku.jobs.handlers import handle_cache_maintenance
from buku.services.cache import CACHE_SUBDIRS, CacheService, cache_service

CacheEnv = tuple[Settings, CacheService]


@pytest.fixture
def cache_env(tmp_path: Path) -> Generator[CacheEnv]:
    """Isolated config/books dirs with the cache hierarchy present."""
    cfg = tmp_path / "config"
    books = tmp_path / "books"
    settings = Settings(config_dir=cfg, books_dir=books, jobs_enabled=False)
    settings.ensure_directories()
    books.mkdir(parents=True, exist_ok=True)
    set_settings(settings)
    yield settings, cache_service
    set_settings(None)


def _write(path: Path, data: bytes = b"payload") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# Layout & stats
# --------------------------------------------------------------------------- #
def test_cache_subdirs_match_plan() -> None:
    assert CACHE_SUBDIRS == ("covers", "metadata", "x4")


def test_stats_counts_files_and_bytes(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    _write(root / "covers" / "1.jpg", b"12345")
    _write(root / "covers" / "2.png", b"abc")
    _write(root / "x4" / "x.epub", b"0123456789")

    stats = svc.stats()
    assert stats.root == root.resolve()
    assert stats.total_files == 3
    assert stats.total_bytes == 5 + 3 + 10
    by_name = {d.name: d for d in stats.dirs}
    assert by_name["covers"].file_count == 2
    assert by_name["covers"].total_bytes == 8
    assert by_name["x4"].total_bytes == 10
    assert by_name["metadata"].file_count == 0


def test_dir_stats_rejects_unknown_subdir(cache_env: CacheEnv) -> None:
    _, svc = cache_env
    with pytest.raises(ValueError, match="Unknown cache subdirectory"):
        svc.dir_stats("../../etc")


def test_path_for_rejects_unknown_subdir(cache_env: CacheEnv) -> None:
    _, svc = cache_env
    with pytest.raises(ValueError, match="Unknown cache subdirectory"):
        svc.path_for("secrets")


# --------------------------------------------------------------------------- #
# Manual invalidation
# --------------------------------------------------------------------------- #
def test_clear_single_subdir_leaves_others(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    _write(root / "covers" / "1.jpg")
    _write(root / "x4" / "x.epub")

    removed = svc.clear("covers")

    assert removed == 1
    assert not (root / "covers" / "1.jpg").exists()
    assert (root / "x4" / "x.epub").exists()
    # Shell directory kept so the /covers static mount stays valid.
    assert (root / "covers").is_dir()


def test_clear_all_wipes_every_subdir(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    _write(root / "covers" / "1.jpg")
    _write(root / "metadata" / "m.json")
    _write(root / "x4" / "x.epub")

    removed = svc.clear()

    assert removed == 3
    assert svc.stats().total_files == 0
    for name in CACHE_SUBDIRS:
        assert (root / name).is_dir()


def test_clear_never_touches_books_dir(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    books_file = settings.books_dir / "keep.me"
    books_file.write_bytes(b"original media")
    _write(settings.config_dir / "cache" / "covers" / "1.jpg")

    svc.clear()

    assert books_file.read_bytes() == b"original media"


# --------------------------------------------------------------------------- #
# Validation & checksum integrity
# --------------------------------------------------------------------------- #
def test_validate_removes_tmp_and_empty_artifacts(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    good = _write(root / "covers" / "1.jpg", b"ok")
    _write(root / "covers" / "2.jpg.tmp", b"partial")
    _write(root / "x4" / "empty.epub", b"")

    report = svc.validate()

    assert report.ok
    assert report.removed_tmp == 1
    assert report.removed_empty == 1
    assert report.checked >= 1
    assert good.exists()
    assert not (root / "covers" / "2.jpg.tmp").exists()
    assert not (root / "x4" / "empty.epub").exists()


def test_validate_checksum_sidecar_passes(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    artifact = _write(settings.config_dir / "cache" / "x4" / "x.epub", b"good-bytes")
    assert svc.write_checksum(artifact) is not None

    report = svc.validate()

    assert report.ok
    assert report.checksum_failures == ()
    assert artifact.exists()


def test_validate_checksum_mismatch_invalidates_artifact(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    artifact = _write(settings.config_dir / "cache" / "x4" / "x.epub", b"good-bytes")
    sidecar = svc.write_checksum(artifact)
    assert sidecar is not None
    artifact.write_bytes(b"tampered!!")

    report = svc.validate()

    assert not report.ok
    assert report.checksum_failures == (str(artifact),)
    assert not artifact.exists()
    assert not sidecar.exists()


def test_verify_checksum_helper(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    artifact = _write(settings.config_dir / "cache" / "covers" / "1.jpg", b"abc")
    sidecar = svc.write_checksum(artifact)
    assert sidecar is not None
    expected = sidecar.read_text(encoding="utf-8").strip()
    assert svc.verify_checksum(artifact, expected)
    assert not svc.verify_checksum(artifact, "0" * 64)


def test_validate_removes_escaping_symlink(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    outside = settings.config_dir / "outside.txt"
    outside.write_bytes(b"do not delete me")
    link = settings.config_dir / "cache" / "covers" / "evil"
    link.symlink_to(outside)

    report = svc.validate()

    assert report.removed_escaped == 1
    assert not report.ok
    assert not link.is_symlink()
    assert outside.read_bytes() == b"do not delete me"


# --------------------------------------------------------------------------- #
# Size limits & LRU eviction
# --------------------------------------------------------------------------- #
def test_enforce_limit_noop_when_unlimited(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    _write(root / "covers" / "1.jpg", b"x" * 1000)
    assert settings.cache_max_bytes == 0

    assert svc.enforce_limit() == 0
    assert (root / "covers" / "1.jpg").exists()


def test_enforce_limit_evicts_oldest_first(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    settings.cache_max_bytes = 10
    root = settings.config_dir / "cache"
    old = _write(root / "covers" / "old.jpg", b"0123456789")  # 10 bytes
    new = _write(root / "covers" / "new.jpg", b"abcdefghij")  # 10 bytes
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    removed = svc.enforce_limit()

    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_evict_respects_explicit_limit(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    _write(root / "x4" / "a.epub", b"a" * 30)
    _write(root / "x4" / "b.epub", b"b" * 30)

    removed = svc.evict(30)

    assert removed == 1
    assert svc.dir_stats("x4").total_bytes <= 30


def test_evict_removes_sidecar_with_artifact(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    root = settings.config_dir / "cache"
    artifact = _write(root / "covers" / "1.jpg", b"1234567890")
    sidecar = svc.write_checksum(artifact)
    assert sidecar is not None

    removed = svc.evict(0)

    assert removed == 1
    assert not artifact.exists()
    assert not sidecar.exists()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_cache_max_bytes_toml_section(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[cache]
max_bytes = 52428800
"""
    )
    settings = load_settings(config_file=config_file)
    assert settings.cache_max_bytes == 52_428_800


def test_cache_max_bytes_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUKU_CACHE_MAX_BYTES", "1024")
    settings = load_settings()
    assert settings.cache_max_bytes == 1024


def test_cache_max_bytes_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Settings(cache_max_bytes=-1)


# --------------------------------------------------------------------------- #
# Automated maintenance (job handler)
# --------------------------------------------------------------------------- #
def test_cache_maintenance_handler_validates_and_enforces(cache_env: CacheEnv) -> None:
    settings, svc = cache_env
    settings.cache_max_bytes = 5
    root = settings.config_dir / "cache"
    stale = _write(root / "covers" / "old.jpg", b"0123456789")
    _write(root / "covers" / "x.tmp", b"staging")
    os.utime(stale, (1_000_000, 1_000_000))

    handle_cache_maintenance(None, {})  # type: ignore[arg-type]

    assert not stale.exists()
    assert not (root / "covers" / "x.tmp").exists()
    assert svc.stats().total_bytes <= 5


def test_cache_maintenance_registered_in_jobs() -> None:
    from buku.jobs.handlers import HANDLERS
    from buku.jobs.queue import JOB_TYPES, POOL_JOB_TYPES

    assert "cache_maintenance" in JOB_TYPES
    assert "cache_maintenance" in HANDLERS
    assert "cache_maintenance" in POOL_JOB_TYPES["metadata"]


def test_cache_service_singleton_exported() -> None:
    from buku.services import CacheService
    from buku.services import cache_service as exported

    assert isinstance(exported, CacheService)
    assert exported is cache_service


def test_service_is_filesystem_only(cache_env: CacheEnv) -> None:
    """CacheService must never import or open the database / media tree."""
    settings, svc = cache_env
    books_file = settings.books_dir / "book.epub"
    books_file.write_bytes(b"epub-bytes")
    _write(settings.config_dir / "cache" / "metadata" / "m.json", b"{}")

    svc.stats()
    svc.validate()
    svc.clear("metadata")
    svc.enforce_limit()

    assert books_file.read_bytes() == b"epub-bytes"
