"""Authentication routes — ``/api/v1/auth``.

The cookie and the response body both carry the session token. The cookie is for
the browser; the body value is for a script or a mobile client that has no cookie
jar. Both are the same opaque token, and both are revocable server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from atr.api.deps import (
    SESSION_COOKIE,
    CurrentPrincipal,
    auth_error_to_http,
    client_ip,
    require_permission,
)
from atr.auth.rbac import Permission, Role, permissions_for
from atr.auth.service import AuthError, get_auth_service
from atr.config.settings import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# --------------------------------------------------------------------- models
class BootstrapRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    username: str = Field(min_length=2, max_length=64)
    #: Bounded for request size only. The policy itself lives in
    #: ``atr.auth.passwords.strength_problems`` — one authority, so the schema and
    #: the service can never disagree about what "too weak" means, and a rejection
    #: carries the documented ``weak_password`` code instead of a generic 422.
    password: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)


class RegisterRequest(BootstrapRequest):
    pass


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=255, description="email or username")
    password: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, max_length=16)


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=4, max_length=16)


class MfaDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ApiKeyCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class UserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=128)


# -------------------------------------------------------------------- helpers
def _set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    settings = get_settings()
    # Starlette formats `expires` with `usegmt=True`, which raises on a naive
    # datetime. Every timestamp in this codebase is naive-UTC by design, so
    # attach the offset here rather than changing the storage convention.
    aware = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        # HttpOnly so an XSS cannot read it; SameSite=Lax so a cross-site form
        # POST cannot ride it; Secure only when the host is actually on TLS,
        # because asserting it over plain HTTP silently breaks the cookie.
        httponly=True,
        samesite="lax",
        secure=settings.env == "live",
        expires=aware,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# --------------------------------------------------------------------- routes
@router.get("/bootstrap")
def bootstrap_status() -> dict[str, Any]:
    """Whether this instance still needs its first account.

    Public on purpose: the first-run screen has no credential to present, and the
    answer reveals only whether the platform has been set up.
    """
    service = get_auth_service()
    settings = get_settings()
    return {
        "needs_setup": service.needs_bootstrap(),
        "allow_signup": settings.allow_signup,
        "env": settings.env,
        "auth_required": settings.auth_required,
    }


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
def bootstrap(payload: BootstrapRequest, request: Request, response: Response) -> dict[str, Any]:
    """Create the first account as owner. Refused once any account exists."""
    service = get_auth_service()
    try:
        user, token, expires_at = service.bootstrap(
            email=payload.email,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=_request_id(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    _set_session_cookie(response, token, expires_at)
    return {"token": token, "expires_at": expires_at, "user": user}


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request) -> dict[str, Any]:
    """Self-registration. Disabled unless ``ATR_ALLOW_SIGNUP`` is on.

    New accounts are always ``viewer``; the role is not taken from the request.
    """
    settings = get_settings()
    if not settings.allow_signup:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "sign-up is disabled on this instance",
                "code": "signup_disabled",
            },
        )
    service = get_auth_service()
    try:
        user = service.register(
            email=payload.email,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            role=Role.VIEWER,
            ip=client_ip(request),
            request_id=_request_id(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    return {"user": user}


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> Any:
    """Password login, with a TOTP step when the account has MFA enabled.

    Returns ``202`` with ``mfa_required`` when the password was accepted but the
    second factor is still owed. That is not a 401 — the credentials were right,
    and a client needs to tell the difference to know whether to show a code field.
    """
    service = get_auth_service()
    result = service.login(
        identifier=payload.identifier,
        password=payload.password,
        totp_code=payload.totp_code,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=_request_id(request),
    )

    if result.status == "mfa_required":
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"mfa_required": True, "detail": result.reason},
        )
    if not result.ok:
        raise auth_error_to_http(
            AuthError(result.reason or "invalid credentials", code=result.status, status=401)
        )

    assert result.token and result.expires_at  # noqa: S101 - guaranteed by status == ok
    _set_session_cookie(response, result.token, result.expires_at)
    return {"token": result.token, "expires_at": result.expires_at, "user": result.user}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    from atr.api.deps import credential_from

    credential, source = credential_from(request)
    removed = get_auth_service().logout(credential if source != "api-key" else None, ip=client_ip(request))
    _clear_session_cookie(response)
    return {"logged_out": removed}


@router.post("/logout-all")
def logout_all(request: Request, response: Response, principal: CurrentPrincipal) -> dict[str, Any]:
    count = get_auth_service().logout_all(principal.user_id, actor=principal.actor, ip=client_ip(request))
    _clear_session_cookie(response)
    return {"sessions_revoked": count}


@router.get("/me")
def me(principal: CurrentPrincipal) -> dict[str, Any]:
    """The caller's profile and effective permissions.

    Deliberately reachable by a session that has not satisfied MFA — this is how
    the client discovers it needs to enrol.
    """
    return {
        **principal.as_dict(),
        "role_permissions": sorted(p.value for p in permissions_for(principal.role)),
    }


@router.patch("/me")
def update_me(
    payload: ProfileUpdateRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        user = get_auth_service().update_profile(
            principal.user_id,
            display_name=payload.display_name,
            email=payload.email,
            ip=client_ip(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    if user is None:
        raise auth_error_to_http(AuthError("no such user", code="not_found", status=404))
    return {"user": user}


@router.post("/me/password")
def change_password(
    payload: PasswordChangeRequest, request: Request, response: Response, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        get_auth_service().change_password(
            principal.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            ip=client_ip(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    # Every session was revoked, including this one — the client must log in again.
    _clear_session_cookie(response)
    return {"changed": True, "sessions_revoked": True}


@router.post("/me/mfa/enroll")
def mfa_enroll(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        secret, uri = get_auth_service().enroll_mfa(principal.user_id, ip=client_ip(request))
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "enabled": False,
        "next": "scan the URI, then POST /api/v1/auth/me/mfa/activate with a code",
    }


@router.post("/me/mfa/activate")
def mfa_activate(
    payload: MfaCodeRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        ok = get_auth_service().activate_mfa(principal.user_id, payload.code, ip=client_ip(request))
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    if not ok:
        raise auth_error_to_http(AuthError("that code did not verify", code="bad_code", status=400))
    return {"enabled": True}


@router.post("/me/mfa/disable")
def mfa_disable(
    payload: MfaDisableRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        ok = get_auth_service().disable_mfa(principal.user_id, payload.password, ip=client_ip(request))
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    if not ok:
        raise auth_error_to_http(AuthError("no such user", code="not_found", status=404))
    return {"enabled": False}


@router.get("/me/sessions")
def my_sessions(principal: CurrentPrincipal) -> dict[str, Any]:
    rows = get_auth_service().list_sessions(principal.user_id)
    return {
        "sessions": [
            {
                "session_id": r["session_id"],
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
                "expires_at": r["expires_at"],
                "ip": r["ip"],
                "user_agent": r["user_agent"],
                "current": r["session_id"] == principal.session_id,
            }
            for r in rows
        ]
    }


@router.delete("/me/sessions/{session_id}")
def revoke_session(session_id: str, request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    ok = get_auth_service().revoke_session(principal.user_id, session_id, ip=client_ip(request))
    if not ok:
        # 404 rather than 403: confirming that a session id exists but belongs to
        # someone else is an information leak with no upside.
        raise auth_error_to_http(AuthError("no such session", code="not_found", status=404))
    return {"revoked": True}


@router.get("/me/api-keys", dependencies=[Depends(require_permission(Permission.APIKEY_MANAGE))])
def list_api_keys(principal: CurrentPrincipal) -> dict[str, Any]:
    return {"keys": get_auth_service().list_api_keys(principal.user_id)}


@router.post(
    "/me/api-keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.APIKEY_MANAGE))],
)
def create_api_key(
    payload: ApiKeyCreateRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        key = get_auth_service().create_api_key(
            principal.user_id,
            label=payload.label,
            scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
            ip=client_ip(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    key["warning"] = "store this now — it is not retrievable"
    return key


@router.delete(
    "/me/api-keys/{key_id}", dependencies=[Depends(require_permission(Permission.APIKEY_MANAGE))]
)
def revoke_api_key(key_id: str, request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    ok = get_auth_service().revoke_api_key(principal.user_id, key_id, ip=client_ip(request))
    if not ok:
        raise auth_error_to_http(AuthError("no such key", code="not_found", status=404))
    return {"revoked": True}


@router.get("/users", dependencies=[Depends(require_permission(Permission.USER_READ))])
def list_users() -> dict[str, Any]:
    return {"users": get_auth_service().list_users()}


@router.patch("/users/{user_id}", dependencies=[Depends(require_permission(Permission.USER_WRITE))])
def update_user(
    user_id: str, payload: UserUpdateRequest, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        user = get_auth_service().update_user(
            principal,
            user_id,
            role=payload.role,
            is_active=payload.is_active,
            display_name=payload.display_name,
            ip=client_ip(request),
        )
    except AuthError as exc:
        raise auth_error_to_http(exc) from exc
    if user is None:
        raise auth_error_to_http(AuthError("no such user", code="not_found", status=404))
    return {"user": user}
