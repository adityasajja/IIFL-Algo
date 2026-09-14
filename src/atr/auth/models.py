"""Identity types shared by the auth service and the API layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atr.auth.rbac import Permission, PermissionDenied, Role, has_permission


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and what they may do.

    Permissions are materialised on the principal rather than looked up per
    check, so a request's authority cannot change underneath it — a role edit
    mid-request must not grant a permission halfway through.
    """

    user_id: str
    username: str
    email: str
    role: Role
    display_name: str | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    auth_method: str = "session"  # session | apikey | anonymous
    session_id: str | None = None
    api_key_id: str | None = None
    mfa_satisfied: bool = True

    @classmethod
    def build(
        cls,
        *,
        user: dict[str, Any],
        auth_method: str,
        session_id: str | None = None,
        api_key_id: str | None = None,
        mfa_satisfied: bool = True,
        extra_permissions: frozenset[Permission] | None = None,
    ) -> Principal:
        role = Role.parse(user["role"])
        perms = permissions_from(user["role"])
        if extra_permissions is not None:
            # An API key may narrow its own authority, never widen it. Intersecting
            # with the role is what makes a leaked read-only key read-only.
            # The check is `is not None`, not truthiness: an API key created with
            # an empty scope list must hold *no* permissions, and `frozenset()`
            # is falsy — treating that as "unspecified" would hand out the full
            # role instead.
            perms = perms & extra_permissions
        return cls(
            user_id=user["user_id"],
            username=user["username"],
            email=user["email"],
            role=role,
            display_name=user.get("display_name"),
            permissions=perms,
            auth_method=auth_method,
            session_id=session_id,
            api_key_id=api_key_id,
            mfa_satisfied=mfa_satisfied,
        )

    def can(self, permission: Permission | str) -> bool:
        try:
            return Permission.parse(permission) in self.permissions
        except ValueError:
            return False

    def require(self, permission: Permission | str) -> None:
        if not self.can(permission):
            raise PermissionDenied(permission, self.role)

    @property
    def is_authenticated(self) -> bool:
        return self.auth_method != "anonymous"

    @property
    def actor(self) -> str:
        """The string written to the audit trail."""
        if not self.is_authenticated:
            return "anonymous"
        return self.email or self.username

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role.value,
            "permissions": sorted(p.value for p in self.permissions),
            "auth_method": self.auth_method,
            "mfa_satisfied": self.mfa_satisfied,
        }


def permissions_from(role: Role | str) -> frozenset[Permission]:
    from atr.auth.rbac import permissions_for

    return permissions_for(role)


def anonymous_principal() -> Principal:
    """A read-only principal, used only when ``AUTH_REQUIRED`` is switched off.

    Holds exactly the viewer role, so it can read and cannot write. It is not an
    escape hatch for a missing session — with ``AUTH_REQUIRED`` at its default of
    true, an unauthenticated request is refused outright.
    """
    return Principal(
        user_id="anonymous",
        username="anonymous",
        email="",
        role=Role.VIEWER,
        display_name="Anonymous (auth disabled)",
        permissions=permissions_from(Role.VIEWER),
        auth_method="anonymous",
    )


@dataclass(slots=True)
class LoginResult:
    """Outcome of a login attempt.

    ``mfa_required`` is a distinct status rather than a failure: the password was
    correct, so returning 401 would be a lie the client cannot act on.
    """

    status: str  # ok | mfa_required | invalid | locked | inactive
    token: str | None = None
    expires_at: datetime | None = None
    user: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def has_any(role: Role | str, permissions: list[Permission | str]) -> bool:
    return any(has_permission(role, p) for p in permissions)


__all__ = [
    "LoginResult",
    "Principal",
    "anonymous_principal",
    "has_any",
    "permissions_from",
]
