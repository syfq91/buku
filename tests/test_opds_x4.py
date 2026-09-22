"""Phase 16 tests: On-Demand X4 OPDS endpoints.

Covers authentication, the X4 navigation root, acquisition feeds with
single `.epub` open-access links, series navigation/sub-feeds, FTS search
restricted to X4-ready books, on-demand download (200 / 404 / 406), cache
placement under ``config/cache/x4``, and the original-media immutability
invariant (Rule 1 / Rule 6).
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import OpdsEnv
from tests.opds_helpers import (
    ATOM_NS,
    basic_auth,
    entry_links,
    feed_links,
    parse_feed,
    seed_book,
)

ACQUISITION = "application/atom+xml;profile=opds-catalog;kind=acquisition"
NAVIGATION = "application/atom+xml;profile=opds-catalog;kind=navigation"
EPUB = "application/epub+zip"
OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_epub(
    opds_env: OpdsEnv,
    tmp_path: Path,
    *,
    title: str = "Dune",
    filename: str = "dune.epub",
) -> int:
    _, factory, _, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, title=title, formats=[("epub", filename)])
        db.commit()
        return book.id


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_x4_requires_authentication(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, _, _ = opds_env
    client.cookies.clear()
    response = client.get("/opds/x4")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Basic")


def test_x4_accepts_http_basic(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, _, _ = opds_env
    response = client.get("/opds/x4", headers=basic_auth("reader", "readerpass123"))
    assert response.status_code == 200
    assert NAVIGATION in response.headers["content-type"]


# --------------------------------------------------------------------------- #
# Navigation root
# --------------------------------------------------------------------------- #
def test_x4_root_sections(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get("/opds/x4", headers=_headers(reader_token))
    assert response.status_code == 200
    feed = parse_feed(response.content)
    links = feed_links(feed)
    assert links["self"][0]["href"].endswith("/opds/x4")
    titles = [el.text for el in feed.iter(f"{{{ATOM_NS}}}title")]
    assert "Books" in titles
    assert "Series" in titles


# --------------------------------------------------------------------------- #
# Acquisition feeds
# --------------------------------------------------------------------------- #
def test_x4_books_single_epub_link(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    book_id = _seed_epub(opds_env, tmp_path)
    response = client.get("/opds/x4/books", headers=_headers(reader_token))
    assert response.status_code == 200
    assert ACQUISITION in response.headers["content-type"]
    feed = parse_feed(response.content)
    links = entry_links(feed, 0)
    open_access = links.get(OPEN_ACCESS, [])
    assert len(open_access) == 1
    assert open_access[0]["href"].endswith(f"/opds/x4/books/{book_id}.epub")
    assert open_access[0]["type"] == EPUB


def test_x4_books_excludes_non_epub(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory, reader_token, _ = opds_env
    with factory() as db:
        seed_book(db, tmp_path, title="PDF Only", formats=[("pdf", "only.pdf")])
        db.commit()
    response = client.get("/opds/x4/books", headers=_headers(reader_token))
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    titles = [entry.find(f"{{{ATOM_NS}}}title").text for entry in entries]  # type: ignore[union-attr]
    assert "PDF Only" not in titles


def test_x4_books_excludes_missing_epub(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory, reader_token, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, title="Gone", formats=[("epub", "gone.epub")])
        for file_row in book.files:
            file_row.is_missing = True
        db.commit()
    response = client.get("/opds/x4/books", headers=_headers(reader_token))
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    titles = [entry.find(f"{{{ATOM_NS}}}title").text for entry in entries]  # type: ignore[union-attr]
    assert "Gone" not in titles


# --------------------------------------------------------------------------- #
# Series feeds
# --------------------------------------------------------------------------- #
def test_x4_series_navigation(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    _seed_epub(opds_env, tmp_path)
    response = client.get("/opds/x4/series", headers=_headers(reader_token))
    assert response.status_code == 200
    feed = parse_feed(response.content)
    titles = [el.text for el in feed.iter(f"{{{ATOM_NS}}}title")]
    assert "Dune Chronicles" in titles


def test_x4_series_books_feed(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory, reader_token, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, series="Dune Chronicles")
        series_id = book.series_id
        db.commit()
    response = client.get(f"/opds/x4/series/{series_id}", headers=_headers(reader_token))
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    assert any(entry.find(f"{{{ATOM_NS}}}title").text == "Dune" for entry in entries)  # type: ignore[union-attr]


def test_x4_series_unknown_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get("/opds/x4/series/99999", headers=_headers(reader_token))
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_x4_search_empty_query(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get("/opds/x4/search?q=dune", headers=_headers(reader_token))
    assert response.status_code == 200
    feed = parse_feed(response.content)
    assert list(feed.iter(f"{{{ATOM_NS}}}entry")) == []


def test_x4_search_returns_ready_book(opds_env: OpdsEnv, tmp_path: Path) -> None:
    from buku.services.search import search_service

    client, factory, reader_token, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, title="Dune Messiah")
        db.flush()
        search_service.index_book(db, book)
        db.commit()
    response = client.get("/opds/x4/search?q=Dune", headers=_headers(reader_token))
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    titles = [entry.find(f"{{{ATOM_NS}}}title").text for entry in entries]  # type: ignore[union-attr]
    assert "Dune Messiah" in titles
    if entries:
        acquisition = entry_links(feed, 0).get(OPEN_ACCESS, [])
        assert acquisition and acquisition[0]["href"].endswith(".epub")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def test_x4_download_generates_and_serves(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    book_id = _seed_epub(opds_env, tmp_path)
    response = client.get(f"/opds/x4/books/{book_id}.epub", headers=_headers(reader_token))
    assert response.status_code == 200
    assert response.headers["content-type"] == EPUB
    assert response.content.startswith(b"PK")


def test_x4_download_unknown_book_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get("/opds/x4/books/99999.epub", headers=_headers(reader_token))
    assert response.status_code == 404


def test_x4_download_no_epub_source_406(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory, reader_token, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, title="PDF Book", formats=[("pdf", "only.pdf")])
        book_id = book.id
        db.commit()
    response = client.get(f"/opds/x4/books/{book_id}.epub", headers=_headers(reader_token))
    assert response.status_code == 406


# --------------------------------------------------------------------------- #
# Cache & original-media immutability (Rule 1 / Rule 6)
# --------------------------------------------------------------------------- #
def test_x4_download_writes_cache_not_media(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory, reader_token, _ = opds_env
    with factory() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "dune.epub")])
        book_id = book.id
        source = next(f for f in book.files if f.file_format == "epub")
        source_path = Path(source.file_path)
        source_mtime_before = source_path.stat().st_mtime_ns
        source_bytes_before = source_path.read_bytes()
        db.commit()

    response = client.get(f"/opds/x4/books/{book_id}.epub", headers=_headers(reader_token))
    assert response.status_code == 200

    cache_dir = tmp_path / "cache" / "x4"
    assert cache_dir.is_dir()
    cache_files = list(cache_dir.iterdir())
    assert len(cache_files) == 1

    # Original media untouched (Rule 1).
    assert source_path.is_file()
    assert source_path.stat().st_mtime_ns == source_mtime_before
    assert source_path.read_bytes() == source_bytes_before

    # Second download is idempotent (same cache file).
    again = client.get(f"/opds/x4/books/{book_id}.epub", headers=_headers(reader_token))
    assert again.status_code == 200
    assert list(cache_dir.iterdir()) == cache_files
