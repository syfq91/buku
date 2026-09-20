"""Server-side EPUB reader service powering the Phase 11 web reader.

Parses the EPUB package (manifest, spine, navigation) and serves spine
chapters with their relative links rewritten to book-local endpoints
(``/reader/{book}/chapter/{n}`` for the reading order, ``/reader/{book}/
resource/...`` for images/CSS/fonts). Everything goes through the safe ZIP
helpers in :mod:`buku.scanner.archive` so the read-only-media and
zip-bomb/traversal invariants of AGENTS.md hold: nothing here ever writes to
disk, and no request can reach a file outside the EPUB container.

A tiny in-process cache (bounded LRU keyed on file identity) avoids re-parsing
the package document on every chapter navigation while staying memory-frugal
for home servers and SBCs.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import posixpath
import re
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from buku.scanner.archive import (
    ArchiveSafetyError,
    find_member,
    read_member,
    validate_archive,
)

logger = logging.getLogger("buku.reader")

# Memory bounds for individual member reads (SBC-friendly caps).
MAX_CHAPTER_BYTES = 32 * 1024 * 1024
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_CONTAINER_BYTES = 64 * 1024
MAX_OPF_BYTES = 2 * 1024 * 1024

_OPS_NS = "http://www.idpf.org/2007/ops"
_DC_NS = "http://purl.org/dc/elements/1.1/"

_CHUNK_CAPACITY = 24  # in-process parsed-structure cache entries

_ATTR_NAMES = frozenset({"href", "src", "xlink:href", "poster"})

_MIME_OVERRIDES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".ncx": "application/x-dtbncx+xml",
    ".svg": "image/svg+xml",
}

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")\s]+)\1\s*\)")


class ReaderError(ValueError):
    """Raised when an EPUB cannot be opened or parsed for reading."""


# --------------------------------------------------------------------------- #
# Package structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReaderChapter:
    """One spine item in the reading order."""

    index: int
    href: str  # archive member path of the content document
    title: str
    media_type: str


@dataclass(frozen=True)
class ReaderTocEntry:
    """A navigation entry, possibly nested."""

    label: str
    href: str | None  # archive member path, when resolvable
    fragment: str | None  # anchor within the target document
    index: int | None  # spine index when the entry targets a chapter
    children: tuple[ReaderTocEntry, ...] = ()


@dataclass(frozen=True)
class ReaderBook:
    """Parsed EPUB package ready for in-browser reading."""

    title: str
    authors: tuple[str, ...]
    language: str | None
    chapters: tuple[ReaderChapter, ...]
    toc: tuple[ReaderTocEntry, ...]
    members: frozenset[str]

    def chapter_index(self, href: str) -> int | None:
        """Return the spine index whose member matches *href*, or None."""
        for chapter in self.chapters:
            if chapter.href == href:
                return chapter.index
        return None


@dataclass(frozen=True)
class _ManifestItem:
    mid: str
    media_type: str
    member: str | None
    properties: frozenset[str]


def _local(elem: ET.Element) -> str:
    """Return the local (non-namespaced) name of an XML element."""
    return elem.tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def _children(elem: ET.Element, local: str) -> list[ET.Element]:
    return [child for child in elem if _local(child) == local]


def _resolve_member(opf_dir: str, href: str) -> str | None:
    """Resolve a manifest/TOC *href* against the OPF directory.

    Returns a normalized archive member path, or None when the reference is
    absolute, traverses above the package, or is otherwise unusable.
    """
    path, _, _ = href.partition("#")
    path = path.split("?", 1)[0]
    if not path:
        return None
    member = posixpath.normpath(posixpath.join(opf_dir, unquote(path)))
    if member in ("", ".", "..") or member.startswith("../") or member.startswith("/"):
        return None
    return member


def _humanized_member(member: str) -> str:
    """Fallback chapter title derived from the content-document filename."""
    stem = posixpath.splitext(posixpath.basename(member))[0]
    return stem.replace("_", " ").replace("-", " ").strip().title() or "Chapter"


def guess_content_type(member: str) -> str:
    """Guess a content type for a resource by its filename."""
    ext = posixpath.splitext(member)[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return mimetypes.guess_type(member)[0] or "application/octet-stream"


def _build_reader(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> ReaderBook:
    """Parse an opened EPUB container into a ReaderBook."""
    container = find_member(infos, "META-INF/container.xml")
    if container is None:
        raise ReaderError("EPUB has no META-INF/container.xml.")
    try:
        container_root = ET.fromstring(read_member(zf, container, max_bytes=MAX_CONTAINER_BYTES))
    except ET.ParseError as exc:
        raise ReaderError("EPUB container.xml is malformed.") from exc

    rootfile_path: str | None = None
    for rootfile in container_root.iter():
        if _local(rootfile) != "rootfile":
            continue
        rootfile_path = rootfile.get("full-path")
        if rootfile_path:
            break
    if not rootfile_path:
        raise ReaderError("EPUB container lists no package document.")

    opf_member = _resolve_member("", rootfile_path)
    if opf_member is None:
        raise ReaderError("EPUB package path is unsafe.")
    opf_info = find_member(infos, opf_member)
    if opf_info is None:
        raise ReaderError(f"EPUB package '{opf_member}' is missing.")
    try:
        opf_root = ET.fromstring(read_member(zf, opf_info, max_bytes=MAX_OPF_BYTES))
    except ET.ParseError as exc:
        raise ReaderError("EPUB package document is malformed.") from exc

    opf_dir = posixpath.dirname(opf_member)

    # ------------------------------------------------------------------ #
    # Metadata (dc:* fields)
    # ------------------------------------------------------------------ #
    title = ""
    authors: list[str] = []
    language: str | None = None
    for elem in opf_root.iter():
        if not elem.tag.startswith(f"{{{_DC_NS}}}"):
            continue
        name = elem.tag.rsplit("}", 1)[-1]
        value = _text(elem)
        if name == "title" and not title:
            title = value
        elif name == "creator":
            authors.append(value)
        elif name == "language" and language is None:
            language = value

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #
    manifest_items: dict[str, _ManifestItem] = {}
    manifest_root = next((e for e in opf_root.iter() if _local(e) == "manifest"), None)
    if manifest_root is not None:
        for manifest_el in _children(manifest_root, "item"):
            mid = manifest_el.get("id")
            href = manifest_el.get("href")
            if not mid or not href:
                continue
            media_type = (manifest_el.get("media-type") or "").strip().lower()
            member = _resolve_member(opf_dir, href)
            props = frozenset((manifest_el.get("properties") or "").split())
            manifest_items[mid] = _ManifestItem(mid, media_type, member, props)

    members = frozenset(
        manifest_item.member
        for manifest_item in manifest_items.values()
        if manifest_item.member is not None and find_member(infos, manifest_item.member) is not None
    )

    # ------------------------------------------------------------------ #
    # Spine (reading order)
    # ------------------------------------------------------------------ #
    spine_root = next((e for e in opf_root.iter() if _local(e) == "spine"), None)
    toc_id: str | None = None
    order: list[_ManifestItem] = []
    if spine_root is not None:
        toc_id = spine_root.get("toc")
        for itemref in _children(spine_root, "itemref"):
            idref = itemref.get("idref")
            spine_item = manifest_items.get(idref) if idref else None
            if spine_item is not None and spine_item.member is not None:
                order.append(spine_item)
    if not order:
        raise ReaderError("EPUB has no reading order (spine).")

    chapter_index_map: dict[str, int] = {}
    for position, spine_item in enumerate(order):
        member = spine_item.member
        if member is not None:
            chapter_index_map.setdefault(member, position)

    spine_chapters: list[ReaderChapter] = []
    for position, spine_item in enumerate(order):
        member = spine_item.member
        if member is None:
            continue
        spine_chapters.append(
            ReaderChapter(
                index=position,
                href=member,
                title="",
                media_type=spine_item.media_type or "application/xhtml+xml",
            )
        )

    # ------------------------------------------------------------------ #
    # Table of contents: EPUB3 nav document, else EPUB2 NCX
    # ------------------------------------------------------------------ #
    def resolve_entry(href: str) -> tuple[str | None, str | None, int | None]:
        member = _resolve_member(opf_dir, href)
        if member is None:
            return None, None, None
        fragment = href.partition("#")[2] or None
        return member, fragment, chapter_index_map.get(member)

    def parse_ol(ol_elem: ET.Element) -> tuple[ReaderTocEntry, ...]:
        def parse_li(li_elem: ET.Element) -> ReaderTocEntry:
            link = next((c for c in li_elem if _local(c) == "a"), None)
            label = _text(link) if link is not None else _text(li_elem)
            href = link.get("href") if link is not None else None
            member, fragment, index = resolve_entry(href) if href else (None, None, None)
            nested_ol = next((c for c in li_elem if _local(c) == "ol"), None)
            children = (
                tuple(parse_li(child) for child in _children(nested_ol, "li"))
                if nested_ol is not None
                else ()
            )
            return ReaderTocEntry(
                label=label, href=member, fragment=fragment, index=index, children=children
            )

        return tuple(parse_li(li) for li in _children(ol_elem, "li"))

    def parse_nav_doc(nav_info: zipfile.ZipInfo) -> tuple[ReaderTocEntry, ...] | None:
        try:
            root = ET.fromstring(read_member(zf, nav_info, max_bytes=MAX_CHAPTER_BYTES))
        except ET.ParseError:
            return None
        for elem in root.iter():
            if _local(elem) != "nav":
                continue
            nav_type = elem.get(f"{{{_OPS_NS}}}type") or elem.get("type") or ""
            if "toc" not in nav_type.split():
                continue
            ol = next((c for c in elem if _local(c) == "ol"), None)
            if ol is not None:
                return parse_ol(ol)
        return None

    def parse_ncx_toc(root: ET.Element) -> tuple[ReaderTocEntry, ...]:
        def parse_navpoint(np_elem: ET.Element) -> ReaderTocEntry:
            label = ""
            for elem in np_elem.iter():
                if _local(elem) == "text":
                    label = "".join(elem.itertext()).strip()
                    if label:
                        break
            content = next((c for c in np_elem.iter() if _local(c) == "content"), None)
            src = content.get("src") if content is not None else None
            member, fragment, index = resolve_entry(src) if src else (None, None, None)
            children = tuple(parse_navpoint(c) for c in _children(np_elem, "navPoint"))
            return ReaderTocEntry(
                label=label, href=member, fragment=fragment, index=index, children=children
            )

        nav_map = next((e for e in root.iter() if _local(e) == "navMap"), None)
        if nav_map is None:
            return ()
        return tuple(parse_navpoint(np) for np in _children(nav_map, "navPoint"))

    toc: tuple[ReaderTocEntry, ...] = ()
    nav_item = next(
        (
            manifest_item
            for manifest_item in manifest_items.values()
            if "nav" in manifest_item.properties and manifest_item.member
        ),
        None,
    )
    if nav_item is not None and nav_item.member is not None and nav_item.member in members:
        nav_info = find_member(infos, nav_item.member)
        if nav_info is not None:
            toc = parse_nav_doc(nav_info) or ()
    if not toc and toc_id:
        ncx_item = manifest_items.get(toc_id)
        if ncx_item is not None and ncx_item.member is not None and ncx_item.member in members:
            ncx_info = find_member(infos, ncx_item.member)
            if ncx_info is not None:
                try:
                    ncx_root = ET.fromstring(read_member(zf, ncx_info, max_bytes=MAX_OPF_BYTES))
                except ET.ParseError:
                    ncx_root = None
                if ncx_root is not None:
                    toc = parse_ncx_toc(ncx_root)

    # ------------------------------------------------------------------ #
    # Chapter titles harvested from TOC labels
    # ------------------------------------------------------------------ #
    titles_by_member: dict[str, str] = {}

    def harvest(entries: tuple[ReaderTocEntry, ...]) -> None:
        for entry in entries:
            if (
                entry.href
                and entry.label
                and entry.href in chapter_index_map
                and entry.href not in titles_by_member
            ):
                titles_by_member[entry.href] = entry.label
            harvest(entry.children)

    harvest(toc)

    chapters = tuple(
        ReaderChapter(
            index=chapter.index,
            href=chapter.href,
            title=titles_by_member.get(chapter.href, _humanized_member(chapter.href)),
            media_type=chapter.media_type,
        )
        for chapter in spine_chapters
    )

    return ReaderBook(
        title=title or "Untitled",
        authors=tuple(authors),
        language=language,
        chapters=chapters,
        toc=toc,
        members=members,
    )


# --------------------------------------------------------------------------- #
# Link rewriting
# --------------------------------------------------------------------------- #
def _chapter_resolver(
    book_id: int,
    reader: ReaderBook,
    chapter: ReaderChapter,
    chapter_index_map: dict[str, int],
) -> Callable[[str], str | None]:
    """Build a URL rewriter for one chapter document."""
    chapter_dir = posixpath.dirname(chapter.href)

    def resolve(raw: str) -> str | None:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            return None  # same-document anchor
        if (
            "://" in raw
            or raw.startswith("//")
            or raw.startswith("data:")
            or raw.startswith("mailto:")
            or raw.startswith("tel:")
        ):
            return None  # external or absolute reference — leave untouched
        path_part = raw.split("?", 1)[0].split("#", 1)[0]
        if raw.startswith("/"):
            target = posixpath.normpath(unquote(path_part).lstrip("/"))
        else:
            target = posixpath.normpath(posixpath.join(chapter_dir, unquote(path_part)))
        if target in ("", ".", "..") or target.startswith("../") or target.startswith("/"):
            return None
        fragment = raw.partition("#")[2] or None
        if target in chapter_index_map:
            url = f"/reader/{book_id}/chapter/{chapter_index_map[target]}"
            if fragment:
                url += f"#{quote(fragment, safe='')}"
            return url
        if target in reader.members:
            return f"/reader/{book_id}/resource/{quote(target, safe='/')}"
        return None

    return resolve


class _LinkRewriter(HTMLParser):
    """Replay an XHTML document, rewriting chapter/resource links."""

    def __init__(self, resolve: Callable[[str], str | None]) -> None:
        super().__init__(convert_charrefs=True)
        self._resolve = resolve
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.out.append(data)

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.out.append(f"<?{data}>")

    def _emit(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        parts = [f"<{tag}"]
        for name, value in attrs:
            if value is None:
                parts.append(f" {name}")
                continue
            if name.lower() in _ATTR_NAMES:
                rewritten = self._resolve(value)
                if rewritten is not None:
                    value = rewritten
            parts.append(f' {name}="{html.escape(value, quote=True)}"')
        parts.append("/>" if self_closing else ">")
        self.out.append("".join(parts))


def _rewrite_chapter_html(
    reader: ReaderBook, chapter: ReaderChapter, raw: bytes, book_id: int
) -> bytes:
    """Rewrite in-chapter links so the document renders inside the reader."""
    index_of = {c.href: c.index for c in reader.chapters}
    resolve = _chapter_resolver(book_id, reader, chapter, index_of)
    rewriter = _LinkRewriter(resolve)
    rewriter.feed(raw.decode("utf-8", "replace"))
    return "".join(rewriter.out).encode("utf-8")


def _rewrite_css(book_id: int, reader: ReaderBook, css_member: str, data: bytes) -> bytes:
    """Rewrite relative ``url()`` references inside a CSS resource."""
    css_dir = posixpath.dirname(css_member)

    def repl(match: re.Match[str]) -> str:
        quote_char, ref = match.group(1), match.group(2)
        if (
            not ref
            or ref.startswith("#")
            or ref.startswith("//")
            or "://" in ref
            or ref.startswith("data:")
        ):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(css_dir, unquote(ref.split("?", 1)[0])))
        if resolved in reader.members:
            return (
                f"url({quote_char}/reader/{book_id}/resource/"
                f"{quote(resolved, safe='/')}{quote_char})"
            )
        return match.group(0)

    decoded = data.decode("utf-8", "replace")
    return _CSS_URL_RE.sub(repl, decoded).encode("utf-8")


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class ReaderService:
    """Parse EPUB packages and serve chapters/resources for the web reader."""

    def __init__(self, capacity: int = _CHUNK_CAPACITY) -> None:
        self._cache: OrderedDict[tuple[str, int, int], ReaderBook] = OrderedDict()
        self._capacity = capacity

    def get_reader(self, path: Path) -> ReaderBook:
        """Return the parsed structure for *path*, cached by file identity."""
        try:
            stat = path.stat()
        except OSError as exc:
            raise ReaderError(f"Cannot stat EPUB file: {exc}") from exc
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        reader = self._parse(path)
        self._cache[key] = reader
        self._cache.move_to_end(key)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return reader

    def _parse(self, path: Path) -> ReaderBook:
        try:
            with zipfile.ZipFile(path) as zf:
                infos = validate_archive(zf)
                return _build_reader(zf, infos)
        except (ArchiveSafetyError, zipfile.BadZipFile, OSError) as exc:
            raise ReaderError(f"EPUB {path.name} could not be opened: {exc}") from exc

    def chapter_document(
        self, path: Path, reader: ReaderBook, book_id: int, index: int
    ) -> tuple[bytes, str]:
        """Return (rewritten XHTML, content type) for one spine chapter."""
        chapter = reader.chapters[index]
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            info = find_member(infos, chapter.href)
            if info is None:
                raise ReaderError(f"Spine member missing: {chapter.href}")
            raw = read_member(zf, info, max_bytes=MAX_CHAPTER_BYTES)
        return _rewrite_chapter_html(reader, chapter, raw, book_id), "text/html; charset=utf-8"

    def resource(self, path: Path, reader: ReaderBook, member: str) -> bytes:
        """Return the raw bytes of a verified archive member."""
        if member not in reader.members:
            raise ReaderError(f"Resource rejected: {member}")
        with zipfile.ZipFile(path) as zf:
            infos = validate_archive(zf)
            info = find_member(infos, member)
            if info is None:
                raise ReaderError(f"Resource missing: {member}")
            return read_member(zf, info, max_bytes=MAX_RESOURCE_BYTES)

    def css_bytes(self, path: Path, reader: ReaderBook, book_id: int, member: str) -> bytes:
        """Return resource bytes with relative ``url()`` references rewritten."""
        return _rewrite_css(book_id, reader, member, self.resource(path, reader, member))


reader_service = ReaderService()
