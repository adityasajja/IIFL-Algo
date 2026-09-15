"""Screener routes — ``/api/v1/screener``.

The contract (``docs/API_CONTRACT.md``) is::

    GET    /universes              available universes and their sizes
    POST   /run                    { universe, conditions, limit } -> ranked rows
    POST   /validate               validate a condition tree without running it
    GET    /saved                  saved scans
    POST   /saved                  save a scan
    DELETE /saved/{id}
    GET    /saved/{id}/results     last results (runs it now — no scheduler yet)
    GET    /indicators             the indicator catalog
    GET    /columns                the display-column registry

Two deliberate omissions, both recorded rather than stubbed:

* ``POST /saved/{id}/schedule`` and ``WS /stream`` are not implemented. A
  scheduler that fires an HTTP request on a cron is a feature with real
  operational weight (missed windows, overlapping runs, what happens when the
  box was asleep) and building the endpoint without that would be a lie. The
  routes are absent so a client gets a clean 404 instead of a 202 that does
  nothing.
* ``GET /saved/{id}/results`` runs the scan live. There is no result cache to
  read from, and a stale result served as "last results" would be worse than no
  answer. It is named ``/results`` for contract compatibility and documented as
  live.

Everything is gated on ``Permission.SCREENER_RUN``. There is no separate
screener-read permission in the RBAC table, and inventing one here would put the
authority for a permission outside ``atr.auth.rbac``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from atr.api.deps import CurrentPrincipal, require_permission
from atr.auth.rbac import Permission
from atr.screener.conditions import ConditionError
from atr.screener.indicators import INDICATOR_CATALOG
from atr.screener.service import (
    COLUMN_DEFS,
    DEFAULT_SORT,
    SORT_FIELDS,
    ScreenerError,
    get_screener_service,
)

router = APIRouter(prefix="/api/v1/screener", tags=["screener"])

_RUN = Depends(require_permission(Permission.SCREENER_RUN))


def _fail(exc: ScreenerError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


def _bad_request(exc: ConditionError, where: str = "conditions") -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail={"detail": str(exc), "code": exc.code, "path": exc.path, "where": where},
    )


# --------------------------------------------------------------------- models
class ConditionTree(BaseModel):
    """A condition node: either a group or a leaf.

    Deliberately untyped beyond ``dict``. The tree is owned by
    ``atr.screener.conditions``, which already validates it thoroughly and
    produces far better messages than a Pydantic schema could — duplicating the
    grammar here would create a second definition of the same thing, and the two
    would drift. This model exists only to give FastAPI a body to bind.
    """

    model_config = {"extra": "allow"}


class RunRequest(BaseModel):
    universe: str = Field(default="all", max_length=64)
    conditions: Any = Field(default_factory=dict)
    exchange: str = Field(default="NSEEQ", max_length=16)
    symbols: list[str] | None = Field(default=None, max_length=500)
    columns: list[str] | None = Field(default=None, max_length=64)
    sort: str = Field(default=DEFAULT_SORT, max_length=32)
    descending: bool = True
    limit: int = Field(default=50, ge=1, le=500)


class ValidateRequest(BaseModel):
    conditions: Any = Field(default_factory=dict)


class SaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    definition: dict[str, Any] = Field(default_factory=dict)


class UpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    definition: dict[str, Any] | None = None


# --------------------------------------------------------------------- routes
@router.get("/universes", dependencies=[_RUN])
def universes(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Scannable universes, with the size actually backed by local history."""
    return {"exchange": exchange.upper(), "universes": get_screener_service().universes(exchange)}


@router.get("/indicators", dependencies=[_RUN])
def indicators() -> dict[str, Any]:
    """The indicator catalog.

    Unavailable indicators are returned with ``available: false`` and the
    capability they need, so the UI can grey them out and say why. They are
    never dropped from the list — a field that silently disappears is one the
    user cannot tell apart from a typo.
    """
    specs = [spec.as_dict() for spec in INDICATOR_CATALOG]
    return {
        "indicators": specs,
        "groups": sorted({s["group"] for s in specs}),
        "available": sorted(s["key"] for s in specs if s["available"]),
        "unavailable": sorted(s["key"] for s in specs if not s["available"]),
    }


@router.get("/columns", dependencies=[_RUN])
def columns() -> dict[str, Any]:
    """The display-column registry, and the sort keys that are permitted."""
    return {
        "columns": [{"key": k, "label": lbl, "indicator": ind} for k, lbl, ind in COLUMN_DEFS],
        "sort_fields": [{"key": k, "description": v} for k, v in sorted(SORT_FIELDS.items())],
        "default_sort": DEFAULT_SORT,
    }


@router.post("/validate", dependencies=[_RUN])
def validate(payload: ValidateRequest) -> dict[str, Any]:
    """Validate a tree without touching market data. Never 4xx for data problems.

    Returns ``valid: false`` plus the reason, because the caller is usually a UI
    validating as the user types and a 400 for "you have not finished typing" is
    the wrong shape of answer.
    """
    return get_screener_service().validate(payload.conditions)


@router.post("/run", dependencies=[_RUN])
def run(payload: RunRequest) -> dict[str, Any]:
    """Evaluate the tree over the universe and return ranked rows with evidence."""
    try:
        return get_screener_service().run(
            conditions=payload.conditions,
            universe=payload.universe,
            exchange=payload.exchange,
            symbols=payload.symbols,
            columns=payload.columns,
            sort=payload.sort,
            limit=payload.limit,
            descending=payload.descending,
        )
    except ConditionError as exc:
        raise _bad_request(exc) from exc
    except ScreenerError as exc:
        raise _fail(exc) from exc


@router.get("/saved", dependencies=[_RUN])
def list_saved(principal: CurrentPrincipal) -> dict[str, Any]:
    return {"scans": get_screener_service().list_saved(principal.user_id)}


@router.post("/saved", status_code=status.HTTP_201_CREATED, dependencies=[_RUN])
def save(payload: SaveRequest, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        return get_screener_service().save(
            principal.user_id,
            name=payload.name,
            definition=payload.definition,
            description=payload.description,
        )
    except ConditionError as exc:
        raise _bad_request(exc, where="definition.conditions") from exc
    except ScreenerError as exc:
        raise _fail(exc) from exc


@router.get("/saved/{scan_id}", dependencies=[_RUN])
def get_saved(scan_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        return get_screener_service().get_saved(principal.user_id, scan_id)
    except ScreenerError as exc:
        raise _fail(exc) from exc


@router.put("/saved/{scan_id}", dependencies=[_RUN])
def update_saved(
    scan_id: str, payload: UpdateRequest, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Edit a saved scan. Absent fields are left alone.

    All the work happens in the service, including the ownership check and the
    definition validation. A route that opened its own session and called a
    repository directly would move the ownership check out of the one place the
    other saved-scan routes already do it.
    """
    try:
        return get_screener_service().update_saved(
            principal.user_id,
            scan_id,
            name=payload.name,
            description=payload.description,
            definition=payload.definition,
        )
    except ConditionError as exc:
        raise _bad_request(exc, where="definition.conditions") from exc
    except ScreenerError as exc:
        raise _fail(exc) from exc


@router.delete("/saved/{scan_id}", dependencies=[_RUN])
def delete_saved(scan_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        get_screener_service().delete_saved(principal.user_id, scan_id)
    except ScreenerError as exc:
        raise _fail(exc) from exc
    return {"deleted": scan_id}


@router.get("/saved/{scan_id}/results", dependencies=[_RUN])
def saved_results(
    scan_id: str,
    principal: CurrentPrincipal,
    limit: int = 50,
    sort: str | None = None,
) -> dict[str, Any]:
    """Run a saved scan.

    Live, not cached — see the module docstring. ``limit`` and ``sort`` may be
    overridden per call without mutating the stored definition, so "show me more
    of this screen" does not rewrite what is saved.
    """
    overrides: dict[str, Any] = {"limit": limit}
    if sort:
        overrides["sort"] = sort
    try:
        return get_screener_service().run_saved(principal.user_id, scan_id, **overrides)
    except ConditionError as exc:
        raise _bad_request(exc, where="saved definition") from exc
    except ScreenerError as exc:
        raise _fail(exc) from exc


__all__ = ["router"]
