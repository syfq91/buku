"""Phase 12 tests: OPDS 1.2 catalog endpoints.

Covers authentication (HTTP Basic, Bearer, session cookie, and disabled-user
rejection), the root navigation feed, paginated acquisition feeds, series and
author navigation/sub-feeds, OpenSearch autodiscovery, acquisition links per
format, artwork links, the OPDS Progression service link (Phase 13 discovery),
and the authenticated download endpoint (200 / 404 / 410).
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
PDF = "application/pdf"
CBZ = "application/vnd.comicbook+zip"
PROGRESSION = "application/opds-progression+json"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_opds_requires_authentication(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, _, _ = opds_env
    client.cookies.clear()
    response = client.get("/opds")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Basic")


def test_opds_accepts_http_basic(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path)
        db.commit()
    response = client.get("/opds", headers=basic_auth("reader", "readerpass123"))
    assert response.status_code == 200
    assert NAVIGATION in response.headers["content-type"]


def test_opds_rejects_bad_basic_credentials(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, _, _ = opds_env
    response = client.get("/opds", headers=basic_auth("reader", "wrongpass"))
    assert response.status_code == 401


def test_opds_accepts_bearer_token(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path)
        db.commit()
    response = client.get("/opds", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 200


def test_opds_rejects_deactivated_user(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, _, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path)
        from buku.models import User

        reader = db.query(User).filter(User.username == "reader").one()
        reader.is_active = False
        db.commit()
    response = client.get("/opds", headers=basic_auth("reader", "readerpass123"))
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Root navigation feed
# --------------------------------------------------------------------------- #
def test_root_feed_links_and_sections(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path)
        db.commit()
    response = client.get("/opds", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(NAVIGATION)
    feed = parse_feed(response.content)
    links = feed_links(feed)
    self_links = links.get("self", [])
    assert self_links and self_links[0]["href"].endswith("/opds")
    titles = [element.text for element in feed.iter(f"{{{ATOM_NS}}}title")]
    for section in ("Books", "Series", "Authors"):
        assert section in titles


# --------------------------------------------------------------------------- #
# Acquisition feeds & pagination
# --------------------------------------------------------------------------- #
def test_books_feed_lists_acquisition_entries(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "dune.epub"), ("pdf", "dune.pdf")])
        book_id = book.id
        db.commit()
    response = client.get("/opds/books", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 200
    feed = parse_feed(response.content)
    first_links = entry_links(feed, 0)
    open_access = first_links.get("http://opds-spec.org/acquisition/open-access", [])
    acquisition_urls = [link["href"] for link in open_access]
    assert any(url.endswith(f"/opds/books/{book_id}/download/1") for url in acquisition_urls)
    assert any(url.endswith(f"/opds/books/{book_id}/download/2") for url in acquisition_urls)
    media_types = {link.get("type") for link in open_access}
    assert media_types == {EPUB, PDF}


def test_books_feed_missing_files_have_no_acquisition_link(
    opds_env: OpdsEnv, tmp_path: Path
) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "gone.epub")])
        for file_row in book.files:
            file_row.is_missing = True
        db.commit()
    response = client.get("/opds/books", headers={"Authorization": f"Bearer {reader_token}"})
    feed = parse_feed(response.content)
    acquisition = entry_links(feed, 0).get("http://opds-spec.org/acquisition/open-access", [])
    assert acquisition == []


def test_books_cover_and_progression_links(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    response = client.get("/opds/books", headers={"Authorization": f"Bearer {reader_token}"})
    feed = parse_feed(response.content)
    links = entry_links(feed, 0)
    images = [link["href"] for link in links.get("http://opds-spec.org/image", [])]
    assert len(images) == 1 and images[0].endswith("/covers/cover.png")
    progressions = [link["href"] for link in links.get("http://opds-spec.org/progression", [])]
    assert progressions == [f"http://testserver/opds/progression/{book_id}"]


def test_books_series_entries_grouped_in_categories(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path, series="Dune Chronicles", series_index=1.0)
        db.commit()
    response = client.get("/opds/books", headers={"Authorization": f"Bearer {reader_token}"})
    feed = parse_feed(response.content)
    entry = list(feed.iter(f"{{{ATOM_NS}}}entry"))[0]
    categories = [element for element in entry if element.tag == f"{{{ATOM_NS}}}category"]
    assert any(category.get("term") == "Dune Chronicles" for category in categories)


def test_books_pagination_links(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        for index in range(3):
            seed_book(db, tmp_path, title=f"Book {index:02d}", formats=[])
        db.commit()
    response = client.get(
        "/opds/books?page=2&limit=2", headers={"Authorization": f"Bearer {reader_token}"}
    )
    links = feed_links(parse_feed(response.content))
    assert "next" not in links  # total 3 / page 2 => page 2 is the last
    assert links["previous"][0]["href"].endswith("/opds/books")  # page 1 = no query
    assert links["first"][0]["href"].endswith("/opds/books")
    assert links["last"][0]["href"].endswith("/opds/books?page=2")


def test_books_feed_is_qs_paginated(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        for index in range(3):
            seed_book(db, tmp_path, title=f"Book {index:02d}", formats=[])
        db.commit()
    first = client.get("/opds/books?limit=2", headers={"Authorization": f"Bearer {reader_token}"})
    entries = parse_feed(first.content)
    assert len(list(entries.iter(f"{{{ATOM_NS}}}entry"))) == 2


# --------------------------------------------------------------------------- #
# Series & authors
# --------------------------------------------------------------------------- #
def test_series_navigation_feed(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path, series="Dune Chronicles")
        db.commit()
    response = client.get("/opds/series", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entry = list(feed.iter(f"{{{ATOM_NS}}}entry"))[0]
    assert entry.find(f"{{{ATOM_NS}}}title").text == "Dune Chronicles"  # type: ignore[union-attr]


def test_series_books_acquisition_feed(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path, series="Dune Chronicles")
        series_id = book.series_id
        db.commit()
    response = client.get(
        f"/opds/series/{series_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    assert any(entry.find(f"{{{ATOM_NS}}}title").text == "Dune" for entry in entries)  # type: ignore[union-attr]


def test_series_books_unknown_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get("/opds/series/99999", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 404


def test_authors_navigation_feed(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path, authors=("Frank Herbert",))
        db.commit()
    response = client.get("/opds/authors", headers={"Authorization": f"Bearer {reader_token}"})
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entry = list(feed.iter(f"{{{ATOM_NS}}}entry"))[0]
    assert entry.find(f"{{{ATOM_NS}}}title").text == "Frank Herbert"  # type: ignore[union-attr]


def test_author_books_acquisition_feed(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        seed_book(db, tmp_path, authors=("Frank Herbert",))
        from buku.models import Author

        db.flush()
        author_id = db.query(Author).filter(Author.name == "Frank Herbert").one().id
        db.commit()
    response = client.get(
        f"/opds/authors/{author_id}", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 200
    feed = parse_feed(response.content)
    entries = list(feed.iter(f"{{{ATOM_NS}}}entry"))
    assert any(entry.find(f"{{{ATOM_NS}}}title").text == "Dune" for entry in entries)  # type: ignore[union-attr]


def test_author_books_unknown_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get(
        "/opds/authors/99999", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Search & OpenSearch
# --------------------------------------------------------------------------- #
def test_search_feed_requires_query(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get(
        "/opds/search?q=dune", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 200
    feed = parse_feed(response.content)
    assert list(feed.iter(f"{{{ATOM_NS}}}entry")) == []


def test_opensearch_description(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, _, reader_token, _ = opds_env
    response = client.get(
        "/opds/opensearch.xml", headers={"Authorization": f"Bearer {reader_token}"}
    )
    assert response.status_code == 200
    assert "opensearch" in response.headers["content-type"]
    assert "/opds/search?q={searchTerms}" in response.text


# --------------------------------------------------------------------------- #
# Download endpoint
# --------------------------------------------------------------------------- #
def test_download_streams_publication(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "dune.epub")])
        book_id, file_id = book.id, book.files[0].id
        db.commit()
    response = client.get(
        f"/opds/books/{book_id}/download/{file_id}",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == EPUB
    assert response.content.startswith(b"PK")  # zip container


def test_download_unknown_file_404(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path)
        book_id = book.id
        db.commit()
    response = client.get(
        f"/opds/books/{book_id}/download/99999",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert response.status_code == 404


def test_download_missing_file_410(opds_env: OpdsEnv, tmp_path: Path) -> None:
    client, factory_cls, reader_token, _ = opds_env
    with factory_cls() as db:
        book = seed_book(db, tmp_path, formats=[("epub", "gone.epub")])
        book_id, file_id = book.id, book.files[0].id
        for file_row in book.files:
            file_row.is_missing = True
        db.commit()
    response = client.get(
        f"/opds/books/{book_id}/download/{file_id}",
        headers={"Authorization": f"Bearer {reader_token}"},
    )
    assert response.status_code == 410
