"""Batched parameter sweep for the SMA-crossover family, on the GPU when there is one.

What it answers: *across the whole market, which (fast, slow) windows would have
worked best?* — for thousands of pairs at once, in seconds instead of the many
minutes the event-driven backtester needs for the same grid.

What it is not: a backtest you can trade on. It is a **screening pass**, and
deliberately simpler than ``atr.backtest``:

* signals act on the next bar's *close-to-close* return, not a next-open fill;
* every symbol is weighted equally and capital is not constrained, so the number
  is "average per-name result", not a portfolio with sizing and cash limits;
* costs are a flat basis-point charge per unit of position change.

Use it to narrow a large grid to a short list, then run the real engine on that
list. The long-only entry/exit rule matches ``SmaCrossover`` for equities: enter
on the bar the fast average crosses above the slow one, exit when it crosses
below, hold in between.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np

TRADING_DAYS = 252


# --------------------------------------------------------------------------- data
def close_matrix(
    exchange: str = "NSEEQ",
    *,
    days: int = 750,
    min_bars: int = 250,
    max_symbols: int | None = None,
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Closes as a dense ``[dates, symbols]`` float array (NaN where a name has no bar).

    Read from the consolidated panel, so it costs one file read. Names with fewer than
    ``min_bars`` bars in the window are dropped: a 40-bar history cannot support a
    200-day average and would only add NaNs.
    """
    import polars as pl

    from atr.data import panel
    from atr.data.history import CACHE_ROOT

    source = CACHE_ROOT / exchange.upper()
    panel.frames(source)  # makes sure the panel file is current
    _, pq, _ = panel._paths(source)
    df = pl.read_parquet(pq, columns=["symbol", "ts", "close"])
    last = df["ts"].max()
    df = df.filter(pl.col("ts") > last - pl.duration(days=int(days * 1.5)))

    # A name can exist as both "X" and "X-EQ" (duplicate files, often with different
    # lengths): keep the copy with the most recent and longest history.
    info = (
        df.group_by("symbol")
        .agg(pl.col("ts").max().alias("last"), pl.len().alias("n"))
        .with_columns(pl.col("symbol").str.replace(r"-EQ$", "").alias("ticker"))
        .sort(["ticker", "last", "n"], descending=[False, True, True])
        .unique(subset="ticker", keep="first", maintain_order=True)
    )
    df = df.filter(pl.col("symbol").is_in(info["symbol"].to_list()))
    # Drop thin histories before pivoting: the pivot cost is per column, and a name
    # with too few bars is discarded afterwards anyway.
    enough = df.group_by("symbol").len().filter(pl.col("len") >= min_bars)["symbol"].to_list()
    df = df.filter(pl.col("symbol").is_in(enough))

    wide = df.pivot(on="symbol", index="ts", values="close").sort("ts").tail(days)
    dates = wide["ts"].to_numpy()
    names = [c for c in wide.columns if c != "ts"]
    mat = wide.select(names).to_numpy().astype(np.float64)
    ok = np.isfinite(mat).sum(axis=0) >= min_bars
    mat, names = mat[:, ok], [n for n, k in zip(names, ok) if k]
    if max_symbols and len(names) > max_symbols:
        mat, names = mat[:, :max_symbols], names[:max_symbols]
    return mat, names, dates


# ------------------------------------------------------------------------- core
@dataclass
class SweepResult:
    rows: list[dict[str, Any]]
    combos: int
    symbols: int
    bars: int
    seconds: float
    device: str

    def top(self, n: int = 10, by: str = "sharpe") -> list[dict[str, Any]]:
        return sorted(self.rows, key=lambda r: (r[by] is None, -(r[by] or 0)))[:n]


def _rolling_mean(cs: Any, ok_cs: Any, n: int, torch: Any) -> Any:
    """Mean of the last ``n`` bars per symbol; NaN until ``n`` valid bars exist."""
    T = cs.shape[0]
    out = torch.full_like(cs, float("nan"), dtype=torch.float32)
    if n > T:
        return out
    total = cs[n - 1 :] - torch.cat([torch.zeros_like(cs[:1]), cs[: T - n]], 0)
    count = ok_cs[n - 1 :] - torch.cat([torch.zeros_like(ok_cs[:1]), ok_cs[: T - n]], 0)
    mean = (total / n).to(torch.float32)
    out[n - 1 :] = torch.where(count == n, mean, out[n - 1 :])
    return out


def sma_cross_sweep(
    close: np.ndarray,
    fasts: list[int],
    slows: list[int],
    *,
    device: str = "auto",
    cost_bps: float = 10.0,
    chunk: int = 16,
) -> SweepResult:
    """Evaluate every ``fast < slow`` pair. ``close`` is ``[dates, symbols]``."""
    import torch

    from atr.compute.device import get_device

    dev = get_device(device)
    t0 = time.perf_counter()
    pairs = [(f, s) for f, s in product(sorted(set(fasts)), sorted(set(slows))) if f < s]
    T, S = close.shape

    c64 = torch.as_tensor(close, dtype=torch.float64, device=dev)
    valid = torch.isfinite(c64)
    c0 = torch.where(valid, c64, torch.zeros_like(c64))
    # float64 cumulative sums: prices near 1e3 summed over hundreds of bars lose the
    # precision a crossover test needs in float32.
    cs = torch.cumsum(c0, 0)
    ok_cs = torch.cumsum(valid.to(torch.float64), 0)

    price = c0.to(torch.float32)
    prev = torch.cat([price[:1], price[:-1]], 0)
    both = valid & torch.cat([torch.zeros_like(valid[:1]), valid[:-1]], 0)
    ret = torch.where(both, price / torch.where(prev > 0, prev, torch.ones_like(prev)) - 1, torch.zeros_like(price))
    nvalid = both.sum(1).clamp(min=1).to(torch.float32)  # names with a return on each date

    windows = sorted({n for p in pairs for n in p})
    sma = {n: _rolling_mean(cs, ok_cs, n, torch) for n in windows}
    idx = torch.arange(T, device=dev).view(1, T, 1)
    cost = cost_bps / 1e4

    rows: list[dict[str, Any]] = []
    for i in range(0, len(pairs), chunk):
        batch = pairs[i : i + chunk]
        fast = torch.stack([sma[f] for f, _ in batch])  # [P, T, S]
        slow = torch.stack([sma[s] for _, s in batch])
        have = torch.isfinite(fast) & torch.isfinite(slow)
        above = have & (fast > slow)
        below = have & (fast < slow)
        had = torch.cat([torch.zeros_like(have[:, :1]), have[:, :-1]], 1)
        pa = torch.cat([torch.zeros_like(above[:, :1]), above[:, :-1]], 1)
        pb = torch.cat([torch.zeros_like(below[:, :1]), below[:, :-1]], 1)
        # Entry: fast above slow now, and not above on the previous bar (a cross, or the
        # first bar both averages exist while already above — which the strategy skips,
        # so require the previous bar to have averages too).
        cross_up = above & had & ~pa
        cross_dn = below & had & ~pb

        # State machine as a forward fill: position = value of the latest event.
        event = cross_up | cross_dn
        last = torch.cummax(torch.where(event, idx.expand_as(event), torch.full_like(event, -1, dtype=torch.long)), 1).values
        up_at = torch.gather(cross_up.to(torch.float32), 1, last.clamp(min=0))
        pos = torch.where(last >= 0, up_at, torch.zeros_like(up_at))

        held = torch.cat([torch.zeros_like(pos[:, :1]), pos[:, :-1]], 1)  # act on the next bar
        turnover = (held - torch.cat([torch.zeros_like(held[:, :1]), held[:, :-1]], 1)).abs()
        per_name = held * ret.unsqueeze(0) - turnover * cost * both.unsqueeze(0)
        port = per_name.sum(2) / nvalid  # [P, T] equal-weight average over names

        equity = torch.cumprod(1 + port, 1)
        peak = torch.cummax(equity, 1).values
        max_dd = (equity / peak - 1).min(1).values
        total = equity[:, -1] - 1
        mean = port.mean(1)
        std = port.std(1)
        sharpe = torch.where(std > 0, mean / std * (TRADING_DAYS**0.5), torch.zeros_like(std))
        trades = cross_up.sum((1, 2))
        exposure = held.mean((1, 2))

        out = torch.stack([total, sharpe, max_dd, trades.to(torch.float32), exposure], 1).cpu().numpy()
        for (f, s), (tr, sh, dd, n, ex) in zip(batch, out):
            rows.append(
                {
                    "fast": f,
                    "slow": s,
                    "total_return": float(tr),
                    "sharpe": float(sh),
                    "max_drawdown": float(dd),
                    "entries": int(n),
                    "exposure": float(ex),
                }
            )
        del fast, slow, have, above, below, per_name, held, pos, equity

    if dev.type == "cuda":
        torch.cuda.synchronize()
    return SweepResult(rows, len(pairs), S, T, time.perf_counter() - t0, dev.type)


def reference_sma_cross(close: np.ndarray, fast: int, slow: int, cost_bps: float = 10.0) -> dict[str, float]:
    """The same screen as plain per-symbol loops. Slow; for checking the tensor version."""
    T, S = close.shape
    cost = cost_bps / 1e4
    port = np.zeros(T)
    nvalid = np.zeros(T)
    entries = 0
    for j in range(S):
        c = close[:, j]
        ok = np.isfinite(c)
        sf = np.full(T, np.nan)
        ss = np.full(T, np.nan)
        for t in range(T):
            for w, arr in ((fast, sf), (slow, ss)):
                if t >= w - 1 and ok[t - w + 1 : t + 1].all():
                    arr[t] = c[t - w + 1 : t + 1].mean()
        pos = np.zeros(T)
        cur = 0.0
        for t in range(T):
            have = np.isfinite(sf[t]) and np.isfinite(ss[t])
            hadp = t > 0 and np.isfinite(sf[t - 1]) and np.isfinite(ss[t - 1])
            if have and hadp:
                prev_above = sf[t - 1] > ss[t - 1]
                prev_below = sf[t - 1] < ss[t - 1]
                if sf[t] > ss[t] and not prev_above:
                    cur = 1.0
                    entries += 1
                elif sf[t] < ss[t] and not prev_below:
                    cur = 0.0
            pos[t] = cur
        held = np.r_[0.0, pos[:-1]]
        for t in range(1, T):
            if ok[t] and ok[t - 1] and c[t - 1] > 0:
                r = c[t] / c[t - 1] - 1
                turn = abs(held[t] - held[t - 1])
                port[t] += held[t] * r - turn * cost
                nvalid[t] += 1
    port = port / np.maximum(nvalid, 1)
    equity = np.cumprod(1 + port)
    dd = (equity / np.maximum.accumulate(equity) - 1).min()
    sd = port.std(ddof=1)
    return {
        "total_return": float(equity[-1] - 1),
        "sharpe": float(port.mean() / sd * TRADING_DAYS**0.5) if sd > 0 else 0.0,
        "max_drawdown": float(dd),
        "entries": entries,
    }
