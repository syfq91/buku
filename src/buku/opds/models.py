"""Immutable OPDS presentation models (Phases 12-13).

These dataclasses are the **publication** layer of the OPDS surface: they
describe what an OPDS Catalog Feed Document / Entry Document / Progression
Document looks like, independently of how books are stored in the SQLAlchemy
domain. The serializer turns them into wire formats (Atom/XML for the catalog,
JSON for Progression 1.0) and the OPDS service maps domain rows onto them.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

# --------------------------------------------------------------------------- #
# XML namespaces used across feeds
# --------------------------------------------------------------------------- #
ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
THR_NS = "http://purl.org/syndication/thread/1.0"

# --------------------------------------------------------------------------- #
# Feed kinds & media types (OPDS 1.2 section 2)
# --------------------------------------------------------------------------- #
KIND_NAVIGATION = "navigation"
KIND_ACQUISITION = "acquisition"

ATOM_FEED_TYPE = "application/atom+xml"
NAVIGATION_FEED_TYPE = f"{ATOM_FEED_TYPE};profile=opds-catalog;kind={KIND_NAVIGATION}"
ACQUISITION_FEED_TYPE = f"{ATOM_FEED_TYPE};profile=opds-catalog;kind={KIND_ACQUISITION}"
ENTRY_TYPE = "application/atom+xml;type=entry;profile=opds-catalog"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"
PROGRESSION_TYPE = "application/opds-progression+json"

# --------------------------------------------------------------------------- #
# Link relations
# --------------------------------------------------------------------------- #
REL_ACQUISITION = "http://opds-spec.org/acquisition"
REL_ACQUISITION_OPEN_ACCESS = f"{REL_ACQUISITION}/open-access"
REL_IMAGE = "http://opds-spec.org/image"
REL_IMAGE_THUMBNAIL = f"{REL_IMAGE}/thumbnail"
REL_PROGRESSION = "http://opds-spec.org/progression"
REL_SUBSECTION = "subsection"
REL_START = "start"
REL_SELF = "self"
REL_UP = "up"
REL_SEARCH = "search"

# Media types for the formats buku can serve (both acquisition links and the
# matched download responses must advertise the same type).
FORMAT_MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "cbz": "application/vnd.comicbook+zip",
}

# Scheme used to classify series membership in a catalog entry.
SERIES_CATEGORY_SCHEME = "http://opds-spec.org/category/series"

OPDS_PAGE_SIZE = 25


def format_media_type(file_format: str) -> str | None:
    """Map a stored ``BookFile.file_format`` to its OPDS media type."""
    return FORMAT_MEDIA_TYPES.get(file_format.lower())


def cover_media_type(cover_path: str) -> str:
    """Guess the image media type of a cached cover by its filename."""
    mime, _ = mimetypes.guess_type(Path(cover_path).name)
    return mime or "application/octet-stream"


def feed_id(base_url: str, path: str) -> str:
    """Deterministic ``urn:uuid`` identifier for a feed document."""
    return f"urn:uuid:{uuid5(NAMESPACE_URL, base_url + path)}"


def entity_id(kind: str, entity_ident: int) -> str:
    """Deterministic ``urn:uuid`` identifier for a catalog entry."""
    return f"urn:uuid:{uuid5(NAMESPACE_URL, f'buku://{kind}/{entity_ident}')}"


@dataclass(frozen=True)
class OpdsLink:
    """An ``atom:link`` element (may carry an optional ``thr:count``)."""

    rel: str
    href: str
    type: str
    title: str | None = None
    count: int | None = None


@dataclass(frozen=True)
class OpdsAuthor:
    """The ``atom:author`` name of a publication creator."""

    name: str


@dataclass(frozen=True)
class OpdsEntry:
    """An OPDS Catalog Entry: publication metadata plus acquisition links."""

    id: str
    title: str
    updated: datetime
    links: list[OpdsLink] = field(default_factory=list)
    authors: list[OpdsAuthor] = field(default_factory=list)
    language: str | None = None
    issued: str | None = None
    publisher: str | None = None
    summary: str | None = None
    content: str | None = None
    series: str | None = None
    series_index: float | None = None
    identifiers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpdsFeed:
    """An OPDS Catalog Feed Document (navigation or acquisition)."""

    id: str
    title: str
    updated: datetime
    kind: str  # KIND_NAVIGATION | KIND_ACQUISITION
    links: list[OpdsLink] = field(default_factory=list)
    entries: list[OpdsEntry] = field(default_factory=list)
    total_results: int | None = None
    items_per_page: int | None = None


__all__ = [
    "ACQUISITION_FEED_TYPE",
    "ATOM_FEED_TYPE",
    "ATOM_NS",
    "DC_NS",
    "ENTRY_TYPE",
    "FORMAT_MEDIA_TYPES",
    "KIND_ACQUISITION",
    "KIND_NAVIGATION",
    "NAVIGATION_FEED_TYPE",
    "OPDS_NS",
    "OPDS_PAGE_SIZE",
    "OPENSEARCH_NS",
    "OPENSEARCH_TYPE",
    "PROGRESSION_TYPE",
    "REL_ACQUISITION",
    "REL_ACQUISITION_OPEN_ACCESS",
    "REL_IMAGE",
    "REL_IMAGE_THUMBNAIL",
    "REL_PROGRESSION",
    "REL_SEARCH",
    "REL_START",
    "REL_SUBSECTION",
    "REL_UP",
    "SERIES_CATEGORY_SCHEME",
    "THR_NS",
    "OpdsAuthor",
    "OpdsEntry",
    "OpdsFeed",
    "OpdsLink",
    "cover_media_type",
    "entity_id",
    "feed_id",
    "format_media_type",
]
