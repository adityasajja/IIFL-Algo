"""A forward test of the one pattern that held up: oversold, in a volatile stock.

Every backtest here was tuned on the same history it was scored on, and the stock data
is survivorship-biased, so none of them can say whether the pattern is real. A forward
test can. Each Friday's signals are written down before the week happens; a week later
they are graded against what the price actually did. Nothing can be fitted afterwards.

The rule, exactly as it was found: RSI(14) below 30 at the close, in a stock whose
20-day volatility is in the top fifth of the tracked universe that day. The claim being
tested is that such a stock gains 2% or more the following week about 45-50% of the
time, against roughly 32% for any stock-week.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from math import sqrt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

RULE = "oversold_volatile"
HIT = 0.02
HORIZON_BARS = 5
RSI_BELOW = 30.0
VOL_QUANTILE = 0.80
#: What a stock-week does with no pattern at all, and what the study claimed for this one.
BASE_RATE = 0.32
CLAIMED_RATE = 0.45
#: Below this many graded signals the hit rate cannot be told apart from the base rate.
MIN_SAMPLES = 30
#: Round-trip cost of a delivery share trade, as a fraction: 0.227% statutory and
#: brokerage plus 0.1% for slippage on the volatile names this rule selects.
ROUND_TRIP = 0.00327


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def compute_signals(frames: dict[str, pd.DataFrame]) -> tuple[date | None, list[dict[str, Any]]]:
    """Stocks matching the rule at the latest close, and the date that close is.

    ``frames`` maps symbol -> daily bars with ``ts`` and ``close``. Volatility is ranked
    against every symbol on the same last date, so a signal always means "unusually
    jumpy relative to the rest of the market today", as the study defined it.
    """
    rows = []
    for symbol, df in frames.items():
        if len(df) < 40:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        ts = pd.to_datetime(df["ts"]).reset_index(drop=True)
        rows.append(
            {
                "symbol": symbol,
                "ts": ts.iloc[-1],
                "close": float(close.iloc[-1]),
                "rsi": float(_rsi(close).iloc[-1]),
                "vol20": float(close.pct_change().rolling(20).std().iloc[-1]),
            }
        )
    if not rows:
        return None, []
    table = pd.DataFrame(rows).dropna(subset=["rsi", "vol20"])
    latest = table["ts"].max()
    table = table[table["ts"] == latest]  # a name whose last bar is older is stale, not "today"
    if table.empty:
        return None, []
    cutoff = float(table["vol20"].quantile(VOL_QUANTILE))
    hits = table[(table["rsi"] < RSI_BELOW) & (table["vol20"] >= cutoff)].sort_values("rsi")
    signals = [
        {"symbol": r.symbol, "entry_close": round(r.close, 2), "rsi": round(r.rsi, 1), "vol20_pct": round(100 * r.vol20, 2)}
        for r in hits.itertuples()
    ]
    return latest.date(), signals


def store_path(data_root: Path) -> Path:
    return Path(data_root) / "research" / "forward_oversold_volatile.json"


def load_frames(data_root: Path) -> dict[str, pd.DataFrame]:
    """Daily bars for the long-history names, the universe the pattern was found in."""
    from atr.research.hunt import STOCKS, stock_universe

    stocks = Path(data_root) / "iifl_daily" / "NSEEQ" if data_root else STOCKS
    frames = {}
    for symbol in stock_universe(min_bars=1500):
        try:
            frames[symbol] = pd.read_parquet(stocks / f"{symbol}.parquet", columns=["ts", "close"]).dropna()
        except Exception:  # noqa: BLE001 - one unreadable file must not stop the run
            continue
    return frames


def run_daily(data_root: Path) -> dict[str, int]:
    """Grade what is due, then write down today's signals if today closes a week."""
    store = Store(store_path(data_root))
    frames = load_frames(data_root)
    graded = grade(store, frames)
    as_of, signals = compute_signals(frames)
    return {"graded": graded, "recorded": record(store, as_of, signals), "signals_today": len(signals)}


@dataclass
class Store:
    path: Path

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {"rule": RULE, "signals": []}

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf8")
        tmp.replace(self.path)  # a crash mid-write never leaves half a file


def record(store: Store, as_of: date | None, signals: list[dict[str, Any]]) -> int:
    """Write down this Friday's signals. Runs on Fridays only, once per date and symbol."""
    if as_of is None or as_of.weekday() != 4:
        return 0
    state = store.read()
    seen = {(s["entry_date"], s["symbol"]) for s in state["signals"]}
    added = 0
    for s in signals:
        key = (as_of.isoformat(), s["symbol"])
        if key in seen:
            continue
        state["signals"].append({**s, "entry_date": as_of.isoformat(), "status": "open"})
        added += 1
    if added:
        store.write(state)
    return added


def grade(store: Store, frames: dict[str, pd.DataFrame]) -> int:
    """Close every open signal that now has a full week of bars behind it."""
    state = store.read()
    closed = 0
    for s in state["signals"]:
        if s["status"] != "open":
            continue
        df = frames.get(s["symbol"])
        if df is None:
            continue
        dates = pd.to_datetime(df["ts"]).dt.date.astype(str).tolist()
        if s["entry_date"] not in dates:
            continue
        i = dates.index(s["entry_date"])
        if i + HORIZON_BARS >= len(df):
            continue
        exit_close = float(df["close"].iloc[i + HORIZON_BARS])
        ret = exit_close / s["entry_close"] - 1
        s.update(status="closed", exit_close=round(exit_close, 2), ret_pct=round(100 * ret, 2), hit=bool(ret >= HIT))
        closed += 1
    if closed:
        store.write(state)
    return closed


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% range for a hit rate. With few signals it is wide, and that is the point."""
    if n == 0:
        return 0.0, 1.0
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def summary(store: Store) -> dict[str, Any]:
    """The running score, with how much it can be trusted stated alongside it."""
    signals = store.read()["signals"]
    closed = [s for s in signals if s["status"] == "closed"]
    open_ = [s for s in signals if s["status"] == "open"]
    n, hits = len(closed), sum(1 for s in closed if s["hit"])
    low, high = wilson(hits, n)
    rate = hits / n if n else None
    net = float(np.mean([s["ret_pct"] for s in closed])) - 100 * ROUND_TRIP if n else None

    if n < MIN_SAMPLES:
        verdict = f"Too early to say: {n} of {MIN_SAMPLES} graded signals needed."
        state = "collecting"
    elif low > BASE_RATE:
        verdict = "Beating the ordinary 32% rate, with the range clear of it."
        state = "working"
    elif high < CLAIMED_RATE:
        verdict = "Not reaching the 45% the backtest suggested."
        state = "not_working"
    else:
        verdict = "Inconclusive: the range spans both the ordinary rate and the claimed one."
        state = "inconclusive"
    return {
        "rule": RULE,
        "description": "RSI below 30 in a top-fifth volatility stock",
        "state": state,
        "verdict": verdict,
        "graded": n,
        "needed": MIN_SAMPLES,
        "hits": hits,
        "hit_rate_pct": None if rate is None else round(100 * rate, 1),
        "range_pct": [round(100 * low, 1), round(100 * high, 1)] if n else None,
        "avg_net_pct": None if net is None else round(net, 2),
        "base_rate_pct": round(100 * BASE_RATE),
        "claimed_rate_pct": round(100 * CLAIMED_RATE),
        "open": open_,
        "recent": sorted(closed, key=lambda s: s["entry_date"], reverse=True)[:12],
    }
