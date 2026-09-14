"""Is the momentum "edge" just a volatility/leverage effect in disguise?

The factor scan found every momentum window U-shaped: the top quintile wins,
but so does the bottom, and the middle loses. A monotone factor does not look
like that. Two candidate explanations:

(a) **Volatility.** Both extremes of a momentum ranking may simply be the
    highest-volatility names, so "momentum" would be a noisy proxy for the
    high-vol/beta effect the scan isolated. Test: correlation between the
    momentum factor and realised vol, and momentum's spread *within* vol
    terciles. If the spread survives inside a vol bucket, momentum is
    contributing something of its own; if it collapses towards zero, it was
    vol (and therefore beta) all along.

(b) **Beta.** If the top quintile's returns are explained by a higher loading
    on the equal-weight market, the spread is leverage, not selection. Test:
    beta-demean the forward return and recompute.

Also tests the reverse form of the volatility finding at the *portfolio*
level, because a time-series effect and a cross-sectional effect are different
claims and conflating them is the classic error:

- **time series**: low-volatility *periods* are followed by higher returns
- **cross section**: low-volatility *stocks* outperform high-volatility stocks

The scan suggested the latter is false here (high-vol stocks won). This script
checks whether the former is tradeable once you account for the beta it carries.

Usage:
    ./.venv/Scripts/python.exe scripts/research_momentum_or_vol.py
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


def spread_by(long: pd.DataFrame, bucket_col: str, factor: str, n: int = 5) -> pd.Series:
    ranked = long[factor].rank(method="first")
    labels = pd.qcut(ranked, n, labels=False)
    return long.groupby(labels)["fwd"].mean() * 100.0


def main() -> int:
    close = load_close()
    returns = close.pct_change()
    fwd = close.pct_change(HORIZON).shift(-HORIZON)
    mom = (close.shift(21) / close.shift(147) - 1).shift(1)
    vol = returns.rolling(126).std().shift(1)

    long = pd.concat(
        [mom.stack().rename("mom"), vol.stack().rename("vol"), fwd.stack().rename("fwd")],
        axis=1,
    ).dropna()

    print(f"n = {len(long):,} symbol-months, "
          f"{close.index[0]:%Y-%m} -> {close.index[-1]:%Y-%m}\n")

    # ---- (a) is momentum a proxy for vol? ---------------------------------
    rank_corr = long["mom"].rank().corr(long["vol"].rank())
    print(f"(a) rank corr(momentum, vol) = {rank_corr:+.3f}")
    print("    -> momentum is", "strongly" if abs(rank_corr) > 0.6 else "only weakly",
          "related to volatility\n")

    print("    momentum quintile spread WITHIN each vol tercile:")
    long = long.copy()
    long["vter"] = pd.qcut(long["vol"].rank(method="first"), 3, labels=False)
    for v in range(3):
        part = long[long["vter"] == v]
        table = spread_by(part, "vter", "mom")
        print(f"      vol T{v + 1} (n={len(part):,}): "
              + "  ".join(f"Q{i + 1} {x:+.2f}%" for i, x in enumerate(table))
              + f"   spread {table.iloc[-1] - table.iloc[0]:+.2f}%")
    print()

    # ---- (b) beta-demeaned momentum ---------------------------------------
    market = long.groupby(level=0)["fwd"].mean()
    long["mkt"] = long.index.get_level_values(0).map(market)
    long["exc"] = long["fwd"] - long["mkt"]
    table = spread_by(long, "vter", "mom")
    print("(b) momentum quintiles on BETA-DEMEANED forward return (excess vs EW):")
    ranked = long["mom"].rank(method="first")
    labels = pd.qcut(ranked, 5, labels=False)
    excess_table = long.groupby(labels)["exc"].mean() * 100.0
    print("      " + "  ".join(f"Q{i + 1} {x:+.2f}%" for i, x in enumerate(excess_table))
          + f"   spread {excess_table.iloc[-1] - excess_table.iloc[0]:+.2f}%")
    print()

    # ---- cross-sectional vol, beta-demeaned --------------------------------
    vol_table = long.groupby(pd.qcut(long["vol"].rank(method="first"), 5, labels=False))["exc"].mean() * 100.0
    print("    vols quintiles on BETA-DEMEANED return (the honest cross-sectional test):")
    print("      " + "  ".join(f"Q{i + 1} {x:+.2f}%" for i, x in enumerate(vol_table))
          + f"   spread {vol_table.iloc[-1] - vol_table.iloc[0]:+.2f}%")
    print("    -> a spread near zero means the cross-sectional vol effect was beta\n")

    # ---- time-series: does low index vol precede high index return? --------
    ew = returns.mean(axis=1).fillna(0)
    idx = (1 + ew).cumprod()
    idx_vol = ew.rolling(20).std() * np.sqrt(252)
    fwd_idx = idx.pct_change(HORIZON).shift(-HORIZON) * 100
    ts = pd.concat([idx_vol.rename("v"), fwd_idx.rename("f")], axis=1).dropna()
    ts_table = ts.groupby(pd.qcut(ts["v"].rank(method="first"), 5, labels=False))["f"].mean()
    print("(c) TIME-SERIES: index vol quintile -> mean forward 21d return")
    print("      " + "  ".join(f"Q{i + 1} {x:+.2f}%" for i, x in enumerate(ts_table))
          + f"   spread {ts_table.iloc[-1] - ts_table.iloc[0]:+.2f}%")
    ar1 = ts["v"].rank().corr(ts["f"].rank())
    print(f"      rank corr(index vol, forward return) = {ar1:+.3f}")
    print("    -> negative means low-vol periods precede higher returns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
