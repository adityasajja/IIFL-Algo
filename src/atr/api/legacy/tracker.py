"""The forward test of the oversold-and-volatile pattern, for the Today page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from atr.market_intel.service import DATA_ROOT
from atr.research.forward_tracker import Store, store_path, summary

router = APIRouter()


@router.get("/tracker/oversold-volatile")
def forward_tracker() -> dict[str, Any]:
    """Running score of the pattern, graded only on weeks that had not happened when it was recorded."""
    return summary(Store(store_path(DATA_ROOT)))
