"""Daily profit-and-loss calendars: one for paper trades, one for the real book."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from atr.api.deps import get_principal
from atr.insights.holdings import load_holdings
from atr.market_intel.service import DATA_ROOT
from atr.services import pnl_calendar

router = APIRouter()


class RealisedIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount: float = Field(ge=-1e9, le=1e9, description="Profit (or loss, negative) made by trading that day, in rupees")
    note: str = Field("", max_length=200)


@router.get("/pnl/calendar", dependencies=[Depends(get_principal)])
def pnl_calendar_month(
    scope: Literal["paper", "real"],
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$", description="YYYY-MM; defaults to the latest month with data"),
) -> dict[str, Any]:
    """One month of daily P&L. ``paper`` adds up every paper trade; ``real`` is the holdings' daily change."""
    days = pnl_calendar.paper_days(DATA_ROOT) if scope == "paper" else pnl_calendar.real_days(DATA_ROOT)
    if month is None:
        month = max((d[:7] for d in days), default=date.today().strftime("%Y-%m"))
    try:
        date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(400, "month must be a real YYYY-MM") from exc
    view = pnl_calendar.month_view(days, month)
    view["scope"] = scope
    view["ticket"] = pnl_calendar.VIRTUAL_TICKET if scope == "paper" else None
    if scope == "real":
        held = load_holdings(DATA_ROOT)
        view["holdings_source"] = held["source"]
        view["unpriced"] = pnl_calendar.unpriced_holdings(DATA_ROOT, held["holdings"])
    return view


@router.get("/pnl/day", dependencies=[Depends(get_principal)])
def pnl_day(
    scope: Literal["paper", "real"],
    day: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    """What is behind one calendar day: the trades that closed (paper) or the holdings that moved (real)."""
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(400, "date must be a real YYYY-MM-DD") from exc
    if scope == "paper":
        rows = pnl_calendar.paper_day_detail(DATA_ROOT, day)
        return {"scope": scope, "date": day, "rows": rows, "total": round(sum(r["pnl"] for r in rows), 2),
                "gainers": sum(1 for r in rows if r["pnl"] > 0), "losers": sum(1 for r in rows if r["pnl"] < 0), "unpriced": []}
    held = load_holdings(DATA_ROOT)
    detail = pnl_calendar.real_day_detail(DATA_ROOT, held["holdings"], day)
    return {"scope": scope, "date": day, **detail, "holdings_source": held["source"]}


@router.put("/pnl/realised", dependencies=[Depends(get_principal)])
def record_trading_profit(body: RealisedIn) -> dict[str, Any]:
    """Record what trading made (or lost) on a day. Replaces any earlier entry for that day."""
    try:
        day = date.fromisoformat(body.date)
    except ValueError as exc:
        raise HTTPException(400, "date must be a real YYYY-MM-DD") from exc
    if day > date.today():
        raise HTTPException(400, "that day has not happened yet")
    pnl_calendar.set_realised(DATA_ROOT, body.date, body.amount, note=body.note)
    return {"date": body.date, "amount": body.amount}


@router.delete("/pnl/realised", dependencies=[Depends(get_principal)])
def remove_trading_profit(day: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")) -> dict[str, bool]:
    return {"removed": pnl_calendar.clear_realised(DATA_ROOT, day)}
