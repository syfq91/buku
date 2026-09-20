"""Phase 5 tests: format handler metadata/cover/content extraction.

These exercise real EPUB, CBZ, and PDF containers (built by ``tests.fixtures``)
plus graceful degradation on malformed inputs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from buku.scanner.archive import ArchiveSafetyError
from buku.scanner.handlers import (
    CBZFormatHandler,
    EPUBFormatHandler,
    PDFFormatHandler,
    get_default_handlers,
    get_handler_for_file,
    parse_filename_metadata,
)
from tests.fixtures import TINY_JPEG, build_cbz, build_epub, build_pdf, tiny_png_data

EPUB_HANDLER = EPUBFormatHandler()
CBZ_HANDLER = CBZFormatHandler()
PDF_HANDLER = PDFFormatHandler()


# --------------------------------------------------------------------------- #
# Detection & registry
# --------------------------------------------------------------------------- #


def test_default_handlers_registry(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "a.epub", title="A")
    cbz = build_cbz(tmp_path / "b.cbz", count=2)
    pdf = build_pdf(tmp_path / "c.pdf")

    handlers = get_default_handlers()
    assert [h.format_name for h in handlers] == ["epub", "cbz", "pdf"]
    epub_handler = get_handler_for_file(epub)
    cbz_handler = get_handler_for_file(cbz)
    pdf_handler = get_handler_for_file(pdf)
    assert epub_handler is not None and epub_handler.format_name == "epub"
    assert cbz_handler is not None and cbz_handler.format_name == "cbz"
    assert pdf_handler is not None and pdf_handler.format_name == "pdf"
    assert get_handler_for_file(tmp_path / "readme.txt") is None


def test_detect_rejects_wrong_suffix(tmp_path: Path) -> None:
    (tmp_path / "book.pdf").write_bytes(b"PK\x03\x04 fake zip")
    assert not EPUB_HANDLER.detect(tmp_path / "book.pdf")


def test_detect_is_never_crashing(tmp_path: Path) -> None:
    """detect() must return bool, never raise, so discovery loops stay safe."""
    unreadable = tmp_path / "x.epub"
    unreadable.write_bytes(b"PK\x03\x04" + b"data")
    unreadable.chmod(0o000)
    try:
        assert EPUB_HANDLER.detect(unreadable) is False
    finally:
        unreadable.chmod(0o644)


# --------------------------------------------------------------------------- #
# Filename conventions
# --------------------------------------------------------------------------- #


def test_parse_filename_metadata_author_title() -> None:
    meta = parse_filename_metadata(Path("/books/Frank Herbert - Dune.epub"))
    assert meta.title == "Dune"
    assert meta.authors == ["Frank Herbert"]


def test_parse_filename_metadata_title_only() -> None:
    meta = parse_filename_metadata(Path("/books/Snow Crash.pdf"))
    assert meta.title == "Snow Crash"
    assert meta.authors == []


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #


def test_epub_extract_metadata_full(tmp_path: Path) -> None:
    epub = build_epub(
        tmp_path / "Dune.epub",
        title="Dune",
        subtitle="The Desert Planet Saga",
        authors=["Frank Herbert"],
        language="en",
        publisher="Chilton Books",
        published_date="2020-05-03",
        description="A desert planet saga.",
        series="Dune Chronicles",
        series_index=1.0,
        identifiers={"isbn": "urn:isbn:9780441013593"},
    )
    meta = EPUB_HANDLER.extract_metadata(epub)

    assert meta.title == "Dune"
    assert meta.subtitle == "The Desert Planet Saga"
    assert meta.authors == ["Frank Herbert"]
    assert meta.language == "en"
    assert meta.publisher == "Chilton Books"
    assert meta.published_date == "2020-05-03"
    assert meta.description == "A desert planet saga."
    assert meta.series == "Dune Chronicles"
    assert meta.series_index == 1.0
    assert meta.identifiers.get("isbn") == "9780441013593"


def test_epub_dates_normalized(tmp_path: Path) -> None:
    epub = build_epub(
        tmp_path / "Dates.epub",
        title="Dated",
        published_date="2020-05-03T12:30:00+00:00",
    )
    meta = EPUB_HANDLER.extract_metadata(epub)
    assert meta.published_date == "2020-05-03"


def test_epub_falls_back_to_filename_when_unlabelled(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "Ghost.epub", title=None)
    meta = EPUB_HANDLER.extract_metadata(epub)
    assert meta.title == "Ghost"


def test_epub_cover_extraction(tmp_path: Path) -> None:
    cover = tiny_png_data(width=8, height=8, rgb=(10, 20, 30))
    epub = build_epub(tmp_path / "cover.epub", title="Covered", cover=cover)
    extracted = EPUB_HANDLER.extract_cover(epub)
    assert extracted == cover


def test_epub_get_content(tmp_path: Path) -> None:
    epub = build_epub(
        tmp_path / "content.epub",
        title="Content",
        chapter_text="Hello world chapter",
    )
    stream = EPUB_HANDLER.get_content(epub, "OEBPS/chapter1.xhtml")
    data = stream.read()
    assert b"Hello world chapter" in data


def test_epub_get_content_unknown_rejected(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "content.epub", title="Content")
    with pytest.raises(KeyError):
        EPUB_HANDLER.get_content(epub, "OEBPS/nope.xhtml")
    # Path-traversal identifiers must never resolve.
    with pytest.raises(KeyError):
        EPUB_HANDLER.get_content(epub, "../buku.db")
    with pytest.raises(KeyError):
        EPUB_HANDLER.get_content(epub, "/etc/passwd")


def test_epub_corrupt_container_raises_parse_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.epub"
    corrupt.write_bytes(b"PK\x03\x04junk junk junk")
    with pytest.raises((zipfile.BadZipFile, ArchiveSafetyError, ValueError)):
        EPUB_HANDLER.extract_metadata(corrupt)


def test_epub_missing_container_raises(tmp_path: Path) -> None:
    """A zip with an OPF but no container.xml must be rejected."""
    epub = tmp_path / "nocontainer.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("OEBPS/content.opf", "<package/>")
    with pytest.raises(ValueError, match="container.xml"):
        EPUB_HANDLER.extract_metadata(epub)


# --------------------------------------------------------------------------- #
# CBZ
# --------------------------------------------------------------------------- #


def test_cbz_metadata_page_count_and_cover(tmp_path: Path) -> None:
    page1 = tiny_png_data()
    cbz = build_cbz(
        tmp_path / "Comic.cbz",
        pages=[
            ("cover.png", page1),
            ("p002.jpg", b"\xff\xd8\xfffake"),
            ("p003.png", tiny_png_data()),
        ],
    )
    meta = CBZ_HANDLER.extract_metadata(cbz)
    assert meta.page_count == 3
    assert meta.title == "Comic"

    cover = CBZ_HANDLER.extract_cover(cbz)
    assert cover == page1


def test_cbz_get_content(tmp_path: Path) -> None:
    cbz = build_cbz(tmp_path / "Comic.cbz", count=3)
    data = CBZ_HANDLER.get_content(cbz, "page002.png").read()
    assert data.startswith(b"\x89PNG")
    with pytest.raises(KeyError):
        CBZ_HANDLER.get_content(cbz, "../nothing.png")


def test_cbz_no_images_page_count_zero(tmp_path: Path) -> None:
    cbz = build_cbz(
        tmp_path / "Empty.cbz",
        pages=[("readme.txt", b"no images here")],
    )
    meta = CBZ_HANDLER.extract_metadata(cbz)
    assert meta.page_count == 0
    assert CBZ_HANDLER.extract_cover(cbz) is None


def test_cbz_corrupt_raises(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.cbz"
    corrupt.write_bytes(b"PK\x03\x04not a real cbz")
    with pytest.raises((zipfile.BadZipFile, ArchiveSafetyError, ValueError)):
        CBZ_HANDLER.extract_metadata(corrupt)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def test_pdf_extract_metadata(tmp_path: Path) -> None:
    pdf = build_pdf(
        tmp_path / "Manual.pdf",
        title="The Manual",
        author="M. Author",
        subject="How-to guide",
        language="en-US",
        creation_date="20210601120000",  # PDF date literal: D:20210601120000
        pages=1,
    )
    meta = PDF_HANDLER.extract_metadata(pdf)
    assert meta.title == "The Manual"
    assert meta.authors == ["M. Author"]
    assert meta.description == "How-to guide"
    assert meta.language == "en-US"
    assert meta.published_date == "2021-06-01"
    assert meta.page_count == 1


def test_pdf_unparseable_creation_date_does_not_degrade(tmp_path: Path) -> None:
    """A malformed /CreationDate literal (pypdf raises ValueError) must not
    nuke the rest of the metadata extraction or trigger the filename fallback.
    """
    pdf = build_pdf(
        tmp_path / "Undated Manual.pdf",
        title="Undated Manual",
        author="M. Author",
        subject="How-to guide",
        language="en-GB",
        creation_date="notadate",  # pypdf: "Can not convert date: D:notadate"
    )
    meta = PDF_HANDLER.extract_metadata(pdf)

    # Full extraction survives; only the date is dropped.
    assert meta.title == "Undated Manual"
    assert meta.authors == ["M. Author"]
    assert meta.description == "How-to guide"
    assert meta.language == "en-GB"
    assert meta.published_date is None
    assert meta.page_count == 1


def test_pdf_multipage_count(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "Volumes.pdf", title="Volumes", pages=3)
    meta = PDF_HANDLER.extract_metadata(pdf)
    assert meta.page_count == 3


def test_pdf_falls_back_to_filename_when_corrupt(tmp_path: Path) -> None:
    corrupt = tmp_path / "Broken Guide.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nnot a real pdf body at all")
    meta = PDF_HANDLER.extract_metadata(corrupt)
    assert meta.title == "Broken Guide"
    assert meta.page_count == 0


def test_pdf_cover_embedded_image(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "Covered.pdf", title="Covered", image=TINY_JPEG)
    cover = PDF_HANDLER.extract_cover(pdf)
    assert cover == TINY_JPEG


def test_pdf_cover_none_when_no_images(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "Plain.pdf", title="Plain")
    assert PDF_HANDLER.extract_cover(pdf) is None


def test_pdf_get_content(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "Doc.pdf", title="Doc")
    data = PDF_HANDLER.get_content(pdf, "Doc.pdf").read()
    assert data.startswith(b"%PDF-")
    with pytest.raises(KeyError):
        PDF_HANDLER.get_content(pdf, "missing.pdf")
