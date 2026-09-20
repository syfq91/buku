"""Matching priority flow and confidence scoring (Phase 6).

Implements the plan's matching hierarchy:

    ISBN → ISBN + other identifiers → Title + Author → Title

with conservative confidence thresholds. The plan warns against blindly applying
fuzzy matches, so only high-confidence candidates are auto-applied by default:
- an ISBN query whose candidate confirms the exact ISBN scores 0.95
- an exact title + author overlap scores 0.85
- a bare exact-title match scores 0.60, below the default 0.70 threshold
  (available for manual review, never auto-applied)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from buku.metadata.models import BookMetadata, MetadataQuery

if TYPE_CHECKING:
    from buku.models.book import Book

DEFAULT_MIN_CONFIDENCE = 0.70

# Confidence ceilings per matching tier.
CONFIDENCE_ISBN_CONFIRMED = 0.95
CONFIDENCE_TITLE_AUTHOR = 0.85
CONFIDENCE_EXACT_TITLE = 0.60

_NON_ISBN_RE = re.compile(r"[^0-9xX]")


def normalize_isbn(value: str | None) -> str | None:
    """Return an ISBN with all non-digit characters (and case) removed."""
    if not value:
        return None
    return _NON_ISBN_RE.sub("", value).lower() or None


def build_query(book: Book) -> MetadataQuery:
    """Construct a :class:`MetadataQuery` from an ORM ``Book``.

    The identifiers dict prefers the normalized ``isbn`` key (ISBN-13 preferred),
    matching what the scanner stores from embedded metadata.
    """
    identifiers = {
        identifier.identifier_type: identifier.identifier_value for identifier in book.identifiers
    }
    authors = [link.author.name for link in book.author_links if link.author.name]
    return MetadataQuery(
        title=book.title or None,
        authors=authors,
        identifiers=identifiers,
    )


def score_match(query: MetadataQuery, candidate: BookMetadata) -> float:
    """Score a candidate's parsed metadata against the query on ``[0.0, 1.0]``.

    High scores require strong, specific evidence:
    - A queried ISBN confirmed in the candidate's identifiers is near-certain.
    - An ISBN query whose candidate carries a *different* ISBN is rejected.
    - Title + author exact matches are strong; bare title matches are weak.
    """
    queried_isbn = normalize_isbn(query.primary_isbn)
    candidate_isbns = {
        normalized
        for key, value in candidate.identifiers.items()
        if "isbn" in key.lower()
        if (normalized := normalize_isbn(value)) is not None
    }

    query_title = (query.title or "").strip().lower()
    candidate_title = (candidate.title or "").strip().lower()
    query_authors = {a.strip().lower() for a in query.authors if a and a.strip()}
    candidate_authors = {a.strip().lower() for a in candidate.authors if a and a.strip()}

    if queried_isbn:
        if queried_isbn in candidate_isbns:
            return CONFIDENCE_ISBN_CONFIRMED
        if candidate_isbns:
            # A conflicting ISBN means a different edition/book.
            return 0.05
        # The provider did not echo the ISBN; fall back to a title heuristic.
        if query_title and candidate_title and query_title == candidate_title:
            return 0.50
        return 0.20

    if query_title and query_authors:
        if query_title == candidate_title:
            if query_authors & candidate_authors:
                return CONFIDENCE_TITLE_AUTHOR
            return 0.70
        return 0.30

    if query_title:
        if query_title == candidate_title:
            return CONFIDENCE_EXACT_TITLE
        query_tokens = set(query_title.split())
        candidate_tokens = set(candidate_title.split())
        if query_tokens and candidate_tokens:
            overlap = len(query_tokens & candidate_tokens) / max(
                len(query_tokens), len(candidate_tokens)
            )
            if overlap >= 0.8:
                return 0.50
        return 0.25

    return 0.0


__all__ = [
    "CONFIDENCE_ISBN_CONFIRMED",
    "CONFIDENCE_EXACT_TITLE",
    "CONFIDENCE_TITLE_AUTHOR",
    "DEFAULT_MIN_CONFIDENCE",
    "BookMetadata",
    "MetadataQuery",
    "build_query",
    "normalize_isbn",
    "score_match",
]
