"""Official index series (Nifty 50, ...), cached next to the stock history.

Why this exists: the stock cache holds *tradable* instruments downloaded from the
broker, and an index is not tradable, so the Nifty 50 was never in it. The market
overview then fell back to an ETF that tracks the index. The ETF's percentage moves
match, but its price is per unit (about 1/100th of the index), so the headline
level was wrong. IIFL does publish the index: ``INDICES.json`` lists it and
``/marketdata/historicaldata`` serves its candles.

Two properties matter:

* **The candles need a broker session once; reading them never does.** The series
  is downloaded whenever a session exists and cached as parquet, so the page works
  the rest of the time from the cache.
* **Without a session the same file is filled from a public series.** Waiting for a
  daily broker login before the headline shows the real index was not acceptable, so
  when there is no session the daily bars come from Yahoo Finance's public chart
  endpoint (``^NSEI``). IIFL remains the preferred source whenever it is reachable.
* **It lives in its own folder** (``iifl_daily/INDICES``), not beside the stocks.
  The scanners read every parquet under ``NSEEQ`` as a stock, and an index would
  show up in their rankings as a company called NIFTY50.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from urllib.parse import quote
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from loguru import logger

INDEX_EXCHANGE = "NSEEQ"
NIFTY_50 = "NIFTY 50"
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

# instrumentId values published in INDICES.json, used only if that file can't be
# fetched and no copy is cached. They are exchange-assigned and do not change.
_KNOWN_IDS = {NIFTY_50: "999920000"}

# A cached index older than this is not shown as "the latest": the stock data it
# would sit beside is newer, and a stale level is worse than the ETF's fresh one.
MAX_STALE_DAYS = 5

_PUBLIC_SYMBOLS = {NIFTY_50: "^NSEI"}
_PUBLIC_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def index_dir(data_root: Path) -> Path:
    return Path(data_root) / "iifl_daily" / "INDICES"


def index_path(data_root: Path, name: str = NIFTY_50) -> Path:
    return index_dir(data_root) / f"{name.replace(' ', '')}.parquet"


def load_index(data_root: Path, name: str = NIFTY_50) -> pd.DataFrame | None:
    """The cached series, or None when it is missing, unreadable or stale."""
    path = index_path(data_root, name)
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not break the page
        logger.warning("index cache {} unreadable: {}", path, exc)
        return None
    if frame.empty or len(frame) < 20 or not is_fresh(frame):
        return None
    return frame


def is_fresh(frame: pd.DataFrame, today: date | None = None) -> bool:
    today = today or date.today()
    last = pd.to_datetime(frame["ts"]).max().date()
    return (today - last).days <= MAX_STALE_DAYS


def synced_today(data_root: Path, name: str = NIFTY_50) -> bool:
    path = index_path(data_root, name)
    try:
        return path.exists() and date.fromtimestamp(path.stat().st_mtime) >= date.today()
    except OSError:
        return False


def instrument_id(data_root: Path, base_url: str, name: str = NIFTY_50) -> str:
    """The exchange id for an index: cached contract file, then the public one."""
    cache = Path(data_root) / "indices_contract.json"
    rows: list[dict[str, Any]] = []
    if cache.exists():
        try:
            rows = json.loads(cache.read_text(encoding="utf8"))
        except (OSError, ValueError):
            rows = []
    if not rows:
        try:
            # The contract files are public; no session is needed to read them.
            resp = httpx.get(f"{base_url.rstrip('/')}/contractfiles/INDICES.json", timeout=20.0)
            resp.raise_for_status()
            rows = resp.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows), encoding="utf8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not fetch INDICES.json: {}", exc)
    for row in rows:
        if str(row.get("underlyingInstrumentSymbol", "")).upper() == name.upper():
            return str(row["instrumentId"])
    if name in _KNOWN_IDS:
        return _KNOWN_IDS[name]
    raise LookupError(f"no instrument id for index {name!r}")


def sync_index(
    client: Any,
    data_root: Path,
    name: str = NIFTY_50,
    *,
    days: int = 800,
    force: bool = False,
) -> str:
    """Download the index's daily candles. Returns ``ok``, ``skip``, ``empty`` or ``fail``.

    ``client`` must hold a live session (``historical_data`` is authenticated).
    """
    if not force and synced_today(data_root, name):
        return "skip"
    try:
        iid = instrument_id(data_root, getattr(client, "base_url", "https://api.iiflcapital.com/v1"), name)
        today = date.today()
        raw = client.historical_data(
            INDEX_EXCHANGE,
            iid,
            "1d",
            (today - timedelta(days=days)).strftime("%d-%b-%Y"),
            today.strftime("%d-%b-%Y"),
        )
        candles = raw["result"][0]["candles"]
        frame = pd.DataFrame(candles, columns=COLUMNS)
        if frame.empty:
            return "empty"
        frame["ts"] = pd.to_datetime(frame["ts"])
        _write(data_root, name, frame, source="iifl")
        return "ok"
    except Exception as exc:  # noqa: BLE001 - a failed refresh keeps the old file
        logger.warning("index {} sync failed: {}", name, str(exc)[:160])
        return "fail"


def _write(data_root: Path, name: str, frame: pd.DataFrame, *, source: str) -> None:
    frame = frame.sort_values("ts").drop_duplicates("ts")
    path = index_path(data_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)  # readers never see a half-written file
    path.with_suffix(".source").write_text(source, encoding="utf8")
    logger.info("index {}: cached {} daily bars from {}", name, len(frame), source)


def sync_index_public(
    data_root: Path,
    name: str = NIFTY_50,
    *,
    years: int = 3,
    force: bool = False,
) -> str:
    """Fill the same cache from a public series, with no broker session.

    Returns ``ok``, ``skip``, ``empty`` or ``fail``. A failure leaves any existing
    file alone.
    """
    symbol = _PUBLIC_SYMBOLS.get(name)
    if symbol is None:
        return "fail"
    if not force and synced_today(data_root, name):
        return "skip"
    try:
        resp = httpx.get(
            _PUBLIC_URL.format(symbol=quote(symbol)),
            params={"range": f"{years}y", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15.0,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        bars = result["indicators"]["quote"][0]
        offset = int(result["meta"].get("gmtoffset", 19800))  # the exchange's UTC offset
        frame = pd.DataFrame(
            {
                "ts": pd.to_datetime(result["timestamp"], unit="s") + pd.to_timedelta(offset, unit="s"),
                "open": bars["open"],
                "high": bars["high"],
                "low": bars["low"],
                "close": bars["close"],
                "volume": bars["volume"],
            }
        )
        frame["ts"] = frame["ts"].dt.normalize()
        # An in-progress or holiday bar can arrive with empty prices; drop it.
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = frame["volume"].fillna(0.0)
        if frame.empty:
            return "empty"
        _write(data_root, name, frame[COLUMNS], source="public")
        return "ok"
    except Exception as exc:  # noqa: BLE001 - offline or the endpoint changed: keep the old file
        logger.warning("public index {} fetch failed: {}", name, str(exc)[:160])
        return "fail"
