"""Role → permission matrix. UI hiding is not authorization."""
from __future__ import annotations

from app.dashboard.authorization.permissions import Permission

Role = str

_STAFF: frozenset[Permission] = frozenset(
    {
        Permission.PATIENT_VIEW,
        Permission.PATIENT_SEARCH,
        Permission.APPOINTMENT_VIEW,
        Permission.APPROVAL_VIEW,
        Permission.MARKETING_VIEW,
        Permission.SYSTEM_VIEW,
        Permission.DOCUMENT_REVIEW,
        Permission.EXPENSE_REVIEW,
        Permission.COMMUNICATION_VIEW,
    }
)

_PRACTITIONER: frozenset[Permission] = _STAFF | frozenset(
    {
        Permission.APPOINTMENT_CHANGE,
        Permission.APPROVAL_APPROVE,
        Permission.APPROVAL_REJECT,
        Permission.REFERRER_VIEW,
        Permission.CASE_VIEW,
        Permission.CASE_ACKNOWLEDGE,
    }
)

_ADMIN: frozenset[Permission] = _PRACTITIONER | frozenset(
    {
        Permission.REFERRER_RESOLVE,
        Permission.AUDIT_VIEW,
        Permission.MARKETING_MANAGE,
        Permission.SYSTEM_KILL_SWITCH,
        Permission.COMMUNICATION_MANAGE,
    }
)

_OWNER: frozenset[Permission] = frozenset(Permission)  # all permissions

_OTHER: frozenset[Permission] = frozenset(
    {
        Permission.PATIENT_VIEW,
        Permission.PATIENT_SEARCH,
        Permission.APPOINTMENT_VIEW,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    "other": _OTHER,
    "staff": _STAFF,
    "practitioner": _PRACTITIONER,
    "admin": _ADMIN,
    "owner": _OWNER,
}


class AuthorizationError(Exception):
    """Caller lacks a required permission."""

    def __init__(self, permission: Permission | str, role: str | None = None) -> None:
        self.permission = str(permission)
        self.role = role
        super().__init__(f"forbidden: missing permission {self.permission}")


class AuthorizationPolicy:
    """Central authorization service — routes must call this, not ad-hoc checks."""

    def permissions_for(self, role: str) -> frozenset[Permission]:
        return ROLE_PERMISSIONS.get(role, frozenset())

    def has_permission(self, role: str, permission: Permission | str) -> bool:
        perm = Permission(permission) if not isinstance(permission, Permission) else permission
        return perm in self.permissions_for(role)

    def require(self, role: str, permission: Permission | str) -> None:
        if not self.has_permission(role, permission):
            raise AuthorizationError(permission, role=role)

    def known_roles(self) -> frozenset[str]:
        return frozenset(ROLE_PERMISSIONS)
