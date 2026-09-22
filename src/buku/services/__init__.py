"""Domain services for buku."""

from buku.services.admin import AdminService, admin_service
from buku.services.auth import AuthenticationService, auth_service
from buku.services.authorization import AuthorizationService, Role, authorization_service
from buku.services.cache import CacheService, cache_service
from buku.services.catalog import CatalogService, catalog_service
from buku.services.metadata_review import MetadataReviewService, review_service
from buku.services.progression import (
    ProgressionService,
    ProgressionStatus,
    progression_service,
)
from buku.services.reader import (
    ReaderBook,
    ReaderChapter,
    ReaderError,
    ReaderService,
    ReaderTocEntry,
    reader_service,
)
from buku.services.representation import RepresentationService, representation_service
from buku.services.search import SearchService, search_service

__all__ = [
    "AdminService",
    "AuthenticationService",
    "AuthorizationService",
    "CacheService",
    "CatalogService",
    "MetadataReviewService",
    "ProgressionService",
    "ProgressionStatus",
    "ReaderBook",
    "ReaderChapter",
    "ReaderError",
    "ReaderService",
    "ReaderTocEntry",
    "RepresentationService",
    "Role",
    "SearchService",
    "admin_service",
    "auth_service",
    "authorization_service",
    "cache_service",
    "catalog_service",
    "progression_service",
    "reader_service",
    "representation_service",
    "review_service",
    "search_service",
]
