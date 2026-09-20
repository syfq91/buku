"""Full-text search HTTP transport routes (Phase 8).

Thin transport layer: validates query inputs, delegates to
:class:`buku.services.search.SearchService`, and serializes results.
``GET /search`` renders a lightweight, dependency-free placeholder page that
exercises the API; the themed Jinja2/HTMX web UI replaces it in Phase 9.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
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


@router.get("/search", response_class=HTMLResponse, include_in_schema=False)
def search_page(
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    """Serve the search UI (placeholder until the Phase 9 web interface)."""
    return HTMLResponse(_SEARCH_PAGE_HTML)


_SEARCH_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Search &middot; buku</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 720px;
         margin: 2rem auto; padding: 0 1rem; }
  input { width: 100%; padding: .6rem .8rem; font-size: 1rem;
          border: 1px solid #888; border-radius: .4rem; }
  ul { list-style: none; padding: 0; }
  li { border-bottom: 1px solid #ddd; padding: .8rem 0; }
  .muted { color: #666; }
  .meta { font-size: .85rem; }
</style>
</head>
<body>
<h1>Search</h1>
<form id="search-form" method="get" action="/search">
  <input id="q" name="q" type="search" placeholder="Search title, author, ISBN&hellip;"
         autofocus autocomplete="off" aria-label="Search query" />
</form>
<p class="muted" id="summary"></p>
<ul id="results"></ul>
<p class="muted">Rendered by &lt;GET /api/v1/search&gt; (SQLite FTS5).
The themed interface ships with the Phase 9 web UI.</p>
<script>
  const qInput = document.getElementById('q');
  const resultsList = document.getElementById('results');
  const summary = document.getElementById('summary');

  function render(items) {
    resultsList.replaceChildren();
    for (const item of items) {
      const li = document.createElement('li');
      const title = document.createElement('strong');
      title.textContent = item.title;
      li.appendChild(title);
      if (item.subtitle) {
        const subtitle = document.createTextNode(' \u2014 ' + item.subtitle);
        li.appendChild(subtitle);
      }
      const meta = document.createElement('div');
      meta.className = 'meta muted';
      meta.textContent = [
        item.authors.join(', '),
        item.series,
        item.publisher,
        item.published_date,
        item.language,
      ].filter(Boolean).join(' \u00b7 ');
      li.appendChild(meta);
      resultsList.appendChild(li);
    }
  }

  async function search(q) {
    const url = '/api/v1/search?q=' + encodeURIComponent(q) + '&limit=25';
    const response = await fetch(url);
    if (!response.ok) { summary.textContent = 'Search failed (' + response.status + ').'; return; }
    const data = await response.json();
    summary.textContent = data.total + ' result' + (data.total === 1 ? '' : 's');
    render(data.items);
  }

  qInput.addEventListener('input', () => {
    const q = qInput.value.trim();
    if (!q) { resultsList.replaceChildren(); summary.textContent = ''; return; }
    search(q);
  });
  qInput.focus();
</script>
</body>
</html>
"""
