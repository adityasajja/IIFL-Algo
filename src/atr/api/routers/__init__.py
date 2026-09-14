"""Versioned API routers (``/api/v1``).

The legacy unversioned routes in :mod:`atr.api.main` are untouched. These are the
new surface: each route declares the permission it needs as a dependency, so the
check cannot be forgotten in the body.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from atr.api.routers import (
    audit,
    auth,
    instruments,
    orders,
    paper,
    reconciliation,
    risk,
    watchlists,
)

ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    watchlists.router,
    instruments.router,
    audit.router,
    orders.router,
    risk.router,
    paper.router,
    reconciliation.router,
)


def include_routers(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)


__all__ = ["ROUTERS", "include_routers"]
