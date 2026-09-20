"""Metadata providers, matching, provenance, and enrichment (Phase 6).

Public surface for the metadata architecture:

- :class:`~buku.metadata.provider.MetadataProvider` — provider abstraction
- :class:`~buku.metadata.google_books.GoogleBooksProvider` — Google Books API
- :class:`~buku.metadata.matching` — matching priority flow & confidence scores
- :class:`~buku.metadata.provenance.ProvenanceService` — provenance persistence
- :class:`~buku.metadata.enrichment.MetadataEnrichmentService` — enrichment jobs
"""

from buku.metadata.enrichment import (
    EnrichmentResult,
    EnrichmentRunStats,
    MetadataEnrichmentService,
    process_metadata_jobs,
)
from buku.metadata.google_books import GoogleBooksProvider
from buku.metadata.matching import (
    DEFAULT_MIN_CONFIDENCE,
    build_query,
    normalize_isbn,
    score_match,
)
from buku.metadata.models import (
    SOURCE_EMBEDDED,
    SOURCE_FILENAME,
    SOURCE_GOOGLE_BOOKS,
    SOURCE_USER,
    BookMetadata,
    MetadataMatch,
    MetadataQuery,
    populated_fields,
)
from buku.metadata.provenance import ProvenanceService, provenance_service
from buku.metadata.provider import MetadataProvider, get_default_providers

__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "SOURCE_EMBEDDED",
    "SOURCE_FILENAME",
    "SOURCE_GOOGLE_BOOKS",
    "SOURCE_USER",
    "BookMetadata",
    "EnrichmentResult",
    "EnrichmentRunStats",
    "GoogleBooksProvider",
    "MetadataEnrichmentService",
    "MetadataMatch",
    "MetadataProvider",
    "MetadataQuery",
    "ProvenanceService",
    "build_query",
    "get_default_providers",
    "normalize_isbn",
    "populated_fields",
    "process_metadata_jobs",
    "provenance_service",
    "score_match",
]
