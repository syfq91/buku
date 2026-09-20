"""FastAPI dependency injection utilities for authentication and authorization."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from buku.db import get_db
from buku.models.user import User
from buku.services.auth import auth_service
from buku.services.authorization import authorization_service

SESSION_COOKIE_NAME = "buku_session"


def extract_session_token(request: Request) -> str | None:
    """Extract session token from Authorization header or fallback to cookie."""
    # 1. Check explicit Authorization header first (API / OPDS clients)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # 2. Check HTTP cookie (web browsers)
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token

    return None


def get_current_user(
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
) -> User:
    """Validate session token and return authenticated User."""
    token = extract_session_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = auth_service.validate_session(db, token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )

    return user


def get_current_admin_user(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Ensure authenticated user has administrator privileges."""
    try:
        authorization_service.require_admin(user)
    except PermissionError as err:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(err),
        ) from err

    return user
