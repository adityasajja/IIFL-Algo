"""Does the high-volatility factor survive a falling market?

The stress test found a monotone, stable +2.26%/month excess spread for high
cross-sectional volatility. But every observation came from a window in which
97% of the 120 names rose. A high-beta tilt looks brilliant in a bull market
and catastrophic in a bear market, and a spread measured on average can hide
that entirely.

This script separates the two states and asks the question that decides whether
the factor is investable:

    In months when the equal-weight market fell, did the high-vol quintile
    outperform, match, or lose harder?

Three possible answers, three meanings:

- **outperforms in down months too** -> a genuine anomaly (leverage-constraint
  or lottery-demand driven). Investable.
- **matches in down months** -> the spread is down-market beta that is simply
  earning its risk premium. Investable only if you knowingly accept the tail.
- **loses harder in down months** -> it is leverage. The spread is compensation
  for risk you are taking, not an edge, and it will take back the gains in the
  first real drawdown.

Also reports the conditional Sharpe in each state, because a factor that earns
2%/month with 12% monthly volatility is not the same as one earning 2% with
4%, and only the ratio tells you which you have.

Usage:
    ./.venv/Scripts/python.exe scripts/research_vol_factor_regime.py
"""

# ruff: noqa: I001
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

PANEL = Path("data/signals/panel_nseeq_120.parquet")
HORIZON = 21


def load_close() -> pd.DataFrame:
    frame = pd.read_parquet(PANEL)
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame.pivot_table(index="ts", columns="symbol", values="close").sort_index()


def main() -> int:
    close = load_close()
    returns = close.pct_change()
    ew = returns.mean(axis=1).fillna(0)

    # Month-end rebalance, point-in-time factor from the prior 126 sessions.
    #
    # IMPORTANT: use *one-month* forward returns here, not HORIZON-day ones.
    # Resampling to month-end already sets the spacing, so applying a 21-session
    # forward return on top of it stacks overlapping windows and compounds the
    # same move several times — the first version of this script reported a
    # +5.7e15% return because of exactly that. The horizon is the sampling
    # frequency, and it must be applied once.
    monthly = close.resample("ME").last()
    realised = returns.rolling(126).std().shift(1)
    vol_m = realised.resample("ME").last()
    fwd_m = monthly.pct_change().shift(-1)
    mkt_m = monthly.mean(axis=1).pct_change().shift(-1)

    rows = []
    for ts in monthly.index[6:-1]:
        v = vol_m.loc[:ts].iloc[-1].dropna()
        r = fwd_m.loc[ts].dropna()
        m = mkt_m.loc[ts]
        common = v.index.intersection(r.index)
        if len(common) < 20 or pd.isna(m):
            continue
        n = max(len(common) // 5, 1)
        ranked = v[common].rank()
        top = set(ranked.nlargest(n).index)
        bot = set(ranked.nsmallest(n).index)
        rows.append({
            "ts": ts,
            "mkt": float(m),
            "top": float(r[list(top)].mean()),
            "bot": float(r[list(bot)].mean()),
            "spread": float(r[list(top)].mean() - r[list(bot)].mean()),
            "hi_minus_mkt": float(r[list(top)].mean() - m),
        })

    df = pd.DataFrame(rows).set_index("ts")
    print(f"{len(df)} monthly observations, {df.index[0]:%Y-%m} -> {df.index[-1]:%Y-%m}\n")

    print("== market state vs factor performance (monthly, decimals) ==")
    up, down = df[df["mkt"] > 0], df[df["mkt"] <= 0]
    for label, part in [("up months", up), ("down months", down)]:
        if part.empty:
            print(f"  {label}: none")
            continue
        print(f"  {label:<12} n={len(part):>3}  "
              f"mkt {part['mkt'].mean() * 100:+6.2f}%  "
              f"top {part['top'].mean() * 100:+6.2f}%  "
              f"bot {part['bot'].mean() * 100:+6.2f}%  "
              f"spread {part['spread'].mean() * 100:+6.2f}%")
    print()

    print("== does the top quintile beat the market in each state? ==")
    for label, part in [("up", up), ("down", down)]:
        if part.empty:
            continue
        excess = part["hi_minus_mkt"]
        print(f"  {label:<5} top-minus-market mean {excess.mean() * 100:+6.2f}%/mo   "
              f"(median {excess.median() * 100:+6.2f}%)   "
              f"wins {int((excess > 0).sum())}/{len(excess)}")
    print()

    print("== risk-adjusted, per state ==")
    for label, part in [("all", df), ("up", up), ("down", down)]:
        if part.empty:
            continue
        sp = part["spread"]
        sd = sp.std(ddof=1)
        sharpe = sp.mean() / sd * np.sqrt(12) if sd > 0 else 0.0
        print(f"  {label:<5} spread mean {sp.mean() * 100:+6.2f}%/mo  "
              f"vol {sd * 100:5.2f}%/mo  ann Sharpe {sharpe:+5.2f}")
    print()

    # ---- worst episodes ---------------------------------------------------
    print("== the five worst market months, and what the factor did ==")
    worst = df.nsmallest(5, "mkt")
    for ts, r in worst.iterrows():
        print(f"  {ts:%Y-%m}  mkt {r['mkt'] * 100:+6.2f}%  "
              f"top {r['top'] * 100:+6.2f}%  bot {r['bot'] * 100:+6.2f}%  "
              f"spread {r['spread'] * 100:+6.2f}%")
    print()

    # ---- drawdown of a naive long-only top-quintile portfolio -------------
    print("== equity of a long-only top-quintile portfolio vs the market ==")
    top_series, mkt_series = [], []
    for ts, r in df.iterrows():
        top_series.append(r["top"])
        mkt_series.append(r["mkt"])
    tops = (1 + pd.Series(top_series, index=df.index)).cumprod()
    mkts = (1 + pd.Series(mkt_series, index=df.index)).cumprod()
    for name, s in [("top quintile", tops), ("market (EW)", mkts)]:
        dd = (s / s.cummax() - 1).min() * 100
        print(f"  {name:<14} total {(s.iloc[-1] - 1) * 100:+8.1f}%  maxDD {dd:6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
