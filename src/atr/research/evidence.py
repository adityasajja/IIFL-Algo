"""Honesty layer: what a number is allowed to claim, and what it must disclose.

Why this module exists
----------------------
Between 2026-09-10 and 2026-09-13 this repository produced a series of
backtests that were each individually correct and collectively misleading:

* A walk-forward reported **+480%** on a 120-name universe. The universe had
  been chosen by *today's* turnover, so it was stacked with names that already
  worked. Rebuilt from every symbol with a long history, the same window
  returned **+124%**. The gap -- **+356 points** -- was manufactured entirely by
  how the universe was selected, and it was silently inherited by both the
  strategy and its benchmark.
* A cross-sectional momentum spread of **+1.01%/month** fell to **+0.27%** once
  the equal-weight market return was subtracted. Most of it was leverage.
* A high-volatility factor looked like the one clean survivor: monotone across
  quintiles, positive in **every** calendar year, **44 of 61** rolling
  six-month blocks positive. Split by market direction it was **+4.07%/month
  when the market rose and -2.06% when it fell**, with a maximum drawdown of
  -25.9% against the market's -18.1%. It was beta wearing a costume.
* Five risk overlays beat buy-and-hold on Calmar over the full window and over
  the first half. Over the second half, **none of them did** -- at every one of
  six split points tested.

None of those results was a coding error. Each was a *reporting* failure: the
number was printed without the context that determines whether it means
anything. A backtest that omits its universe's provenance, its trial count, or
its sub-period stability is not evidence; it is a selected summary, and the
selection is the problem.

This module makes the disclosure mandatory and mechanical.

What it enforces
----------------
:class:`Evidence` — a result plus the six facts required to read it. Building
one is cheap; the point is that you cannot publish a number without them.

:func:`assess` — returns a :class:`Credibility` verdict with explicit reasons.
Failing is not an error state; it is the normal, expected outcome, and the
reasons are the useful part.

:func:`regime_split` and :func:`stability` — the two tests that caught the
high-vol factor and the five overlays. Both are one call, so there is no
convenient excuse for skipping them.

Design note
-----------
Every threshold here is deliberately blunt and pre-committed. The failure mode
being defended against is a researcher who, having seen a result, chooses the
test that lets it through. So the tests are fixed in the module, applied to
everything, and their verdicts stand.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = [
    "Evidence",
    "Credibility",
    "UniverseProvenance",
    "SelectionBias",
    "RegimeResult",
    "assess",
    "regime_split",
    "stability",
    "buy_and_hold",
    "selection_bias",
    "summarise",
]


# --------------------------------------------------------------------------
# Universe provenance
# --------------------------------------------------------------------------


@dataclass
class UniverseProvenance:
    """How the tested universe was chosen — the fact most often omitted.

    A universe selected using information from the end of the window is
    look-ahead by construction, and it inflates the strategy and the benchmark
    together. That is why the *comparison* survives while the *level* does not.
    """

    n_names: int
    #: True when membership was decided using information available only at the
    #: end of the test window (e.g. "today's 120 most liquid names").
    selected_on_full_window: bool = False
    #: Days of history per name.
    history_bars: int = 0
    #: True when delisted/merged names are absent, so losers were never counted.
    survivorship_biased: bool = False
    note: str = ""

    def describe(self) -> str:
        flags = []
        if self.selected_on_full_window:
            flags.append("selected on full window (look-ahead)")
        if self.survivorship_biased:
            flags.append("survivors only")
        if not flags:
            flags.append("no known selection bias")
        return f"{self.n_names} names, {self.history_bars} bars — " + "; ".join(flags)


@dataclass
class SelectionBias:
    """The measured gap between a selected universe and an unbiased one.

    Both figures are computed on **identical dates**, so the difference is the
    selection and nothing else.
    """

    selected_total_pct: float
    unbiased_total_pct: float
    n_unbiased: int

    @property
    def gap_pct(self) -> float:
        return self.selected_total_pct - self.unbiased_total_pct

    @property
    def meaningful(self) -> bool:
        return abs(self.gap_pct) >= 25.0

    def describe(self) -> str:
        verdict = (
            "the level of any return here is not quotable"
            if self.meaningful
            else "selection gap is small enough that levels are roughly usable"
        )
        return (
            f"selected {self.selected_total_pct:+.1f}% vs unbiased "
            f"{self.unbiased_total_pct:+.1f}% over the same dates "
            f"(gap {self.gap_pct:+.1f} pts) — {verdict}"
        )


# --------------------------------------------------------------------------
# Regime and stability
# --------------------------------------------------------------------------


@dataclass
class RegimeResult:
    """A strategy's excess return split by the market's direction.

    The single most diagnostic test available. An edge that exists only when
    the market rises is a beta estimate, not an edge.
    """

    up_n: int
    down_n: int
    up_excess_pct: float
    down_excess_pct: float
    up_win_rate: float
    down_win_rate: float

    @property
    def is_leverage(self) -> bool:
        """True when the edge vanishes or reverses in falling markets."""
        return self.down_excess_pct <= 0

    def describe(self) -> str:
        if self.is_leverage:
            return (
                f"LEVERAGE, not edge: {self.up_excess_pct:+.2f}%/mo when the "
                f"market rises ({self.up_win_rate:.0%} of {self.up_n}) but "
                f"{self.down_excess_pct:+.2f}%/mo when it falls "
                f"({self.down_win_rate:.0%} of {self.down_n})"
            )
        return (
            f"edge holds both ways: {self.up_excess_pct:+.2f}%/mo up, "
            f"{self.down_excess_pct:+.2f}%/mo down"
        )


@dataclass
class Stability:
    """Whether a result is present throughout, or only in one stretch.

    ``quantity`` names what each value *is*, because two very different
    measurements get compared against the same "mostly positive?" rule:

    * ``"excess_return_pct"`` — a strategy's return minus the benchmark's, per
      sub-period. Positive means genuine outperformance that period.
    * ``"calmar_advantage"`` — the difference between the strategy's and the
      benchmark's Calmar ratio. Positive means better risk-adjusted outcome,
      which is a different claim from earning more.

    Carrying the label keeps the two from being silently averaged together, and
    keeps a reason string from saying "spread" about a ratio difference.
    """

    spreads: list[float] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    quantity: str = "excess_return_pct"

    @property
    def unit(self) -> str:
        return "%" if self.quantity.endswith("_pct") else ""

    @property
    def n(self) -> int:
        return len(self.spreads)

    @property
    def positive_share(self) -> float:
        if not self.spreads:
            return 0.0
        return float(np.mean([s > 0 for s in self.spreads]))

    @property
    def consistent(self) -> bool:
        """Two thirds of blocks positive is the bar; below that it is episodic."""
        return self.n >= 4 and self.positive_share >= 2 / 3

    def describe(self) -> str:
        if not self.spreads:
            return "no sub-periods measured"
        arr = np.asarray(self.spreads)
        unit = self.unit
        below = [
            f"{lb} {s:+.2f}{unit}" for lb, s in zip(self.labels, self.spreads) if s <= 0
        ]
        tail = f"; negative: {', '.join(below[:4])}" if below else ""
        what = {
            "excess_return_pct": "sub-periods with positive excess return",
            "calmar_advantage": "sub-periods where Calmar beat the benchmark",
        }.get(self.quantity, f"sub-periods with positive {self.quantity}")
        return (
            f"{int((arr > 0).sum())}/{self.n} {what} "
            f"(median {np.median(arr):+.2f}{unit}){tail}"
        )


# --------------------------------------------------------------------------
# The evidence container
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    """A backtest result together with the facts required to read it.

    ``headline_pct`` is whatever number you want to quote. Everything else is
    what stops that quote from being misleading. Construct with
    :func:`summarise` for the common case.
    """

    claim: str
    headline_pct: float
    provenance: UniverseProvenance
    n_trials: int = 1
    oos_sharpe: float = float("nan")
    benchmark_sharpe: float = float("nan")
    p_edge_real: float = float("nan")
    bias: SelectionBias | None = None
    regime: RegimeResult | None = None
    stability: Stability | None = None
    #: Maximum drawdown of the candidate, as a positive percentage.
    max_drawdown_pct: float = float("nan")
    benchmark_drawdown_pct: float = float("nan")
    notes: list[str] = field(default_factory=list)


@dataclass
class Credibility:
    """Why a result should or should not be believed."""

    credible: bool
    reasons: list[str]

    def describe(self) -> str:
        head = "CREDIBLE" if self.credible else "NOT CREDIBLE"
        return "\n".join([head] + [f"  - {r}" for r in self.reasons])


def assess(ev: Evidence) -> Credibility:
    """Apply every test and return a verdict with the reasons spelled out.

    A single failure is enough to withhold belief. The tests are intentionally
    strict: the cost of rejecting a real edge is missing a trade, and the cost
    of accepting a fake one is losing the account.
    """
    reasons: list[str] = []
    ok = True

    # 1. Selection bias — disqualifying on its own, because it invalidates the
    #    strategy and the benchmark in the same direction and cannot be
    #    corrected after the fact.
    if ev.provenance.selected_on_full_window or ev.provenance.survivorship_biased:
        ok = False
        reasons.append(
            "universe was selected using end-of-window information "
            "or excludes dead names — the return level is not quotable "
            f"({ev.provenance.describe()})"
        )
    if ev.bias is not None and ev.bias.meaningful:
        ok = False
        reasons.append(f"measured selection bias: {ev.bias.describe()}")

    # 2. Multiple testing — how many ideas were tried to find this one.
    if ev.n_trials >= 20 and (not math.isfinite(ev.p_edge_real) or ev.p_edge_real < 0.95):
        ok = False
        reasons.append(
            f"{ev.n_trials} trials with P(edge is real) = "
            f"{ev.p_edge_real:.3f} — the search explains the result"
        )

    # 3. Significance on its own terms.
    if not math.isfinite(ev.p_edge_real):
        reasons.append("no multiple-testing correction reported — treat as unproven")
        ok = False
    elif ev.p_edge_real < 0.95:
        ok = False
        reasons.append(f"P(edge is real) = {ev.p_edge_real:.3f} < 0.95")

    # 4. The benchmark. A strategy that cannot beat doing nothing is a fee.
    if math.isfinite(ev.oos_sharpe) and math.isfinite(ev.benchmark_sharpe):
        if ev.oos_sharpe <= ev.benchmark_sharpe:
            ok = False
            reasons.append(
                f"Sharpe {ev.oos_sharpe:.2f} does not beat buy-and-hold "
                f"{ev.benchmark_sharpe:.2f}"
            )

    # 5. Regime. This is the test that catches leverage masquerading as skill.
    if ev.regime is not None:
        if ev.regime.is_leverage:
            ok = False
            reasons.append(ev.regime.describe())
        else:
            reasons.append(ev.regime.describe())

    # 6. Stability across sub-periods.
    if ev.stability is not None:
        if not ev.stability.consistent:
            ok = False
            reasons.append(f"unstable across time: {ev.stability.describe()}")
        else:
            reasons.append(f"stable: {ev.stability.describe()}")
    # 7. Risk, stated even when it is not disqualifying.
    #
    # Drawdowns arrive in both sign conventions — `Metrics.max_drawdown_pct` is
    # signed (negative), while a hand-computed figure is often written as a
    # positive magnitude. Comparing the two directly inverted this check the
    # first time it ran, reporting a *shallower* drawdown as "deeper than the
    # benchmark". Normalise to a positive magnitude before comparing.
    dd = abs(ev.max_drawdown_pct) if math.isfinite(ev.max_drawdown_pct) else float("nan")
    dd_bench = (
        abs(ev.benchmark_drawdown_pct)
        if math.isfinite(ev.benchmark_drawdown_pct)
        else float("nan")
    )
    if math.isfinite(dd):
        if dd > 40:
            ok = False
            reasons.append(f"max drawdown {dd:.1f}% exceeds 40%")
        elif math.isfinite(dd_bench):
            delta = dd - dd_bench
            if delta > 5:
                reasons.append(
                    f"deeper drawdowns than the benchmark "
                    f"({dd:.1f}% vs {dd_bench:.1f}%) "
                    "— any extra return is being paid for in risk"
                )
            elif delta < -5:
                reasons.append(
                    f"shallower drawdowns than the benchmark "
                    f"({dd:.1f}% vs {dd_bench:.1f}%)"
                )

    if ok and not reasons:
        reasons.append("passed every test applied")
    return Credibility(credible=ok, reasons=reasons)


# --------------------------------------------------------------------------
# Measurement helpers
# --------------------------------------------------------------------------


def buy_and_hold(prices: pd.DataFrame) -> pd.Series:
    """Equal-weight buy-and-hold daily returns over the panel."""
    return prices.pct_change().mean(axis=1).fillna(0.0)


def regime_split(
    strategy_returns: pd.Series,
    market_returns: pd.Series,
) -> RegimeResult:
    """Excess return of a strategy, split by whether the market rose or fell.

    ``strategy_returns`` and ``market_returns`` are both simple per-period
    returns. Excess is strategy minus market, so a result that is pure beta
    collapses to zero in the up bucket and turns negative in the down bucket.
    """
    joined = pd.concat(
        [strategy_returns.rename("s"), market_returns.rename("m")], axis=1
    ).dropna()
    up = joined[joined["m"] > 0]
    down = joined[joined["m"] <= 0]

    def excess(part: pd.DataFrame) -> float:
        if part.empty:
            return 0.0
        return float((part["s"] - part["m"]).mean() * 100)

    def wins(part: pd.DataFrame) -> float:
        if part.empty:
            return 0.0
        return float(((part["s"] - part["m"]) > 0).mean())

    return RegimeResult(
        up_n=len(up),
        down_n=len(down),
        up_excess_pct=excess(up),
        down_excess_pct=excess(down),
        up_win_rate=wins(up),
        down_win_rate=wins(down),
    )


def stability(
    spreads: Sequence[float],
    labels: Sequence[str] | None = None,
    quantity: str = "excess_return_pct",
) -> Stability:
    """Package a series of sub-period measurements into a verdict.

    ``quantity`` should say what the numbers are — see :class:`Stability`.
    """
    labs = list(labels) if labels is not None else [str(i + 1) for i in range(len(spreads))]
    return Stability(
        spreads=[float(s) for s in spreads], labels=labs, quantity=quantity
    )


def selection_bias(
    selected_returns: pd.Series,
    unbiased_returns: pd.Series,
    n_unbiased: int,
) -> SelectionBias:
    """Compare a selected universe's cumulative return with an unbiased one."""
    def total(r: pd.Series) -> float:
        r = r.fillna(0.0)
        return float(((1 + r).cumprod().iloc[-1] - 1) * 100)

    return SelectionBias(
        selected_total_pct=total(selected_returns),
        unbiased_total_pct=total(unbiased_returns),
        n_unbiased=n_unbiased,
    )


def summarise(ev: Evidence) -> str:
    """Render an :class:`Evidence` as a block that cannot be read in isolation."""
    lines = [
        "=" * 74,
        f"CLAIM: {ev.claim}",
        "=" * 74,
        f"headline                : {ev.headline_pct:+.2f}%",
        f"universe                : {ev.provenance.describe()}",
        f"trials run              : {ev.n_trials}",
        f"OOS Sharpe              : {ev.oos_sharpe:.2f}",
        f"buy & hold Sharpe       : {ev.benchmark_sharpe:.2f}",
        f"P(edge is real)         : {ev.p_edge_real:.3f}  (need >= 0.95)",
        f"max drawdown            : {ev.max_drawdown_pct:.1f}% "
        f"(buy & hold {ev.benchmark_drawdown_pct:.1f}%)",
    ]
    if ev.bias is not None:
        lines.append(f"selection bias          : {ev.bias.describe()}")
    if ev.regime is not None:
        lines.append(f"regime                  : {ev.regime.describe()}")
    if ev.stability is not None:
        lines.append(f"stability               : {ev.stability.describe()}")
    for note in ev.notes:
        lines.append(f"note                    : {note}")
    lines += ["", assess(ev).describe()]
    return "\n".join(lines)
