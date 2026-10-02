"""Nightly check that the price history has no suspect jumps (unadjusted splits/bonuses) or holes.

The result is written to ``<data_root>/data_check.json`` so the data-status page can show it and the
backtest and paper runner are not silently fed a bad series.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from atr.backtest.validity import gap_findings, jump_findings
from atr.data.eod_refresh import cache_dir

FILE = "data_check.json"


def scan(data_root: Path) -> dict[str, Any]:
    frames: dict[str, pd.DataFrame] = {}
    for path in sorted(cache_dir(data_root).glob("*.parquet")):
        try:
            frame = pd.read_parquet(path)
            frames[path.stem] = frame.set_index(pd.to_datetime(frame["ts"]))
        except Exception:  # noqa: BLE001 - an unreadable file is reported, not fatal
            frames[path.stem] = pd.DataFrame()
    unreadable = [s for s, f in frames.items() if f.empty]
    good = {s: f for s, f in frames.items() if not f.empty}
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": len(frames),
        "jumps": jump_findings(good),
        "gaps": gap_findings(good),
        "unreadable": unreadable,
    }


def run_daily(data_root: Path) -> dict[str, Any]:
    result = scan(Path(data_root))
    Path(data_root).mkdir(parents=True, exist_ok=True)
    (Path(data_root) / FILE).write_text(json.dumps(result), encoding="utf8")
    return result


def read(data_root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((Path(data_root) / FILE).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
