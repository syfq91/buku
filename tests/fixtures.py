"""Real EPUB / CBZ / PDF fixture builders for format handler tests.

The format handlers now parse actual containers, so test fixtures must be
genuinely well-formed files rather than magic-byte stubs.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

# A classic minimal valid 1x1 JPEG used to embed in PDF fixtures.
TINY_JPEG = bytes.fromhex(
    "FFD8FFE000104A46494600010100000100010000FFDB004300080606070605080707070909080A0C"
    "140D0C0B0B0C1912130F141D1A1F1E1D1A1C1C20242E2720222C231C1C2837292C30313434341F27"
    "393D38323C2E333432FFC0000B080001000101011100FFC4001F0000010501010101010100000000"
    "00000000000102030405060708090A0BFFC400B5100002010303020403050504040000017D010203"
    "00041105122131410613516107227114328191A1082342B1C11552D1F02433627282090A161718191A"
    "25262728292A3435363738393A434445464748494A535455565758595A636465666768696A73747576"
    "7778797A838485868788898A92939495969798999AA2A3A4A5A6A7A8A9AAB2B3B4B5B6B7B8B9BAC2C3"
    "C4C5C6C7C8C9CAD2D3D4D5D6D7D8D9DAE1E2E3E4E5E6E7E8E9EAF1F2F3F4F5F6F7F8F9FAFFDA0008"
    "010100003F00F97F698201E20223203F2D73DFD3F5AFD7ECFFD9"
)


def tiny_png_data(
    width: int = 1, height: int = 1, rgb: tuple[int, int, int] = (255, 0, 0)
) -> bytes:
    """Construct a minimal valid PNG image."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        payload = typ + data
        checksum = struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + payload + checksum

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def _opf_xml(
    *,
    title: str | None,
    subtitle: str | None,
    authors: list[str],
    language: str,
    publisher: str | None,
    published_date: str | None,
    description: str | None,
    series: str | None,
    series_index: float | None,
    identifiers: dict[str, str],
    has_cover: bool,
) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="pub-id">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">',
    ]
    if title is not None:
        lines.append(f'<dc:title id="title">{escape(title)}</dc:title>')
    for author in authors:
        lines.append(f'<dc:creator opf:role="aut">{escape(author)}</dc:creator>')
    lines.append(f"<dc:language>{escape(language)}</dc:language>")
    if publisher is not None:
        lines.append(f"<dc:publisher>{escape(publisher)}</dc:publisher>")
    if published_date is not None:
        lines.append(f"<dc:date>{escape(published_date)}</dc:date>")
    if description is not None:
        lines.append(f"<dc:description>{escape(description)}</dc:description>")
    if identifiers:
        for scheme, value in identifiers.items():
            lines.append(
                '<dc:identifier id="pub-id" opf:scheme="'
                f'{escape(scheme)}">{escape(value)}</dc:identifier>'
            )
    if subtitle is not None:
        lines.append(f'<meta property="dcterms:alternative">{escape(subtitle)}</meta>')
    if series is not None:
        lines.append(f'<meta name="calibre:series" content="{escape(series)}"/>')
    if series_index is not None:
        lines.append(f'<meta name="calibre:series_index" content="{series_index}"/>')
    if has_cover:
        lines.append('<meta name="cover" content="cover-img"/>')
    lines.append("</metadata>")

    manifest_items = [
        '<item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    if has_cover:
        manifest_items.insert(0, '<item id="cover-img" href="cover.png" media-type="image/png"/>')
    lines.append("<manifest>")
    lines.extend(manifest_items)
    lines.append("</manifest>")
    lines.append('<spine toc="ncx">')
    lines.append('<itemref idref="chapter1"/>')
    lines.append("</spine>")
    lines.append("</package>")
    return "\n".join(lines)


def build_epub(
    path: Path,
    *,
    title: str | None,
    subtitle: str | None = None,
    authors: list[str] | None = None,
    language: str = "en",
    publisher: str | None = None,
    published_date: str | None = None,
    description: str | None = None,
    series: str | None = None,
    series_index: float | None = None,
    identifiers: dict[str, str] | None = None,
    cover: bytes | None = None,
    chapter_text: str = "Chapter one content",
) -> Path:
    """Build a well-formed EPUB2 container at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    authors = authors or []
    identifiers = identifiers or {}
    has_cover = True

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>"
    )
    content_opf = _opf_xml(
        title=title,
        subtitle=subtitle,
        authors=authors,
        language=language,
        publisher=publisher,
        published_date=published_date,
        description=description,
        series=series,
        series_index=series_index,
        identifiers=identifiers,
        has_cover=has_cover,
    )
    chapter_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        f"<body><p>{escape(chapter_text)}</p></body></html>"
    )
    ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        '<navMap><navPoint id="np-1"><navLabel><text>Chapter 1</text></navLabel>'
        '<content src="chapter1.xhtml"/></navPoint></navMap></ncx>'
    )
    cover_bytes = cover if cover is not None else tiny_png_data()

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", content_opf)
        zf.writestr("OEBPS/chapter1.xhtml", chapter_xhtml)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/cover.png", cover_bytes)
    return path


def build_cbz(
    path: Path,
    pages: list[tuple[str, bytes]] | None = None,
    *,
    count: int = 3,
) -> Path:
    """Build a CBZ archive from image members (default: numbered PNG pages)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    members = pages or [(f"page{index:03d}.png", tiny_png_data()) for index in range(1, count + 1)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(
    path: Path,
    *,
    title: str | None = None,
    author: str | None = None,
    subject: str | None = None,
    language: str | None = None,
    creation_date: str | None = None,
    image: bytes | None = None,
    pages: int = 1,
) -> Path:
    """Build a valid PDF with correct xref offsets and optional metadata + image.

    Only one anonymous image XObject (``/Im0``) is drawn on every page; this is
    enough for pypdf to report ``page.images``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    buffer = bytearray()
    buffer += b"%PDF-1.4\n"
    offsets: dict[int, int] = {}

    def add_object(num: int, body: bytes) -> None:
        offsets[num] = len(buffer)
        buffer.extend(f"{num} 0 obj\n".encode("ascii") + body + b"\nendobj\n")

    assert pages >= 1
    total = 3 + 2 * pages  # catalog, pages, pages*page + pages*content
    image_obj: int | None = total
    if image is None:
        image_obj = None
    info_obj: int | None = image_obj + 1 if image_obj is not None else total
    info_obj = (
        None
        if not any(x is not None for x in (title, author, subject, creation_date))
        else info_obj
    )

    # Content streams
    for i in range(pages):
        ops = b""
        if image_obj is not None:
            ops = b"q 1 0 0 1 0 0 cm /Im0 Do Q"
        content_num = 3 + pages + i
        body = (
            b"<< /Length " + str(len(ops)).encode("ascii") + b" >>\nstream\n" + ops + b"\nendstream"
        )
        add_object(content_num, body)

    # Image XObject
    if image_obj is not None and image is not None:
        body = (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            b"/Length "
            + str(len(image)).encode("ascii")
            + b" >>\nstream\n"
            + image
            + b"\nendstream"
        )
        add_object(image_obj, body)

    # Info dictionary
    if info_obj is not None:
        entries = []
        if title is not None:
            entries.append(f"/Title ({_escape_pdf_text(title)})")
        if author is not None:
            entries.append(f"/Author ({_escape_pdf_text(author)})")
        if subject is not None:
            entries.append(f"/Subject ({_escape_pdf_text(subject)})")
        if creation_date is not None:
            entries.append(f"/CreationDate (D:{creation_date})")
        add_object(info_obj, ("<<\n" + "\n".join(entries) + "\n>>").encode("ascii"))

    # Page objects
    resource_dict = b"<< >>"
    if image_obj is not None:
        resource_dict = f"<< /XObject << /Im0 {image_obj} 0 R >> >>".encode("ascii")
    for i in range(pages):
        page_num = 3 + i
        body = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources ".encode("ascii")
            + resource_dict
            + f" /Contents {3 + pages + i} 0 R >>".encode("ascii")
        )
        add_object(page_num, body)

    # Pages tree
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    add_object(2, f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("ascii"))

    # Catalog
    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if language is not None:
        catalog += f" /Lang ({_escape_pdf_text(language)})"
    add_object(1, (catalog + " >>").encode("ascii"))

    # Cross-reference table
    xref_pos = len(buffer)
    count = max(offsets) + 1
    buffer += f"xref\n0 {count}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for num in range(1, count):
        buffer += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    trailer = f"trailer\n<< /Size {count} /Root 1 0 R"
    if info_obj is not None:
        trailer += f" /Info {info_obj} 0 R"
    buffer += f"{trailer} >>\nstartxref\n{xref_pos}\n%%EOF".encode("ascii")

    path.write_bytes(bytes(buffer))
    return path


__all__ = ["TINY_JPEG", "build_cbz", "build_epub", "build_pdf", "tiny_png_data"]
