"""Fill single missing sessions in the daily price files from the broker's own history.

The free price feed used for the nightly top-up sometimes lacks a day for a stock: a hole
in the middle of an otherwise current series. A missing day makes that stock's change on
the next day unmeasurable, so the P&L calendar leaves the day blank rather than credit
two days' movement to one. The broker's daily history has those bars.

This only ever *inserts* a bar for a session the file does not have. It never replaces
an existing bar, and it skips a session for which the broker returns two different
candles, because there is no telling which is right.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]
#: A date counts as a trading session when at least this share of the names have a bar.
SESSION_SHARE = 0.5


def _path(folder: Path, symbol: str) -> Path | None:
    for name in (symbol, symbol[:-3] if symbol.endswith("-EQ") else symbol):
        candidate = folder / f"{name}.parquet"
        if candidate.exists():
            return candidate
    return None


def missing_sessions(dates_by_symbol: dict[str, set[date]], window: int = 15) -> dict[str, list[date]]:
    """Recent trading sessions each symbol has no bar for."""
    if not dates_by_symbol:
        return {}
    counts: dict[date, int] = {}
    for dates in dates_by_symbol.values():
        for d in dates:
            counts[d] = counts.get(d, 0) + 1
    need = SESSION_SHARE * len(dates_by_symbol)
    sessions = sorted(d for d, n in counts.items() if n >= need)[-window:]
    out: dict[str, list[date]] = {}
    for symbol, dates in dates_by_symbol.items():
        first = min(dates) if dates else None
        gaps = [d for d in sessions if d not in dates and first is not None and d > first]
        if gaps:
            out[symbol] = gaps
    return out


def _candles(payload: Any) -> list[list[Any]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, list) and result and isinstance(result[0], dict):
        if str(result[0].get("status", "")).startswith("EC"):
            raise RuntimeError(str(result[0].get("message") or result[0]["status"]))
        return list(result[0].get("candles") or [])
    return []


def repair(client: Any, data_root: Path, symbols: list[str], *, window: int = 15) -> dict[str, int]:
    """Insert missing recent sessions for ``symbols``. Returns counts of what was done."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    folder = Path(data_root) / "iifl_daily" / "NSEEQ"
    paths = {s: p for s in symbols if (p := _path(folder, s)) is not None}
    frames = {s: pd.read_parquet(p) for s, p in paths.items()}
    dates = {s: {t.date() for t in pd.to_datetime(f["ts"])} for s, f in frames.items()}
    gaps = missing_sessions(dates, window)
    if not gaps:
        return {"symbols": 0, "inserted": 0, "skipped": 0, "failed": 0}

    master = InstrumentMaster(client)
    master.load_cached(["NSEEQ"])
    done = {"symbols": 0, "inserted": 0, "skipped": 0, "failed": 0}
    for symbol, missing in gaps.items():
        try:
            conid = resolve_conid(master, symbol if symbol.endswith("-EQ") else f"{symbol}-EQ", "NSEEQ")
            start, end = min(missing), max(missing)
            payload = client.historical_data("NSEEQ", str(conid), "1d", start.strftime("%d-%b-%Y"), (end + pd.Timedelta(days=1)).strftime("%d-%b-%Y"))
            by_day: dict[date, list[list[Any]]] = {}
            for c in _candles(payload):
                by_day.setdefault(pd.Timestamp(c[0]).date(), []).append(c)
            frame = frames[symbol]
            rows = []
            for day in missing:
                candidates = by_day.get(day, [])
                if not candidates:
                    done["skipped"] += 1
                    continue
                if any(c[1:6] != candidates[0][1:6] for c in candidates):  # two different candles for one day
                    done["skipped"] += 1
                    continue
                _, o, h, l, close, vol = candidates[0][:6]
                rows.append({"ts": pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=15), "open": o, "high": h, "low": l, "close": close, "volume": vol})
            if not rows:
                continue
            new = pd.DataFrame(rows)[COLUMNS].astype(frame[COLUMNS].dtypes.to_dict())
            merged = pd.concat([frame[COLUMNS], new], ignore_index=True).sort_values("ts").drop_duplicates("ts", keep="first")
            tmp = paths[symbol].with_suffix(".parquet.tmp")
            merged.to_parquet(tmp, index=False)
            os.replace(tmp, paths[symbol])
            done["symbols"] += 1
            done["inserted"] += len(rows)
        except Exception as exc:  # noqa: BLE001 - one stock failing must not stop the rest
            logger.debug("gap repair failed for {}: {}", symbol, str(exc)[:120])
            done["failed"] += 1
    return done
