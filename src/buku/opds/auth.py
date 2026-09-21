"""HTTP Basic / Bearer authentication for OPDS endpoints (Phase 12).

External e-readers (Kobo, KOReader, etc.) authenticate against OPDS catalogs
with HTTP Basic credentials; browser users reuse the same server-side session
cookie (or an explicit ``Authorization: Bearer`` token). The dependency
ensures disabled users are rejected regardless of which mechanism they use.
"""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import extract_session_token
from buku.db import get_db
from buku.models.user import User
from buku.services.auth import auth_service

_WWW_AUTHENTICATE = 'Basic realm="buku OPDS catalog", Bearer'


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    """Decode ``Authorization: Basic`` credentials, or None when absent/malformed."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    raw = header[6:].strip()
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except ValueError, UnicodeDecodeError:
        return None
    username, _, password = decoded.partition(":")
    if not username:
        return None
    return username, password


def get_opds_user(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
) -> User:
    """Resolve the OPDS caller from Basic credentials, a Bearer token, or a session cookie."""
    credentials = _basic_credentials(request)
    if credentials is not None:
        username, password = credentials
        user = auth_service.authenticate_user(db, username, password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid OPDS credentials.",
                headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
            )
        return user

    token = extract_session_token(request)
    if token:
        user = auth_service.validate_session(db, token)
        if user is not None and user.is_active:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="OPDS authentication required.",
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


__all__ = ["get_opds_user"]
