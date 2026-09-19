"""Your holdings, from the broker when a session exists and from the last snapshot otherwise.

The alert engine used to call the broker for holdings and swallow the failure, so with an
expired session it quietly watched nothing. Here a successful read is saved, and the last
saved copy is used when there is no session, with its age reported so the page can say so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger


def _snapshot_path(data_root: Path) -> Path:
    return Path(data_root) / "insights" / "holdings.json"


def normalize(rows: Any) -> list[dict[str, Any]]:
    """Broker rows -> ``{symbol, qty, avg_price}``, tolerant of IIFL's field names."""
    if isinstance(rows, dict):
        rows = rows.get("result", [])
    out: list[dict[str, Any]] = []
    for h in rows or []:
        raw = str(h.get("symbol") or h.get("TradingSymbol") or h.get("tradingSymbol") or "").strip()
        if not raw:
            continue
        symbol = raw if raw.endswith("-EQ") else f"{raw}-EQ"
        qty = int(float(h.get("qty") or h.get("TotalQty") or h.get("quantity") or 0))
        avg = float(h.get("avg_price") or h.get("BuyAvgRate") or h.get("averagePrice") or 0)
        if qty > 0:
            out.append({"symbol": symbol, "qty": qty, "avg_price": avg})
    return out


def load_holdings(data_root: Path, client: Any | None = None) -> dict[str, Any]:
    """``{"holdings": [...], "source": "live"|"snapshot"|"none", "as_of": iso|None}``."""
    path = _snapshot_path(data_root)
    if client is not None:
        try:
            rows = normalize(client.holdings())
            path.parent.mkdir(parents=True, exist_ok=True)
            as_of = datetime.now(UTC).isoformat()
            path.write_text(json.dumps({"as_of": as_of, "holdings": rows}), encoding="utf8")
            return {"holdings": rows, "source": "live", "as_of": as_of}
        except Exception as exc:  # noqa: BLE001 - fall back to the snapshot
            logger.info("live holdings unavailable, using the snapshot: {}", exc)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf8"))
            return {"holdings": saved.get("holdings", []), "source": "snapshot", "as_of": saved.get("as_of")}
        except (OSError, ValueError):
            pass
    return {"holdings": [], "source": "none", "as_of": None}
