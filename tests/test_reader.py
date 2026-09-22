"""Phase 11 tests: the in-browser EPUB reader.

Builds small, real EPUB containers inside a temporary library root and drives
the reader endpoints — the reader shell with TOC + resume, chapter documents
with their links rewritten to reader-local endpoints, resources served with
correct MIME types and CSS ``url()`` rewriting, traversal rejection, the EPUB2
NCX fallback, and the graceful empty states — verifying the thin-route
contract that pages and chapter responses only delegate to ReaderService.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from buku.app import create_app
from buku.config import Settings, set_settings
from buku.db import get_engine, get_session_factory, reset_engine, run_migrations
from buku.models import Book, BookFile, Library, User
from buku.services.auth import auth_service
from buku.services.progression import progression_service

ReaderEnv = tuple[TestClient, sessionmaker[Session], str]  # client, factory, reader username


@pytest.fixture
def reader_env(tmp_path: Path) -> Generator[ReaderEnv]:
    """Set up a migrated DB, a reader user, and an authenticated client."""
    reset_engine()
    database_file = tmp_path / "reader_http.db"
    url = f"sqlite:///{database_file}"
    settings = Settings(config_dir=tmp_path, database_url=url, jobs_enabled=False)
    set_settings(settings)
    run_migrations(url)

    app = create_app(settings)
    factory_cls = get_session_factory(get_engine(url))
    with TestClient(app) as client:
        with factory_cls() as db:
            auth_service.create_user(db, "reader", "readerpass123", "Reader", is_admin=False)
        client.post("/api/v1/auth/login", json={"username": "reader", "password": "readerpass123"})
        yield client, factory_cls, "reader"
    reset_engine()
    set_settings(None)


# --------------------------------------------------------------------------- #
# EPUB builder
# --------------------------------------------------------------------------- #
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(32)

_CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

_NAV_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body>
<nav epub:type="toc" id="toc">
  <h1>Contents</h1>
  <ol>
    <li><a href="chapter1.xhtml">Chapter One</a></li>
    <li><a href="chapter2.xhtml">Chapter Two</a>
      <ol><li><a href="chapter2.xhtml#p15">A sub-section</a></li></ol>
    </li>
    <li><a href="chapter3.xhtml#p33">Chapter Three</a></li>
  </ol>
</nav>
</body>
</html>"""


def _ncx(title: str) -> str:
    nav_points = [
        '<navPoint id="n1"><navLabel><text>Chapter One</text></navLabel>'
        '<content src="chapter1.xhtml"/></navPoint>',
        '<navPoint id="n2"><navLabel><text>Chapter Two</text></navLabel>'
        '<content src="chapter2.xhtml"/>'
        '<navPoint id="n2a"><navLabel><text>A sub-section</text></navLabel>'
        '<content src="chapter2.xhtml#p15"/></navPoint>'
        "</navPoint>",
        '<navPoint id="n3"><navLabel><text>Chapter Three</text></navLabel>'
        '<content src="chapter3.xhtml#p33"/></navPoint>',
    ]
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="id"/></head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
    {"".join(nav_points)}
  </navMap>
</ncx>"""


def build_epub(library: Path, *, epub2: bool = False, title: str = "The Reader") -> Path:
    """Write a small three-chapter EPUB into the library root and return its path."""
    epub_path = library / "book.epub"
    opf_dir = "OEBPS"

    chapters = {
        1: """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title>
<link rel="stylesheet" type="text/css" href="styles.css"/>
</head>
<body>
<h1 id="p1">Chapter One</h1>
<p id="p2">Opening paragraph.</p>
<img src="images/pic.png" alt="A picture"/>
<p><a href="#p2">Jump within chapter.</a></p>
<p><a href="chapter3.xhtml#p33">Jump ahead.</a></p>
<p><a href="https://example.org/x">External link.</a></p>
</body>
</html>""",
        2: """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Two</title></head>
<body><h1 id="p10">Chapter Two</h1><p>Middle of the book.</p></body>
</html>""",
        3: """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Three</title></head>
<body><h1 id="p30">Chapter Three</h1><p id="p33">Deep in chapter three.</p></body>
</html>""",
    }

    manifest = [
        f'<item id="ch{i}" href="chapter{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in chapters
    ]
    manifest += [
        '<item id="css" href="styles.css" media-type="text/css"/>',
        '<item id="img" href="images/pic.png" media-type="image/png"/>',
    ]
    spine = [f'<itemref idref="ch{i}"/>' for i in chapters]

    if epub2:
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        toc_attr = ' toc="ncx"'
    else:
        manifest.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        toc_attr = ""

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">{title}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>Ada Lovelace</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    {"".join(manifest)}
  </manifest>
  <spine{toc_attr}>
    {"".join(spine)}
  </spine>
</package>"""

    css = "body { background: url(images/pic.png) no-repeat; color: #333; }\n"

    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER)
        zf.writestr(f"{opf_dir}/content.opf", opf)
        for index, body in chapters.items():
            zf.writestr(f"{opf_dir}/chapter{index}.xhtml", body)
        zf.writestr(f"{opf_dir}/styles.css", css)
        zf.writestr(f"{opf_dir}/images/pic.png", PNG_BYTES)
        if epub2:
            zf.writestr(f"{opf_dir}/toc.ncx", _ncx(title))
        else:
            zf.writestr(f"{opf_dir}/nav.xhtml", _NAV_XHTML)
    return epub_path


def seed_epub_book(
    factory: sessionmaker[Session],
    tmp_path: Path,
    *,
    epub2: bool = False,
    title: str = "The Reader",
) -> int:
    """Attach a real EPUB file to a Book row and return the book id."""
    lib = tmp_path / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    epub = build_epub(lib, epub2=epub2, title=title)
    with factory() as db:
        library = db.scalar(select(Library).where(Library.path == str(lib)))
        if library is None:
            library = Library(name="reader-lib", path=str(lib))
            db.add(library)
            db.flush()
        book = Book(library_id=library.id, title=title)
        db.add(book)
        db.flush()
        db.add(
            BookFile(
                book_id=book.id,
                file_path=str(epub),
                file_format="epub",
                file_size_bytes=epub.stat().st_size,
                file_hash=hashlib.sha256(epub.read_bytes()).hexdigest(),
                file_mtime=datetime.now(UTC),
            )
        )
        db.commit()
        return book.id


def seed_plain_book(factory: sessionmaker[Session], tmp_path: Path) -> int:
    """Book row with no files attached (no EPUB to open)."""
    lib = tmp_path / "plain"
    lib.mkdir(parents=True, exist_ok=True)
    with factory() as db:
        library = Library(name="plain-lib", path=str(lib))
        db.add(library)
        db.flush()
        book = Book(library_id=library.id, title="No Ebook Yet")
        db.add(book)
        db.commit()
        return book.id


# --------------------------------------------------------------------------- #
# Reader shell
# --------------------------------------------------------------------------- #
def test_reader_page_requires_auth(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)
    client.cookies.clear()
    response = client.get(f"/reader/{book_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_reader_page_renders_toc_chapters_and_frame(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    body = response.text
    # TOC labels (nav document) and chapter titles
    assert "Chapter One" in body
    assert "A sub-section" in body
    assert "Chapter Three" in body
    # chapter frame + progress wiring
    assert f'src="/reader/{book_id}/chapter/0"' in body
    assert f'data-progress-api="/api/v1/progress/{book_id}"' in body
    assert "data-chapter-hrefs=" in body
    assert "reader.js" in body


def test_reader_resumes_at_stored_progress(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)
    with factory_cls() as db:
        user = db.scalar(select(User).where(User.username == "reader"))
        assert user is not None
        progression_service.update(
            db,
            user.id,
            book_id,
            progression=0.66,
            href="OEBPS/chapter3.xhtml",
            fragment="p33",
        )

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    body = response.text
    assert f'src="/reader/{book_id}/chapter/2"' in body
    assert 'data-resume-fragment="p33"' in body


def test_reader_page_without_epub_shows_empty_state(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_plain_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    assert "has no EPUB to open in the browser" in response.text
    assert "reader-frame" not in response.text


def test_reader_page_unknown_book_404(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, _, _ = reader_env
    response = client.get("/reader/999999")
    assert response.status_code == 404


def test_reader_page_with_missing_file_shows_empty_state(
    reader_env: ReaderEnv, tmp_path: Path
) -> None:
    client, factory_cls, _ = reader_env
    lib = tmp_path / "ghost"
    lib.mkdir(parents=True, exist_ok=True)
    epub = build_epub(lib)
    book_id = seed_plain_book(factory_cls, tmp_path)
    # Attach a BookFile pointing at a file that no longer exists.
    with factory_cls() as db:
        db.add(
            BookFile(
                book_id=book_id,
                file_path=str(epub),
                file_format="epub",
                file_size_bytes=0,
                file_hash="0" * 64,
                file_mtime=datetime.now(UTC),
            )
        )
        db.commit()

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    assert "could not be found" in response.text


# --------------------------------------------------------------------------- #
# Chapter documents
# --------------------------------------------------------------------------- #
def test_reader_chapter_requires_auth(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)
    client.cookies.clear()
    response = client.get(f"/reader/{book_id}/chapter/0", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_reader_chapter_serves_rewritten_html(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}/chapter/0")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "script-src 'none'" in response.headers["content-security-policy"]
    body = response.text
    # CSS link rewritten to the resource endpoint
    assert f'href="/reader/{book_id}/resource/OEBPS/styles.css"' in body
    # Image src rewritten to the resource endpoint
    assert f'src="/reader/{book_id}/resource/OEBPS/images/pic.png"' in body
    # Cross-chapter link rewritten to the chapter endpoint (fragment kept)
    assert f'href="/reader/{book_id}/chapter/2#p33"' in body
    # Same-document anchor and external links left untouched
    assert 'href="#p2"' in body
    assert 'href="https://example.org/x"' in body
    assert "Chapter One" in body


def test_reader_chapter_out_of_range_404(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}/chapter/99")
    assert response.status_code == 404


def test_reader_chapter_unknown_book_404(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, _, _ = reader_env
    response = client.get("/reader/999999/chapter/0")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
def test_reader_resource_serves_bytes_and_mime(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}/resource/OEBPS/images/pic.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG_BYTES
    assert response.headers.get("content-security-policy") is None


def test_reader_resource_rewrites_css_urls(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    response = client.get(f"/reader/{book_id}/resource/OEBPS/styles.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert f"url(/reader/{book_id}/resource/OEBPS/images/pic.png)" in response.text


def test_reader_resource_rejects_traversal_and_unknown(
    reader_env: ReaderEnv, tmp_path: Path
) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path)

    # Parent-directory traversal cannot reach members outside the manifest.
    traversal = client.get(f"/reader/{book_id}/resource/OEBPS/../../META-INF/container.xml")
    assert traversal.status_code == 404
    # Members that exist in the archive but are not manifest resources are refused.
    unknown = client.get(f"/reader/{book_id}/resource/META-INF/container.xml")
    assert unknown.status_code == 404
    # Percent-encoded traversal is decoded before the membership check.
    encoded = client.get(f"/reader/{book_id}/resource/%2e%2e/META-INF/container.xml")
    assert encoded.status_code == 404


# --------------------------------------------------------------------------- #
# EPUB2 NCX fallback
# --------------------------------------------------------------------------- #
def test_reader_epub2_ncx_toc(reader_env: ReaderEnv, tmp_path: Path) -> None:
    client, factory_cls, _ = reader_env
    book_id = seed_epub_book(factory_cls, tmp_path, epub2=True)

    response = client.get(f"/reader/{book_id}")
    assert response.status_code == 200
    body = response.text
    assert "Chapter One" in body
    assert "A sub-section" in body
    assert "Chapter Three" in body

    chapter = client.get(f"/reader/{book_id}/chapter/1")
    assert chapter.status_code == 200
    assert "Chapter Two" in chapter.text
