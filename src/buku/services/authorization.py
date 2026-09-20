"""Authorization service and role-based access control (RBAC)."""

from __future__ import annotations

from enum import StrEnum

from buku.models.user import User


class Role(StrEnum):
    """System authorization roles."""

    ADMIN = "ADMIN"
    USER = "USER"


class AuthorizationService:
    """Service handling role and resource access permissions."""

    @staticmethod
    def get_role(user: User) -> Role:
        """Return primary role for user."""
        return Role.ADMIN if user.is_admin else Role.USER

    @staticmethod
    def can_manage_users(user: User) -> bool:
        """Admins can create, edit, disable users and reset passwords."""
        return user.is_active and user.is_admin

    @staticmethod
    def can_configure_libraries(user: User) -> bool:
        """Admins can add, edit, and remove library root configurations."""
        return user.is_active and user.is_admin

    @staticmethod
    def can_scan_libraries(user: User) -> bool:
        """Admins can trigger library scans."""
        return user.is_active and user.is_admin

    @staticmethod
    def can_manage_metadata(user: User) -> bool:
        """Admins can manage external metadata providers and match approvals."""
        return user.is_active and user.is_admin

    @staticmethod
    def can_manage_system_settings(user: User) -> bool:
        """Admins can manage global server settings."""
        return user.is_active and user.is_admin

    @staticmethod
    def can_browse_and_read(user: User) -> bool:
        """Active users can browse the catalog and read books."""
        return user.is_active

    @staticmethod
    def can_access_user_data(current_user: User, target_user_id: int) -> bool:
        """Check if current user can access another user's private data.

        Rule 3 Invariant: Reading progression and personal collections are strictly
        user-isolated. Only the resource owner (or admin for support) may access.
        """
        if not current_user.is_active:
            return False
        return current_user.id == target_user_id or current_user.is_admin

    @classmethod
    def require_admin(cls, user: User) -> None:
        """Raise PermissionError if user lacks admin privileges."""
        if not cls.can_manage_users(user):
            raise PermissionError("Administrator privileges required.")

    @classmethod
    def require_user_access(cls, current_user: User, target_user_id: int) -> None:
        """Raise PermissionError if user tries to access another user's private data."""
        if not cls.can_access_user_data(current_user, target_user_id):
            raise PermissionError("Access to another user's private state is forbidden.")


authorization_service = AuthorizationService()
