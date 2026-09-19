"""Download long daily history for NSE ETFs from the public chart feed.

The broker cache holds only four ETFs with real history, and three of them track
almost the same exposure (Nifty 50 twice, plus Next 50 and Bank). Rotation needs
assets that move apart from each other — gold, offshore equity, cash — so this
fills the gap from the same public source the nightly top-up uses.

Written to ``data/iifl_daily/ETF`` rather than beside the stocks: everything under
``NSEEQ`` is treated as a tradable share by the scanners, and an ETF would show up
in their rankings as a company.

Run:  ./.venv/Scripts/python.exe scripts/fetch_etf_history.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "iifl_daily" / "ETF"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
BAR_OPEN = pd.Timedelta(hours=9, minutes=15)  # how the broker stamps a daily bar
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

#: Ticker -> what it actually holds. Chosen for *different* exposures, because a
#: rotation over three Nifty proxies is a rotation over one asset.
ETFS = {
    "NIFTYBEES": "Nifty 50",
    "JUNIORBEES": "Nifty Next 50",
    "BANKBEES": "Bank Nifty",
    "GOLDBEES": "Gold",
    "SILVERBEES": "Silver",
    "LIQUIDBEES": "Overnight cash",
    "MON100": "Nasdaq 100 (offshore)",
    "MAFANG": "US tech basket (offshore)",
    "ITBEES": "IT sector",
    "PHARMABEES": "Pharma sector",
    "PSUBNKBEES": "PSU banks",
    "CPSEETF": "CPSE basket",
    "INFRABEES": "Infrastructure",
    "CONSUMBEES": "Consumption",
    "MOM100": "Midcap 100",
    "SETFNIF50": "Nifty 50 (alt)",
    "HNGSNGBEES": "Hang Seng (offshore)",
    "ICICIB22": "Bharat 22",
    "MOM50": "Midcap 50",
    "AXISGOLD": "Gold (alt)",
}


def fetch(ticker: str) -> tuple[str, str, int]:
    """Return ``(ticker, status, bars)``. Never raises: one dead ticker is not fatal."""
    try:
        # Explicit epoch bounds, not ``range=max``: asked for the whole history by
        # range, the feed quietly answers in monthly bars.
        resp = httpx.get(
            URL.format(symbol=quote(f"{ticker}.NS")),
            params={
                "period1": int(pd.Timestamp("2008-01-01").timestamp()),
                "period2": int(pd.Timestamp.today().timestamp()),
                "interval": "1d",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            return ticker, f"http {resp.status_code}", 0
        result = resp.json()["chart"]["result"]
        if not result:
            return ticker, "empty", 0
        result = result[0]
        q = result["indicators"]["quote"][0]
        offset = int(result["meta"].get("gmtoffset", 19800))
        frame = pd.DataFrame(
            {
                "ts": (
                    pd.to_datetime(result["timestamp"], unit="s") + pd.to_timedelta(offset, unit="s")
                ).normalize()
                + BAR_OPEN,
                "open": q["open"],
                "high": q["high"],
                "low": q["low"],
                "close": q["close"],
                "volume": q["volume"],
            }
        ).dropna(subset=["open", "high", "low", "close"])
        if frame.empty:
            return ticker, "no bars", 0
        frame["volume"] = frame["volume"].fillna(0)
        frame[["open", "high", "low", "close"]] = frame[["open", "high", "low", "close"]].round(4)
        # A same-day partial bar would be treated as a finished session downstream.
        frame = frame[frame["ts"].dt.date < pd.Timestamp.today().date()]
        frame = frame.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        OUT.mkdir(parents=True, exist_ok=True)
        frame[COLUMNS].to_parquet(OUT / f"{ticker}.parquet", index=False)
        return ticker, "ok", len(frame)
    except Exception as exc:  # noqa: BLE001 - report and continue
        return ticker, f"fail: {str(exc)[:60]}", 0


def main() -> int:
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = sorted(pool.map(fetch, ETFS), key=lambda r: -r[2])
    ok = [r for r in results if r[1] == "ok"]
    for ticker, status, bars in results:
        if status == "ok":
            df = pd.read_parquet(OUT / f"{ticker}.parquet", columns=["ts", "volume"])
            span = f"{df.ts.iloc[0].date()} -> {df.ts.iloc[-1].date()}"
            med = int(df.volume.tail(120).median())
            print(f"{ticker:<12} {bars:>5} bars  {span}  medvol {med:>12,}  {ETFS[ticker]}")
        else:
            print(f"{ticker:<12} {'':>5}       {status}")
    print(f"\n{len(ok)}/{len(results)} downloaded in {time.monotonic() - t0:.1f}s -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
