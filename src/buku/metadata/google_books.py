"""Google Books API metadata provider (Phase 6).

Uses only the Python standard library (``urllib``) to keep the runtime
dependency footprint lean. The HTTP transport is injectable so tests can
exercise parsing and matching without network access.

Response parsing maps ``volumeInfo`` onto :class:`BookMetadata` and derives a
confidence score via :func:`buku.metadata.matching.score_match`. Network or
parse failures degrade gracefully to an empty candidate list.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, cast

from buku.metadata.matching import score_match
from buku.metadata.models import (
    SOURCE_GOOGLE_BOOKS,
    BookMetadata,
    MetadataMatch,
    MetadataQuery,
    populated_fields,
)
from buku.metadata.provider import MetadataProvider

logger = logging.getLogger("buku.metadata.google_books")

SEARCH_URL = "https://www.googleapis.com/books/v1/volumes"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 10
DEFAULT_TIMEOUT = 10.0

HttpGet = Callable[[str, float], bytes]


def _default_http_get(url: str, timeout: float) -> bytes:
    """Fetch a URL with a bounded body size and a sane user agent."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "buku/0.1 (self-hosted book server)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length_header = response.headers.get("Content-Length")
        if length_header is not None:
            try:
                if int(length_header) > MAX_RESPONSE_BYTES:
                    raise ValueError("Google Books response exceeds size limit")
            except ValueError as exc:
                raise ValueError(f"Invalid Content-Length header: {exc}") from exc
        data = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Google Books response exceeds size limit")
    return data


def google_books_query_strings(query: MetadataQuery, max_results: int = MAX_RESULTS) -> list[str]:
    """Build provider ``q`` strings following the Phase 6 priority flow.

    ISBN first, then ISBN+title, then title+author, then bare title. Each tier
    is only reached if the previous one returned no candidates.
    """
    result_description: list[str] = []
    isbn = query.primary_isbn
    title = (query.title or "").strip()
    author = (query.authors[0] if query.authors else "").strip()

    if isbn:
        result_description.append(f"isbn:{isbn}")
    if isbn and title:
        result_description.append(f'isbn:{isbn} intitle:"{title.replace(chr(34), "")}"')
    if title and author:
        result_description.append(
            f'intitle:"{title.replace(chr(34), "")}" inauthor:"{author.replace(chr(34), "")}"'
        )
    if title:
        result_description.append(f'intitle:"{title.replace(chr(34), "")}"')
    return result_description


class GoogleBooksProvider(MetadataProvider):
    """Enrichment provider backed by the Google Books Volumes API."""

    name = "google_books"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_get: HttpGet | None = None,
        max_results: int = MAX_RESULTS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self._http_get = http_get or _default_http_get
        self.max_results = max(max_results, 1)
        self.timeout = timeout

    def _build_url(self, q: str) -> str:
        params: dict[str, str | int] = {
            "q": q,
            "maxResults": self.max_results,
            "country": "US",
        }
        if self.api_key:
            params["key"] = self.api_key
        return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"

    def search(self, query: MetadataQuery) -> list[MetadataMatch]:
        """Run the priority query tiers and return scored candidates.

        Stops at the first tier that yields any items (per the matching
        priority flow). Candidates are returned sorted best-first.
        """
        matches: list[MetadataMatch] = []
        if not query.has_content:
            return matches

        for q in google_books_query_strings(query, self.max_results):
            try:
                payload = self._http_get(self._build_url(q), self.timeout)
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
                logger.warning("Google Books request failed (q=%r): %s", q, exc)
                continue
            try:
                data = json.loads(payload)
            except (ValueError, TypeError) as exc:
                logger.warning("Google Books response not JSON (q=%r): %s", q, exc)
                continue

            items = data.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                parsed = self._parse_item(item)
                if parsed is None:
                    continue
                confidence = score_match(query, parsed)
                matches.append(
                    MetadataMatch(
                        provider=self.name,
                        external_id=str(item.get("id") or ""),
                        confidence=confidence,
                        metadata=parsed,
                        raw=item,
                    )
                )
            if matches:
                break

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> BookMetadata | None:
        """Convert a Google Books volume item into normalized metadata."""
        volume_info = item.get("volumeInfo")
        if not isinstance(volume_info, dict):
            return None
        title = (volume_info.get("title") or "").strip()
        if not title:
            return None

        authors = [
            a for a in (volume_info.get("authors") or []) if isinstance(a, str) and a.strip()
        ]

        identifiers: dict[str, str] = {}
        for identifier in volume_info.get("industryIdentifiers") or []:
            if not isinstance(identifier, dict):
                continue
            identifier_type = (identifier.get("type") or "").lower()
            identifier_value = (identifier.get("identifier") or "").strip()
            if not identifier_value:
                continue
            if identifier_type == "isbn_13":
                identifiers.setdefault("isbn", identifier_value)
            elif identifier_type == "isbn_10" and "isbn" not in identifiers:
                identifiers.setdefault("isbn", identifier_value)
                identifiers.setdefault("isbn_10", identifier_value)
            elif identifier_type == "isbn_10":
                identifiers.setdefault("isbn_10", identifier_value)

        metadata = BookMetadata(
            title=title,
            subtitle=_optional_str(volume_info.get("subtitle")),
            authors=authors,
            description=_optional_str(volume_info.get("description")),
            publisher=_optional_str(volume_info.get("publisher")),
            published_date=_optional_str(volume_info.get("publishedDate")),
            language=_optional_str(volume_info.get("language")),
            page_count=volume_info.get("pageCount"),
            identifiers=identifiers,
        )
        metadata.sources = {field: SOURCE_GOOGLE_BOOKS for field in populated_fields(metadata)}
        return metadata


def _optional_str(value: Any) -> str | None:
    """Coerce a possibly-empty field to ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "DEFAULT_TIMEOUT",
    "GoogleBooksProvider",
    "MAX_RESULTS",
    "MAX_RESPONSE_BYTES",
    "SEARCH_URL",
    "HttpGet",
    "google_books_query_strings",
]
