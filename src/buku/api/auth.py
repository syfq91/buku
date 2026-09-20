"""Authentication and user management HTTP transport routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from buku.api.deps import (
    SESSION_COOKIE_NAME,
    extract_session_token,
    get_current_admin_user,
    get_current_user,
)
from buku.db import get_db
from buku.models.user import User
from buku.services.auth import auth_service

router = APIRouter(tags=["Authentication"])


class LoginRequest(BaseModel):
    """Credentials payload for logging in."""

    username: str
    password: str


class UserResponse(BaseModel):
    """User account details response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    is_admin: bool
    is_active: bool


class LoginResponse(BaseModel):
    """Authentication success payload with session token and user details."""

    token: str
    user: UserResponse


class ChangePasswordRequest(BaseModel):
    """Password update payload."""

    old_password: str
    new_password: str


class MessageResponse(BaseModel):
    """Standard message response."""

    message: str


@router.post("/login", response_model=LoginResponse)
@router.post("/api/v1/auth/login", response_model=LoginResponse, include_in_schema=False)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
) -> Any:
    """Authenticate user credentials, create a server-side session, and set cookie."""
    user = auth_service.authenticate_user(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    user_agent = request.headers.get("user-agent")
    client_ip = request.client.host if request.client else None

    session = auth_service.create_session(
        db,
        user=user,
        user_agent=user_agent,
        ip_address=client_ip,
    )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=30 * 86400,
    )

    return {
        "token": session.id,
        "user": user,
    }


@router.post("/logout", response_model=MessageResponse)
@router.post("/api/v1/auth/logout", response_model=MessageResponse, include_in_schema=False)
def logout(
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
) -> Any:
    """Revoke active session token and clear authentication cookie."""
    token = extract_session_token(request)
    if token:
        auth_service.revoke_session(db, token)

    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return {"message": "Logged out successfully."}


@router.post("/change-password", response_model=MessageResponse)
@router.post(
    "/api/v1/auth/change-password",
    response_model=MessageResponse,
    include_in_schema=False,
)
def change_password(
    payload: ChangePasswordRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[DbSession, Depends(get_db)],
) -> Any:
    """Change password for currently authenticated user."""
    if len(payload.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters.",
        )

    success = auth_service.change_password(
        db,
        user=user,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    return {"message": "Password changed successfully."}


@router.get("/api/v1/auth/me", response_model=UserResponse)
def get_me(user: Annotated[User, Depends(get_current_user)]) -> Any:
    """Return profile details for currently authenticated user."""
    return user


@router.get("/api/v1/admin/users", response_model=list[UserResponse])
def list_admin_users(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[DbSession, Depends(get_db)],
) -> Any:
    """List all users in the system (Admin only)."""
    users = db.scalars(select(User).order_by(User.id)).all()
    return users
