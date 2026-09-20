"""The Monday gap plan: buy a stock that gaps down at the open, sell at a profit target.

The rule comes from the strategy search (see ``scripts/study_target_size.py``):

* it applies only after a week in which the market rose: the median stock gained more
  than 1% over the five sessions before Monday;
* on Monday, buy at the open any stock that opened more than 1% below Friday's close;
* sell the moment the price touches +3%; stop out at -5%; otherwise sell Friday's close.

It is tracked as paper trades so the result can be read without any capital at risk. The
entries are not chosen after seeing the outcome: they follow mechanically from Monday's
open and Friday's close. Weeks that ended before the plan was switched on are marked as
*replay* and reported separately from *live* weeks, because a rule that was picked by
looking at history cannot vouch for itself on that history.

Fills are modelled on daily bars, pessimistically: if one day's range touches both the
stop and the target the stop is assumed first, and a later day that opens beyond either
fills at that open. A trade sold on the day it was bought is a day-trade and pays the
cheaper day-trade cost; anything carried overnight pays the delivery cost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from atr.research.hunt import STOCK_COSTS, Costs

DAYS = 5
NOTIONAL = 100_000.0  # a Rs 1 lakh ticket: what the Rs 20 brokerage cap means in percent
SLIPPAGE = 0.0005  # per side
#: Below this many graded live trades the result cannot be told apart from luck.
MIN_LIVE_TRADES = 30


@dataclass(frozen=True)
class Plan:
    target: float = 0.03
    stop: float = 0.05
    gap: float = -0.01  # the open must be at least this far below Friday's close
    market_min: float = 0.01  # the median stock's prior-week gain must exceed this


PLAN = Plan()

#: A day-trade: STT 0.025% on the sell only, stamp duty 0.003% on the buy.
_INTRADAY = Costs(name="NSE day-trade share", stt_buy=0.0, stt_sell=0.00025, stamp_buy=0.00003, slippage=SLIPPAGE)
INTRADAY_ROUND_TRIP = _INTRADAY.round_trip_pct(NOTIONAL) / 100
DELIVERY_ROUND_TRIP = STOCK_COSTS.round_trip_pct(NOTIONAL) / 100


@dataclass
class Bars:
    """One stock's daily bars as parallel arrays, indexed by session date."""

    dates: np.ndarray  # datetime64[D]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray

    def index_of(self, day: date | np.datetime64) -> int | None:
        target = np.datetime64(day, "D")
        i = int(np.searchsorted(self.dates, target))
        return i if i < len(self.dates) and self.dates[i] == target else None


def load_bars(data_root: Path) -> dict[str, Bars]:
    """Bars for the long-history names, the universe the plan was researched on."""
    from atr.data.hygiene import reverting_spike_mask
    from atr.research.hunt import stock_universe

    folder = Path(data_root) / "iifl_daily" / "NSEEQ"
    out: dict[str, Bars] = {}
    for symbol in stock_universe(min_bars=1500):
        try:
            df = pd.read_parquet(folder / f"{symbol}.parquet", columns=["ts", "open", "high", "low", "close"]).dropna()
        except Exception:  # noqa: BLE001 - one unreadable file must not stop the run
            continue
        df = df[(df["close"] > 0) & (df["open"] > 0)].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        df = df[~reverting_spike_mask(df["close"]).to_numpy()].reset_index(drop=True)
        if len(df) < 300:
            continue
        out[symbol] = Bars(
            dates=df["ts"].to_numpy().astype("datetime64[D]"),
            o=df["open"].to_numpy(float), h=df["high"].to_numpy(float),
            l=df["low"].to_numpy(float), c=df["close"].to_numpy(float),
        )
    return out


def week_starts(bars: dict[str, Bars]) -> list[date]:
    """The first trading day of each week, from the dates the universe actually traded."""
    days = pd.Series(sorted({d for b in bars.values() for d in b.dates[-400:]}))
    days = pd.to_datetime(days)
    firsts = days.groupby(days.dt.to_period("W-FRI")).min()
    return [d.date() for d in firsts]


def entries_for_week(bars: dict[str, Bars], start: date, plan: Plan = PLAN) -> tuple[float | None, list[dict[str, Any]]]:
    """The market's prior-week gain, and every stock that gapped down at this week's open."""
    rows = []
    for symbol, b in bars.items():
        i = b.index_of(start)
        if i is None or i < 6:
            continue
        prev = i - 1
        rows.append(
            {
                "symbol": symbol, "entry": float(b.o[i]),
                "gap": float(b.o[i] / b.c[prev] - 1), "ret5": float(b.c[prev] / b.c[prev - 5] - 1),
            }
        )
    if len(rows) < 50:  # too few names have this day to call it a market reading
        return None, []
    median = float(np.median([r["ret5"] for r in rows]))
    return median, [r for r in rows if r["gap"] <= plan.gap]


def market_now(bars: dict[str, Bars], plan: Plan = PLAN) -> dict[str, Any]:
    """Was last week a rising one? Read from the latest close, before Monday's open exists."""
    if not bars:
        return {"as_of": None, "median_pct": None, "needed_pct": 100 * plan.market_min, "status": "unknown"}
    latest = max(b.dates[-1] for b in bars.values())
    changes = [
        float(b.c[-1] / b.c[-6] - 1) for b in bars.values() if b.dates[-1] == latest and len(b.c) > 6
    ]
    if len(changes) < 50:
        return {"as_of": None, "median_pct": None, "needed_pct": 100 * plan.market_min, "status": "unknown"}
    median = float(np.median(changes))
    return {
        "as_of": str(latest),
        "median_pct": round(100 * median, 2),
        "needed_pct": round(100 * plan.market_min, 2),
        "status": "trade" if median > plan.market_min else "skip",
        "names": len(changes),
    }


def grade_trade(trade: dict[str, Any], b: Bars, plan: Plan = PLAN) -> dict[str, Any] | None:
    """The trade's exit once enough sessions exist to decide it, else None (still open)."""
    i = b.index_of(date.fromisoformat(trade["entry_date"]))
    if i is None:
        return None
    entry = trade["entry"]
    target, stop = entry * (1 + plan.target), entry * (1 - plan.stop)

    def done(price: float, reason: str, k: int) -> dict[str, Any]:
        cost = INTRADAY_ROUND_TRIP if (k == 0 and reason == "target") else DELIVERY_ROUND_TRIP
        gross = price / entry - 1
        return {
            "status": "closed", "exit_price": round(price, 2), "exit_reason": reason, "exit_date": str(b.dates[i + k]),
            "gross_pct": round(100 * gross, 2), "net_pct": round(100 * (gross - cost), 2), "won": reason == "target",
        }

    for k in range(DAYS):
        j = i + k
        if j >= len(b.c):
            return None  # the week is not over and nothing has decided this trade yet
        if k > 0:  # a gap can only happen after the entry session
            if b.o[j] >= target:
                return done(float(b.o[j]), "target", k)
            if b.o[j] <= stop:
                return done(float(b.o[j]), "stop", k)
        if b.l[j] <= stop:  # a bar that touched both counts as the stop: the pessimistic reading
            return done(stop, "stop", k)
        if b.h[j] >= target:
            return done(target, "target", k)
    return done(float(b.c[i + DAYS - 1]), "friday", DAYS - 1)


@dataclass
class Store:
    path: Path

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}

    def write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf8")
        tmp.replace(self.path)  # a crash mid-write never leaves half a file


def store_path(data_root: Path) -> Path:
    return Path(data_root) / "research" / "gap_plan.json"


def next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def update(store: Store, bars: dict[str, Bars], *, today: date, replay_weeks: int = 12, plan: Plan = PLAN) -> dict[str, int]:
    """Record this week's entries, replay recent ones, and grade everything that can be graded."""
    state = store.read() or {"plan": plan.__dict__, "trades": []}
    # The first Monday that starts after the plan was switched on. Anything earlier is replay.
    state.setdefault("live_from", str(next_monday(today)))
    live_from = date.fromisoformat(state["live_from"])
    have = {t["id"] for t in state["trades"]}

    added = 0
    for start in week_starts(bars)[-(replay_weeks + 1):]:
        median, entries = entries_for_week(bars, start, plan)
        if median is None:
            continue
        for e in entries:
            tid = f"{start}-{e['symbol']}"
            if tid in have:
                continue
            state["trades"].append(
                {
                    "id": tid, "symbol": e["symbol"], "entry_date": str(start), "entry": round(e["entry"], 2),
                    "gap_pct": round(100 * e["gap"], 2), "market_pct": round(100 * median, 2),
                    "market_ok": bool(median > plan.market_min),
                    "source": "live" if start >= live_from else "replay", "status": "open",
                }
            )
            have.add(tid)
            added += 1

    graded = 0
    for t in state["trades"]:
        if t["status"] != "open" or t["symbol"] not in bars:
            continue
        result = grade_trade(t, bars[t["symbol"]], plan)
        if result:
            t.update(result)
            graded += 1
    state["market"] = market_now(bars, plan)  # kept so the page can read it without reloading every bar
    store.write(state)
    return {"recorded": added, "graded": graded}


def run_daily(data_root: Path, today: date | None = None) -> dict[str, int]:
    return update(Store(store_path(data_root)), load_bars(data_root), today=today or date.today())


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [t for t in trades if t["status"] == "closed"]
    if not closed:
        return {"graded": 0, "open": len(trades) - len(closed), "win_rate_pct": None, "avg_net_pct": None, "median_net_pct": None, "weeks": 0}
    net = [t["net_pct"] for t in closed]
    return {
        "graded": len(closed),
        "open": len(trades) - len(closed),
        "win_rate_pct": round(100 * sum(1 for t in closed if t["won"]) / len(closed), 1),
        "avg_net_pct": round(float(np.mean(net)), 2),
        "median_net_pct": round(float(np.median(net)), 2),
        "weeks": len({t["entry_date"] for t in closed}),
    }


def summary(store: Store, bars: dict[str, Bars] | None = None, plan: Plan = PLAN) -> dict[str, Any]:
    """Everything the page shows: this week's call, live results, replay, and the comparison."""
    state = store.read()
    trades = state.get("trades", [])
    qualifying = [t for t in trades if t["market_ok"]]
    live = _stats([t for t in qualifying if t["source"] == "live"])
    replay = _stats([t for t in qualifying if t["source"] == "replay"])
    other = _stats([t for t in trades if not t["market_ok"]])
    market = market_now(bars, plan) if bars else state.get("market", {"status": "unknown"})

    if live["graded"] < MIN_LIVE_TRADES:
        verdict = f"Too early to judge: {live['graded']} of {MIN_LIVE_TRADES} live trades graded."
    elif (live["avg_net_pct"] or 0) > 0:
        verdict = "Live trades are averaging a profit after costs."
    else:
        verdict = "Live trades are not making money after costs."

    def brief(t: dict[str, Any]) -> dict[str, Any]:
        return {k: t.get(k) for k in ("symbol", "entry_date", "entry", "gap_pct", "status", "exit_reason", "exit_price", "net_pct", "source", "market_ok")}

    recent = sorted((t for t in qualifying if t["status"] == "closed"), key=lambda t: (t["entry_date"], t["symbol"]), reverse=True)[:15]
    return {
        "plan": {
            "target_pct": 100 * plan.target, "stop_pct": 100 * plan.stop, "gap_pct": 100 * plan.gap,
            "market_min_pct": 100 * plan.market_min,
        },
        "this_week": market,
        "verdict": verdict,
        "live_from": state.get("live_from"),
        "live": live,
        "replay": replay,
        "when_market_did_not_qualify": other,
        "open": [brief(t) for t in qualifying if t["status"] == "open"],
        "recent": [brief(t) for t in recent],
        "needed": MIN_LIVE_TRADES,
    }
