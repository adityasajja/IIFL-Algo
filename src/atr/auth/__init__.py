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
from atr.auth.service import AuthError, AuthService, get_auth_service

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
