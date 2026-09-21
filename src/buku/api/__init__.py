"""API routing and transport package for buku."""

from buku.api.admin_metadata import router as admin_metadata_router
from buku.api.auth import router as auth_router
from buku.api.opds import router as opds_router
from buku.api.progress import router as progress_router
from buku.api.search import router as search_router

__all__ = [
    "admin_metadata_router",
    "auth_router",
    "opds_router",
    "progress_router",
    "search_router",
]
