"""OPDS 1.2 catalog feeds and OPDS Progression 1.0 documents (Phases 12-13).

This package serializes the internal buku catalog into the public OPDS
surface consumed by external e-readers (Kobo, KOReader, Moon+ Reader, etc.).
It is split into four concerns:

- :mod:`buku.opds.models` — immutable, framework-agnostic OPDS presentation
  models (feeds, entries, links, Progression documents) decoupled from the
  SQLAlchemy domain layer,
- :mod:`buku.opds.serializer` — Atom/XML feed and OpenSearch serialization,
- :mod:`buku.opds.service` — query mapping from domain models to OPDS models,
- :mod:`buku.opds.progression` — OPDS Progression 1.0 document adapter
  (parse, validate, serialize, RFC 7807 problem details),
- :mod:`buku.opds.auth` — HTTP Basic / Bearer dependency for OPDS endpoints.
"""

from buku.opds.models import OPDS_PAGE_SIZE
from buku.opds.service import opds_service

__all__ = ["OPDS_PAGE_SIZE", "opds_service"]
