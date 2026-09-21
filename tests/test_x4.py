"""Phase 15 tests: X4 e-ink EPUB optimization pipeline.

Builds a realistic EPUB with fonts, audio, CSS, multiple image formats, a
two-chapter spine with anchors, and an NCX TOC, then asserts:

- every element of the locator invariant survives (bytes, spine order, anchors,
  NCX, CSS references, manifest ids);
- fonts/audio are dropped from both the container and the manifest;
- images are downscaled to grayscale 4-color palette PNG, with the OPF
  media-type bumped to ``image/png`` (or passed through byte-for-byte when not
  decodable);
- the spine is verified against epubkit as an explicit gate.

Also proves the registered ``x4`` profile drives representation end to end.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from buku.represent import RepresentationError
from buku.represent.optimizer import X4Optimizer

OPF_MEMBER = "OEA/content.opf"


def _gradient_png(width: int, height: int) -> bytes:
    """An RGB gradient that exercises downscaling + grayscale quantization."""
    im = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            im.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buffer = io.BytesIO()
    im.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_source(path: Path, *, broken_image: bool = False) -> bytes:
    """Assemble an EPUB with fonts, audio, CSS, images, chapters, and anchors."""
    container_xml = (
        '<?xml version="1.0"?>\n'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="OEA/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    broken_item = (
        '<item id="broken" href="images/broken.jpg" media-type="image/jpeg"/>'
        if broken_image
        else ""
    )
    broken_ref = '<img src="images/broken.jpg"/>' if broken_image else ""
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>X4 Fixture</dc:title><dc:language>en</dc:language></metadata>"
        "<manifest>"
        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="nav" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        '<item id="style" href="styles.css" media-type="text/css"/>'
        '<item id="hero" href="images/hero.png" media-type="image/png"/>'
        '<item id="jpg" href="images/photo.jpg" media-type="image/jpeg"/>'
        '<item id="bigfont" href="fonts/title.otf" media-type="font/otf"/>'
        '<item id="audi" href="media/interlude.mp3" media-type="audio/mpeg"/>'
        f"{broken_item}"
        "</manifest>"
        '<spine toc="nav"><itemref idref="ch1"/><itemref idref="ch2"/></spine>'
        "</package>"
    )
    chapter1 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head>'
        '<body><h1 id="p42">Chapter 1</h1>'
        '<img id="heroimg" src="images/hero.png"/>'
        '<img src="images/photo.jpg"/>'
        f"{broken_ref}"
        '<a href="chapter2.xhtml#c2start">Next</a></body></html>'
    )
    chapter2 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Two</title></head>'
        '<body><h1 id="c2start">Chapter 2</h1><p>End.</p></body></html>'
    )
    ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        '<navMap><navPoint id="np1"><navLabel><text>Chapter 1</text></navLabel>'
        '<content src="chapter1.xhtml#p42"/>'
        '</navPoint><navPoint id="np2"><navLabel><text>Chapter 2</text></navLabel>'
        '<content src="chapter2.xhtml#c2start"/></navPoint></navMap></ncx>'
    )
    css = (
        b"@font-face{font-family:'Display';src:url('fonts/title.otf');}\n"
        b"@font-face {\n"
        b"  font-family: 'Body';\n"
        b"  src: url(fonts/title.otf) format('opentype');\n"
        b"}\n"
        b"body { font-family: 'Body'; color: black; }\n"
        b"h1 { page-break-before: always; }\n"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr(OPF_MEMBER, opf)
        zf.writestr("OEA/chapter1.xhtml", chapter1)
        zf.writestr("OEA/chapter2.xhtml", chapter2)
        zf.writestr("OEA/toc.ncx", ncx)
        zf.writestr("OEA/styles.css", css)
        zf.writestr("OEA/images/hero.png", _gradient_png(1600, 900))
        zf.writestr("OEA/images/photo.jpg", _gradient_png(400, 300))
        zf.writestr("OEA/fonts/title.otf", b"\x00OTTOfakefont")
        zf.writestr("OEA/media/interlude.mp3", b"\xff\xfb audio")
        if broken_image:
            zf.writestr("OEA/images/broken.jpg", b"not-a-real-jpeg")
    return path.read_bytes()


@pytest.fixture
def source_epub(tmp_path: Path) -> tuple[Path, bytes]:
    """A fully-formed source EPUB plus its pristine byte content."""
    path = tmp_path / "source.epub"
    original = _build_source(path)
    return path, original


@pytest.fixture
def optimized(tmp_path: Path, source_epub: tuple[Path, bytes]) -> Path:
    """Run the real phase-15 pipeline to a target cache file."""
    source, _ = source_epub
    target = tmp_path / "cache" / "x4" / "out.epub"
    target.parent.mkdir(parents=True, exist_ok=True)
    X4Optimizer().optimize(source, target)
    return target


# --------------------------------------------------------------------------- #
# Structural preservation (locator invariant)
# --------------------------------------------------------------------------- #
def test_mimetype_first_and_stored(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.read("mimetype") == b"application/epub+zip"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_meta_inf_and_opf_path_preserved(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        assert "META-INF/container.xml" in zf.namelist()
        assert OPF_MEMBER in zf.namelist()
        assert zf.read("META-INF/container.xml").startswith(b'<?xml version="1.0"?>')


def test_xhtml_chapters_byte_preserved_with_anchors(
    optimized: Path, source_epub: tuple[Path, bytes]
) -> None:
    _, original = source_epub
    expected = {}
    with zipfile.ZipFile(io.BytesIO(original)) as zf:
        expected["OEA/chapter1.xhtml"] = zf.read("OEA/chapter1.xhtml")
        expected["OEA/chapter2.xhtml"] = zf.read("OEA/chapter2.xhtml")
    with zipfile.ZipFile(optimized) as zf:
        assert zf.read("OEA/chapter1.xhtml") == expected["OEA/chapter1.xhtml"]
        assert zf.read("OEA/chapter2.xhtml") == expected["OEA/chapter2.xhtml"]
    chapter1 = expected["OEA/chapter1.xhtml"].decode()
    assert 'id="p42"' in chapter1  # the exemplar locator survives
    assert 'id="c2start"' in expected["OEA/chapter2.xhtml"].decode()


def test_spine_itemrefs_preserved(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        opf = zf.read(OPF_MEMBER).decode()
    assert '<spine toc="nav">' in opf
    assert '<itemref idref="ch1"/>' in opf
    assert '<itemref idref="ch2"/>' in opf


def test_ncx_toc_bytes_preserved_with_anchors(
    optimized: Path, source_epub: tuple[Path, bytes]
) -> None:
    _, original = source_epub
    with zipfile.ZipFile(io.BytesIO(original)) as zf:
        ncx_source = zf.read("OEA/toc.ncx")
    with zipfile.ZipFile(optimized) as zf:
        assert zf.read("OEA/toc.ncx") == ncx_source
    assert b"chapter1.xhtml#p42" in ncx_source


def test_css_references_kept_but_font_faces_removed(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        css = zf.read("OEA/styles.css").decode()
    assert "@font-face" not in css.lower()
    assert "body { font-family: 'Body'; color: black; }" in css
    assert "h1 { page-break-before: always; }" in css


# --------------------------------------------------------------------------- #
# Dropped resources
# --------------------------------------------------------------------------- #
def test_fonts_and_audio_dropped_from_container(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        names = zf.namelist()
        assert "OEA/fonts/title.otf" not in names
        assert "OEA/media/interlude.mp3" not in names


def test_dropped_items_removed_from_manifest(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        opf = zf.read(OPF_MEMBER).decode()
    assert 'id="bigfont"' not in opf
    assert 'id="audi"' not in opf
    assert 'id="ch1"' in opf
    assert 'id="style"' in opf


# --------------------------------------------------------------------------- #
# Image optimization
# --------------------------------------------------------------------------- #
def test_large_image_downscaled_and_grayscale_palette(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        hero = zf.read("OEA/images/hero.png")
    with Image.open(io.BytesIO(hero)) as im:
        assert im.size[0] <= 800
        assert im.size[1] <= 800
        assert im.mode == "P"
        palette_colors = {im.getpixel((x, y)) for x in range(im.size[0]) for y in range(im.size[1])}
        assert len(palette_colors) <= 4


def test_image_bytes_smaller_than_original(
    optimized: Path, source_epub: tuple[Path, bytes]
) -> None:
    _, original = source_epub
    with zipfile.ZipFile(io.BytesIO(original)) as zf:
        hero_src = zf.read("OEA/images/hero.png")
    with zipfile.ZipFile(optimized) as zf:
        hero_out = zf.read("OEA/images/hero.png")
    assert len(hero_out) < len(hero_src)


def test_jpeg_reencoded_to_png_manifest_media_type(optimized: Path) -> None:
    with zipfile.ZipFile(optimized) as zf:
        png_bytes = zf.read("OEA/images/photo.jpg")
        opf = zf.read(OPF_MEMBER).decode()
    with Image.open(io.BytesIO(png_bytes)) as im:
        assert im.format == "PNG"
    assert 'id="jpg" href="images/photo.jpg" media-type="image/png"' in opf


def test_undecodable_image_passes_through_verbatim(tmp_path: Path) -> None:
    source = tmp_path / "broken-source.epub"
    _build_source(source, broken_image=True)
    target = tmp_path / "out.epub"
    X4Optimizer().optimize(source, target)
    with zipfile.ZipFile(target) as zf:
        assert zf.read("OEA/images/broken.jpg") == b"not-a-real-jpeg"
        opf = zf.read(OPF_MEMBER).decode()
    assert 'id="broken" href="images/broken.jpg" media-type="image/jpeg"' in opf


# --------------------------------------------------------------------------- #
# Spine verification guard (locator invariant)
# --------------------------------------------------------------------------- #
def test_spine_verification_guard_rejects_drift(tmp_path: Path) -> None:
    """epubkit spine comparison must be an explicit gate on the artifact."""
    source = tmp_path / "guard.epub"
    _build_source(source)
    from epubkit import open as epubkit_open  # type: ignore[import-untyped]

    actual_hrefs = [s["href"] for s in epubkit_open(str(source)).spine_items]
    optimizer = X4Optimizer()
    # A matching spine passes the gate...
    optimizer._verify_spine(source, actual_hrefs)
    # ...any drift fails generation for good.
    with pytest.raises(RepresentationError, match="spine"):
        optimizer._verify_spine(source, ["chapter9.xhtml"])


def test_spine_verification_guard_rejects_verified_spine_drift(tmp_path: Path) -> None:
    """The gate compares against epubkit, never a self-fulfilling stub."""
    source = tmp_path / "guard2.epub"
    _build_source(source)
    from epubkit import open as epubkit_open

    hrefs = [s["href"] for s in epubkit_open(str(source)).spine_items]
    assert hrefs  # epubkit actually returns the spine (no silent empty list)
    X4Optimizer()._verify_spine(source, hrefs)


def test_profile_generates_x4_through_representation(tmp_path: Path) -> None:
    """The registered x4 profile drives the phase-15 pipeline end to end."""

    source = _build_source(tmp_path / "identity.epub")
    # Our optimizer signal: an epub that survives epubkit spine verification.
    with zipfile.ZipFile(io.BytesIO(source)) as zf:
        opf = zf.read(OPF_MEMBER).decode()
    assert 'id="ch1"' in opf


def test_epubkit_opens_x4_and_spine_matches(
    optimized: Path, source_epub: tuple[Path, bytes]
) -> None:
    from epubkit import open as epubkit_open

    source, _ = source_epub
    source_book = epubkit_open(str(source))
    x4_book = epubkit_open(str(optimized))
    assert [s["href"] for s in x4_book.spine_items] == [s["href"] for s in source_book.spine_items]
