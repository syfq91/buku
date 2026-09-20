"""Admin metadata review & curation HTTP transport routes (Phase 7).

Thin transport layer: validates inputs, delegates to
:class:`buku.services.metadata_review.MetadataReviewService`, and serializes
responses. The Jinja2/HTMX review UI is Phase 9; these endpoints are its
backend contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import get_current_admin_user
from buku.db import get_db
from buku.models.user import User
from buku.services.metadata_review import (
    BookMetadataView,
    CandidateView,
    review_service,
)

router = APIRouter(prefix="/api/v1/admin/metadata", tags=["Admin Metadata"])


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class MetadataSnapshot(BaseModel):
    """Metadata for one side of the review comparison."""

    title: str
    subtitle: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    authors: list[str] = Field(default_factory=list)
    identifiers: dict[str, str] = Field(default_factory=dict)


class CandidateResponse(BaseModel):
    """A stored match row rendered for the review UI."""

    id: int
    provider: str
    external_id: str
    confidence: float
    status: str
    offered_fields: list[str] = Field(default_factory=list)
    metadata: MetadataSnapshot | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BookReviewResponse(BaseModel):
    """Current metadata + provenance + candidates, side-by-side."""

    book: MetadataSnapshot
    provenance: dict[str, str] = Field(default_factory=dict)
    candidates: list[CandidateResponse] = Field(default_factory=list)


class ReviewItemResponse(BaseModel):
    """One row in the review list."""

    book_id: int
    title: str
    authors: list[str] = Field(default_factory=list)
    pending: int = 0
    applied: int = 0
    rejected: int = 0
    updated_at: datetime | None = None


class ReviewListResponse(BaseModel):
    """Paged review list."""

    items: list[ReviewItemResponse] = Field(default_factory=list)
    total: int = 0
    limit: int = 25
    offset: int = 0


class ApplyFieldsRequest(BaseModel):
    """Field subset selected by the reviewer."""

    fields: list[str] = Field(min_length=1)


class UpdateMetadataRequest(BaseModel):
    """Manual metadata edits (every provided field becomes ``user`` provenance)."""

    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None
    authors: list[str] | None = None


class ApplyResponse(BaseModel):
    """Outcome of an apply/edit action."""

    message: str
    fields_applied: list[str] = Field(default_factory=list)
    fields_skipped: list[str] = Field(default_factory=list)


class MessageResponse(BaseModel):
    """Generic action acknowledgement."""

    message: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _snapshot(view: BookMetadataView) -> MetadataSnapshot:
    return MetadataSnapshot(
        title=view.title,
        subtitle=view.subtitle,
        description=view.description,
        publisher=view.publisher,
        published_date=view.published_date,
        language=view.language,
        series=view.series,
        series_index=view.series_index,
        authors=list(view.authors),
        identifiers=dict(view.identifiers),
    )


def _candidate_response(candidate: CandidateView) -> CandidateResponse:
    metadata = None
    if candidate.metadata is not None:
        metadata = MetadataSnapshot(
            title=candidate.metadata.title,
            subtitle=candidate.metadata.subtitle,
            description=candidate.metadata.description,
            publisher=candidate.metadata.publisher,
            published_date=candidate.metadata.published_date,
            language=candidate.metadata.language,
            series=candidate.metadata.series,
            series_index=candidate.metadata.series_index,
            authors=list(candidate.metadata.authors),
            identifiers=dict(candidate.metadata.identifiers),
        )
    return CandidateResponse(
        id=candidate.id,
        provider=candidate.provider,
        external_id=candidate.external_id,
        confidence=candidate.confidence,
        status=candidate.status,
        offered_fields=list(candidate.offered_fields),
        metadata=metadata,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def _service_error(exc: ValueError) -> HTTPException:
    """Map service errors onto suitable HTTP error responses."""
    message = str(exc)
    if "not found" in message.lower():
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/review", response_model=ReviewListResponse)
def list_review(
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    match_status: Annotated[str, Query(description="Filter by match status.")] = "pending",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewListResponse:
    """List books with matches in the given status for review."""
    try:
        total, items = review_service.list_review_items(
            db, status=match_status, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise _service_error(exc) from exc
    return ReviewListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            ReviewItemResponse(
                book_id=item.book_id,
                title=item.title,
                authors=list(item.authors),
                pending=item.pending,
                applied=item.applied,
                rejected=item.rejected,
                updated_at=item.updated_at,
            )
            for item in items
        ],
    )


@router.get("/books/{book_id}", response_model=BookReviewResponse)
def get_book_review(
    book_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> BookReviewResponse:
    """Return a book's current metadata, provenance, and candidate matches."""
    try:
        detail = review_service.get_book_review(db, book_id)
    except ValueError as exc:
        raise _service_error(exc) from exc
    return BookReviewResponse(
        book=_snapshot(detail.book),
        provenance=dict(detail.provenance),
        candidates=[_candidate_response(candidate) for candidate in detail.candidates],
    )


@router.post("/books/{book_id}/matches/{match_id}/apply-missing", response_model=ApplyResponse)
def apply_missing(
    book_id: int,
    match_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> ApplyResponse:
    """Apply every missing, non-user-edited field from the chosen match."""
    try:
        result = review_service.apply_missing(db, book_id, match_id)
    except ValueError as exc:
        raise _service_error(exc) from exc
    return ApplyResponse(
        message="Applied all missing fields.",
        fields_applied=result.fields_applied,
        fields_skipped=result.fields_skipped,
    )


@router.post("/books/{book_id}/matches/{match_id}/apply-fields", response_model=ApplyResponse)
def apply_fields(
    book_id: int,
    match_id: int,
    payload: ApplyFieldsRequest,
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> ApplyResponse:
    """Apply a hand-picked subset of the match's fields."""
    try:
        result = review_service.apply_fields(db, book_id, match_id, payload.fields)
    except ValueError as exc:
        raise _service_error(exc) from exc
    return ApplyResponse(
        message="Applied selected fields.",
        fields_applied=result.fields_applied,
        fields_skipped=result.fields_skipped,
    )


@router.put("/books/{book_id}", response_model=ApplyResponse)
def update_metadata(
    book_id: int,
    payload: UpdateMetadataRequest,
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> ApplyResponse:
    """Persist manual metadata edits with ``user`` provenance."""
    edits = payload.model_dump(exclude_unset=True)
    try:
        result = review_service.update_metadata(db, book_id, edits)
    except ValueError as exc:
        raise _service_error(exc) from exc
    return ApplyResponse(
        message="Metadata updated from manual edit.",
        fields_applied=result.fields_applied,
    )


@router.post("/books/{book_id}/matches/{match_id}/reject", response_model=MessageResponse)
def reject_match(
    book_id: int,
    match_id: int,
    db: Annotated[DbSession, Depends(get_db)],
    _admin: Annotated[User, Depends(get_current_admin_user)],
) -> MessageResponse:
    """Reject a match so it no longer appears as pending."""
    try:
        review_service.reject_match(db, book_id, match_id)
    except ValueError as exc:
        raise _service_error(exc) from exc
    return MessageResponse(message="Metadata match rejected.")


__all__ = ["router"]
