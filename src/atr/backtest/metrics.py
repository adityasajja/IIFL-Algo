"""Performance and risk metrics.

Everything is computed from the equity curve and the round-trip trade log, so
the numbers reflect costs that were actually charged in the simulation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def infer_periods_per_year(index: pd.DatetimeIndex) -> float:
    """Estimate bars per year from observed data density.

    Uses bars-per-calendar-year rather than median spacing: intraday series
    have overnight and weekend gaps, and spacing-based estimates silently
    assume the market trades 24/7, which inflates annualised vol by ~5x on
    minute data. (Also avoids pandas datetime64 unit differences — ns vs us —
    which have bitten this function before.)
    """
    if len(index) < 3:
        return 252.0
    span_days = (index[-1] - index[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return 252.0
    return len(index) * 365.25 / span_days


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """Return (max drawdown as a positive fraction, longest DD in calendar days).

    Duration is peak-to-end-of-underwater-period, not bar count — 50,000 bars
    below high is meaningless, 11 days is actionable.
    """
    if equity.empty:
        return 0.0, 0.0
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min())

    under = (drawdown < -1e-12).to_numpy()
    longest = 0.0
    i = 0
    n = len(under)
    while i < n:
        if not under[i]:
            i += 1
            continue
        start = i
        while i < n and under[i]:
            i += 1
        end = i - 1
        # Peak is the bar before the run; recovery ends the run.
        peak_idx = max(start - 1, 0)
        days = (equity.index[end] - equity.index[peak_idx]).total_seconds() / 86400.0
        longest = max(longest, days)
    return abs(max_dd), longest


@dataclass
class Metrics:
    start_equity: float
    end_equity: float
    total_return_pct: float
    cagr_pct: float
    annualized_vol_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    max_drawdown_days: float
    calmar: float
    num_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_trade: float
    best_trade: float
    worst_trade: float
    avg_holding_days: float
    total_commission: float
    total_slippage: float
    exposure_pct: float
    final_positions: int

    def as_dict(self) -> dict[str, float]:
        """JSON-safe metrics. Counts stay integers; only reals are coerced.

        ``int`` used to be swept up by the same ``float()`` as everything else,
        so ``num_trades`` came out as ``12.0``. That is a count, and a UI that
        renders "12.0 trades" (or a consumer that has to know to round it) is
        being told something untrue about the type. ``bool`` is a subclass of
        int, so it is excluded explicitly or the flags would become 0.0/1.0.
        """
        out: dict[str, float] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, (bool, str, type(None))):
                out[key] = value
            elif isinstance(value, int):
                out[key] = int(value)
            elif isinstance(value, (float, np.floating)):
                out[key] = float(value)
            else:
                out[key] = value
        return out

    def summary(self) -> str:
        rows = [
            ("Start equity", f"{self.start_equity:,.2f}"),
            ("End equity", f"{self.end_equity:,.2f}"),
            ("Total return", f"{self.total_return_pct:.2f}%"),
            ("CAGR", f"{self.cagr_pct:.2f}%"),
            ("Annualised vol", f"{self.annualized_vol_pct:.2f}%"),
            ("Sharpe", f"{self.sharpe:.2f}"),
            ("Sortino", f"{self.sortino:.2f}"),
            ("Max drawdown", f"{self.max_drawdown_pct:.2f}%"),
            ("Max DD (days)", f"{self.max_drawdown_days:.1f}"),
            ("Calmar", f"{self.calmar:.2f}"),
            ("Trades", f"{self.num_trades}"),
            ("Win rate", f"{self.win_rate_pct:.2f}%"),
            ("Profit factor", f"{self.profit_factor:.2f}"),
            ("Avg trade", f"{self.avg_trade:,.2f}"),
            ("Best / worst", f"{self.best_trade:,.2f} / {self.worst_trade:,.2f}"),
            ("Avg holding (d)", f"{self.avg_holding_days:.2f}"),
            ("Commission", f"{self.total_commission:,.2f}"),
            ("Slippage", f"{self.total_slippage:,.2f}"),
            ("Avg exposure", f"{self.exposure_pct:.2f}%"),
            ("Open positions", f"{self.final_positions}"),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"{k.ljust(width)} : {v}" for k, v in rows)


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float | None = None,
    total_commission: float = 0.0,
    total_slippage: float = 0.0,
    exposure: pd.Series | None = None,
    final_positions: int = 0,
) -> Metrics:
    if equity.empty:
        raise ValueError("empty equity curve")

    ppy = periods_per_year or infer_periods_per_year(equity.index)
    returns = equity.pct_change().dropna()
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365 * 24 * 3600), 1e-9)

    total_return = end / start - 1.0
    cagr = (end / start) ** (1 / years) - 1 if start > 0 else 0.0

    vol = float(returns.std(ddof=1)) * np.sqrt(ppy) if len(returns) > 1 else 0.0
    mean_return = float(returns.mean()) * ppy if len(returns) else 0.0
    sharpe = (mean_return - risk_free_rate) / vol if vol > 1e-12 else 0.0

    downside = returns[returns < 0]
    downside_vol = float(downside.std(ddof=1)) * np.sqrt(ppy) if len(downside) > 1 else 0.0
    sortino = (mean_return - risk_free_rate) / downside_vol if downside_vol > 1e-12 else 0.0

    mdd, mdd_days = max_drawdown(equity)
    calmar = (cagr / mdd) if mdd > 1e-9 else 0.0

    if not trades.empty and "net_pnl" in trades:
        pnls = trades["net_pnl"].astype(float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        num_trades = int(len(pnls))
        win_rate = float(len(wins) / num_trades * 100) if num_trades else 0.0
        gross_win = float(wins.sum())
        gross_loss = abs(float(losses.sum()))
        profit_factor = gross_win / gross_loss if gross_loss > 1e-9 else float("inf")
        avg_trade = float(pnls.mean())
        best = float(pnls.max())
        worst = float(pnls.min())
        holding = (
            float(trades["duration_days"].dropna().mean())
            if "duration_days" in trades and trades["duration_days"].notna().any()
            else 0.0
        )
    else:
        num_trades = win_rate = profit_factor = avg_trade = best = worst = holding = 0

    exposure_pct = float(exposure.mean() * 100) if exposure is not None and len(exposure) else 0.0

    return Metrics(
        start_equity=start,
        end_equity=end,
        total_return_pct=total_return * 100,
        cagr_pct=cagr * 100,
        annualized_vol_pct=vol * 100,
        sharpe=float(sharpe),
        sortino=float(sortino),
        max_drawdown_pct=mdd * 100,
        max_drawdown_days=mdd_days,
        calmar=float(calmar),
        num_trades=num_trades,
        win_rate_pct=float(win_rate),
        profit_factor=float(profit_factor),
        avg_trade=float(avg_trade),
        best_trade=float(best),
        worst_trade=float(worst),
        avg_holding_days=float(holding),
        total_commission=float(total_commission),
        total_slippage=float(total_slippage),
        exposure_pct=exposure_pct,
        final_positions=final_positions,
    )
