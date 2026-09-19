"""Let a model search for the pattern, and grade it only on years it never saw.

The hand-picked patterns tried so far are a tiny corner of what could be tested. A
gradient-boosted model can weigh dozens of measurements at once and find interactions
nobody would think to write down. The danger is the opposite one: with that much
freedom it will happily memorise the past. So the rules of the test are strict:

* **Walk-forward.** For each test year, train only on the years before it. The model
  never sees a row from the year it is scored on, and is retrained the next year.
* **Calibration is the real question.** The user wants a rule that is right 60% of the
  time. So the test is whether, when the model says "60%", things actually happen 60%
  of the time, not whether it ranks stocks well.
* **A week is one observation.** Hundreds of stocks share a week's market move, so the
  top few picks per week are scored, and reported per week, not per stock-row.

Entry is modelled at the signal close, which is slightly generous (a real order fills
the next session). The stock universe is today's long-history names, so failed
companies are absent and oversold-bounce style results are flattered.

Run:  ./.venv/Scripts/python.exe scripts/study_ml_walkforward.py
"""

from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from atr.data.hygiene import reverting_spike_mask
from atr.research.hunt import STOCK_COSTS, STOCKS, stock_universe

warnings.filterwarnings("ignore")
OUT = STOCKS.parents[2] / "research" / "ml_walkforward.json"
HIT = 0.02
ROUND_TRIP = STOCK_COSTS.round_trip_pct() / 100
FIRST_TEST_YEAR = 2019


def load() -> dict[str, pd.DataFrame]:
    names = stock_universe(min_bars=1500)
    out: dict[str, list[pd.Series]] = {k: [] for k in ("close", "high", "low", "volume")}
    for s in names:
        df = pd.read_parquet(STOCKS / f"{s}.parquet", columns=["ts", "close", "high", "low", "volume"])
        df = df[(df["close"] > 0) & (df["volume"] >= 0)].drop_duplicates("ts").set_index("ts").loc["2015-01-01":]
        bad = reverting_spike_mask(df["close"]).to_numpy()
        df.loc[df.index[bad], "close"] = np.nan
        for k in out:
            out[k].append(df[k].rename(s))
    return {k: pd.concat(v, axis=1, sort=True) for k, v in out.items()}


def feature_frames(d: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Numeric measurements per stock per day, each using only data up to that close."""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    ret = c.pct_change()
    f: dict[str, pd.DataFrame] = {}
    for n in (1, 2, 3, 5, 10, 20, 60, 120):
        f[f"ret_{n}d"] = c / c.shift(n) - 1
    f["vol_10d"] = ret.rolling(10).std()
    f["vol_20d"] = ret.rolling(20).std()
    f["vol_60d"] = ret.rolling(60).std()
    f["vol_ratio_10_60"] = f["vol_10d"] / f["vol_60d"]
    delta = c.diff()
    gain, loss = delta.clip(lower=0).ewm(alpha=1 / 14).mean(), (-delta.clip(upper=0)).ewm(alpha=1 / 14).mean()
    f["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    for n in (20, 50, 200):
        f[f"dist_ema{n}"] = c / c.ewm(span=n).mean() - 1
    f["dist_52w_high"] = c / c.rolling(252).max() - 1
    f["dist_52w_low"] = c / c.rolling(252).min() - 1
    f["dist_20d_high"] = c / c.rolling(20).max() - 1
    f["range_10d"] = (h.rolling(10).max() - l.rolling(10).min()) / c
    f["today_range"] = (h - l) / c
    f["vol_ratio_5_20"] = v.rolling(5).mean() / v.rolling(20).mean()
    f["vol_today_vs_20"] = v / v.rolling(20).mean()
    f["gap_max_5d"] = ret.rolling(5).max()
    f["drop_max_5d"] = ret.rolling(5).min()
    f["up_days_10"] = (ret > 0).rolling(10).mean()
    # Market context: the same measurement on the average stock. A stock's week is mostly
    # its market's week, so the model needs to know what the market is doing.
    mkt = ret.mean(axis=1)
    mkt_level = (1 + mkt.fillna(0)).cumprod()
    for n, name in ((5, "mkt_ret_5d"), (20, "mkt_ret_20d"), (60, "mkt_ret_60d")):
        f[name] = pd.DataFrame(np.repeat((mkt_level / mkt_level.shift(n) - 1).to_numpy()[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
    f["mkt_vol_20d"] = pd.DataFrame(np.repeat(mkt.rolling(20).std().to_numpy()[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
    f["mkt_above_200d"] = pd.DataFrame(np.repeat((mkt_level > mkt_level.rolling(200).mean()).astype(float).to_numpy()[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
    # Where the stock ranks against its peers today: relative strength is the classic pattern.
    f["rank_ret_20d"] = f["ret_20d"].rank(axis=1, pct=True)
    f["rank_ret_5d"] = f["ret_5d"].rank(axis=1, pct=True)
    f["rank_vol_20d"] = f["vol_20d"].rank(axis=1, pct=True)
    return f


def dataset() -> tuple[pd.DataFrame, list[str]]:
    d = load()
    feats = feature_frames(d)
    close = d["close"]
    weeks = list(close.groupby(close.index.to_period("W-FRI")).tail(1).index)
    fwd = (close.shift(-5) / close - 1).loc[weeks]
    stacked = {"fwd": fwd.stack()}
    for name, frame in feats.items():
        stacked[name] = frame.loc[weeks].stack()
    df = pd.DataFrame(stacked).replace([np.inf, -np.inf], np.nan).dropna(subset=["fwd"])
    df = df.dropna(thresh=int(0.8 * df.shape[1]))
    df["hit"] = (df["fwd"] >= HIT).astype(int)
    df["date"] = df.index.get_level_values(0)
    df["year"] = df["date"].dt.year
    return df, list(feats)


def main() -> None:
    df, cols = dataset()
    print(f"{len(df):,} stock-weeks, {len(cols)} measurements, {df['date'].nunique()} weeks, base rate {100 * df['hit'].mean():.1f}%\n")

    scored = []
    print(f"{'test year':<10}{'base':>7}{'AUC':>7}{'top-3/wk P':>12}{'P(model>=.5)':>14}{'n':>6}{'P(model>=.6)':>14}{'n':>6}")
    for year in range(FIRST_TEST_YEAR, int(df["year"].max()) + 1):
        train, test = df[df["year"] < year], df[df["year"] == year].copy()
        if len(train) < 20000 or len(test) < 2000:
            continue
        model = HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.05, max_iter=200, min_samples_leaf=200, l2_regularization=1.0, random_state=0
        )
        model.fit(train[cols], train["hit"])
        test["p"] = model.predict_proba(test[cols])[:, 1]
        scored.append(test)
        auc = roc_auc_score(test["hit"], test["p"])
        top3 = test.sort_values("p", ascending=False).groupby("date").head(3)
        hi5, hi6 = test[test["p"] >= 0.5], test[test["p"] >= 0.6]
        cell = lambda x: f"{100 * x['hit'].mean():>13.1f}%" if len(x) >= 20 else f"{'--':>14}"  # noqa: E731
        print(f"{year:<10}{100 * test['hit'].mean():>6.1f}%{auc:>7.3f}{100 * top3['hit'].mean():>11.1f}%{cell(hi5)}{len(hi5):>6}{cell(hi6)}{len(hi6):>6}")

    all_test = pd.concat(scored)
    print("\nCALIBRATION over every out-of-sample year pooled: when the model says X, how often does it happen?")
    print(f"{'model says':<14}{'actually':>10}{'n':>9}{'net avg %/wk':>15}")
    bins = [0, 0.2, 0.3, 0.4, 0.5, 0.6, 1.01]
    all_test["bin"] = pd.cut(all_test["p"], bins, right=False)
    rows = []
    for b, g in all_test.groupby("bin", observed=True):
        net = 100 * (g["fwd"].mean() - ROUND_TRIP)
        print(f"{str(b):<14}{100 * g['hit'].mean():>9.1f}%{len(g):>9,}{net:>15.3f}")
        rows.append({"bin": str(b), "actual_p_hit": round(100 * g["hit"].mean(), 2), "n": int(len(g)), "net_pct_per_week": round(net, 3)})

    top3 = all_test.sort_values("p", ascending=False).groupby("date").head(3)
    by_week = top3.groupby("date").agg(hit=("hit", "mean"), ret=("fwd", "mean"))
    print(f"\nTop 3 picks each week, {len(by_week)} out-of-sample weeks:")
    print(f"   P(a pick gains >= 2%)        {100 * top3['hit'].mean():.1f}%")
    print(f"   weeks where the 3-stock basket gained >= 2%:  {100 * (by_week['ret'] >= HIT).mean():.1f}%")
    print(f"   basket average net per week: {100 * (by_week['ret'].mean() - ROUND_TRIP):+.3f}%   median {100 * by_week['ret'].median():+.3f}%")
    print(f"   base rate for comparison:     {100 * all_test['hit'].mean():.1f}%")

    # Is the top-3 result broad, or carried by a few great weeks or one lucky year?
    by_week["year"] = by_week.index.year
    print("\nTop-3 basket by year (net of costs):")
    for y, g in by_week.groupby("year"):
        print(f"   {y}: {len(g):>3} weeks   avg {100 * (g['ret'].mean() - ROUND_TRIP):+6.2f}%/wk   median {100 * (g['ret'].median() - ROUND_TRIP):+6.2f}%   weeks>=2%: {100 * (g['ret'] >= HIT).mean():>4.0f}%")
    ordered = by_week["ret"].sort_values()
    trimmed = ordered.iloc[: int(len(ordered) * 0.9)]
    print(f"   dropping the best 10% of weeks: avg {100 * (trimmed.mean() - ROUND_TRIP):+.3f}%/wk   worst week {100 * ordered.iloc[0]:+.1f}%   best {100 * ordered.iloc[-1]:+.1f}%")
    curve = (1 + by_week["ret"] - ROUND_TRIP).cumprod()
    print(f"   compounded over the {len(by_week)} weeks: x{curve.iloc[-1]:.1f}   max drawdown {100 * float((curve / curve.cummax() - 1).min()):.0f}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "method": "gradient-boosted classifier, expanding-window walk-forward, one retrain per test year",
        "measurements": cols,
        "calibration_pooled_out_of_sample": rows,
        "top3_per_week": {
            "p_pick_hits": round(100 * float(top3["hit"].mean()), 2),
            "weeks_basket_gained_2pct": round(100 * float((by_week["ret"] >= HIT).mean()), 2),
            "avg_net_pct_per_week": round(100 * float(by_week["ret"].mean() - ROUND_TRIP), 3),
            "weeks": int(len(by_week)),
        },
        "base_rate_pct": round(100 * float(all_test["hit"].mean()), 2),
        "survivorship_note": "universe is today's long-history names; failed companies are absent",
    }, indent=1), encoding="utf8")


if __name__ == "__main__":
    main()
