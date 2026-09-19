"""FastAPI control plane.

Exposes the engine over HTTP so you can drive it from a dashboard, a scheduler,
or a notebook. Live endpoints refuse to act unless ``env`` is paper/live and a
session exists — the API is a convenience layer, not a risk control.

This file only assembles the app. The routes live in ``atr.api.routers`` (the
versioned API) and ``atr.api.legacy`` (the root-path routes, grouped by feature).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atr.api.legacy import (
    alerts,
    backtest,
    briefing,
    dashboard,
    frontend,
    login,
    market_data,
    orders,
    research,
    risk,
    scanner,
    signals,
    startup,
    system,
    tracker,
    validation,
)
from atr.api.middleware import install_middleware
from atr.api.routers import include_routers

app = FastAPI(title="ATR — algo trading backend", version="0.1.0")
app.router.add_event_handler("startup", startup.on_startup)

# Request identity, security headers, CSRF and rate limiting. Registered *before*
# CORS so that CORS ends up outermost — Starlette wraps in reverse registration
# order, and a 429 or 403 produced by our middleware must still carry CORS
# headers or the browser reports a network failure instead of the real reason.
install_middleware(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes match in registration order: the versioned API first, the root-path routes
# next, and the web app's catch-all last so it cannot swallow either.
include_routers(app)
for module in (
    system, backtest, orders, risk, signals, alerts, briefing, scanner,
    market_data, research, validation, dashboard, login, tracker,
):
    app.include_router(module.router)

# Consumed by the watchlist router through ``request.app.state`` rather than an
# import, because that router is imported by this package and importing back would
# be circular.
app.state.live_quote_provider = market_data.watchlist_live_quotes

frontend.mount_frontend(app)
