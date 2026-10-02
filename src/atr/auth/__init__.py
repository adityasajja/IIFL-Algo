"""Authentication and authorization.

Layering: :mod:`atr.auth` owns identity and never imports the API layer. Routes
ask :class:`~atr.auth.service.AuthService` questions and never implement the
answers themselves.
"""

from __future__ import annotations

from atr.auth.models import LoginResult, Principal, anonymous_principal
from atr.auth.rbac import (
    MFA_REQUIRED_ROLES,
    Permission,
    PermissionDenied,
    Role,
    has_permission,
    permissions_for,
)

__all__ = [
    "MFA_REQUIRED_ROLES",
    "AuthError",
    "AuthService",
    "LoginResult",
    "Permission",
    "PermissionDenied",
    "Principal",
    "Role",
    "anonymous_principal",
    "get_auth_service",
    "has_permission",
    "permissions_for",
]

#: `atr.auth.service` pulls in sqlalchemy + cryptography (~450ms of import
#: time). Every submodule import here (`atr.auth.models`, `atr.auth.rbac`, ...)
#: runs this package's `__init__` first, so an eager import above paid that
#: cost on the path to routes that only wanted `Principal` or `Permission` and
#: never touch the auth service itself. PEP 562 module `__getattr__` keeps
#: `from atr.auth import AuthService` working unchanged, deferred to whoever
#: actually asks for it.
def __getattr__(name: str):
    if name in ("AuthError", "AuthService", "get_auth_service"):
        from atr.auth import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
