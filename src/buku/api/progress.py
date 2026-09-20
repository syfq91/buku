"""Reading-progression HTTP transport routes (Phase 10).

Thin transport layer: validates path params and payloads, delegates all state
to :class:`buku.services.progression.ProgressionService`, and serializes
responses. The same service powers the Phase 11 web reader and the Phase 13
OPDS Progression 1.0 sync; there is exactly one canonical progression store.

Conflict rule (Rule 3 invariant): ``PUT`` with a timestamp older than the
stored one yields ``HTTP 409 Conflict`` including both snapshots so the
client can reconcile. Newer/equal timestamps and first-time writes apply.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import get_current_user
from buku.db import get_db
from buku.models.user import User
from buku.services.progression import (
    ProgressionStatus,
    ProgressState,
    progression_service,
)

router = APIRouter(prefix="/api/v1/progress", tags=["Progress"])


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class ProgressResponse(BaseModel):
    """Canonical progression state for one (user, book) pair."""

    book_id: int
    progression: float
    href: str | None = None
    fragment: str | None = None
    title: str | None = None
    modified_at: datetime
    device_id: str | None = None
    device_name: str | None = None


class ProgressGetResponse(BaseModel):
    """GET response: the stored state, or None when no progress was recorded."""

    progress: ProgressResponse | None = None


class ProgressUpdateRequest(BaseModel):
    """Payload for a progression write (client wall-clock in ``modified_at``)."""

    progression: float = Field(ge=0.0, le=1.0, description="Reading position (0.0–1.0).")
    href: str | None = Field(default=None, max_length=500, description="Chapter/resource href.")
    fragment: str | None = Field(
        default=None, max_length=500, description="Anchor/DOM locator, e.g. chapter03.xhtml#p42."
    )
    title: str | None = Field(default=None, max_length=500, description="Section title.")
    modified_at: datetime | None = Field(
        default=None, description="UTC modification timestamp; server time when omitted."
    )
    device_id: str | None = Field(default=None, max_length=100, description="Sync client id.")
    device_name: str | None = Field(default=None, max_length=100, description="Sync client name.")


class ProgressUpdateResponse(BaseModel):
    """PUT response: outcome plus the applied state."""

    status: str
    progress: ProgressResponse


class ProgressConflictDetail(BaseModel):
    """Body payload carried inside a 409 response."""

    message: str
    stored: ProgressResponse
    incoming: ProgressResponse


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/{book_id}", response_model=ProgressGetResponse)
def get_progress(
    book_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[DbSession, Depends(get_db)],
) -> ProgressGetResponse:
    """Return the current user's stored progression for a logical book."""
    if not progression_service.book_exists(db, book_id):
        raise HTTPException(status_code=404, detail="Book not found.")
    state = progression_service.get_state(db, user.id, book_id)
    return ProgressGetResponse(progress=_serialize(state) if state is not None else None)


@router.put("/{book_id}", response_model=ProgressUpdateResponse)
def update_progress(
    book_id: int,
    payload: ProgressUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[DbSession, Depends(get_db)],
) -> ProgressUpdateResponse:
    """Create or update the user's progression for a logical book."""
    if not progression_service.book_exists(db, book_id):
        raise HTTPException(status_code=404, detail="Book not found.")

    result = progression_service.update(
        db,
        user.id,
        book_id,
        progression=payload.progression,
        href=payload.href,
        fragment=payload.fragment,
        title=payload.title,
        modified_at=payload.modified_at,
        device_id=payload.device_id,
        device_name=payload.device_name,
    )

    if result.status is ProgressionStatus.CONFLICT:
        assert result.previous is not None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ProgressConflictDetail(
                message="Incoming update is older than the stored progression.",
                stored=_serialize(result.previous),
                incoming=_serialize(result.state),
            ).model_dump(mode="json"),
        )

    return ProgressUpdateResponse(status=result.status.value, progress=_serialize(result.state))


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _serialize(state: ProgressState) -> ProgressResponse:
    """Convert a service snapshot into the API response model."""
    return ProgressResponse(
        book_id=state.book_id,
        progression=state.progression,
        href=state.href,
        fragment=state.fragment,
        title=state.title,
        modified_at=state.modified_at,
        device_id=state.device_id,
        device_name=state.device_name,
    )
