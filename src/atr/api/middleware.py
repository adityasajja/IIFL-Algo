"""HTTP middleware: request identity, security headers, CSRF, rate limiting.

Installed through :func:`install_middleware`, which must be called **before** the
CORS middleware is registered — Starlette wraps in reverse registration order, so
the last one added ends up outermost. CORS has to be outermost or an error
response generated here would arrive without CORS headers and the browser would
report a network failure instead of the real 429/403.
"""

from __future__ import annotations

import logging
import time
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from atr.auth.rate_limit import get_rate_limiter
from atr.config.settings import get_settings

logger = logging.getLogger("atr.api.middleware")

#: Paths that must never be rate limited or charged headers on the hot path.
_STATIC_PREFIXES = ("/assets/", "/favicon", "/static/", "/@vite", "/@react-refresh")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Origins allowed to make credentialed *cross-origin* requests. This is the
#: `atr dev` case only: Vite serves the SPA on :5173 while FastAPI listens on
#: :8000, so the page's origin genuinely differs from the API's.
#:
#: This list is deliberately **not** the same-origin rule. Same-origin requests
#: are handled by comparing against the `Host` header — see `_same_origin`.
_ALLOWED_ORIGINS = frozenset(
    {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }
)


def _same_origin(origin: str, request: Request) -> bool:
    """Whether ``origin`` is the origin the browser actually connected to.

    Browsers send ``Origin`` on **every** unsafe method, including same-origin
    ones — this is not a CORS-only header. So the check has to answer "is this
    the page's own origin?", and comparing against the request's ``Host`` is what
    makes that work on whatever port the operator chose.

    Without this, the dashboard only worked on the handful of ports hardcoded
    above: `atr serve --port 8123` served the SPA and the API from
    ``http://127.0.0.1:8123``, the browser sent that as ``Origin``, it was not in
    the list, and every write was rejected with ``csrf_origin_rejected`` — a
    same-origin request refused as cross-origin.
    """
    if origin == "null":
        # A sandboxed iframe or a `file://` page. No legitimate caller here.
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    host = request.headers.get("host")
    if not host:
        # Cannot verify, so refuse rather than assume.
        return False
    return parsed.netloc.lower() == host.lower()

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    # 'unsafe-inline' for styles only: React sets style attributes, and Tailwind
    # injects a stylesheet. Scripts stay locked to 'self'.
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


def _is_static(path: str) -> bool:
    return path.startswith(_STATIC_PREFIXES)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, and set security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "").strip()
        # Echo a client-supplied id so a browser trace and a server log line can
        # be joined, but bound it — an unbounded header would be written straight
        # into the audit table and the logs.
        request_id = incoming[:32] if incoming else uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.monotonic()

        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.monotonic() - started) * 1000
            logger.exception(
                "%s %s failed after %.0fms [rid=%s]", request.method, request.url.path, elapsed, request_id
            )
            raise

        elapsed = (time.monotonic() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed:.0f}"

        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not _is_static(request.url.path):
            headers.setdefault("Content-Security-Policy", _CSP)
        if get_settings().env == "live":
            # Only meaningful over TLS; asserting it on a plain-HTTP dev host
            # would break the app and teach the operator to ignore it.
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Origin check for cookie-authenticated, state-changing requests.

    Only cookie auth needs this. A bearer token or an ``X-API-Key`` header is not
    attached automatically by the browser, so there is no ambient authority for a
    cross-site page to borrow — that is precisely why the check is scoped to the
    cookie path rather than applied to everything.

    ``SameSite=Lax`` on the session cookie already blocks the classic cross-site
    form POST; this is the second layer for the cases ``Lax`` does not cover.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        has_explicit_credential = bool(
            request.headers.get("authorization") or request.headers.get("x-api-key")
        )
        uses_cookie = bool(request.cookies.get("atr_session"))

        if uses_cookie and not has_explicit_credential:
            origin = request.headers.get("origin")
            if origin and origin not in _ALLOWED_ORIGINS and not _same_origin(origin, request):
                logger.warning(
                    "rejected cross-origin %s %s from origin %s (host %s)",
                    request.method,
                    request.url.path,
                    origin,
                    request.headers.get("host"),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "cross-origin request rejected",
                        "code": "csrf_origin_rejected",
                    },
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket limiting per client address, with a stricter login bucket.

    Fails open on internal error and closed on a real breach: a bug in the
    limiter must not take the trading API down, but a flood must still be refused.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if _is_static(path) or path.startswith("/ws/"):
            return await call_next(request)

        settings = get_settings()
        client = request.client.host if request.client else "unknown"
        is_auth_route = "/auth/login" in path or "/auth/bootstrap" in path
        per_minute = (
            settings.rate_limit_login_per_minute if is_auth_route else settings.rate_limit_per_minute
        )
        scope = "auth" if is_auth_route else "api"

        allowed, retry_after = get_rate_limiter().check(
            f"{scope}:{client}", per_minute=per_minute
        )
        if not allowed:
            logger.warning("rate limited %s %s from %s", request.method, path, client)
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": f"{retry_after:.0f}"},
                content={
                    "detail": "too many requests",
                    "code": "rate_limited",
                    "retry_after": round(retry_after, 2),
                },
            )
        return await call_next(request)


def install_middleware(app: FastAPI) -> None:
    """Register middleware. Call before ``add_middleware(CORSMiddleware, ...)``."""
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RequestContextMiddleware)


__all__ = [
    "CSRFMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "install_middleware",
]
