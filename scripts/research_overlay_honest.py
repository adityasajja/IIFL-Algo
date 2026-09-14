"""Can any overlay beat buy-and-hold on risk-adjusted terms?

Context for why this is the right question
------------------------------------------
Research on this repo's data established three things:

1. There is no reliable cross-sectional alpha in these 120 names. Momentum's
   spread fell from +1.01% to +0.27%/month once beta was removed; the only
   monotone factor (high realised vol) is leverage -- it wins +4.07%/month in
   up months and loses -2.06% in down months.
2. The liquid-120 universe carries a +356 point survivorship gap versus the
   unbiased long-history set, so its absolute returns are not quotable.
3. Index-level trend filtering and volatility targeting both *reduced* returns
   more than they reduced risk.

That leaves one hypothesis worth testing, and it is the one a professional
would test first: that nothing predicts *return*, but something predicts
*risk*, and risk is controllable. If a simple, pre-specified overlay improves
the return/drawdown ratio out of sample on the unbiased universe, it is worth
having -- not because it makes money, but because it lets you hold the same
asset with less chance of being forced out at the bottom.

The rules are fixed in advance and tested unchanged on every split, so the
result cannot be the product of a search. The decision metric is **Calmar**
(CAGR / max drawdown) because that is the ratio a leveraged or drawdown-averse
investor actually faces, and it is the one thing the earlier overlays failed.

Usage:
    ./.venv/Scripts/python.exe scripts/research_overlay_honest.py
"""

# ruff: noqa: I001
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = Path("data/iifl_daily/NSEEQ")
MIN_BARS = 1000


def unbiased_panel() -> pd.DataFrame:
    """Every cache symbol with a long history — no liquidity selection."""
    series = {}
    for path in sorted(CACHE.glob("*.parquet")):
        try:
            frame = pd.read_parquet(path, columns=["ts", "close"])
        except Exception:  # noqa: BLE001
            continue
        if len(frame) < MIN_BARS:
            continue
        frame = frame.copy()
        frame["ts"] = pd.to_datetime(frame["ts"])
        series[path.stem.upper()] = frame.set_index("ts")["close"]
    return pd.DataFrame(series).sort_index()


def metrics(rets: pd.Series, label: str) -> dict:
    rets = rets.fillna(0.0)
    eq = (1 + rets).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    years = len(rets) / 252
    cagr = (eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0
    vol = rets.std() * np.sqrt(252)
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0.0
    return {
        "label": label,
        "total_pct": (eq.iloc[-1] - 1) * 100,
        "cagr_pct": cagr * 100,
        "vol_pct": vol * 100,
        "sharpe": sharpe,
        "maxdd_pct": dd * 100,
        "calmar": (cagr / abs(dd)) if dd < 0 else float("nan"),
        "exposure_pct": 100.0,
    }


def overlays(ew: pd.Series) -> dict[str, pd.Series]:
    """Position weight (0..1) applied to the next day's return.

    Every rule is lagged by one bar. A weight computed from a return that
    includes the day it is applied to is the most common way an overlay
    backtest lies, and it can turn any rule into a winner.
    """
    out = {}
    n = len(ew)
    ones = pd.Series(1.0, index=ew.index)

    # 1. Vol scaling: hold less when recent vol is high. Target 15% annual.
    vol20 = ew.rolling(20).std() * np.sqrt(252)
    raw = (0.15 / vol20).clip(upper=1.0)
    out["vol_scale_20"] = raw.shift(1).fillna(0.0)

    # 2. Only the vol brake, no leverage up: full size below median vol.
    med = vol20.rolling(252).median()
    out["vol_brake"] = (vol20 <= med).astype(float).shift(1).fillna(0.0)

    # 3. Trend: hold only above the 200-session average of the index.
    ma200 = (1 + ew).cumprod().rolling(200).mean()
    px = (1 + ew).cumprod()
    out["trend_200"] = (px > ma200).astype(float).shift(1).fillna(0.0)

    # 4. Drawdown brake: cut to half size after a 10% drawdown, restore on a
    #    new high. Asymmetric on purpose -- de-risking fast, re-risking slow.
    eq = (1 + ew).cumprod()
    dd = eq / eq.cummax() - 1
    w = pd.Series(np.where(dd <= -0.10, 0.5, 1.0), index=ew.index)
    out["dd_brake"] = w.shift(1).fillna(0.0)

    # 5. Combination: vol brake AND trend, the two with independent logic.
    out["vol_brake+trend"] = (
        (vol20 <= med).astype(float) * (px > ma200).astype(float)
    ).shift(1).fillna(0.0)

    out["buy_hold"] = ones
    return out


def main() -> int:
    close = unbiased_panel()
    rets = close.pct_change()
    ew = rets.mean(axis=1).fillna(0)
    print(f"unbiased universe: {close.shape[1]} names, {len(close)} sessions")
    print(f"window: {close.index[0]:%Y-%m-%d} -> {close.index[-1]:%Y-%m-%d}\n")

    splits = {
        "full": close.index,
        "first half": close.index[: len(close) // 2],
        "second half": close.index[len(close) // 2:],
    }

    for split_name, index in splits.items():
        e = ew.loc[index]
        print(f"== {split_name} ({index[0]:%Y-%m} -> {index[-1]:%Y-%m}) ==")
        header = (f"{'overlay':<18} {'CAGR':>7} {'vol':>7} {'Sharpe':>7} "
                  f"{'maxDD':>8} {'Calmar':>7} {'expo':>6}")
        print(header)
        print("-" * len(header))
        rows = []
        for name, weight in overlays(e).items():
            w = weight.loc[index] if len(weight) == len(ew) else weight
            m = metrics(e * w, name)
            m["exposure_pct"] = w.mean() * 100
            rows.append(m)
            print(f"{name:<18} {m['cagr_pct']:>6.1f}% {m['vol_pct']:>6.1f}% "
                  f"{m['sharpe']:>7.2f} {m['maxdd_pct']:>7.1f}% "
                  f"{m['calmar']:>7.2f} {m['exposure_pct']:>5.0f}%")
        base = next(r for r in rows if r["label"] == "buy_hold")
        winners = [r for r in rows if r["calmar"] > base["calmar"] and r["label"] != "buy_hold"]
        print(f"\n  beat buy-and-hold on Calmar: "
              f"{', '.join(r['label'] for r in winners) if winners else 'NONE'}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
