"""Keep the daily price history current without a broker session.

The stock history is downloaded from the broker, which needs a login, so when the login
lapses the cache stops moving and every page quietly describes an older day. This tops
the tracked names up from Yahoo Finance's public daily bars.

It only ever *appends* bars newer than the last one in each file. The broker's history
is never rewritten, so a bad public bar cannot damage what is already there. Three
guards keep the mix honest:

* today's bar is dropped until after the close, because a half-finished day looks real;
* a new bar whose close is far from the last stored close is refused (a split or a data
  error), and that symbol is left for the broker sync to correct;
* the stored dtypes are kept, because the readers depend on them.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd
from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_CLOSE_AFTER = (16, 30)  # IST: after the close, once the day's bar is final
_MAX_JUMP = 0.35  # a new close this far from the last stored one is treated as suspect
_BAR_OPEN = pd.Timedelta(hours=9, minutes=15)  # how the broker stamps a daily bar
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def cache_dir(data_root: Path) -> Path:
    return Path(data_root) / "iifl_daily" / "NSEEQ"


def _state_path(data_root: Path) -> Path:
    return Path(data_root) / "eod_refresh.json"


def read_state(data_root: Path) -> dict[str, Any]:
    try:
        return json.loads(_state_path(data_root).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def _yahoo_symbol(stem: str) -> str:
    base = stem[:-3] if stem.upper().endswith("-EQ") else stem
    return f"{base}.NS"


def fetch_recent(stem: str, *, now: datetime | None = None) -> pd.DataFrame | None:
    """The last month of daily bars, or None if unavailable. Today's bar is dropped until the close."""
    now = now or datetime.now(IST)
    resp = httpx.get(
        _URL.format(symbol=quote(_yahoo_symbol(stem))),
        params={"range": "1mo", "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15.0,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    result = resp.json()["chart"]["result"]
    if not result:
        return None
    result = result[0]
    quote_ = result["indicators"]["quote"][0]
    offset = int(result["meta"].get("gmtoffset", 19800))
    frame = pd.DataFrame(
        {
            "ts": (pd.to_datetime(result["timestamp"], unit="s") + pd.to_timedelta(offset, unit="s")).normalize()
            + _BAR_OPEN,
            "open": quote_["open"],
            "high": quote_["high"],
            "low": quote_["low"],
            "close": quote_["close"],
            "volume": quote_["volume"],
        }
    ).dropna(subset=["open", "high", "low", "close"])
    frame[["open", "high", "low", "close"]] = frame[["open", "high", "low", "close"]].astype(float).round(2)
    frame["volume"] = frame["volume"].fillna(0)
    if (now.hour, now.minute) < _CLOSE_AFTER:
        frame = frame[frame["ts"].dt.date < now.date()]
    return frame


def append_new_bars(path: Path, fresh: pd.DataFrame) -> str:
    """Append bars newer than the file's last one. Returns ``ok``, ``current``, ``suspect`` or ``fail``."""
    try:
        existing = pd.read_parquet(path)
        if existing.empty:
            return "fail"
        last_day = existing["ts"].iloc[-1].normalize()
        new = fresh[fresh["ts"].dt.normalize() > last_day]
        if new.empty:
            return "current"
        last_close = float(existing["close"].iloc[-1])
        if last_close > 0 and abs(float(new["close"].iloc[0]) / last_close - 1.0) > _MAX_JUMP:
            return "suspect"
        new = new[COLUMNS].astype(existing[COLUMNS].dtypes.to_dict())
        merged = pd.concat([existing[COLUMNS], new], ignore_index=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        logger.debug("eod append failed for {}: {}", path.name, exc)
        return "fail"


def refresh(
    data_root: Path,
    symbols: list[str] | None = None,
    *,
    workers: int = 6,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Top up the given names (default: every file in the NSEEQ cache)."""
    root = cache_dir(data_root)
    stems = symbols if symbols is not None else sorted(p.stem for p in root.glob("*.parquet"))
    counts = {"ok": 0, "current": 0, "suspect": 0, "fail": 0, "missing": 0}

    def one(stem: str) -> str:
        path = root / f"{stem}.parquet"
        if not path.exists() and not stem.upper().endswith("-EQ"):
            stem = f"{stem}-EQ"  # the tracked list may hold bare tickers; the files carry the series
            path = root / f"{stem}.parquet"
        if not path.exists():
            return "missing"
        try:
            fresh = fetch_recent(stem, now=now)
        except Exception as exc:  # noqa: BLE001 - offline, rate-limited or the endpoint changed
            logger.debug("eod fetch failed for {}: {}", stem, str(exc)[:120])
            return "fail"
        if fresh is None or fresh.empty:
            return "fail"
        return append_new_bars(path, fresh)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for outcome in pool.map(one, stems):
            counts[outcome] += 1

    summary = {
        "ran_at": datetime.now(UTC).isoformat(),
        "ran_date_ist": (now or datetime.now(IST)).date().isoformat(),
        "names": len(stems),
        **counts,
    }
    try:
        _state_path(data_root).parent.mkdir(parents=True, exist_ok=True)
        _state_path(data_root).write_text(json.dumps(summary), encoding="utf8")
    except OSError as exc:
        logger.warning("could not save the eod refresh state: {}", exc)
    logger.info("eod refresh: {}", summary)
    return summary


def due(data_root: Path, now: datetime | None = None) -> bool:
    """Once per weekday after the close; and a catch-up when the app was off for days."""
    now = now or datetime.now(IST)
    state = read_state(data_root)
    last = state.get("ran_date_ist")
    if last == now.date().isoformat():
        return False
    if last is None or (now.date() - datetime.fromisoformat(last).date()).days >= 3:
        return now.weekday() < 5 or last is None
    return now.weekday() < 5 and (now.hour, now.minute) >= _CLOSE_AFTER
