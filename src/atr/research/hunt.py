"""Shared measurement harness for the strategy search.

Every track in the search answers the same question — *does this survive costs
out of sample?* — so they must answer it the same way. This module owns the
parts that decide whether a result is real, and the tracks own only the signal.

Three facts drive everything here:

1. **Costs decide the rebalance frequency.** A delivery round trip in NSE shares
   costs 0.227% of turnover in statutory charges and brokerage, and 0.327% once
   the default 5bp-a-side slippage is added. Only a rule that rotates its whole
   book weekly pays the full 17%/yr that implies; a top-20 momentum basket
   replaces a few names at a time and turns over far less, so the frequency
   question is settled per rule by measuring turnover, not assumed.
2. **Exchange-traded funds are taxed differently.** STT on shares is 0.1% a side;
   on an exchange-traded fund it is 0.001% and only on the sell. Statutory cost
   falls from 0.227% to 0.028% a round trip — an eightfold gap. Note that
   slippage then dominates: with 5bp a side the gap narrows to 0.327% vs 0.128%,
   so any ETF conclusion is only as good as its slippage assumption. Vary
   ``Costs.slippage`` before believing a high-turnover ETF result.
3. **The stock universe is survivorship-biased.** The long-history files are
   today's index members, so names that failed are absent. Levels measured on it
   are not quotable; only like-for-like comparisons inside the same universe are.

Nothing here reports a return without its turnover, its cost drag and its
out-of-sample split, because those are what separate an edge from an artefact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
STOCKS = ROOT / "data" / "iifl_daily" / "NSEEQ"
ETFS = ROOT / "data" / "iifl_daily" / "ETF"

TRADING_DAYS = 252
TRADING_WEEKS = 52


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Costs:
    """One-way cost as a fraction of notional, plus slippage.

    Rates are the 2026 statutory ones. ``stt_buy``/``stt_sell`` are what separate
    a share from an exchange-traded fund, and they are the dominant term.
    """

    name: str
    stt_buy: float
    stt_sell: float
    stamp_buy: float = 0.00015
    exchange: float = 0.0000297
    sebi: float = 0.000001
    gst: float = 0.18
    brokerage_pct: float = 0.0003
    brokerage_cap: float = 20.0
    slippage: float = 0.0005  # half-spread plus impact, per side

    def one_way(self, notional: float, *, sell: bool) -> float:
        """Cost of one leg, in rupees."""
        brokerage = min(self.brokerage_cap, self.brokerage_pct * notional)
        exchange = self.exchange * notional
        sebi = self.sebi * notional
        statutory = (self.stt_sell if sell else self.stt_buy) * notional
        if not sell:
            statutory += self.stamp_buy * notional
        gst = self.gst * (brokerage + exchange + sebi)
        return brokerage + exchange + sebi + gst + statutory + self.slippage * notional

    def round_trip_pct(self, notional: float = 1_000_000.0) -> float:
        """Buy-and-sell cost as a percentage of notional, at a realistic ticket size."""
        both = self.one_way(notional, sell=False) + self.one_way(notional, sell=True)
        return 100 * both / notional

    def turnover_cost(self, turnover_fraction: float, notional: float) -> float:
        """Cost of trading ``turnover_fraction`` of a book worth ``notional``.

        Turnover is measured as the sum of absolute weight changes, so a full
        rotation out of one holding into another is 2.0 and costs one round trip.
        """
        traded = abs(turnover_fraction) * notional
        if traded <= 0:
            return 0.0
        # Half the traded notional is sold and half bought.
        return self.one_way(traded / 2, sell=True) + self.one_way(traded / 2, sell=False)


#: NSE cash equity, delivery. STT 0.1% each side dominates.
STOCK_COSTS = Costs(name="NSE delivery share", stt_buy=0.001, stt_sell=0.001)

#: Exchange-traded fund units. STT 0.001%, sell side only — the structural gap.
ETF_COSTS = Costs(name="NSE exchange-traded fund", stt_buy=0.0, stt_sell=0.00001)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def load_series(symbol: str, *, etf: bool = False, min_bars: int = 250) -> pd.Series | None:
    """One clean close series, or None when there is not enough history.

    The broker cache carries corrupt bars (a handful of sessions priced at
    several times the surrounding level) which fabricate enormous single-week
    returns. They are removed with the repository's reversion-keyed filter.
    """
    path = (ETFS if etf else STOCKS) / f"{symbol}.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path, columns=["ts", "close"]).dropna(subset=["close"])
    frame = frame[frame["close"] > 0].sort_values("ts")
    if len(frame) < min_bars:
        return None

    from atr.data.hygiene import drop_reverting_spikes

    frame, _ = drop_reverting_spikes(frame)
    if len(frame) < min_bars:
        return None
    series = pd.Series(frame["close"].to_numpy(), index=pd.DatetimeIndex(frame["ts"]), name=symbol)
    return series[~series.index.duplicated(keep="last")]


def load_panel(symbols: list[str], *, etf: bool = False, min_bars: int = 250) -> pd.DataFrame:
    """Close prices as ``date x symbol``. Columns that fail the history test are dropped."""
    series = [s for s in (load_series(x, etf=etf, min_bars=min_bars) for x in symbols) if s is not None]
    if not series:
        return pd.DataFrame()
    panel = pd.concat(series, axis=1).sort_index()
    return panel[~panel.index.duplicated(keep="last")]


def etf_universe(min_bars: int = 1000) -> list[str]:
    """Every downloaded exchange-traded fund with enough history."""
    return sorted(p.stem for p in ETFS.glob("*.parquet") if len(pd.read_parquet(p, columns=["ts"])) >= min_bars)


def stock_universe(min_bars: int = 1500) -> list[str]:
    """Long-history share names. Survivorship-biased: these are today's members."""
    out = []
    for path in STOCKS.glob("*.parquet"):
        if "-EQ" in path.stem:
            continue  # the -EQ spellings carry only ~1 year
        try:
            if len(pd.read_parquet(path, columns=["ts"])) >= min_bars:
                out.append(path.stem)
        except Exception:  # noqa: BLE001 - a corrupt file is simply not in the universe
            continue
    return sorted(out)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
@dataclass
class Result:
    """What a rule earned, and everything needed to disbelieve it."""

    equity: pd.Series
    returns: pd.Series
    turnover: pd.Series
    costs: Costs
    gross_returns: pd.Series

    def metrics(self) -> dict[str, float]:
        r = self.returns.dropna()
        if r.empty or len(r) < 20:
            return {"years": 0.0, "cagr_pct": 0.0, "sharpe": 0.0}
        years = len(r) / TRADING_DAYS
        total = float((1 + r).prod())
        cagr = (total ** (1 / years) - 1) if years > 0 and total > 0 else -1.0
        vol = float(r.std() * np.sqrt(TRADING_DAYS))
        curve = (1 + r).cumprod()
        drawdown = float((curve / curve.cummax() - 1).min())
        weekly = curve.resample("W-FRI").last().pct_change().dropna()
        gross_total = float((1 + self.gross_returns.dropna()).prod())
        gross_cagr = (gross_total ** (1 / years) - 1) if years > 0 and gross_total > 0 else -1.0
        return {
            "years": round(years, 2),
            "total_return_pct": round(100 * (total - 1), 2),
            "cagr_pct": round(100 * cagr, 2),
            "gross_cagr_pct": round(100 * gross_cagr, 2),
            "cost_drag_pct_yr": round(100 * (gross_cagr - cagr), 2),
            "ann_vol_pct": round(100 * vol, 2),
            "sharpe": round(float(cagr / vol) if vol > 0 else 0.0, 3),
            "max_drawdown_pct": round(100 * drawdown, 2),
            "weekly_mean_pct": round(100 * float(weekly.mean()), 3),
            "weekly_win_rate_pct": round(100 * float((weekly > 0).mean()), 1),
            "weeks_ge_2pct_pct": round(100 * float((weekly >= 0.02).mean()), 1),
            "turnover_per_yr": round(float(self.turnover.sum() / years), 1),
        }


def run_weights(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    costs: Costs,
    *,
    notional: float = 1_000_000.0,
) -> Result:
    """Hold ``weights`` and pay to change them.

    ``weights`` is indexed by rebalance date and may be sparse; it is held
    forward between dates. Weights are applied from the *next* session, because a
    signal computed from a close cannot be traded at that same close.
    """
    prices = prices.sort_index()
    daily = prices.pct_change().fillna(0.0)
    held = weights.reindex(prices.index).ffill().fillna(0.0)
    held = held.reindex(columns=prices.columns, fill_value=0.0)

    # Shift so today's holding was decided on yesterday's information.
    traded = held.shift(1).fillna(0.0)
    gross = (traded * daily).sum(axis=1)

    # Turnover is charged on the day the book actually changes.
    turnover = traded.diff().abs().sum(axis=1).fillna(0.0)
    cost_fraction = turnover.map(lambda t: costs.turnover_cost(t, notional) / notional)
    net = gross - cost_fraction

    return Result(
        equity=(1 + net).cumprod(),
        returns=net,
        turnover=turnover,
        costs=costs,
        gross_returns=gross,
    )


def buy_and_hold(prices: pd.DataFrame, symbol: str, costs: Costs) -> Result:
    """The benchmark any rule has to beat: own it and do nothing."""
    weights = pd.DataFrame(0.0, index=prices.index[:1], columns=prices.columns)
    weights.loc[prices.index[0], symbol] = 1.0
    return run_weights(prices, weights, costs)


# ---------------------------------------------------------------------------
# Out-of-sample discipline
# ---------------------------------------------------------------------------
def split_halves(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Early and late halves. A rule that works in only one half is not a rule."""
    mid = len(prices) // 2
    return prices.iloc[:mid], prices.iloc[mid:]


def deflated_sharpe(observed: float, n_trials: int, n_obs: int) -> float:
    """Shrink a Sharpe for the number of variants that were tried.

    Searching many rules and reporting the best one overstates it. This is the
    standard correction: the expected maximum Sharpe of ``n_trials`` useless
    rules, subtracted from what was seen.
    """
    if n_trials < 1 or n_obs < 10:
        return observed
    euler = 0.5772156649
    expected_max = (1 - euler) * _z(1 - 1 / n_trials) + euler * _z(1 - 1 / (n_trials * np.e))
    return float(observed - expected_max / np.sqrt(n_obs))


def _z(p: float) -> float:
    """Inverse normal CDF, good enough for the correction above."""
    from statistics import NormalDist

    return NormalDist().inv_cdf(min(max(p, 1e-9), 1 - 1e-9))


def compare(name: str, result: Result, benchmark: Result) -> dict:
    """A rule next to its benchmark, with the difference stated plainly."""
    a, b = result.metrics(), benchmark.metrics()
    return {
        "name": name,
        "strategy": a,
        "benchmark": b,
        "excess_cagr_pct": round(a["cagr_pct"] - b["cagr_pct"], 2),
        "sharpe_gap": round(a["sharpe"] - b["sharpe"], 3),
        "beats_benchmark": a["sharpe"] > b["sharpe"] and a["cagr_pct"] > b["cagr_pct"],
    }
