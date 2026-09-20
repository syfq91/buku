"""EPUB format handler implemented with only the Python standard library.

EPUB containers are ZIP archives with an OPF package document describing
metadata, the manifest, and the reading order (spine). Parsing relies on
local-name matching rather than strict namespaces so that both EPUB2 and EPUB3
documents (and slightly non-conforming exports) are handled robustly.

All container access goes through :mod:`buku.scanner.archive`, enforcing the
zip-bomb / path-traversal guards required by AGENTS.md.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from buku.metadata.models import SOURCE_EMBEDDED, SOURCE_FILENAME
from buku.scanner.archive import (
    ArchiveSafetyError,
    find_member,
    read_member,
    validate_archive,
)
from buku.scanner.handlers.base import BookMetadata, FormatHandler, parse_filename_metadata

logger = logging.getLogger("buku.scanner.epub")

CONTAINER_PATH = "META-INF/container.xml"
# Safe, generous caps for individual resources.
MAX_OPF_BYTES = 2 * 1024 * 1024
MAX_CONTAINER_BYTES = 64 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024
MAX_RESOURCE_BYTES = 64 * 1024 * 1024

ISBN_RE = re.compile(r"^\d{9}[\dXx]$|^\d{13}$")


def _local(elem: ET.Element) -> str:
    """Return the local (non-namespaced) name of an XML element."""
    return elem.tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element) -> str:
    return (elem.text or "").strip()


def _children(elem: ET.Element, local: str) -> list[ET.Element]:
    return [child for child in elem if _local(child) == local]


def _normalize_date(value: str) -> str | None:
    """Normalize a dc:date-like value to YYYY-MM-DD or YYYY when possible."""
    value = value.strip()
    if not value:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"^(\d{4})$", value)
    if match:
        return value
    return value


def _resolve_title_and_subtitle(
    titles: list[ET.Element], metas: list[ET.Element]
) -> tuple[str, str | None]:
    """Extract main title and subtitle using EPUB3 title-type refinements."""
    title_by_id: dict[str, str] = {}
    for title in titles:
        title_id = title.get("id")
        if title_id:
            title_by_id[title_id] = _text(title)

    type_by_id: dict[str, str] = {}
    alternative: str | None = None
    for meta in metas:
        prop = (meta.get("property") or meta.get("name") or "").strip().lower()
        if prop == "title-type":
            refines = meta.get("refines")
            if refines:
                type_by_id[refines.lstrip("#")] = _text(meta).lower()
        elif prop in ("dcterms:alternative", "alternative") and not meta.get("refines"):
            alternative = _text(meta) or None

    main_title: str | None = None
    subtitle: str | None = None
    for title in titles:
        text = _text(title)
        if not text:
            continue
        ttype = type_by_id.get(title.get("id") or "", "")
        if ttype == "main" and main_title is None:
            main_title = text
        elif ttype == "subtitle" and subtitle is None:
            subtitle = text
        elif main_title is None:
            main_title = text
        elif subtitle is None and ttype != "main":
            subtitle = text

    if subtitle is None:
        subtitle = alternative
    return main_title or "", subtitle


def _resolve_authors(creators: list[ET.Element]) -> list[str]:
    """Collect authors, honoring opf:role filters when declared."""
    authors: list[str] = []
    has_roles = any(
        creator.get("role") or creator.get("{http://www.idpf.org/2007/opf}role")
        for creator in creators
    )
    for creator in creators:
        text = _text(creator)
        if not text:
            continue
        role = creator.get("role") or creator.get("{http://www.idpf.org/2007/opf}role") or ""
        if has_roles and role not in ("", "aut", "author"):
            continue
        authors.append(text)
    return authors


def _resolve_identifiers(identifier_elems: list[ET.Element]) -> dict[str, str]:
    """Extract standard identifiers (ISBN first, then explicit schemes)."""
    identifiers: dict[str, str] = {}
    for elem in identifier_elems:
        text = _text(elem)
        if not text:
            continue
        scheme = (
            (elem.get("scheme") or elem.get("{http://www.idpf.org/2007/opf}scheme") or "")
            .strip()
            .lower()
        )
        lowered = text.lower()
        if "isbn" in scheme or lowered.startswith("urn:isbn:"):
            candidate = text.replace("urn:isbn:", "", 1).strip()
            if ISBN_RE.match(candidate):
                identifiers.setdefault("isbn", candidate)
            continue
        if lowered.startswith("urn:uuid:"):
            identifiers.setdefault("uuid", text.replace("urn:uuid:", "", 1).strip())
            continue
        if scheme:
            identifiers.setdefault(scheme, text.strip())
    return identifiers


def _resolve_series(metas: list[ET.Element]) -> tuple[str | None, float | None]:
    """Extract series name and index from calibre / EPUB3 collection metas.

    Calibre exports use ``<meta name="calibre:series" content="..."/>`` (value in
    the ``content`` attribute), while EPUB3 ``belongs-to-collection`` carries the
    value as element text — handle both.
    """
    series: str | None = None
    index: float | None = None
    for meta in metas:
        prop = (meta.get("property") or meta.get("name") or "").strip().lower()
        value = _text(meta) or (meta.get("content") or "").strip()
        if prop in ("calibre:series", "belongs-to-collection"):
            if value and series is None:
                series = value
        elif prop in ("calibre:series_index", "group-position"):
            if value:
                try:
                    index = float(value)
                except ValueError:
                    continue
    return series, index


def _find_opf_path(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> str:
    """Locate the OPF package path via META-INF/container.xml."""
    container_info = find_member(infos, CONTAINER_PATH)
    if container_info is None:
        raise ValueError("EPUB missing META-INF/container.xml.")
    data = read_member(zf, container_info, max_bytes=MAX_CONTAINER_BYTES)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("Corrupt META-INF/container.xml.") from exc

    for elem in root.iter():
        if _local(elem) == "rootfile":
            media_type = (elem.get("media-type") or "").lower()
            full_path = elem.get("full-path")
            if media_type.startswith("application/oebps-package") and full_path:
                return full_path.strip()
    raise ValueError("No OPF rootfile declared in container.xml.")


def _parse_opf(
    zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo], opf_path: str
) -> tuple[ET.Element, str]:
    """Parse the OPF package document, returning (root, opf_directory)."""
    info = find_member(infos, opf_path)
    if info is None:
        raise ValueError(f"OPF package '{opf_path}' not found in EPUB.")
    data = read_member(zf, info, max_bytes=MAX_OPF_BYTES)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Corrupt OPF package '{opf_path}'.") from exc
    return root, posixpath.dirname(opf_path)


def _find_cover_item(root: ET.Element, opf_dir: str) -> str | None:
    """Resolve the cover image item to its normalized archive member path."""
    metadata = next((e for e in root.iter() if _local(e) == "metadata"), None)
    manifest = next((e for e in root.iter() if _local(e) == "manifest"), None)
    if manifest is None:
        return None

    cover_item_id: str | None = None
    if metadata is not None:
        for meta in _children(metadata, "meta"):
            is_cover = (meta.get("name") or "").lower() == "cover"
            is_cover_prop = (meta.get("property") or "").lower() == "cover-image"
            content = meta.get("content")
            if (is_cover or is_cover_prop) and content:
                cover_item_id = content.strip()
                break

    cover_item: ET.Element | None = None
    items = _children(manifest, "item")
    for item in items:
        if item.get("id") == cover_item_id:
            cover_item = item
            break
    if cover_item is None:
        for item in items:
            if "cover-image" in (item.get("properties") or "").split():
                cover_item = item
                break
    if cover_item is None or not cover_item.get("href"):
        return None

    href = cover_item.get("href")
    assert href is not None
    href = unquote(href).split("#")[0].split("?")[0]
    return posixpath.normpath(posixpath.join(opf_dir, href)).lstrip("/")


class EPUBFormatHandler(FormatHandler):
    """Handler for EPUB digital publications (.epub)."""

    @property
    def format_name(self) -> str:
        return "epub"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".epub":
            return False
        if not path.is_file():
            return False
        try:
            with path.open("rb") as f:
                return f.read(4) == b"PK\x03\x04"
        except OSError:
            return False

    def extract_metadata(self, path: Path) -> BookMetadata:
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            opf_path = _find_opf_path(zf, infos)
            root, _ = _parse_opf(zf, infos, opf_path)

            metadata = next((e for e in root.iter() if _local(e) == "metadata"), None)
            if metadata is None:
                meta_data: dict[str, list[ET.Element]] = {}
            else:
                meta_data = {
                    "titles": _children(metadata, "title"),
                    "creators": _children(metadata, "creator"),
                    "language": _children(metadata, "language"),
                    "publisher": _children(metadata, "publisher"),
                    "dates": _children(metadata, "date"),
                    "identifiers": _children(metadata, "identifier"),
                    "descriptions": _children(metadata, "description"),
                    "metas": _children(metadata, "meta"),
                }

            title, subtitle = _resolve_title_and_subtitle(
                meta_data.get("titles", []), meta_data.get("metas", [])
            )
            authors = _resolve_authors(meta_data.get("creators", []))
            identifiers = _resolve_identifiers(meta_data.get("identifiers", []))
            series, series_index = _resolve_series(meta_data.get("metas", []))

            language = None
            if meta_data.get("language"):
                language = _text(meta_data["language"][0]) or None
            publisher = None
            if meta_data.get("publisher"):
                publisher = _text(meta_data["publisher"][0]) or None
            description = None
            if meta_data.get("descriptions"):
                description = _text(meta_data["descriptions"][0]) or None
            published_date = None
            if meta_data.get("dates"):
                published_date = _normalize_date(_text(meta_data["dates"][0]))

            # Graceful fallback to filename conventions for malformed documents.
            title_from_filename = False
            authors_from_filename = False
            if not title:
                fallback = parse_filename_metadata(path)
                title = fallback.title
                title_from_filename = True
                if not authors:
                    authors = fallback.authors
                    authors_from_filename = True

            # Per-field provenance: OPF-derived fields are "embedded";
            # anything taken from the filename is a weak "filename" fallback.
            sources: dict[str, str] = {}
            if title:
                sources["title"] = SOURCE_FILENAME if title_from_filename else SOURCE_EMBEDDED
            if authors:
                sources["authors"] = SOURCE_FILENAME if authors_from_filename else SOURCE_EMBEDDED
            if subtitle:
                sources["subtitle"] = SOURCE_EMBEDDED
            if description:
                sources["description"] = SOURCE_EMBEDDED
            if publisher:
                sources["publisher"] = SOURCE_EMBEDDED
            if published_date:
                sources["published_date"] = SOURCE_EMBEDDED
            if language:
                sources["language"] = SOURCE_EMBEDDED
            if series or series_index is not None:
                sources["series"] = SOURCE_EMBEDDED
            if identifiers:
                sources["identifiers"] = SOURCE_EMBEDDED

            return BookMetadata(
                title=title,
                subtitle=subtitle,
                authors=authors,
                description=description,
                publisher=publisher,
                published_date=published_date,
                language=language,
                series=series,
                series_index=series_index,
                identifiers=identifiers,
                sources=sources,
            )

    def extract_cover(self, path: Path) -> bytes | None:
        try:
            with zipfile.ZipFile(path) as zf:
                infos = validate_archive(zf)
                opf_path = _find_opf_path(zf, infos)
                root, opf_dir = _parse_opf(zf, infos, opf_path)
                member = _find_cover_item(root, opf_dir)
                if member is None:
                    return None
                info = find_member(infos, member)
                if info is None:
                    return None
                return read_member(zf, info, max_bytes=MAX_COVER_BYTES)
        except (
            ArchiveSafetyError,
            zipfile.BadZipFile,
            KeyError,
            ET.ParseError,
            ValueError,
            OSError,
        ) as exc:
            logger.warning("Failed to extract cover from '%s': %s", path, exc)
            return None

    def get_content(self, path: Path, identifier: str) -> BinaryIO:
        """Return a resource as an in-memory stream.

        The file-like object is fully materialized (bounded by
        ``MAX_RESOURCE_BYTES``) so the underlying archive can be closed without
        leaking file handles.
        """
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            info = find_member(infos, identifier)
            if info is None:
                raise KeyError(f"Resource '{identifier}' not found in EPUB.")
            data = read_member(zf, info, max_bytes=MAX_RESOURCE_BYTES)
        return io.BytesIO(data)


__all__ = ["EPUBFormatHandler"]
