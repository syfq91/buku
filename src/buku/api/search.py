"""Full-text search HTTP transport routes (Phase 8).

Thin transport layer: validates query inputs, delegates to
:class:`buku.services.search.SearchService`, and serializes results. The
``GET /search`` browser page is served by the Phase 9 web UI in
:mod:`buku.web.views` (this module exposes only the JSON API).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import get_current_user
from buku.db import get_db
from buku.models.user import User
from buku.services.search import search_service

router = APIRouter(tags=["Search"])


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #
class SearchItemResponse(BaseModel):
    """A single catalog entry surfaced by a search query."""

    book_id: int
    title: str
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    series: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    cover_path: str | None = None
    rank: float | None = None


class SearchResponse(BaseModel):
    """Paged full-text search results."""

    query: str
    total: int
    limit: int
    offset: int
    items: list[SearchItemResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/api/v1/search", response_model=SearchResponse)
def search_books(
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
    q: Annotated[str, Query(description="Full-text search query.")] = "",
    limit: Annotated[int, Query(ge=1, le=100, description="Max results per page.")] = 25,
    offset: Annotated[int, Query(ge=0, description="Pagination offset.")] = 0,
) -> SearchResponse:
    """Search the catalog with SQLite FTS5 (no external search engine)."""
    results = search_service.search(db, q, limit=limit, offset=offset)
    return SearchResponse(
        query=results.query,
        total=results.total,
        limit=results.limit,
        offset=results.offset,
        items=[SearchItemResponse(**item.__dict__) for item in results.items],
    )
