"""Research track C: does short-horizon mean reversion survive Indian costs?

The question is not whether oversold names bounce — they do, in almost every
market — but whether the bounce is bigger than what it costs to collect. In NSE
cash equity a round trip is ~0.33% of turnover; a rule that rotates its whole
book every three days pays roughly a quarter of the account a year. Exchange
traded funds cost ~0.13% a round trip, so the same rule is a different animal in
the cheaper wrapper. This script measures both, on the shared harness, and
reports the holding-period/net-return tradeoff curve that decides the matter.

Everything here is long-only cash equity or ETF units. No derivatives, no
shorting. Signals are computed from closes and traded from the *next* session;
``run_weights`` does that lag and is never bypassed.

Health warning that applies to every stock number below: the share universe is
today's surviving long-history names. Companies that were delisted or collapsed
are simply absent from the files. Mean reversion is the strategy most damaged by
that omission, because its entry condition — "this has fallen a long way" — is
exactly the condition that precedes the deletions we cannot see.

Run:  ./.venv/Scripts/python.exe scripts/hunt_mean_reversion.py
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from atr.research.hunt import (
    ETF_COSTS,
    ROOT,
    STOCK_COSTS,
    TRADING_DAYS,
    Result,
    buy_and_hold,
    compare,
    deflated_sharpe,
    etf_universe,
    load_panel,
    run_weights,
    split_halves,
    stock_universe,
)

OUT = ROOT / "data" / "research" / "mean_reversion.json"

#: Liquidity is ranked on this window only. Ranking on the full sample would
#: quietly import knowledge of which names stayed tradable.
LIQUIDITY_WINDOW = ("2015-01-01", "2016-12-31")
MAX_STOCKS = 200


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def liquid_stocks(names: list[str], limit: int = MAX_STOCKS) -> list[str]:
    """Top ``limit`` names by median rupee volume in the *early* sub-window."""
    start, end = (pd.Timestamp(x) for x in LIQUIDITY_WINDOW)
    scored: list[tuple[float, str]] = []
    for name in names:
        path = ROOT / "data" / "iifl_daily" / "NSEEQ" / f"{name}.parquet"
        try:
            frame = pd.read_parquet(path, columns=["ts", "close", "volume"])
        except Exception:  # noqa: BLE001 - a corrupt file is simply not in the universe
            continue
        window = frame[(frame["ts"] >= start) & (frame["ts"] <= end)]
        if len(window) < 200:
            continue
        scored.append((float((window["close"] * window["volume"]).median()), name))
    scored.sort(reverse=True)
    return sorted(n for _, n in scored[:limit])


def liquid_etfs(names: list[str], min_rupee_volume: float = 5e5) -> list[str]:
    """ETFs that really traded in the early window.

    Several listed exchange traded funds print a handful of units a day; their
    closes go stale and then jump, which manufactures mean reversion that nobody
    could have captured. This filter is the control for that. Five lakh a day is
    thin but tradable for a small book. Funds that had not listed by the end of
    the ranking window are excluded, which costs breadth but keeps the universe
    free of hindsight.
    """
    start, end = (pd.Timestamp(x) for x in LIQUIDITY_WINDOW)
    keep = []
    for name in names:
        path = ROOT / "data" / "iifl_daily" / "ETF" / f"{name}.parquet"
        try:
            frame = pd.read_parquet(path, columns=["ts", "close", "volume"])
        except Exception:  # noqa: BLE001
            continue
        window = frame[(frame["ts"] >= start) & (frame["ts"] <= end)]
        if len(window) < 200:
            continue
        if float((window["close"] * window["volume"]).median()) >= min_rupee_volume:
            keep.append(name)
    return sorted(keep)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def wilder_rsi(prices: pd.DataFrame, period: int) -> pd.DataFrame:
    """Wilder's RSI on closes, computed per column with no forward leakage."""
    delta = prices.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    roll_down = down.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(roll_down > 0, 100.0)


def changes_only(weights: pd.DataFrame) -> pd.DataFrame:
    """Keep a row only where the target actually changes.

    ``run_weights`` treats every row as a rebalance and charges the distance from
    the drifted book, so repeating an unchanged target would bill the account for
    re-equalising a 20-name book every session — something no one does. Rows are
    therefore emitted on entry and exit days only; in between the basket drifts,
    which is what actually happens.
    """
    if weights.empty:
        return weights
    keep = weights.ne(weights.shift()).any(axis=1)
    keep.iloc[0] = True
    return weights[keep]


def equal_weight_from_mask(mask: pd.DataFrame, max_names: int | None = None) -> pd.DataFrame:
    """Turn a boolean holding mask into equal weights, capped at ``max_names``.

    Days with nothing selected sit in cash, which is the honest representation of
    a rule that simply has no candidates.
    """
    held = mask.fillna(False)
    if max_names is not None:
        # Keep the first ``max_names`` by column order for determinism.
        rank = held.cumsum(axis=1)
        held = held & (rank <= max_names)
    count = held.sum(axis=1)
    weights = held.astype(float).div(count.replace(0, np.nan), axis=0).fillna(0.0)
    return weights


def entry_exit_weights(
    prices: pd.DataFrame,
    entry: pd.DataFrame,
    *,
    hold_days: int,
    exit_signal: pd.DataFrame | None = None,
    max_names: int = 20,
) -> pd.DataFrame:
    """Hold a name for ``hold_days`` from entry, or until ``exit_signal`` fires.

    Implemented as a loop over sessions because the exit is path dependent. The
    resulting frame is a daily weight book; ``run_weights`` applies the one-day
    trade lag.
    """
    dates = prices.index
    cols = prices.columns
    entry_arr = entry.reindex(index=dates, columns=cols).fillna(False).to_numpy()
    exit_arr = (
        exit_signal.reindex(index=dates, columns=cols).fillna(False).to_numpy()
        if exit_signal is not None
        else np.zeros_like(entry_arr, dtype=bool)
    )
    alive = np.zeros(len(cols), dtype=np.int32)  # sessions remaining, 0 = flat
    book = np.zeros((len(dates), len(cols)), dtype=bool)

    for i in range(len(dates)):
        alive = np.where(alive > 0, alive - 1, 0)
        alive = np.where(exit_arr[i] & (alive > 0), 0, alive)
        free = max_names - int((alive > 0).sum())
        if free > 0:
            candidates = np.flatnonzero(entry_arr[i] & (alive == 0))
            for j in candidates[:free]:
                alive[j] = hold_days
        book[i] = alive > 0

    mask = pd.DataFrame(book, index=dates, columns=cols)
    return changes_only(equal_weight_from_mask(mask))


def cross_sectional_losers(
    prices: pd.DataFrame,
    *,
    lookback: int,
    top_k: int,
    hold_weeks: int,
) -> pd.DataFrame:
    """Each week buy the ``top_k`` worst performers of the last ``lookback`` days.

    Overlapping tranches: with ``hold_weeks`` > 1 the book is the average of the
    last ``hold_weeks`` weekly selections, which is how such a rule is actually
    run and roughly divides turnover by ``hold_weeks``.
    """
    past = prices.pct_change(lookback)
    # The last *actual* session of each week; the broker stamps bars at 09:15, so
    # a plain W-FRI label is not a member of the index.
    fridays = list(prices.index.to_series().resample("W-FRI").last().dropna())
    picks: list[pd.Series] = []
    index: list[pd.Timestamp] = []
    for date in fridays:
        row = past.loc[date].dropna()
        row = row[prices.loc[date, row.index].notna()]
        if len(row) < top_k:
            continue
        chosen = row.nsmallest(top_k).index
        weight = pd.Series(0.0, index=prices.columns)
        weight[chosen] = 1.0 / top_k
        picks.append(weight)
        index.append(date)
    if not picks:
        return pd.DataFrame(columns=prices.columns)
    weekly = pd.DataFrame(picks, index=pd.DatetimeIndex(index))
    return changes_only(weekly.rolling(hold_weeks, min_periods=1).mean())


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
def equal_weight_weights(prices: pd.DataFrame) -> pd.DataFrame:
    """Own everything that is already listed on day one, equally, forever."""
    first = prices.index[0]
    valid = prices.loc[first].notna()
    weights = pd.DataFrame(0.0, index=prices.index[:1], columns=prices.columns)
    weights.loc[first, valid[valid].index] = 1.0 / max(int(valid.sum()), 1)
    return weights


def equal_weight_hold(prices: pd.DataFrame, costs) -> Result:
    """The survivorship-controlled benchmark for every stock rule."""
    return run_weights(prices.ffill(), equal_weight_weights(prices), costs)


def regime_mask(index_prices: pd.Series, window: int = 200) -> pd.Series:
    """True when the index closed above its ``window``-day average."""
    return index_prices > index_prices.rolling(window).mean()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summarise(name: str, result: Result) -> dict:
    row = {"name": name}
    row.update(result.metrics())
    return row


def halves(prices: pd.DataFrame, build, costs) -> dict:
    """Run the same rule on both halves of the sample, built inside each half."""
    early, late = split_halves(prices)
    out = {}
    for label, frame in (("first_half", early), ("second_half", late)):
        res = run_weights(frame, build(frame), costs)
        out[label] = res.metrics()
    return out


def main() -> None:  # noqa: C901 - a research script is a sequence of experiments
    report: dict = {"trials": [], "notes": []}
    trials: list[dict] = []

    # -- universes ---------------------------------------------------------
    stock_names = liquid_stocks(stock_universe(min_bars=1500))
    stocks = load_panel(stock_names, min_bars=1500).ffill()
    etf_names = etf_universe()
    etfs = load_panel(etf_names, etf=True).ffill()
    etfs_liquid = etfs[liquid_etfs(etf_names)].dropna(how="all")
    print(f"stocks {stocks.shape}  etfs {etfs.shape}  liquid etfs {etfs_liquid.shape}")

    report["universe"] = {
        "stocks": len(stocks.columns),
        "stock_selection": f"top {MAX_STOCKS} by median rupee volume in {LIQUIDITY_WINDOW[0]}..{LIQUIDITY_WINDOW[1]}",
        "stock_start": str(stocks.index[0].date()),
        "etfs": len(etfs.columns),
        "etfs_liquid": list(etfs_liquid.columns),
        "etf_start": str(etfs.index[0].date()),
        "survivorship": "stock universe is today's surviving long-history names; failed names absent",
    }

    # -- benchmarks --------------------------------------------------------
    nifty = buy_and_hold(etfs[["NIFTYBEES"]].dropna(), "NIFTYBEES", ETF_COSTS)
    ew = equal_weight_hold(stocks, STOCK_COSTS)
    # A rule that buys five of six exchange traded funds is an equal-weight
    # basket with extra steps, so that basket is the benchmark it must beat.
    etf_ew = equal_weight_hold(etfs_liquid, ETF_COSTS)
    report["benchmarks"] = {
        "niftybees_buy_and_hold": nifty.metrics(),
        "stock_equal_weight_buy_and_hold": ew.metrics(),
        "liquid_etf_equal_weight_buy_and_hold": etf_ew.metrics(),
    }
    print("NIFTYBEES", nifty.metrics())
    print("EW stocks", ew.metrics())

    # -- 1. RSI oversold entries ------------------------------------------
    rsi_cache = {p: wilder_rsi(stocks, p) for p in (2, 14)}
    for period in (2, 14):
        rsi = rsi_cache[period]
        for threshold in (10, 20, 30):
            entry = rsi < threshold
            for hold in (1, 3, 5, 10, 20):
                w = entry_exit_weights(stocks, entry, hold_days=hold, max_names=20)
                res = run_weights(stocks, w, STOCK_COSTS)
                trials.append(
                    summarise(f"stock rsi{period}<{threshold} hold{hold}d", res)
                    | {"family": "rsi", "wrapper": "stock", "hold_days": hold}
                )
            # exit on RSI recovery above 50 instead of a fixed clock
            w = entry_exit_weights(
                stocks, entry, hold_days=60, exit_signal=rsi > 50, max_names=20
            )
            res = run_weights(stocks, w, STOCK_COSTS)
            trials.append(
                summarise(f"stock rsi{period}<{threshold} exit rsi>50", res)
                | {"family": "rsi", "wrapper": "stock", "hold_days": None}
            )

    # -- 2. Distance below a moving average -------------------------------
    for ma in (20, 50):
        avg = stocks.rolling(ma).mean()
        gap = stocks / avg - 1
        for pct in (5, 10, 15, 20):
            entry = gap < -pct / 100
            for hold in (1, 3, 5, 10, 20):
                w = entry_exit_weights(stocks, entry, hold_days=hold, max_names=20)
                res = run_weights(stocks, w, STOCK_COSTS)
                trials.append(
                    summarise(f"stock {pct}% below ma{ma} hold{hold}d", res)
                    | {"family": "ma_gap", "wrapper": "stock", "hold_days": hold}
                )
            w = entry_exit_weights(
                stocks, entry, hold_days=60, exit_signal=gap > 0, max_names=20
            )
            res = run_weights(stocks, w, STOCK_COSTS)
            trials.append(
                summarise(f"stock {pct}% below ma{ma} exit at ma", res)
                | {"family": "ma_gap", "wrapper": "stock", "hold_days": None}
            )

    # -- 3. Cross-sectional N-day losers, stocks --------------------------
    for lookback in (5, 10, 20):
        for top_k in (10, 20):
            for hold_weeks in (1, 2, 4):
                w = cross_sectional_losers(
                    stocks, lookback=lookback, top_k=top_k, hold_weeks=hold_weeks
                )
                res = run_weights(stocks, w, STOCK_COSTS)
                trials.append(
                    summarise(
                        f"stock losers {lookback}d k{top_k} hold{hold_weeks}w", res
                    )
                    | {
                        "family": "reversal",
                        "wrapper": "stock",
                        "hold_days": hold_weeks * 5,
                    }
                )

    # -- 4. The same reversal in the cheap wrapper ------------------------
    # Run on the whole exchange traded fund panel and on the liquid subset, so a
    # result driven by stale closes in dead funds cannot hide.
    for tag, panel in (("etf", etfs), ("etfliq", etfs_liquid)):
        for lookback in (5, 10, 20):
            # k=2 matters on the six-name liquid panel: buying five of six names
            # is an equal-weight basket wearing a signal's clothes.
            for top_k in (2, 3, 5):
                for hold_weeks in (1, 2, 4):
                    w = cross_sectional_losers(
                        panel, lookback=lookback, top_k=top_k, hold_weeks=hold_weeks
                    )
                    res = run_weights(panel, w, ETF_COSTS)
                    trials.append(
                        summarise(
                            f"{tag} losers {lookback}d k{top_k} hold{hold_weeks}w", res
                        )
                        | {
                            "family": "reversal",
                            "wrapper": tag,
                            "hold_days": hold_weeks * 5,
                        }
                    )

        # RSI oversold, for symmetry with the stock test
        panel_rsi = wilder_rsi(panel, 2)
        for threshold in (10, 20, 30):
            for hold in (1, 3, 5, 10, 20):
                w = entry_exit_weights(
                    panel, panel_rsi < threshold, hold_days=hold, max_names=5
                )
                res = run_weights(panel, w, ETF_COSTS)
                trials.append(
                    summarise(f"{tag} rsi2<{threshold} hold{hold}d", res)
                    | {"family": "rsi", "wrapper": tag, "hold_days": hold}
                )

    report["trials"] = trials
    report["n_trials"] = len(trials)
    print(f"{len(trials)} variants")

    # -- 6. Holding-period curve ------------------------------------------
    curve = {}
    for family in ("rsi", "ma_gap", "reversal"):
        for wrapper in ("stock", "etf", "etfliq"):
            rows = [
                t
                for t in trials
                if t["family"] == family
                and t["wrapper"] == wrapper
                and t.get("hold_days")
            ]
            if not rows:
                continue
            by_hold: dict[int, list[dict]] = {}
            for t in rows:
                by_hold.setdefault(int(t["hold_days"]), []).append(t)
            curve[f"{wrapper}_{family}"] = {
                str(h): {
                    "median_net_cagr_pct": round(
                        float(np.median([x["cagr_pct"] for x in v])), 2
                    ),
                    "best_net_cagr_pct": round(max(x["cagr_pct"] for x in v), 2),
                    "median_gross_cagr_pct": round(
                        float(np.median([x["gross_cagr_pct"] for x in v])), 2
                    ),
                    "median_cost_drag_pct_yr": round(
                        float(np.median([x["cost_drag_pct_yr"] for x in v])), 2
                    ),
                    "median_turnover_per_yr": round(
                        float(np.median([x["turnover_per_yr"] for x in v])), 1
                    ),
                }
                for h, v in sorted(by_hold.items())
            }
    report["holding_period_curve"] = curve

    # -- 5. Regime split ---------------------------------------------------
    index_close = etfs["NIFTYBEES"].dropna()
    above = regime_mask(index_close).reindex(stocks.index).ffill().fillna(False)
    report["regime"] = {}
    rsi2 = rsi_cache[2]
    for label, gate in (("index_above_200dma", above), ("index_below_200dma", ~above)):
        entry = (rsi2 < 10).mul(gate.astype(int), axis=0).astype(bool)
        w = entry_exit_weights(stocks, entry, hold_days=5, max_names=20)
        res = run_weights(stocks, w, STOCK_COSTS)
        report["regime"][label] = res.metrics()

    # -- best rules, both halves ------------------------------------------
    panels = {
        "stock": (stocks, STOCK_COSTS),
        "etf": (etfs, ETF_COSTS),
        "etfliq": (etfs_liquid, ETF_COSTS),
    }
    report["best"] = {}

    # Rebuild the best rules half by half so nothing is fitted across the split.
    def rebuild(name: str):
        def build(frame: pd.DataFrame) -> pd.DataFrame:
            parts = name.split()
            if "losers" in parts:
                lookback = int(parts[2].rstrip("d"))
                top_k = int(parts[3].lstrip("k"))
                weeks = int(parts[4].replace("hold", "").rstrip("w"))
                return cross_sectional_losers(
                    frame, lookback=lookback, top_k=top_k, hold_weeks=weeks
                )
            if parts[1].startswith("rsi"):
                period = int(parts[1][3:].split("<")[0])
                threshold = int(parts[1].split("<")[1])
                cap = 20 if parts[0] == "stock" else 5
                rsi = wilder_rsi(frame, period)
                if parts[2] == "exit":
                    return entry_exit_weights(
                        frame,
                        rsi < threshold,
                        hold_days=60,
                        exit_signal=rsi > 50,
                        max_names=cap,
                    )
                hold = int(parts[2].replace("hold", "").rstrip("d"))
                return entry_exit_weights(
                    frame, rsi < threshold, hold_days=hold, max_names=cap
                )
            # "<pct>% below ma<n> ..."
            pct = int(parts[1].rstrip("%"))
            ma = int(parts[3][2:])
            gap = frame / frame.rolling(ma).mean() - 1
            entry = gap < -pct / 100
            if parts[4] == "exit":
                return entry_exit_weights(
                    frame, entry, hold_days=60, exit_signal=gap > 0, max_names=20
                )
            hold = int(parts[4].replace("hold", "").rstrip("d"))
            return entry_exit_weights(frame, entry, hold_days=hold, max_names=20)

        return build

    # Two selections per wrapper: the best full-sample Sharpe (what a careless
    # search would report) and the best *worst half* among the top ten, which is
    # the only one that means anything.
    for wrapper, (prices, costs) in panels.items():
        pool = sorted(
            (t for t in trials if t["wrapper"] == wrapper),
            key=lambda t: t["sharpe"],
            reverse=True,
        )
        if not pool:
            continue
        shortlist = []
        for cand in pool[:10]:
            h = halves(prices, rebuild(cand["name"]), costs)
            shortlist.append(
                {
                    "rule": cand["name"],
                    "full_sample": cand,
                    "halves": h,
                    "worst_half_sharpe": min(
                        h["first_half"]["sharpe"], h["second_half"]["sharpe"]
                    ),
                }
            )
        robust = max(shortlist, key=lambda r: r["worst_half_sharpe"])
        bench = {"stock": ew, "etf": nifty, "etfliq": etf_ew}[wrapper]
        entry = {"shortlist": shortlist}
        for label, chosen in (("by_full_sharpe", shortlist[0]), ("robust", robust)):
            full = run_weights(prices, rebuild(chosen["rule"])(prices), costs)
            entry[label] = dict(chosen) | {
                "deflated_sharpe": round(
                    deflated_sharpe(
                        chosen["full_sample"]["sharpe"],
                        len(trials),
                        int(chosen["full_sample"]["years"] * TRADING_DAYS),
                    ),
                    3,
                ),
                "vs_benchmark": compare(chosen["rule"], full, bench),
            }
        report["best"][wrapper] = entry
        print(
            wrapper,
            "top:",
            shortlist[0]["rule"],
            shortlist[0]["full_sample"]["sharpe"],
            "| robust:",
            robust["rule"],
            round(robust["worst_half_sharpe"], 3),
        )

    # Benchmarks per half, so the halves are judged against something.
    report["benchmark_halves"] = {
        "niftybees": halves(
            etfs[["NIFTYBEES"]].dropna(), equal_weight_weights, ETF_COSTS
        ),
        "stock_equal_weight": halves(stocks, equal_weight_weights, STOCK_COSTS),
        "liquid_etf_equal_weight": halves(etfs_liquid, equal_weight_weights, ETF_COSTS),
    }

    # -- selectivity check -------------------------------------------------
    # If a cross-sectional rule is picking up reversion, the *more selective*
    # basket should earn more. If net return rises monotonically with basket
    # size instead, the rule is buying diversification and calling it a signal.
    selectivity: dict[str, dict] = {}
    for wrapper in ("stock", "etf", "etfliq"):
        rows = [t for t in trials if t["wrapper"] == wrapper and " losers " in t["name"]]
        by_k: dict[str, list[float]] = {}
        for t in rows:
            k = t["name"].split()[3]
            by_k.setdefault(k, []).append(t["cagr_pct"])
        selectivity[wrapper] = {
            "median_net_cagr_by_basket_size": {
                k: round(float(np.median(v)), 2) for k, v in sorted(by_k.items())
            },
            "equal_weight_benchmark_cagr_pct": {
                "stock": ew.metrics()["cagr_pct"],
                "etf": nifty.metrics()["cagr_pct"],
                "etfliq": etf_ew.metrics()["cagr_pct"],
            }[wrapper],
        }
    report["selectivity_check"] = selectivity

    report["notes"] = [
        "Every stock figure sits on a survivorship-biased universe: today's "
        "long-history NSE names. Reversion rules buy the biggest losers, which is "
        "precisely the population the missing delistings came from, so stock "
        "results here are an upper bound and probably a generous one.",
        "Liquidity ranking uses 2015-2016 only; no full-sample information enters "
        "the universe choice.",
        "All weights are traded one session after the signal (run_weights lag).",
        "Baskets drift between rebalances. A target row is emitted only when the "
        "holding set changes, so the account is not billed for re-equalising an "
        "unchanged book; the benchmarks are true drifting buy-and-hold.",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
