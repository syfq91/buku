"""Shared metadata data structures (Phase 6: Metadata Architecture).

These are pure Python dataclasses used across the *metadata provider pipeline*:
- :class:`BookMetadata` — the normalized metadata shape extracted by format
  handlers and produced by enrichment providers. It also carries per-field
  provenance so automated jobs never clobber user edits.
- :class:`MetadataQuery` — what we ask a provider to look up, built in the
  priority order ISBN → ISBN+identifiers → Title+Author → Title.
- :class:`MetadataMatch` — a single provider candidate (external id, confidence,
  parsed metadata, and the raw payload for later review).

.. note::
   The ORM counterpart :mod:`buku.models.metadata` (``metadata_sources``,
   ``metadata_matches``, ``metadata_provenance``) persists provenance in the
   database. This module holds the in-memory structures only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Provenance source names (must match rows in the metadata_sources table).
# --------------------------------------------------------------------------- #
SOURCE_EMBEDDED = "embedded"
SOURCE_GOOGLE_BOOKS = "google_books"
SOURCE_USER = "user"
SOURCE_FILENAME = "filename"

SOURCE_DESCRIPTIONS: dict[str, str] = {
    SOURCE_EMBEDDED: "Metadata extracted from the file itself",
    SOURCE_GOOGLE_BOOKS: "Metadata enriched from the Google Books API",
    SOURCE_USER: "Metadata manually edited by a user",
    SOURCE_FILENAME: "Metadata inferred from the file name",
}

# Fields tracked for provenance. ``authors`` and ``identifiers`` are logical
# fields persisted in their own tables rather than as Book columns.
FIELD_TITLE = "title"
FIELD_SUBTITLE = "subtitle"
FIELD_AUTHORS = "authors"
FIELD_SERIES = "series"
FIELD_DESCRIPTION = "description"
FIELD_PUBLISHER = "publisher"
FIELD_PUBLISHED_DATE = "published_date"
FIELD_LANGUAGE = "language"
FIELD_IDENTIFIERS = "identifiers"

ALL_FIELDS = frozenset(
    {
        FIELD_TITLE,
        FIELD_SUBTITLE,
        FIELD_AUTHORS,
        FIELD_SERIES,
        FIELD_DESCRIPTION,
        FIELD_PUBLISHER,
        FIELD_PUBLISHED_DATE,
        FIELD_LANGUAGE,
        FIELD_IDENTIFIERS,
    }
)


@dataclass
class BookMetadata:
    """Normalized metadata for a logical book.

    ``sources`` maps each *populated* field name to its provenance source
    (e.g. ``{"title": "embedded", "authors": "embedded"}``). Fields that could
    not be extracted are simply absent.
    """

    title: str
    subtitle: str | None = None
    authors: list[str] = field(default_factory=list)
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    page_count: int | None = None
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class MetadataQuery:
    """The lookup request handed to a :class:`MetadataProvider`."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        """True when there is anything meaningful to search for."""
        return bool((self.title or "").strip() or self.identifiers)

    @property
    def primary_isbn(self) -> str | None:
        """Return the preferred ISBN identifier, if any."""
        return self.identifiers.get("isbn")


@dataclass
class MetadataMatch:
    """A provider candidate for a logical book."""

    provider: str
    external_id: str
    confidence: float
    metadata: BookMetadata
    raw: dict[str, Any] = field(default_factory=dict)


def populated_fields(meta: BookMetadata) -> set[str]:
    """Return the set of field names that carry a real value in ``meta``."""
    fields: set[str] = set()
    if meta.title and meta.title.strip():
        fields.add(FIELD_TITLE)
    if meta.subtitle and meta.subtitle.strip():
        fields.add(FIELD_SUBTITLE)
    if meta.authors:
        fields.add(FIELD_AUTHORS)
    if meta.series or meta.series_index is not None:
        fields.add(FIELD_SERIES)
    if meta.description and meta.description.strip():
        fields.add(FIELD_DESCRIPTION)
    if meta.publisher and meta.publisher.strip():
        fields.add(FIELD_PUBLISHER)
    if meta.published_date and meta.published_date.strip():
        fields.add(FIELD_PUBLISHED_DATE)
    if meta.language and meta.language.strip():
        fields.add(FIELD_LANGUAGE)
    if meta.identifiers:
        fields.add(FIELD_IDENTIFIERS)
    return fields


__all__ = [
    "ALL_FIELDS",
    "FIELD_AUTHORS",
    "FIELD_DESCRIPTION",
    "FIELD_IDENTIFIERS",
    "FIELD_LANGUAGE",
    "FIELD_PUBLISHED_DATE",
    "FIELD_PUBLISHER",
    "FIELD_SERIES",
    "FIELD_SUBTITLE",
    "FIELD_TITLE",
    "SOURCE_DESCRIPTIONS",
    "SOURCE_EMBEDDED",
    "SOURCE_FILENAME",
    "SOURCE_GOOGLE_BOOKS",
    "SOURCE_USER",
    "BookMetadata",
    "MetadataMatch",
    "MetadataQuery",
    "populated_fields",
]
