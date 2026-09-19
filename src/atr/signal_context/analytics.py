"""Pure statistics for the signal-context analytics surface.

Everything here is a pure function of already-resolved data: the service layer
assembles ``(bucket_key, forward_n, in_sample_n, pnls, wins)`` and this module
turns a collection of those into bucket summaries with an evidence label and
statistical suppression.

Honesty rules, in order of importance
-------------------------------------

* **A bucket with no resolvable outcome shows counts and nothing else.** The
  mean, win rate and confidence interval are ``None`` — a ``0.00`` in the UI
  would read as *measured zero* rather than *not measured*, and that is the lie
  the whole analytics surface exists to avoid.
* **A backtest outcome is ``in_sample`` by construction.** The trade was chosen
  on the same data its outcome is measured on; that is not out-of-sample
  evidence, and the evidence note must say so.
* **A live/paper outcome is ``forward`` evidence.** ``forward`` is only
  awarded once the entry is genuinely recorded against the live book; a context
  whose entries never resolved has `forward_n == 0` and its stats are suppressed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: Wilson interval z-value for a 95% confidence band.
WILSON_Z = 1.96
#: Fewer forward outcomes than this → stats suppressed, counts still shown.
MIN_FORWARD_N = 5

#: Bucket dimensions the UI can ask for.
DIMENSIONS = (
    "context_class",
    "score_band",
    "regime",
    "sector_rs",
    "stock_rs",
    "breadth",
    "volatility",
)

#: Deterministic render order for each dimension's buckets.
_BUCKET_ORDER: dict[str, tuple[str, ...]] = {
    "context_class": (
        "STRONG_CONTEXT",
        "NEUTRAL_CONTEXT",
        "WEAK_CONTEXT",
        "INSUFFICIENT_DATA",
    ),
    "score_band": ("0-39", "40-69", "70-100", "INSUFFICIENT_DATA"),
    "regime": (
        "BULLISH_TREND",
        "BEARISH_TREND",
        "SIDEWAYS",
        "HIGH_VOLATILITY",
        "LOW_VOLATILITY",
        "UNKNOWN",
    ),
    "sector_rs": ("positive", "non_positive", "unknown"),
    "stock_rs": ("positive", "non_positive", "unknown"),
    "breadth": ("strong", "weak", "unknown"),
    "volatility": ("elevated", "normal", "unknown"),
}


@dataclass(frozen=True)
class Outcome:
    """One resolved outcome attached to a context.

    ``evidence_kind`` is ``"forward"`` for live/paper trades recorded against
    the real book and ``"in_sample"`` for backtest trades.
    """

    net_pnl: float
    return_pct: float | None
    evidence_kind: str


@dataclass
class ContextRow:
    """The parsed context fields the bucketing needs."""

    context_class: str
    context_score: int
    has_insufficient_data: bool
    market_context: dict[str, Any] = field(default_factory=dict)
    sector_context: dict[str, Any] = field(default_factory=dict)
    stock_context: dict[str, Any] = field(default_factory=dict)


def bucket_key(dimension: str, row: ContextRow) -> str:
    """The bucket label for one context under ``dimension``.

    ``INSUFFICIENT_DATA`` contexts always land in their own bucket first: a
    context that could not be measured cannot be credited with any other label.
    """
    if row.has_insufficient_data and dimension in ("context_class", "score_band"):
        return "INSUFFICIENT_DATA"

    if dimension == "context_class":
        return row.context_class
    if dimension == "score_band":
        if row.context_score >= 70:
            return "70-100"
        if row.context_score >= 40:
            return "40-69"
        return "0-39"
    if dimension == "regime":
        return row.market_context.get("regime") or "UNKNOWN"
    if dimension == "sector_rs":
        rs = (row.sector_context or {}).get("relative_strength_1m")
        if rs is None:
            return "unknown"
        return "positive" if rs > 0 else "non_positive"
    if dimension == "stock_rs":
        rs = row.stock_context.get("relative_strength_nifty_20d")
        if rs is None:
            return "unknown"
        return "positive" if rs > 0 else "non_positive"
    if dimension == "breadth":
        b = row.market_context.get("breadth_above_ema50_pct")
        if b is None:
            return "unknown"
        return "strong" if b >= 55.0 else "weak"
    if dimension == "volatility":
        v = row.market_context.get("volatility_ratio")
        if v is None:
            return "unknown"
        return "elevated" if v >= 1.3 else "normal"
    return "unknown"


def wilson_interval(wins: int, n: int) -> tuple[float | None, float | None]:
    """Wilson 95% confidence interval for a binomial win rate.

    Returns ``(None, None)`` when there is nothing to be confident about.
    """
    if n <= 0 or wins < 0 or wins > n:
        return None, None
    z = WILSON_Z
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def profit_factor(pnls: Sequence[float]) -> float | None:
    """Gross profit / |gross loss|. None when there is no losing (or no profit)."""
    gross_profit = sum(max(p, 0.0) for p in pnls)
    gross_loss = abs(sum(min(p, 0.0) for p in pnls))
    if gross_profit <= 0:
        return None
    if gross_loss <= 0:
        return None
    return round(gross_profit / gross_loss, 2)


def build_buckets(
    dimension: str,
    outcomes_by_row: list[tuple[ContextRow, Outcome | None]],
) -> list[dict[str, Any]]:
    """Summarise per-bucket counts and stats, with honest suppression.

    ``outcomes_by_row`` pairs each context with its resolved outcome, or
    ``None`` when the context has no resolvable result (so it feeds counts but
    never the statistics).
    """
    collected: dict[str, dict[str, Any]] = {
        key: _empty_bucket(key) for key in _BUCKET_ORDER.get(dimension, ())
    }

    forward_pnls: dict[str, list[float]] = {k: [] for k in collected}
    forward_returns: dict[str, list[float]] = {k: [] for k in collected}
    in_sample_pnls: dict[str, list[float]] = {k: [] for k in collected}
    in_sample_returns: dict[str, list[float]] = {k: [] for k in collected}

    for row, outcome in outcomes_by_row:
        key = bucket_key(dimension, row)
        bucket = collected.setdefault(key, _empty_bucket(key))
        bucket["n"] += 1
        if outcome is None:
            continue
        if outcome.evidence_kind == "forward":
            bucket["n_forward"] += 1
            forward_pnls[key].append(outcome.net_pnl)
            if outcome.return_pct is not None:
                forward_returns[key].append(outcome.return_pct)
        else:
            bucket["n_in_sample"] += 1
            in_sample_pnls[key].append(outcome.net_pnl)
            if outcome.return_pct is not None:
                in_sample_returns[key].append(outcome.return_pct)

    ordered = [collected[k] for k in _BUCKET_ORDER.get(dimension, ()) if k in collected]
    for extra in collected:
        if extra not in _BUCKET_ORDER.get(dimension, ()):
            ordered.append(collected[extra])

    for bucket in ordered:
        key = bucket["key"]
        fwd = forward_pnls[key]
        ins = in_sample_pnls[key]
        bucket["suppressed"], bucket["evidence_note"] = _suppression(fwd, ins)
        if bucket["suppressed"]:
            continue
        f_returns = forward_returns[key]
        i_returns = in_sample_returns[key]
        returns = sorted(f_returns + i_returns)
        pnls = fwd + ins
        if returns:
            bucket["mean_return"] = round(sum(returns) / len(returns), 3)
            midpoint = len(returns) // 2
            if len(returns) % 2:
                bucket["median_return"] = round(returns[midpoint], 3)
            else:
                bucket["median_return"] = round((returns[midpoint - 1] + returns[midpoint]) / 2.0, 3)
        wins = sum(1 for p in pnls if p > 0)
        bucket["win_rate"] = round(wins / len(pnls), 4) if pnls else None
        low, high = wilson_interval(wins, len(pnls))
        bucket["ci_low"] = round(low, 4) if low is not None else None
        bucket["ci_high"] = round(high, 4) if high is not None else None
        bucket["profit_factor"] = profit_factor(pnls)

    return ordered


def _suppression(
    forward_pnls: list[float], in_sample_pnls: list[float]
) -> tuple[bool, str]:
    """Decide whether a bucket's stats may be shown, and under which label."""
    if len(forward_pnls) >= MIN_FORWARD_N:
        return False, "forward"
    if forward_pnls:
        return True, "insufficient_forward_observations"
    if in_sample_pnls:
        return False, "in_sample_only"
    return True, "no_resolvable_outcomes"


def _empty_bucket(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "n": 0,
        "n_forward": 0,
        "n_in_sample": 0,
        "mean_return": None,
        "median_return": None,
        "win_rate": None,
        "ci_low": None,
        "ci_high": None,
        "profit_factor": None,
        "suppressed": True,
        "evidence_note": "no_resolvable_outcomes",
    }


__all__ = [
    "DIMENSIONS",
    "MIN_FORWARD_N",
    "WILSON_Z",
    "ContextRow",
    "Outcome",
    "build_buckets",
    "bucket_key",
    "profit_factor",
    "wilson_interval",
]