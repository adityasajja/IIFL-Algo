"""Momentum, full-market and custom scans, and saved scans."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from atr.api.deps import get_principal

from atr.api.legacy.common import _authed_client

logger = logging.getLogger("atr.api")

router = APIRouter()


@router.get("/scan")
def scan(symbols: str | None = None) -> dict[str, Any]:
    """Momentum scan over liquid NSE names (real IIFL daily candles).

    Pass `?symbols=RELIANCE-EQ,INFY-EQ` to override the default universe.
    Takes ~30-60s for the full universe — the dashboard shows a spinner.
    """
    from datetime import date

    from atr.scanner import UNIVERSE, run_scan

    client = _authed_client()
    wanted = (
        [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if symbols
        else UNIVERSE
    )
    rows, errors = run_scan(client, wanted)
    return {"as_of": date.today().isoformat(), "rows": rows, "errors": errors}


def _frames_as_of(frames: dict[str, Any]) -> str:
    """The date of the newest bar in the data, not today's date.

    A scan over a cache that stopped updating last week must say so.
    """
    newest = None
    for df in frames.values():
        try:
            ts = df["ts"].iloc[-1]
        except Exception:  # noqa: BLE001 - a frame without timestamps is skipped
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest.date().isoformat() if newest is not None else ""


_SCAN_CACHE: dict[str, Any] = {"data": None, "as_of": 0.0, "exchange": ""}


@router.get("/scan-all")
def scan_all(exchange: str = "NSEEQ") -> dict[str, Any]:
    """Full-market scan over the local history cache (`atr history sync`).

    No broker session needed and no API calls — scores 2000+ names in
    seconds. Caches results in-memory for 120 seconds to make UI tab
    switches instant.
    """
    import time

    import pandas as pd

    from atr.data.history import load_cached
    from atr.scanner import score_frame

    now = time.monotonic()
    ex = exchange.upper()
    if (
        _SCAN_CACHE["data"] is not None
        and _SCAN_CACHE["exchange"] == ex
        and now - float(_SCAN_CACHE["as_of"]) < 120.0
    ):
        return _SCAN_CACHE["data"]

    frames = load_cached(ex)
    if not frames:
        raise HTTPException(503, "history cache is empty — run `atr history sync`")
    rows = []
    for symbol, df in frames.items():
        try:
            rows.append(score_frame(symbol, df))
        except Exception:  # noqa: BLE001 — thin/odd histories just don't rank
            continue
    scan = pd.DataFrame(rows).sort_values("score", ascending=False)
    up = int((scan["trend"] == "UP").sum())
    result = {
        "as_of": _frames_as_of(frames),
        "universe": len(frames),
        "scored": len(scan),
        "breadth_up": up,
        "rows": scan.to_dict(orient="records"),
    }
    _SCAN_CACHE["data"] = result
    _SCAN_CACHE["as_of"] = now
    _SCAN_CACHE["exchange"] = ex
    return result


# ---------------------------------------------------------------------------
# Custom condition-based scanner
# ---------------------------------------------------------------------------

class CustomCondition(BaseModel):
    indicator: str                      # "rsi", "sma", "close", etc.
    period: int | None = None           # period for sma/ema/rsi/atr/bb_*
    op: str                             # ">", "<", ">=", "<=", "=", "crosses_above", "crosses_below"
    rhs_type: str = "value"             # "value" | "indicator"
    rhs_value: float = 0.0             # used when rhs_type == "value"
    rhs_indicator: str | None = None   # used when rhs_type == "indicator"
    rhs_period: int | None = None      # used when rhs_type == "indicator"


class CustomScanRequest(BaseModel):
    conditions: list[CustomCondition] = Field(default_factory=list)
    combine: str = "AND"               # "AND" | "OR"
    exchange: str = "NSEEQ"
    universe: str = "all"              # "all" | "watchlist"
    watchlist: list[str] = Field(default_factory=list)


@router.post("/scanner/custom", dependencies=[Depends(get_principal)])
def scanner_custom(body: CustomScanRequest) -> dict[str, Any]:
    """Evaluate user-defined indicator conditions over the local Parquet cache.

    No broker session required — runs entirely against the cached dailies.
    Returns matching symbols with standard score metrics + condition values.
    """
    import time

    from atr.data.history import load_cached
    from atr.scanner import UNIVERSE
    from atr.scanner_custom import run_custom_scan

    if not body.conditions:
        raise HTTPException(400, "provide at least one condition")

    ex = body.exchange.upper()
    t0 = time.monotonic()

    # Load frames — for "watchlist" mode only load the requested symbols
    if body.universe == "watchlist":
        syms = body.watchlist or list(UNIVERSE)
        frames = load_cached(ex, symbols=syms)
    else:
        frames = load_cached(ex)

    if not frames:
        raise HTTPException(503, "history cache empty — run `atr history sync`")

    conds = [c.model_dump() for c in body.conditions]
    results = run_custom_scan(frames, conds, combine=body.combine)

    elapsed = round(time.monotonic() - t0, 2)
    return {
        "as_of": _frames_as_of(frames),
        "universe_size": len(frames),
        "matched": len(results),
        "elapsed_s": elapsed,
        "rows": results,
    }


# Saved scans — stored in data/scans/custom.json
_SCANS_PATH = Path("data/scans/custom.json")

_DEFAULT_SCANS: list[dict] = [
    {
        "id": "oversold_uptrend",
        "name": "Oversold in Uptrend",
        "combine": "AND",
        "conditions": [
            {"indicator": "rsi", "period": 14, "op": "<", "rhs_type": "value", "rhs_value": 35},
            {"indicator": "close", "op": ">", "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 50},
        ],
    },
    {
        "id": "fresh_breakout",
        "name": "Fresh Breakout",
        "combine": "AND",
        "conditions": [
            {"indicator": "vs_high", "op": ">", "rhs_type": "value", "rhs_value": -3},
            {"indicator": "vol_x", "op": ">", "rhs_type": "value", "rhs_value": 2.0},
        ],
    },
    {
        "id": "golden_cross",
        "name": "Golden Cross (recent)",
        "combine": "AND",
        "conditions": [
            {"indicator": "sma", "period": 20, "op": "crosses_above",
             "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 50},
        ],
    },
    {
        "id": "rsi_momentum",
        "name": "RSI Momentum Zone",
        "combine": "AND",
        "conditions": [
            {"indicator": "rsi", "period": 14, "op": ">=", "rhs_type": "value", "rhs_value": 55},
            {"indicator": "rsi", "period": 14, "op": "<=", "rhs_type": "value", "rhs_value": 70},
            {"indicator": "close", "op": ">", "rhs_type": "indicator", "rhs_indicator": "sma", "rhs_period": 20},
        ],
    },
    {
        "id": "near_52w_high",
        "name": "Near 52-week High",
        "combine": "AND",
        "conditions": [
            {"indicator": "vs_high", "op": ">", "rhs_type": "value", "rhs_value": -5},
            {"indicator": "vol_x", "op": ">", "rhs_type": "value", "rhs_value": 1.5},
        ],
    },
]


def _load_scans() -> list[dict]:
    try:
        import json
        _SCANS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _SCANS_PATH.exists():
            return json.loads(_SCANS_PATH.read_text())
    except Exception:  # noqa: BLE001
        pass
    return list(_DEFAULT_SCANS)


def _save_scans(scans: list[dict]) -> None:
    import json
    _SCANS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SCANS_PATH.write_text(json.dumps(scans, indent=2))


@router.get("/scanner/saved")
def scanner_saved_list() -> dict[str, Any]:
    """List all saved custom scans (includes built-in presets on first run)."""
    scans = _load_scans()
    if not _SCANS_PATH.exists():
        _save_scans(scans)
    return {"scans": scans}


class SavedScanUpsert(BaseModel):
    id: str
    name: str
    combine: str = "AND"
    conditions: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/scanner/saved/{scan_id}", dependencies=[Depends(get_principal)])
def scanner_saved_upsert(scan_id: str, body: SavedScanUpsert) -> dict[str, Any]:
    """Create or overwrite a saved scan."""
    scans = _load_scans()
    entry = body.model_dump()
    entry["id"] = scan_id
    scans = [s for s in scans if s["id"] != scan_id]
    scans.append(entry)
    _save_scans(scans)
    return {"saved": entry}


@router.delete("/scanner/saved/{scan_id}", dependencies=[Depends(get_principal)])
def scanner_saved_delete(scan_id: str) -> dict[str, Any]:
    """Delete a saved scan by id."""
    scans = [s for s in _load_scans() if s["id"] != scan_id]
    _save_scans(scans)
    return {"deleted": scan_id}
