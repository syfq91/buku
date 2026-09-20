"""Safety tests for the ZIP container guards (AGENTS.md §5 safe archive parsing)."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from buku.scanner.archive import (
    DEFAULT_MAX_ENTRIES,
    ArchiveSafetyError,
    find_member,
    read_member,
    validate_archive,
)


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_valid_archive_passes(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "ok.zip", {"a.txt": b"hello", "sub/b.txt": b"world"})
    with zipfile.ZipFile(z) as zf:
        infos = validate_archive(zf)
        assert [i.filename for i in infos] == ["a.txt", "sub/b.txt"]


@pytest.mark.parametrize(
    "entry_name",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/absolute.txt",
        "C:/windows.txt",
        "..\\winstyle.txt",
    ],
)
def test_path_traversal_rejected(tmp_path: Path, entry_name: str) -> None:
    z = _make_zip(tmp_path / "evil.zip", {entry_name: b"payload"})
    with zipfile.ZipFile(z) as zf:
        with pytest.raises(ArchiveSafetyError, match="unsafe path"):
            validate_archive(zf)


def test_total_uncompressed_bomb_rejected(tmp_path: Path) -> None:
    """A central directory declaring huge uncompressed sizes is rejected."""
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payload.bin", b"\x00" * 1000)

    data = bytearray(path.read_bytes())
    eocd_sig = b"PK\x05\x06"
    eocd = data.rfind(eocd_sig)
    assert eocd != -1
    cd_offset = struct.unpack("<I", data[eocd + 16 : eocd + 20])[0]
    cd_sig = b"PK\x01\x02"
    cd_start = data.find(cd_sig, cd_offset)
    assert cd_start != -1
    # Central directory entry: uncompressed size field at offset +24.
    struct.pack_into("<I", data, cd_start + 24, 1024 * 1024 * 1024)  # 1 GiB > 512 MiB guard
    path.write_bytes(bytes(data))

    with zipfile.ZipFile(path) as zf:
        with pytest.raises(ArchiveSafetyError, match="decompression bomb"):
            validate_archive(zf)


def test_member_count_limit(tmp_path: Path) -> None:
    entries = {f"f{i}.txt": b"x" for i in range(10)}
    z = _make_zip(tmp_path / "many.zip", entries)
    with zipfile.ZipFile(z) as zf:
        infos = validate_archive(zf, max_entries=100)  # within limit
        assert len(infos) == 10
        with pytest.raises(ArchiveSafetyError, match="exceeding the limit"):
            validate_archive(zf, max_entries=5)


def test_find_member_traversal_safe(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "lookup.zip", {"book/ch1.xhtml": b"data"})
    with zipfile.ZipFile(z) as zf:
        infos = validate_archive(zf)
        assert find_member(infos, "book/ch1.xhtml") is not None
        assert find_member(infos, "../ch1.xhtml") is None
        assert find_member(infos, "/etc/passwd") is None
        assert find_member(infos, "C:/windows") is None


def test_read_member_size_cap(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "big.zip", {"big.bin": b"x" * 4096})
    with zipfile.ZipFile(z) as zf:
        infos = validate_archive(zf)
        assert len(read_member(zf, infos[0])) == 4096
        with pytest.raises(ArchiveSafetyError, match="read limit"):
            read_member(zf, infos[0], max_bytes=512)


def test_defaults_sane() -> None:
    assert DEFAULT_MAX_ENTRIES > 0
