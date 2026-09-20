"""Phase 9 web views: Jinja2 + HTMX browser interface (thin transport layer).

Every handler here is pure HTTP transport — auth checks, path parsing, and
template rendering. All data access delegates to the domain services
(:mod:`buku.services.catalog`, :mod:`buku.services.admin`,
:mod:`buku.services.metadata_review`); no business logic lives in these
functions and nothing here ever writes to the media directories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import extract_session_token
from buku.db import get_db
from buku.models.user import User
from buku.services.admin import admin_service
from buku.services.auth import auth_service
from buku.services.authorization import authorization_service
from buku.services.catalog import catalog_service
from buku.services.metadata_review import review_service
from buku.services.search import search_service
from buku.web.templates import templates

router = APIRouter(tags=["Web UI"])

_BOOKS_PER_PAGE = 24
_REVIEW_PER_PAGE = 20


# --------------------------------------------------------------------------- #
# Auth dependencies (redirect for pages instead of a JSON 401)
# --------------------------------------------------------------------------- #
def _login_target(request: Request) -> str:
    """Build a safe ``/login`` redirect target carrying the current path."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return f"/login?next={quote(target, safe='')}"


def _safe_next(request: Request) -> str:
    """Read the ``next`` query param, rejecting open-redirect payloads."""
    target = request.query_params.get("next") or "/"
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def page_user(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
) -> User:
    """Require an active session; otherwise bounce to the login page."""
    token = extract_session_token(request)
    user = auth_service.validate_session(db, token) if token else None
    if user is None or not user.is_active:
        raise HTTPException(status_code=303, headers={"Location": _login_target(request)})
    request.state.user = user
    return user


def page_admin(user: Annotated[User, Depends(page_user)]) -> User:
    """Require an admin session for console pages."""
    try:
        authorization_service.require_admin(user)
    except PermissionError:
        raise HTTPException(status_code=303, headers={"Location": "/"}) from None
    return user


def _render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Response:
    """Render a template, always injecting the signed-in user into context."""
    ctx: dict[str, Any] = {
        "current_user": getattr(request.state, "user", None),
        "active": "dashboard",
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, **kwargs)


def _not_found(request: Request) -> Response:
    """Render the styled 404 page."""
    return _render(request, "not_found.html", status_code=404)


# --------------------------------------------------------------------------- #
# Landing & session
# --------------------------------------------------------------------------- #
@router.get("/", include_in_schema=False)
def home(user: Annotated[User, Depends(page_user)]) -> Response:
    """Landing page: signed-in users go straight to the dashboard."""
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login", include_in_schema=False)
def login_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
) -> Response:
    """Login page; authenticated users are sent on to their destination."""
    token = extract_session_token(request)
    user = auth_service.validate_session(db, token) if token else None
    if user is not None and user.is_active:
        return RedirectResponse(_safe_next(request), status_code=303)
    return _render(request, "login.html", {"next": _safe_next(request)})


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@router.get("/dashboard", include_in_schema=False)
def dashboard(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Library overview: stats, continue-reading, and recent additions."""
    stats = catalog_service.dashboard_stats(db)
    recent = catalog_service.recent_books(db, limit=12)
    reading = catalog_service.reading_shelf(db, user.id, limit=8)
    return _render(
        request,
        "dashboard.html",
        {"stats": stats, "recent": recent, "reading": reading},
    )


# --------------------------------------------------------------------------- #
# Catalog: browse, detail, series, authors, downloads
# --------------------------------------------------------------------------- #
@router.get("/books", include_in_schema=False)
def books_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
    q: str = "",
    sort: str = "title",
    page: int = 1,
) -> Response:
    """Paginated catalog grid with optional title filter and sorting."""
    current = max(1, page)
    limit = _BOOKS_PER_PAGE
    offset = (current - 1) * limit
    if sort not in {"title", "recent"}:
        sort = "title"
    catalog = catalog_service.list_books(
        db, query=q.strip() or None, sort=sort, limit=limit, offset=offset
    )
    return _render(
        request,
        "books.html",
        {
            "catalog": catalog,
            "q": q,
            "sort": sort,
            "per_page": limit,
            "active": "books",
        },
    )


@router.get("/books/{book_id}", include_in_schema=False)
def book_detail(
    request: Request,
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Book detail: cover, metadata, formats, and personal progress."""
    book = catalog_service.get_book(db, book_id)
    if book is None:
        return _not_found(request)
    progress = catalog_service.progress_for_user(db, user.id, book_id)
    return _render(request, "book_detail.html", {"book": book, "progress": progress})


@router.get("/books/{book_id}/download/{file_id}", include_in_schema=False)
def book_download(
    request: Request,
    book_id: int,
    file_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Stream a book file from the read-only library (range requests OK)."""
    file_row = catalog_service.resolve_file(db, book_id, file_id)
    if file_row is None:
        return _not_found(request)
    if file_row.is_missing:
        return _render(request, "file_missing.html", status_code=410)
    path = catalog_service.file_download_path(file_row)
    if path is None or not path.is_file():
        return _render(request, "file_missing.html", status_code=410)
    return FileResponse(str(path), filename=Path(file_row.file_path).name)


@router.get("/series/{series_id}", include_in_schema=False)
def series_detail(
    request: Request,
    series_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Books belonging to a series."""
    series = catalog_service.get_series(db, series_id)
    if series is None:
        return _not_found(request)
    return _render(request, "series_detail.html", {"series": series})


@router.get("/authors/{author_id}", include_in_schema=False)
def author_detail(
    request: Request,
    author_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Books written by an author."""
    author = catalog_service.get_author(db, author_id)
    if author is None:
        return _not_found(request)
    return _render(request, "author_detail.html", {"author": author})


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@router.get("/search", include_in_schema=False)
def search_page(
    request: Request,
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Full-text search landing page (FTS5 via the Phase 8 API)."""
    return _render(request, "search.html", {"active": "search"})


@router.get("/search/results", include_in_schema=False)
def search_results_fragment(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
    q: str = "",
) -> Response:
    """HTMX fragment: rendered search results for search-as-you-type."""
    query = q.strip()
    results = search_service.search(db, query, limit=25)
    books = catalog_service.books_by_ids(db, [item.book_id for item in results.items])
    return _render(
        request,
        "partials/search_results.html",
        {"results": results, "books": books, "query": query},
    )


# --------------------------------------------------------------------------- #
# Reader placeholder (the full web reader ships later)
# --------------------------------------------------------------------------- #
@router.get("/reader/{book_id}", include_in_schema=False)
def reader_page(
    request: Request,
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Reader shell; the web reader experience lands in a later phase."""
    book = catalog_service.get_book(db, book_id)
    if book is None:
        return _not_found(request)
    return _render(request, "reader.html", {"book": book})


# --------------------------------------------------------------------------- #
# Personal shelves & settings
# --------------------------------------------------------------------------- #
@router.get("/collections", include_in_schema=False)
def collections_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """The user's personal bookshelves, with cover previews."""
    shelves = catalog_service.list_collections(db, user.id)
    return _render(request, "collections.html", {"collections": shelves, "active": "collections"})


@router.get("/settings", include_in_schema=False)
def settings_page(
    request: Request,
    user: Annotated[User, Depends(page_user)],
) -> Response:
    """Account details and password change."""
    return _render(request, "settings.html", {"active": "settings"})


# --------------------------------------------------------------------------- #
# Admin console
# --------------------------------------------------------------------------- #
@router.get("/admin/users", include_in_schema=False)
def admin_users_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """User accounts table."""
    return _render(
        request, "admin/users.html", {"users": admin_service.list_users(db), "active": "admin"}
    )


@router.get("/admin/libraries", include_in_schema=False)
def admin_libraries_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """Configured library roots table."""
    return _render(
        request,
        "admin/libraries.html",
        {"libraries": admin_service.list_libraries(db), "active": "admin"},
    )


@router.get("/admin/jobs", include_in_schema=False)
def admin_jobs_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """Recent background job queue status."""
    summary = admin_service.job_summary(db)
    return _render(request, "admin/jobs.html", {"summary": summary, "active": "admin"})


# --------------------------------------------------------------------------- #
# Admin metadata review
# --------------------------------------------------------------------------- #
@router.get("/admin/metadata", include_in_schema=False)
def admin_metadata_page(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
    status: str = "pending",
    page: int = 1,
) -> Response:
    """Review queue: books with metadata matches, filterable by status."""
    if status not in {"pending", "applied", "rejected"}:
        status = "pending"
    current = max(1, page)
    offset = (current - 1) * _REVIEW_PER_PAGE
    try:
        total, items = review_service.list_review_items(
            db, status=status, limit=_REVIEW_PER_PAGE, offset=offset
        )
    except ValueError:
        status = "pending"
        total, items = review_service.list_review_items(db, status="pending")
    return _render(
        request,
        "admin/metadata.html",
        {"items": items, "total": total, "status": status, "page": current, "active": "admin"},
    )


def _review_body(request: Request, db: DbSession, book_id: int, **flash: str) -> Response:
    """Render the book-review body fragment (swapped in by HTMX)."""
    try:
        review = review_service.get_book_review(db, book_id)
    except ValueError:
        return _not_found(request)
    return _render(
        request,
        "partials/admin_metadata_body.html",
        {"book_id": book_id, "review": review, "active": "admin", **flash},
    )


def _review_page(request: Request, db: DbSession, book_id: int) -> Response:
    """Render the full review page around the review body fragment."""
    try:
        review = review_service.get_book_review(db, book_id)
    except ValueError:
        return _not_found(request)
    return _render(
        request,
        "admin/metadata_book.html",
        {"book_id": book_id, "review": review, "active": "admin"},
    )


@router.get("/admin/metadata/books/{book_id}", include_in_schema=False)
def admin_metadata_book_page(
    request: Request,
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """Full review page for one book's metadata candidates."""
    return _review_page(request, db, book_id)


@router.post(
    "/admin/metadata/books/{book_id}/matches/{match_id}/apply-missing",
    include_in_schema=False,
)
def admin_apply_missing(
    request: Request,
    book_id: int,
    match_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """Apply every missing, non-user-edited field from a candidate."""
    try:
        result = review_service.apply_missing(db, book_id, match_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    fields = ", ".join(result.fields_applied)
    return _review_body(request, db, book_id, notice=f"Applied missing fields: {fields}")


@router.post(
    "/admin/metadata/books/{book_id}/matches/{match_id}/apply-fields",
    include_in_schema=False,
)
def admin_apply_fields(
    request: Request,
    book_id: int,
    match_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
    fields: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Apply a hand-picked subset of candidate fields."""
    chosen = fields or []
    if not chosen:
        return _review_body(request, db, book_id, error="Select at least one field to apply.")
    try:
        result = review_service.apply_fields(db, book_id, match_id, chosen)
    except ValueError:
        raise HTTPException(status_code=404) from None
    fields_joined = ", ".join(result.fields_applied)
    return _review_body(request, db, book_id, notice=f"Applied fields: {fields_joined}")


@router.post("/admin/metadata/books/{book_id}/matches/{match_id}/reject", include_in_schema=False)
def admin_reject_match(
    request: Request,
    book_id: int,
    match_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
) -> Response:
    """Reject a candidate; already-applied data is untouched."""
    try:
        review_service.reject_match(db, book_id, match_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    return _review_body(request, db, book_id, notice="Candidate rejected.")


@router.post("/admin/metadata/books/{book_id}", include_in_schema=False)
def admin_edit_metadata(
    request: Request,
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(page_admin)],
    title: Annotated[str, Form()] = "",
    subtitle: Annotated[str, Form()] = "",
    authors: Annotated[str, Form()] = "",
    series: Annotated[str, Form()] = "",
    series_index: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    publisher: Annotated[str, Form()] = "",
    published_date: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "",
) -> Response:
    """Persist manual metadata edits (marked as ``user`` provenance)."""
    edits: dict[str, Any] = {
        "title": title,
        "subtitle": subtitle or None,
        "description": description or None,
        "publisher": publisher or None,
        "published_date": published_date or None,
        "language": language or None,
        "authors": [name for name in (part.strip() for part in authors.split(",")) if name],
    }
    series_value = series.strip()
    if series_value:
        edits["series"] = series_value
    else:
        edits["series"] = ""
    if series_index.strip():
        try:
            edits["series_index"] = float(series_index)
        except ValueError:
            return _review_body(request, db, book_id, error="Series number is not numeric.")
    try:
        result = review_service.update_metadata(db, book_id, edits)
    except ValueError as exc:
        return _review_body(request, db, book_id, error=str(exc))
    fields_joined = ", ".join(result.fields_applied)
    return _review_body(request, db, book_id, notice=f"Saved edits: {fields_joined}")
