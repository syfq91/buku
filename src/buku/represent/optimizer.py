"""X4 EPUB optimizer pipeline (Phase 15).

Transforms a source EPUB into a lightweight, e-ink-optimized ``x4`` rendering
that preserves everything a locator can point at (AGENTS.md Rule 6):

- ``META-INF`` and the OPF package path are kept intact;
- XHTML chapters (with internal ids/anchors), the NCX/nav TOC, the spine and
  reading order are preserved **byte-for-byte**;
- CSS files are kept (references intact) with ``@font-face`` declarations
  stripped;
- raster images are downscaled to at most 800px, converted to grayscale and
  quantized to a 4-color palette (re-encoded as PNG, with the OPF manifest
  media-type updated so the container stays valid);
- fonts and audio/video resources are dropped (their manifest entries removed).

The finished container is validated by opening it with ``epubkit`` and
comparing its spine to the source spine — any drift raises
:class:`~buku.represent.base.RepresentationError` and the artifact is never
handed to readers.

Rule-of-thumb resilience: any image that fails to decode is passed through
verbatim; any malformed resource only causes that member to be preserved, never
a crash. Original media files are only ever read.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import epubkit  # type: ignore[import-untyped]
from PIL import Image

from buku.represent.base import RepresentationError
from buku.scanner.archive import find_member, read_member, validate_archive

OPF_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
MAX_IMAGE_SIZE = (800, 800)
IMAGE_READ_LIMIT = 32 * 1024 * 1024  # 32 MiB per image; oversized resources kept verbatim
CSS_READ_LIMIT = 5 * 1024 * 1024
OPF_READ_LIMIT = 1024 * 1024

FONT_EXTS = frozenset({".ttf", ".otf", ".woff", ".woff2", ".ttc", ".eot"})
AV_EXTS = frozenset(
    {
        ".mp3",
        ".mp4",
        ".m4a",
        ".m4b",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".wav",
        ".flac",
        ".webm",
        ".mov",
        ".avi",
        ".mkv",
        ".mpg",
        ".mpeg",
        ".mid",
        ".midi",
    }
)

_ITEM_TAG_RE = re.compile(r"<item\b[^>]*?>", re.I)
_ITEMREF_TAG_RE = re.compile(r"<itemref\b[^>]*?>", re.I)
_ATTR_RE = re.compile(r"""\b([\w:.@-]+)\s*=\s*(["'])(.*?)\2""", re.I)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}", re.I | re.S)
_MEDIA_TYPE_RE = re.compile(r"""\bmedia-type\s*=\s*(["'])([^"']*)\1""", re.I)


@dataclass(frozen=True)
class OptimizerResult:
    """Bookkeeping produced by one optimization run."""

    file_size_bytes: int
    dropped_count: int
    optimized_images: int


@dataclass(frozen=True)
class _ManifestItem:
    element_id: str
    attr_href: str
    media_type: str
    member: str


@dataclass(frozen=True)
class _ParsedOpf:
    member: str
    text: str
    items: list[_ManifestItem]
    spine_members: list[str]


class X4Optimizer:
    """Produce a structurally sound ``x4`` profile EPUB from a source EPUB."""

    version = "2"

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #
    def optimize(self, source_path: Path, target_path: Path) -> OptimizerResult:
        """Render *source_path* into *target_path* (never touching media)."""
        with zipfile.ZipFile(source_path, "r") as src:
            infos = validate_archive(src)
            opf = self._locate_opf(src, infos)
            classifications = {item.element_id: _classify_item(item) for item in opf.items}

            dropped = {element_id for element_id, kind in classifications.items() if kind == "drop"}
            image_members = {
                item.member: item
                for item in opf.items
                if classifications[item.element_id] == "image"
            }
            css_members = {
                item.member for item in opf.items if classifications[item.element_id] == "css"
            }
            dropped_members = {item.member for item in opf.items if item.element_id in dropped}

            reencoded_ids: set[str] = set()
            writes: list[tuple[str, bytes, int]] = []
            for info in infos:
                name = info.filename
                if name in dropped_members or name == opf.member:
                    continue
                compress = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
                if item := image_members.get(name):
                    if info.file_size > IMAGE_READ_LIMIT:
                        writes.append((name, read_member(src, info), compress))
                        continue
                    data = read_member(src, info, max_bytes=IMAGE_READ_LIMIT)
                    converted = _reencode_image(data)
                    if converted is None:
                        writes.append((name, data, compress))
                    else:
                        writes.append((name, converted, compress))
                        reencoded_ids.add(item.element_id)
                elif name in css_members:
                    data = read_member(src, info, max_bytes=CSS_READ_LIMIT)
                    writes.append((name, _strip_font_faces(data), compress))
                else:
                    writes.append((name, read_member(src, info), compress))

            new_opf = _rewrite_opf(opf, dropped, reencoded_ids)

            with zipfile.ZipFile(target_path, "w") as dst:
                for name, data, compress in writes:
                    dst.writestr(name, data, compress_type=compress)
                dst.writestr(opf.member, new_opf, compress_type=zipfile.ZIP_DEFLATED)

            optimized_images = len(reencoded_ids)

        self._verify_spine(target_path, opf.spine_members)
        return OptimizerResult(
            file_size_bytes=target_path.stat().st_size,
            dropped_count=len(dropped),
            optimized_images=optimized_images,
        )

    # ------------------------------------------------------------------ #
    # OPF handling
    # ------------------------------------------------------------------ #
    def _locate_opf(self, src: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> _ParsedOpf:
        """Resolve the package document path from ``META-INF/container.xml``."""
        container = find_member(infos, "META-INF/container.xml")
        if container is None:
            raise RepresentationError("EPUB is missing META-INF/container.xml.")
        payload = read_member(src, container, max_bytes=1024 * 1024)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise RepresentationError("EPUB container.xml is malformed.") from exc
        full_path = None
        for rootfile in root.iter(f"{{{OPF_CONTAINER_NS}}}rootfile"):
            full_path = rootfile.get("full-path")
            if full_path:
                break
        if full_path is None:
            raise RepresentationError("EPUB container.xml declares no rootfile full-path.")

        opf_member = posixpath.normpath(full_path).lstrip("/")
        opf_info = find_member(infos, opf_member)
        if opf_info is None:
            raise RepresentationError(f"OPF package document not found: {opf_member}")
        payload = read_member(src, opf_info, max_bytes=OPF_READ_LIMIT)
        return _parse_opf(opf_member, payload)

    @staticmethod
    def _verify_spine(target_path: Path, expected: list[str]) -> None:
        """Open the generated X4 EPUB with epubkit and compare the spine."""
        try:
            book = epubkit.open(str(target_path))
        except Exception as exc:  # noqa: BLE001 - any epubkit failure voids the artifact
            raise RepresentationError(
                f"Generated X4 EPUB failed epubkit validation: {exc}"
            ) from exc
        actual = [
            str(item.get("href", "")).split("#", 1)[0].lstrip("/") for item in book.spine_items
        ]
        if actual != expected:
            raise RepresentationError(
                "X4 EPUB spine does not match the source spine (locator invariant violated): "
                f"{actual!r} != {expected!r}"
            )


# --------------------------------------------------------------------------- #
# Parsing & rewrite
# --------------------------------------------------------------------------- #
def _parse_opf(opf_member: str, payload: bytes) -> _ParsedOpf:
    directory = posixpath.dirname(opf_member)
    text = payload.decode("utf-8-sig", errors="replace")

    items: list[_ManifestItem] = []
    by_element_id: dict[str, _ManifestItem] = {}
    for match in _ITEM_TAG_RE.finditer(text):
        attrs = _parse_attributes(match.group(0))
        element_id = attrs.get("id", "")
        if not element_id:
            continue
        item = _ManifestItem(
            element_id=element_id,
            attr_href=attrs.get("href", ""),
            media_type=attrs.get("media-type", ""),
            member=_resolve_member(directory, attrs.get("href", "")),
        )
        items.append(item)
        by_element_id[element_id] = item

    spine_members: list[str] = []
    for match in _ITEMREF_TAG_RE.finditer(text):
        attrs = _parse_attributes(match.group(0))
        spine_item = by_element_id.get(attrs.get("idref", ""))
        if spine_item is not None:
            spine_members.append(spine_item.member)

    return _ParsedOpf(member=opf_member, text=text, items=items, spine_members=spine_members)


def _rewrite_opf(opf: _ParsedOpf, dropped_ids: set[str], reencoded_ids: set[str]) -> bytes:
    """Drop removed items and bump re-encoded images to ``image/png``.

    The spine/itemrefs and all non-manifest text (metadata, spine, guide) are
    left untouched, so reading order and locators survive unchanged.
    """
    parts: list[str] = []
    cursor = 0
    for match in _ITEM_TAG_RE.finditer(opf.text):
        tag = match.group(0)
        parts.append(opf.text[cursor : match.start()])
        cursor = match.end()
        attrs = _parse_attributes(tag)
        element_id = attrs.get("id", "")
        if element_id in dropped_ids:
            continue
        if element_id in reencoded_ids:
            tag = _MEDIA_TYPE_RE.sub(r'media-type="image/png"', tag, count=1)
        parts.append(tag)
    parts.append(opf.text[cursor:])
    return "".join(parts).encode("utf-8")


def _classify_item(item: _ManifestItem) -> str:
    """Return ``"drop"``, ``"image"``, ``"css"``, or ``"keep"``."""
    media_type = item.media_type.lower()
    ext = posixpath.splitext(item.attr_href)[1].lower()
    if (
        media_type.startswith("font/")
        or media_type.startswith("application/font")
        or ext in FONT_EXTS
    ):
        return "drop"
    if media_type.startswith(("audio/", "video/")) or ext in AV_EXTS:
        return "drop"
    if media_type.startswith("image/"):
        if media_type == "image/svg+xml":
            return "keep"
        return "image"
    if media_type == "text/css" or ext == ".css":
        return "css"
    return "keep"


def _reencode_image(data: bytes) -> bytes | None:
    """Downscale to grayscale 4-color PNG, or ``None`` when unsupported."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "YCbCr"}:
                return None
            im.thumbnail(MAX_IMAGE_SIZE, Image.Resampling.LANCZOS)
            gray = im.convert("L")
            palette = gray.quantize(
                colors=4, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG
            )
            buffer = io.BytesIO()
            palette.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a bad image must never crash the pipeline
        return None


def _strip_font_faces(data: bytes) -> bytes:
    """Remove ``@font-face`` blocks from a CSS file, keeping every other rule."""
    text = data.decode("utf-8-sig", errors="replace")
    return _FONT_FACE_RE.sub("", text).encode("utf-8")


def _parse_attributes(tag: str) -> dict[str, str]:
    return {key: value for key, _, value in _ATTR_RE.findall(tag)}


def _resolve_member(directory: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(directory, href.split("#", 1)[0])).lstrip("/")


__all__ = ["OptimizerResult", "X4Optimizer"]
