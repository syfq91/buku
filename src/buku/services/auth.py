"""Authentication service implementing Argon2id hashing and server-side sessions."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from buku.models.base import utc_now
from buku.models.user import Session as SessionModel
from buku.models.user import User

if TYPE_CHECKING:
    pass

logger = logging.getLogger("buku.auth")


class AuthenticationService:
    """Service handling password cryptography, user credential validation, and sessions."""

    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash_password(self, password: str) -> str:
        """Hash a plaintext password using Argon2id."""
        return self._hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        """Verify a plaintext password against an Argon2id hash."""
        try:
            return self._hasher.verify(password_hash, password)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return False

    def create_user(
        self,
        db: DbSession,
        username: str,
        password: str,
        display_name: str,
        is_admin: bool = False,
    ) -> User:
        """Create a new user account with an Argon2id password hash."""
        existing = db.scalar(select(User).where(User.username == username))
        if existing is not None:
            raise ValueError(f"Username '{username}' is already taken.")

        user = User(
            username=username,
            password_hash=self.hash_password(password),
            display_name=display_name,
            is_admin=is_admin,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Created user '%s' (admin=%s)", username, is_admin)
        return user

    def authenticate_user(
        self,
        db: DbSession,
        username: str,
        password: str,
    ) -> User | None:
        """Verify user credentials and check active status. Returns None on failure."""
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            return None

        if not user.is_active:
            logger.warning("Authentication rejected: user '%s' is deactivated.", username)
            return None

        if not self.verify_password(user.password_hash, password):
            return None

        return user

    def change_password(
        self,
        db: DbSession,
        user: User,
        old_password: str,
        new_password: str,
    ) -> bool:
        """Change user password after verifying current credentials."""
        if not self.verify_password(user.password_hash, old_password):
            return False

        user.password_hash = self.hash_password(new_password)
        user.updated_at = utc_now()
        db.commit()
        logger.info("Password updated for user '%s'", user.username)
        return True

    def disable_user(self, db: DbSession, user_id: int) -> bool:
        """Deactivate a user account and immediately revoke all active sessions."""
        user = db.get(User, user_id)
        if user is None:
            return False

        user.is_active = False
        user.updated_at = utc_now()
        # Revoke all sessions for security
        db.execute(delete(SessionModel).where(SessionModel.user_id == user_id))
        db.commit()
        logger.info("Deactivated user '%s' and purged active sessions.", user.username)
        return True

    def create_session(
        self,
        db: DbSession,
        user: User,
        user_agent: str | None = None,
        ip_address: str | None = None,
        duration_days: int = 30,
    ) -> SessionModel:
        """Create and persist a new server-side session."""
        token = secrets.token_hex(32)  # 64-char secure random token
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=duration_days)

        session = SessionModel(
            id=token,
            user_id=user.id,
            created_at=now,
            expires_at=expires_at,
            last_active_at=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def validate_session(self, db: DbSession, token: str) -> User | None:
        """Validate session token, check expiry/status, and update last_active_at."""
        session = db.get(SessionModel, token)
        if session is None:
            return None

        now = datetime.now(UTC)
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        # Check expiration
        if expires_at <= now:
            db.delete(session)
            db.commit()
            return None

        # Check user active status
        if not session.user.is_active:
            db.delete(session)
            db.commit()
            return None

        # Update activity timestamp
        session.last_active_at = now
        db.commit()
        return session.user

    def revoke_session(self, db: DbSession, token: str) -> bool:
        """Revoke a single session token (e.g. logout)."""
        session = db.get(SessionModel, token)
        if session is None:
            return False

        db.delete(session)
        db.commit()
        return True

    def revoke_all_user_sessions(self, db: DbSession, user_id: int) -> int:
        """Revoke all sessions belonging to a user."""
        result = db.execute(delete(SessionModel).where(SessionModel.user_id == user_id))
        db.commit()
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount)


auth_service = AuthenticationService()
