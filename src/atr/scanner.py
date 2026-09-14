"""Momentum scanner over liquid NSE names using real IIFL candles.

Shared by the API (`GET /scan`) and any future CLI hook, so dashboard
signals and research math can't drift apart.
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd

from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.strategy.indicators import atr, crossover, rsi, sma

UNIVERSE = [
    "RELIANCE-EQ", "HDFCBANK-EQ", "ICICIBANK-EQ", "INFY-EQ", "TCS-EQ",
    "SBIN-EQ", "AXISBANK-EQ", "KOTAKBANK-EQ", "LT-EQ", "TITAN-EQ",
    "SUNPHARMA-EQ", "ULTRACEMCO-EQ", "MARUTI-EQ", "BHARTIARTL-EQ",
    "HCLTECH-EQ", "TATASTEEL-EQ", "WIPRO-EQ", "ADANIENT-EQ",
    "NIFTYBEES-EQ",
]

SCAN_FROM = "01-Mar-2026"


def resolve_conid(master: InstrumentMaster, symbol: str, exchange: str = "NSEEQ") -> str:
    """EQ-suffix aware lookup with a prefix-search fallback (renames/demergers)."""
    try:
        return str(master.find(symbol, exchange).conid)
    except KeyError:
        root = symbol.split("-")[0]
        hits = master.search(root, exchange=exchange, limit=10)
        hits = hits[hits["symbol"].str.startswith(root)]
        if hits.empty:
            raise KeyError(f"no contract matching symbol={symbol} exchange={exchange}")
        best = hits.sort_values("symbol", key=lambda s: s.str.len()).iloc[0]
        return str(best["conid"])


def score_frame(symbol: str, df: pd.DataFrame) -> dict:
    """Momentum metrics for one OHLCV frame (ts/open/high/low/close/volume)."""
    df = df.sort_values("ts").reset_index(drop=True)
    if len(df) < 60:
        raise ValueError(f"only {len(df)} bars — need 60+")
    c = df["close"]
    last = float(c.iloc[-1])
    prev = float(c.iloc[-2])
    day_chg_pct = round((last / prev - 1) * 100, 2)
    ret_1m = float(c.iloc[-1] / c.iloc[-22] - 1) * 100
    hi = float(df["high"].tail(63).max())
    vs_high = (last / hi - 1) * 100
    s20, s50 = sma(c, 20), sma(c, 50)
    vol_ratio = float(df["volume"].iloc[-1] / df["volume"].tail(21).mean())
    trend = (
        "UP"
        if last > float(s20.iloc[-1]) > float(s50.iloc[-1])
        else ("DOWN" if last < float(s20.iloc[-1]) else "MIXED")
    )
    return {
        "symbol": symbol,
        "last": round(last, 1),
        "day_chg_pct": day_chg_pct,
        "day_high": round(float(df["high"].iloc[-1]), 1),
        "day_low": round(float(df["low"].iloc[-1]), 1),
        "day_value_lakh": round(float(last * df["volume"].iloc[-1] / 1e5), 1),
        "ret_1m": round(ret_1m, 1),
        "vs_high": round(vs_high, 1),
        "trend": trend,
        "rsi": round(float(rsi(c).iloc[-1]), 1),
        "vol_x": round(vol_ratio, 1),
        "atr_pct": round(float(atr(df["high"], df["low"], c).iloc[-1] / last * 100), 1),
        "gold_cross_5d": bool(crossover(s20, s50).tail(5).any()),
        "breakout": bool(vs_high > -2.0 and vol_ratio > 1.5),
        "score": round(ret_1m + vs_high, 1),
        "bars": len(df),
    }


def scan_symbol(
    client: IiflClient,
    master: InstrumentMaster,
    symbol: str,
    *,
    exchange: str = "NSEEQ",
    from_date: str = SCAN_FROM,
    to_date: str | None = None,
) -> dict:
    conid = resolve_conid(master, symbol, exchange)
    raw = client.historical_data("NSEEQ", conid, "1d", from_date,
                                 to_date or date.today().strftime("%d-%b-%Y"))
    df = pd.DataFrame(raw["result"][0]["candles"],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"])
    return score_frame(symbol, df)


def run_scan(
    client: IiflClient,
    symbols: list[str] | None = None,
    *,
    from_date: str = SCAN_FROM,
    to_date: str | None = None,
    pause: float = 0.25,
) -> tuple[list[dict], list[dict]]:
    """Returns (rows sorted by score desc, errors). Never raises per-symbol."""
    master = InstrumentMaster(client)
    master.load_cached(["NSEEQ"])
    rows, errors = [], []
    for sym in symbols or UNIVERSE:
        try:
            rows.append(scan_symbol(client, master, sym, from_date=from_date, to_date=to_date))
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the scan
            errors.append({"symbol": sym, "error": str(exc)[:160]})
        time.sleep(pause)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows, errors
