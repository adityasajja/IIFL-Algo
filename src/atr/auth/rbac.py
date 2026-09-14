"""Roles and the permission matrix.

The matrix is fixed in code (see ``docs/DATA_MODEL.md`` §1.7) rather than stored
in a table. Roles here are a product decision, not user data, and a permission
model that can be edited at runtime is a permission model nobody can reason about.

Two separations are load-bearing and are why this is a matrix rather than an
``is_admin`` flag:

* ``strategy:write`` does **not** imply ``order:place``. Research and execution
  are different authorities — a researcher can build and validate a strategy and
  still be unable to send an order.
* ``execution_mode:change`` is admin-only. Flipping paper → live is the most
  consequential action in the product.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    VIEWER = "viewer"
    RESEARCHER = "researcher"
    TRADER = "trader"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def parse(cls, value: str | Role) -> Role:
        if isinstance(value, Role):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            valid = ", ".join(r.value for r in cls)
            raise ValueError(f"unknown role {value!r}; expected one of: {valid}") from exc


class Permission(str, Enum):
    MARKET_READ = "market:read"
    INSTRUMENT_READ = "instrument:read"

    WATCHLIST_READ = "watchlist:read"
    WATCHLIST_WRITE = "watchlist:write"

    STRATEGY_READ = "strategy:read"
    STRATEGY_WRITE = "strategy:write"
    BACKTEST_RUN = "backtest:run"
    SCREENER_RUN = "screener:run"

    ORDER_READ = "order:read"
    ORDER_PLACE = "order:place"
    ORDER_CANCEL = "order:cancel"
    POSITION_SQUAREOFF = "position:squareoff"

    RISK_READ = "risk:read"
    RISK_CONFIGURE = "risk:configure"

    ALGO_START = "algo:start"
    ALGO_STOP = "algo:stop"
    EXECUTION_MODE_CHANGE = "execution_mode:change"

    USER_READ = "user:read"
    USER_WRITE = "user:write"
    APIKEY_MANAGE = "apikey:manage"
    AUDIT_READ = "audit:read"

    SYSTEM_READ = "system:read"
    SYSTEM_CONFIGURE = "system:configure"

    @classmethod
    def parse(cls, value: str | Permission) -> Permission:
        if isinstance(value, Permission):
            return value
        try:
            return cls(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"unknown permission {value!r}") from exc


_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.MARKET_READ,
        Permission.INSTRUMENT_READ,
        Permission.WATCHLIST_READ,
        Permission.STRATEGY_READ,
        Permission.ORDER_READ,
        Permission.RISK_READ,
        Permission.SYSTEM_READ,
    }
)

_RESEARCHER: frozenset[Permission] = _VIEWER | frozenset(
    {
        Permission.WATCHLIST_WRITE,
        Permission.STRATEGY_WRITE,
        Permission.BACKTEST_RUN,
        Permission.SCREENER_RUN,
        Permission.APIKEY_MANAGE,
    }
)

_TRADER: frozenset[Permission] = _RESEARCHER | frozenset(
    {
        Permission.ORDER_PLACE,
        Permission.ORDER_CANCEL,
        Permission.POSITION_SQUAREOFF,
        Permission.ALGO_START,
        Permission.ALGO_STOP,
    }
)

_ADMIN: frozenset[Permission] = _TRADER | frozenset(
    {
        Permission.RISK_CONFIGURE,
        Permission.EXECUTION_MODE_CHANGE,
        Permission.USER_READ,
        Permission.USER_WRITE,
        Permission.AUDIT_READ,
    }
)

_OWNER: frozenset[Permission] = frozenset(Permission)  # every permission, by construction

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.RESEARCHER: _RESEARCHER,
    Role.TRADER: _TRADER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _OWNER,
}

#: Roles that should carry a second factor when
#: ``settings.require_mfa_for_privileged`` is on. These roles can change risk
#: limits, flip the execution environment, and manage other accounts.
MFA_REQUIRED_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.OWNER})


class PermissionDenied(Exception):
    """Raised when a principal lacks a permission. The API maps this to 403."""

    def __init__(self, permission: Permission | str, role: Role | str | None = None) -> None:
        # `.value`, not `str()`. On Python 3.11+ `str()` on a `(str, Enum)` member
        # returns "Permission.ORDER_PLACE", not "order:place" — so a message built
        # with `str()` names the enum member instead of the permission, and any
        # client matching on the permission string silently stops matching.
        self.permission = getattr(permission, "value", str(permission))
        self.role = getattr(role, "value", None) if role is not None else None
        detail = f"missing permission {self.permission}"
        if self.role:
            detail += f" for role {self.role}"
        super().__init__(detail)


def permissions_for(role: Role | str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[Role.parse(role)]


def has_permission(role: Role | str, permission: Permission | str) -> bool:
    try:
        return Permission.parse(permission) in permissions_for(role)
    except ValueError:
        # An unknown permission is never granted. Fail closed.
        return False


def require(role: Role | str, permission: Permission | str) -> None:
    """Raise :class:`PermissionDenied` unless the role holds the permission."""
    if not has_permission(role, permission):
        raise PermissionDenied(permission, role)


def roles_with(permission: Permission | str) -> list[Role]:
    """Which roles grant a permission — used to explain a 403 in the UI."""
    return [role for role, perms in ROLE_PERMISSIONS.items() if has_permission(role, permission)]


__all__ = [
    "MFA_REQUIRED_ROLES",
    "ROLE_PERMISSIONS",
    "Permission",
    "PermissionDenied",
    "Role",
    "has_permission",
    "permissions_for",
    "require",
    "roles_with",
]
