"""Watchlist routes — ``/api/v1/watchlists``.

Every route is user-scoped. The service filters on ``user_id`` rather than
checking after the fact, so a watchlist id belonging to someone else behaves
exactly like one that does not exist (404, never 403).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from atr.api.deps import CurrentPrincipal, require_permission
from atr.auth.rbac import Permission
from atr.services.watchlists import WatchlistError, WatchlistService

router = APIRouter(prefix="/api/v1/watchlists", tags=["watchlists"])

_WRITE = Depends(require_permission(Permission.WATCHLIST_WRITE))
_READ = Depends(require_permission(Permission.WATCHLIST_READ))


def _service() -> WatchlistService:
    return WatchlistService()


def _fail(exc: WatchlistError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"detail": "no such watchlist", "code": "not_found"},
    )


# --------------------------------------------------------------------- models
class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    exchange: str = Field(default="NSEEQ", max_length=16)
    columns: list[str] | None = None


class WatchlistUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    exchange: str | None = Field(default=None, max_length=16)
    is_default: bool | None = None


class ItemsAdd(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=500)


class ItemsOrder(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=500)


class ColumnsSet(BaseModel):
    columns: list[str] = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------- routes
@router.get("/columns/available", dependencies=[_READ])
def available_columns() -> dict[str, Any]:
    """The column registry.

    Includes columns that cannot be populated yet, each with the reason. The UI
    renders them as unavailable rather than as zero — a zero looks like a
    measurement, and this platform does not do that.
    """
    specs = WatchlistService.available_columns()
    return {
        "columns": specs,
        "groups": sorted({s["group"] for s in specs}),
        "available": sorted(s["key"] for s in specs if s["available"]),
        "unavailable": sorted(s["key"] for s in specs if not s["available"]),
    }


@router.get("", dependencies=[_READ])
@router.get("/", dependencies=[_READ], include_in_schema=False)
def list_watchlists(principal: CurrentPrincipal) -> dict[str, Any]:
    return {"watchlists": _service().list_watchlists(principal.user_id)}


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[_WRITE])
@router.post("/", status_code=status.HTTP_201_CREATED, include_in_schema=False, dependencies=[_WRITE])
def create_watchlist(payload: WatchlistCreate, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        return _service().create(
            principal.user_id,
            name=payload.name,
            exchange=payload.exchange,
            columns=payload.columns,
        )
    except WatchlistError as exc:
        raise _fail(exc) from exc


@router.get("/{watchlist_id}", dependencies=[_READ])
def get_watchlist(watchlist_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    row = _service().get(principal.user_id, watchlist_id)
    if row is None:
        raise _not_found()
    return row


@router.patch("/{watchlist_id}", dependencies=[_WRITE])
def update_watchlist(
    watchlist_id: str, payload: WatchlistUpdate, principal: CurrentPrincipal
) -> dict[str, Any]:
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        row = _service().get(principal.user_id, watchlist_id)
        if row is None:
            raise _not_found()
        return row
    try:
        if fields.get("is_default"):
            row = _service().set_default(principal.user_id, watchlist_id)
        else:
            fields.pop("is_default", None)
            row = _service().update(principal.user_id, watchlist_id, **fields)
    except WatchlistError as exc:
        raise _fail(exc) from exc
    if row is None:
        raise _not_found()
    return row


@router.delete("/{watchlist_id}", dependencies=[_WRITE])
def delete_watchlist(watchlist_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    if not _service().delete(principal.user_id, watchlist_id):
        raise _not_found()
    return {"deleted": True, "watchlist_id": watchlist_id}


@router.post("/{watchlist_id}/items", dependencies=[_WRITE])
def add_items(watchlist_id: str, payload: ItemsAdd, principal: CurrentPrincipal) -> dict[str, Any]:
    """Add symbols. Unknown tickers are reported, not silently dropped."""
    try:
        return _service().add_items(principal.user_id, watchlist_id, payload.symbols)
    except WatchlistError as exc:
        raise _fail(exc) from exc


@router.delete("/{watchlist_id}/items/{symbol}", dependencies=[_WRITE])
def remove_item(watchlist_id: str, symbol: str, principal: CurrentPrincipal) -> dict[str, Any]:
    if not _service().remove_item(principal.user_id, watchlist_id, symbol):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"detail": "symbol not in this watchlist", "code": "not_found"},
        )
    return {"removed": symbol.upper()}


@router.put("/{watchlist_id}/items/order", dependencies=[_WRITE])
def reorder_items(
    watchlist_id: str, payload: ItemsOrder, principal: CurrentPrincipal
) -> dict[str, Any]:
    """Set the full order. Idempotent, so a retry cannot reorder twice."""
    row = _service().reorder(principal.user_id, watchlist_id, payload.symbols)
    if row is None:
        raise _not_found()
    return row


@router.put("/{watchlist_id}/columns", dependencies=[_WRITE])
def set_columns(
    watchlist_id: str, payload: ColumnsSet, principal: CurrentPrincipal
) -> dict[str, Any]:
    try:
        row = _service().set_columns(principal.user_id, watchlist_id, payload.columns)
    except WatchlistError as exc:
        raise _fail(exc) from exc
    if row is None:
        raise _not_found()
    return row


@router.get("/{watchlist_id}/quotes", dependencies=[_READ])
def watchlist_quotes(
    watchlist_id: str, request: Request, principal: CurrentPrincipal, live: bool = True
) -> dict[str, Any]:
    """Every configured column for every symbol.

    ``live=true`` (the default) asks the broker for prices and falls back to the
    cached close per symbol when the broker cannot answer. Each row carries
    ``stale`` so a cached price is never presented as a live one.
    """
    service = _service()
    detail = service.get(principal.user_id, watchlist_id)
    if detail is None:
        raise _not_found()

    overlay: dict[str, dict[str, Any]] = {}
    if live and detail["items"]:
        provider = getattr(request.app.state, "live_quote_provider", None)
        if provider is not None:
            try:
                overlay = provider(detail["items"], detail["exchange"]) or {}
            except Exception:  # noqa: BLE001 - a dead broker must not blank the table
                overlay = {}

    result = service.quotes(principal.user_id, watchlist_id, live=overlay)
    if result is None:
        raise _not_found()
    return result
