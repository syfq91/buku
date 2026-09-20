"""Metadata provider abstraction and default provider construction."""

from __future__ import annotations

from abc import ABC, abstractmethod

from buku.metadata.models import MetadataMatch, MetadataQuery


class MetadataProvider(ABC):
    """Abstract contract for external metadata enrichment providers.

    A provider translates a :class:`MetadataQuery` into provider-specific search
    terms, calls its external API, parses the response into normalized
    :class:`BookMetadata` values, and returns scored candidates. Providers must
    fail gracefully (return empty lists rather than raising) on network or API
    errors so enrichment jobs degrade without crashing the scanner.
    """

    name: str = "abstract"

    @abstractmethod
    def search(self, query: MetadataQuery) -> list[MetadataMatch]:
        """Return candidate matches for the given query, best first.

        Candidates must carry a ``confidence`` in ``[0.0, 1.0]`` computed by the
        shared scoring logic in :mod:`buku.metadata.matching`.
        """
        ...


def get_default_providers() -> list[MetadataProvider]:
    """Build the default provider chain from the active application settings."""
    from buku.config import get_settings
    from buku.metadata.google_books import GoogleBooksProvider

    settings = get_settings()
    return [
        GoogleBooksProvider(
            api_key=settings.google_books_api_key,
        )
    ]


__all__ = ["MetadataProvider", "get_default_providers"]
