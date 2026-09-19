"""Broker login and its callback."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from atr.config.settings import Settings, get_settings

logger = logging.getLogger("atr.api")

router = APIRouter()


# ----------------------------------------------------------------------
# Session / login
# ----------------------------------------------------------------------
class LoginIn(BaseModel):
    client_id: str
    auth_code: str


def _login_client():
    from atr.brokers.iifl.auth import SessionStore
    from atr.brokers.iifl.client import IiflClient

    settings: Settings = get_settings()
    return IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
        session_store=SessionStore(settings.iifl_session_cache),
    )


def _session_info(settings: Settings | None = None) -> dict[str, Any]:
    from atr.brokers.iifl.auth import SessionStore, login_url

    settings = settings or get_settings()
    store = SessionStore(settings.iifl_session_cache)
    session = store.load()
    return {
        "session_active": session is not None,
        "client_id": session.client_id if session else None,
        "expires_at": session.expires_at.isoformat() if session else None,
        "login_url": login_url(settings.iifl_app_key, settings.iifl_redirect_url),
    }


@router.get("/login/status")
def login_status() -> dict[str, Any]:
    return _session_info()


@router.post("/login")
def login_submit(body: LoginIn) -> dict[str, Any]:
    """Manual login: exchange clientId + authCode for a session in one call."""
    client = _login_client()
    try:
        session = client.create_session(body.client_id.strip(), body.auth_code.strip())
    except Exception as exc:  # noqa: BLE001 - surface IIFL error text
        raise HTTPException(400, f"login failed: {exc}") from exc
    return {
        "session_active": True,
        "client_id": session.client_id,
        "expires_at": session.expires_at.isoformat(),
    }


_LOGIN_CALLBACK_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>ATR — login</title>
<style>
  body { font-family: -apple-system, Segoe UI, Inter, sans-serif; background: #0f1319; color: #e6e9ef;
         display: grid; place-items: center; min-height: 100vh; margin: 0; }
  .card { background: #161b22; border: 1px solid #26d3; border-radius: 12px; padding: 28px 36px; max-width: 420px; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  p { color: #9aa4b2; margin: 6px 0; }
  .ok { color: #26a69a; }
  .err { color: #ef5350; }
  button { margin-top: 14px; padding: 8px 16px; border: 0; border-radius: 8px; background: #635bff; color: white; cursor: pointer; }
</style>
<script>setTimeout(function() {{ if (window.opener) window.close(); }}, 1200);</script>
</head>
<body>
"""


@router.get("/login/callback")
def login_callback(
    # IIFL actually sends lowercase, unseparated names:
    #   /login/callback?authcode=...&clientid=...
    # The camelCase spellings are what the README claimed and what the docs
    # suggest, and the snake_case ones are ours. Accepting all of them costs
    # nothing and stops a documentation guess from breaking the only path the
    # broker controls.
    authcode: str | None = Query(None),
    clientid: str | None = Query(None),
    authCode: str | None = Query(None),  # noqa: N803 - IIFL's documented casing
    clientId: str | None = Query(None),  # noqa: N803 - IIFL's documented casing
    auth_code: str | None = Query(None),
    client_id: str | None = Query(None),
) -> HTMLResponse:
    """Landing page for the IIFL redirect. Exchanges the code for a session.

    Declaring these as required `client_id`/`auth_code` meant every real
    redirect came back 422 "Field required" as raw JSON, and no session was
    ever created — the login could not complete by any route through the
    browser.
    """
    auth = authcode or authCode or auth_code
    cid = clientid or clientId or client_id

    if not auth or not cid:
        return HTMLResponse(
            _LOGIN_CALLBACK_HTML
            + '<div class="card"><h1>Login failed</h1>'
            + '<p class="err">The redirect did not carry a client id and auth code.</p>'
            + "<p>Expected <code>?authcode=...&amp;clientid=...</code>, which is what "
            + "markets.iiflcapital.com sends. If you opened this URL by hand, check "
            + "the parameter names.</p></div>",
            status_code=400,
        )

    client = _login_client()
    try:
        session = client.create_session(cid, auth)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            _LOGIN_CALLBACK_HTML
            + f'<div class="card"><h1>Login failed</h1><p class="err">{exc}</p>'
            + "<p>Close this tab and try again from the dashboard.</p></div>",
            status_code=400,
        )
    return HTMLResponse(
        _LOGIN_CALLBACK_HTML
        + '<div class="card"><h1 class="ok">Logged in ✓</h1>'
        + f"<p>Client: <strong>{session.client_id}</strong></p>"
        + f"<p>Expires: <strong>{session.expires_at.strftime('%d-%b-%Y %H:%M')} IST</strong></p>"
        + '<p>You can close this tab and return to the dashboard.</p>'
        + "<button onclick='window.close()'>Close</button></div>"
    )
