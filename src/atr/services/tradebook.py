"""Keep each day's broker tradebook and positions.

The broker only returns the *current* day's trades, and its positions call is day-scoped
too, so a day that is not captured that evening is gone: there is no history to ask for
later. This saves the raw responses, one file per day, so trades are never lost again.

The responses are stored as the broker sent them. They are not turned into profit here,
because the field names have not been checked against a real trading day; recording a
day's profit is done by hand until that is verified.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

#: The broker's own code for "nothing traded": a normal quiet day, not a failure.
NO_TRADES = "EC926"
NO_POSITIONS = "EC920"


def _rows(payload: Any, quiet_code: str) -> list[dict[str, Any]]:
    """The data rows in a broker response, or an error if it carries a failure code."""
    result = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(result, dict) and str(result.get("status", "")).startswith("EC"):
        if result["status"] == quiet_code:
            return []
        raise RuntimeError(str(result.get("message") or result["status"]))
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and str(result[0].get("status", "")).startswith("EC"):
            if result[0]["status"] == quiet_code:
                return []
            raise RuntimeError(str(result[0].get("message") or result[0]["status"]))
        return [r for r in result if isinstance(r, dict)]
    return []


def _save(folder: Path, day: date, rows: list[dict[str, Any]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day.isoformat()}.json").write_text(json.dumps({"date": day.isoformat(), "rows": rows}, indent=1), encoding="utf8")


def capture_day(client: Any, data_root: Path, day: date | None = None) -> dict[str, int]:
    """Save today's trades and positions. Raises if the broker refuses, so the job retries."""
    day = day or date.today()
    root = Path(data_root) / "portfolio"
    trades = _rows(client.trades(), NO_TRADES)
    if trades:
        _save(root / "tradebook", day, trades)
    positions = _rows(client.positions(), NO_POSITIONS)  # raises on "IP address not authorized"
    if positions:
        _save(root / "positions", day, positions)
    return {"trades": len(trades), "positions": len(positions)}
