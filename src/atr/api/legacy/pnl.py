"""Daily profit-and-loss calendars: one for paper trades, one for the real book."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from atr.api.deps import get_principal
from atr.insights.holdings import load_holdings
from atr.market_intel.service import DATA_ROOT
from atr.services import pnl_calendar

router = APIRouter()


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
