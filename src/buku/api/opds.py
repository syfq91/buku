"""OPDS HTTP transport routes (Phases 12-13).

Thin transport layer: validates inputs, delegates to the OPDS catalog service
(:mod:`buku.opds.service`) and the canonical progression store
(:mod:`buku.services.progression`), and serializes Atom/JSON output. No
business logic lives here and nothing ever writes to the media directories.

Phase 12  — OPDS 1.2 catalog: root/books/series/authors/search feeds,
            acquisitions, artwork, pagination, OpenSearch autodiscovery.
Phase 13  — OPDS Progression 1.0: ``GET``/``PUT`` /opds/progression/{book_id}
            with RFC 7807 problem-details conflict/validation errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session as DbSession

from buku.db import get_db
from buku.models.user import User
from buku.opds.auth import get_opds_user
from buku.opds.models import (
    ACQUISITION_FEED_TYPE,
    KIND_NAVIGATION,
    NAVIGATION_FEED_TYPE,
    OPDS_PAGE_SIZE,
    PROGRESSION_TYPE,
    OpdsFeed,
    format_media_type,
)
from buku.opds.progression import (
    document_from_payload,
    document_to_json,
    locator_from_references,
    problem_details,
    state_to_document,
)
from buku.opds.serializer import serialize_feed, serialize_opensearch_description
from buku.opds.service import opds_service
from buku.services.catalog import catalog_service
from buku.services.progression import ProgressionStatus, progression_service

router = APIRouter(tags=["OPDS"])


def _base_url(request: Request) -> str:
    """Absolute base URL used to build dereferenceable OPDS links."""
    return str(request.base_url).rstrip("/")


def _feed_response(feed: OpdsFeed) -> Response:
    """Serialize a feed model to an OPDS Atom response with the right media type."""
    media_type = NAVIGATION_FEED_TYPE if feed.kind == KIND_NAVIGATION else ACQUISITION_FEED_TYPE
    return Response(content=serialize_feed(feed), media_type=media_type)


# --------------------------------------------------------------------------- #
# Phase 12 — OPDS 1.2 catalog
# --------------------------------------------------------------------------- #
@router.get("/opds", response_class=Response)
def opds_root(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """OPDS Catalog Root: a navigation feed over the library sections."""
    return _feed_response(opds_service.root(db, _base_url(request)))


@router.get("/opds/books", response_class=Response)
def opds_books(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = OPDS_PAGE_SIZE,
) -> Response:
    """Acquisition feed of the catalog, paginated by title."""
    return _feed_response(opds_service.books(db, _base_url(request), page=page, per_page=limit))


@router.get("/opds/series", response_class=Response)
def opds_series(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """Navigation feed listing every series."""
    return _feed_response(opds_service.series_listing(db, _base_url(request)))


@router.get("/opds/authors", response_class=Response)
def opds_authors(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """Navigation feed listing every author."""
    return _feed_response(opds_service.author_listing(db, _base_url(request)))


@router.get("/opds/series/{series_id}", response_class=Response)
def opds_series_books(
    series_id: int,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = OPDS_PAGE_SIZE,
) -> Response:
    """Acquisition feed of the books inside one series."""
    feed = opds_service.series_books(db, _base_url(request), series_id, page=page, per_page=limit)
    if feed is None:
        raise HTTPException(status_code=404, detail="Series not found.")
    return _feed_response(feed)


@router.get("/opds/authors/{author_id}", response_class=Response)
def opds_author_books(
    author_id: int,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = OPDS_PAGE_SIZE,
) -> Response:
    """Acquisition feed of the books written by one author."""
    feed = opds_service.author_books(db, _base_url(request), author_id, page=page, per_page=limit)
    if feed is None:
        raise HTTPException(status_code=404, detail="Author not found.")
    return _feed_response(feed)


@router.get("/opds/search", response_class=Response)
def opds_search(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
    q: Annotated[str, Query(description="Full-text search query.")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = OPDS_PAGE_SIZE,
) -> Response:
    """Acquisition feed of FTS5 search results."""
    return _feed_response(opds_service.search(db, _base_url(request), q, page=page, per_page=limit))


@router.get("/opds/opensearch.xml", response_class=Response)
def opds_opensearch(
    request: Request,
    _user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """OpenSearch 1.1 description enabling OPDS search autodiscovery."""
    return Response(
        content=serialize_opensearch_description(_base_url(request)),
        media_type="application/opensearchdescription+xml",
    )


@router.get("/opds/books/{book_id}/download/{file_id}")
def opds_download(
    book_id: int,
    file_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    _user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """Stream a publication from the read-only library for an OPDS client."""
    file_row = catalog_service.resolve_file(db, book_id, file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    if file_row.is_missing:
        raise HTTPException(status_code=410, detail="File is missing from the media directory.")
    path = catalog_service.file_download_path(file_row)
    if path is None or not path.is_file():
        raise HTTPException(status_code=410, detail="File is unavailable.")
    return FileResponse(
        str(path),
        media_type=format_media_type(file_row.file_format),
        filename=Path(file_row.file_path).name,
    )


# --------------------------------------------------------------------------- #
# Phase 13 — OPDS Progression 1.0
# --------------------------------------------------------------------------- #
@router.get("/opds/progression/{book_id}")
def opds_get_progression(
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """Return the caller's last-known progression for a logical book.

    ``204 No Content`` is returned when no progression has been recorded yet
    (an empty payload per the OPDS Progression 1.0 draft).
    """
    if not progression_service.book_exists(db, book_id):
        raise HTTPException(status_code=404, detail="Book not found.")
    state = progression_service.get_state(db, user.id, book_id)
    if state is None:
        return Response(status_code=204)
    return JSONResponse(
        content=document_to_json(state_to_document(state)),
        media_type=PROGRESSION_TYPE,
    )


@router.put("/opds/progression/{book_id}")
def opds_put_progression(
    book_id: int,
    payload: dict[str, Any],
    db: Annotated[DbSession, Depends(get_db)],
    user: Annotated[User, Depends(get_opds_user)],
) -> Response:
    """Create or update the caller's progression for a logical book.

    A payload whose ``modified`` timestamp is older than the stored state is
    answered with ``409 Conflict`` carrying an RFC 7807 problem-details body.
    """
    if not progression_service.book_exists(db, book_id):
        raise HTTPException(status_code=404, detail="Book not found.")

    try:
        document = document_from_payload(payload)
    except ValueError as exc:
        return JSONResponse(
            content=problem_details(
                "progression-invalid-payload",
                "Progression could not be updated due to an invalid payload.",
                detail=str(exc),
            ),
            status_code=400,
            media_type=PROGRESSION_TYPE,
        )

    href, fragment = locator_from_references(document.references)
    result = progression_service.update(
        db,
        user.id,
        book_id,
        progression=document.progression,
        href=href,
        fragment=fragment,
        title=document.title,
        modified_at=document.modified,
        device_id=document.device_id,
        device_name=document.device_name,
    )

    if result.status is ProgressionStatus.CONFLICT:
        assert result.previous is not None
        return JSONResponse(
            content=problem_details(
                "progression-date",
                "A more recent progression point is already available.",
                detail={
                    "stored": document_to_json(state_to_document(result.previous)),
                    "incoming": document_to_json(state_to_document(result.state)),
                },
            ),
            status_code=409,
            media_type=PROGRESSION_TYPE,
        )

    status_code = 201 if result.status is ProgressionStatus.CREATED else 200
    response = document_to_json(state_to_document(result.state))
    return JSONResponse(content=response, status_code=status_code, media_type=PROGRESSION_TYPE)
