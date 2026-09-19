"""Aggregates over attributed trades — the numbers a dashboard shows.

Pure functions over already-persisted rows. Nothing here reads a database, a
cache or a clock, so every figure on the attribution dashboard is reproducible
from the rows it was computed from.

The rules this module keeps, and why each one is a rule
------------------------------------------------------

**A rate with no denominator is ``None``, not zero.** A win rate over zero trades
is not 0%; it is unknown, and printing 0.00 would report a losing system where
there is simply no system yet. Every ratio below returns ``None`` when its
denominator is missing or zero.

**Evidence grades are counted, never blended.** Every aggregate is computed over
whatever rows the caller passed, and every aggregate carries the counts of each
grade that went into it. That is deliberate: the caller decides the scope, and the
result states what it was made of, so a number computed over a mixed book can
never be read as a forward finding. ``counts`` travels beside every table.

**Slippage is averaged over measurable legs only.** A trade whose reference price
was never recorded has *no* slippage measurement, which is not the same as a
slippage of zero. Including it as a zero would drag a mean toward a friction that
was never observed, and the direction of that error is systematic — trades with
poorly-recorded execution would appear to have cheap execution. So
``measurable`` travels beside every slippage figure, and a table where it is
smaller than ``n`` says so.

**No rankings.** There is deliberately no ``best_strategy()`` and no
``rank()`` here. Comparing a strategy across regimes and buckets is the useful
operation; declaring one the winner is what a sample of eleven trades cannot
support and what the rest of this codebase refuses to do. The API exposes the
comparison and withholds the verdict.
"""

from __future__ import annotations

from typing import Any, Sequence

#: Below this many trades a bucket's outcome statistics are suppressed. Matches
#: the learning tier's floor so the two surfaces cannot disagree about what
#: counts as enough to look at.
MIN_SAMPLE = 10


def _clean(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _mean(values: Sequence[Any]) -> float | None:
    clean = _clean(values)
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _median(values: Sequence[Any]) -> float | None:
    clean = sorted(_clean(values))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return round(clean[mid], 4)
    return round((clean[mid - 1] + clean[mid]) / 2.0, 4)


def _sum(values: Sequence[Any]) -> float | None:
    clean = _clean(values)
    if not clean:
        return None
    return round(sum(clean), 4)


def _rate(numerator: int, denominator: int) -> float | None:
    """A ratio, or ``None`` when there is no denominator."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _percentile(values: Sequence[Any], fraction: float) -> float | None:
    clean = sorted(_clean(values))
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 4)
    position = fraction * (len(clean) - 1)
    low = int(position)
    high = min(low + 1, len(clean) - 1)
    weight = position - low
    return round(clean[low] * (1 - weight) + clean[high] * weight, 4)


def evidence_counts(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """How many rows of each grade and class a figure was computed over.

    Always carried beside an aggregate. A mean over a book of forty in-sample
    trades and two forward ones is a statement about the in-sample book, and a
    reader who cannot see that will read it as a finding.

    The class total is only reported as a *class* when the rows actually recorded
    one. Defaulting an absent ``evidence_class`` to ``"IN_SAMPLE"`` would print a
    class for a row whose grade says ``forward``, and the two would then
    contradict each other in the same dictionary — ``by_grade: {forward: 1}``
    beside ``by_class: {IN_SAMPLE: 1}``. Absent classes are counted separately
    under ``class_unrecorded`` so the reader can see the gap instead of being
    handed a class nobody wrote down.
    """
    grades: dict[str, int] = {}
    classes: dict[str, int] = {}
    unrecorded = 0
    for row in rows:
        grade = str(row.get("evidence_grade") or "in_sample")
        grades[grade] = grades.get(grade, 0) + 1
        raw_class = row.get("evidence_class")
        if raw_class is None or not str(raw_class).strip():
            unrecorded += 1
            continue
        klass = str(raw_class).strip()
        classes[klass] = classes.get(klass, 0) + 1
    return {
        "n": len(rows),
        "forward_n": grades.get("forward", 0),
        "in_sample_n": grades.get("in_sample", 0),
        "by_grade": grades,
        "by_class": classes,
        #: Rows with no recorded class. They are counted in ``by_grade`` and are
        #: deliberately *not* given a class here.
        "class_unrecorded": unrecorded,
        "all_forward": bool(rows) and grades.get("in_sample", 0) == 0,
    }


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def overview(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The headline figures: P&L, hit rate, expectancy, costs, time in market.

    ``net_pnl`` is the metric throughout, because rupees are what a decision is
    made on. A book with no ``net_pnl`` at all — the paper ledger records a
    return and no position size — yields ``None`` for every money figure rather
    than a zero, and the caller is expected to fall back to ``return_pct``
    explicitly rather than have the substitution made silently here.
    """
    net = _clean([row.get("net_pnl") for row in rows])
    gross = _clean([row.get("gross_pnl") for row in rows])
    returns = _clean([row.get("net_return_pct") for row in rows])
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    flats = [value for value in net if value == 0]

    gross_profit = sum(value for value in net if value > 0)
    gross_loss = abs(sum(value for value in net if value < 0))

    costs = _sum([row.get("transaction_costs") for row in rows])
    slippage_bps = _mean([row.get("total_slippage_bps") for row in rows])
    slippage_amount = _sum(
        [
            row.get("total_slippage_bps")
            for row in rows
            if row.get("total_slippage_bps") is not None
        ]
    )

    out = {
        "n_trades": len(rows),
        "net_pnl": _sum(net),
        "gross_pnl": _sum(gross),
        "net_return_pct_mean": _mean(returns),
        "net_return_pct_median": _median(returns),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flats),
        "win_rate": _rate(len(wins), len(net)),
        # ``None`` rather than infinity when there are no losses, and rather than
        # zero when there are no wins. Both are states a real book passes through
        # early, and neither is a profit factor.
        "profit_factor": (
            round(gross_profit / gross_loss, 4)
            if gross_loss > 0 and gross_profit > 0
            else None
        ),
        "expectancy": _mean(net),
        "expectancy_return_pct": _mean(returns),
        "average_win": _mean(wins),
        "average_loss": _mean(losses),
        "largest_win": round(max(net), 4) if net else None,
        "largest_loss": round(min(net), 4) if net else None,
        "total_costs": costs,
        "costs_per_trade": (
            round(costs / len(net), 4) if costs is not None and net else None
        ),
        "total_slippage_bps_mean": slippage_bps,
        "total_slippage_bps_sum": slippage_amount,
        "median_holding_sec": _median([row.get("holding_sec") for row in rows]),
        "mean_holding_sec": _mean([row.get("holding_sec") for row in rows]),
        "counts": evidence_counts(rows),
    }

    # The best-week test the rest of this project applies to every claim. A mean
    # without it is a mean that a single trade can be carrying, and on a book of
    # forty trades that is not a hypothetical.
    out["best_trade_share_of_gross_profit"] = (
        round(max(net) / gross_profit, 4)
        if net and gross_profit > 0
        else None
    )
    out["net_without_best"] = (
        round(sum(net) - max(net), 4) if net else None
    )
    return out


# ---------------------------------------------------------------------------
# attribution: where the money came from, branch by branch
# ---------------------------------------------------------------------------


def by_branch(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Break the book down across the six attributable branches.

    Each branch is a *classification of the trades*, not a decomposition of the
    P&L into additive parts. That distinction matters and is the honest one: a
    trade's rupees cannot be split into "signal rupees" and "execution rupees",
    because they are not separable — the signal did not earn anything without an
    execution. What *can* be said is how trades classified a given way performed,
    which is what this returns, with the counts and the evidence grades attached.
    """
    return {
        "signal": _group(rows, _signal_key),
        "context": _group(rows, _context_key),
        "sizing": _group(rows, _sizing_key),
        "risk": _group(rows, _risk_key),
        "execution": _group(rows, _execution_key),
        "exit": _group(rows, _exit_key),
        "note": (
            "Each bucket is a classification of trades, not a decomposition of "
            "P&L into additive parts. A trade's rupees cannot be split between "
            "its signal and its execution; what is reported is how trades "
            "classified this way performed."
        ),
    }


def _signal_key(row: dict[str, Any]) -> str | None:
    codes = row.get("reason_codes") or []
    if "SIGNAL_POSITIVE" in codes:
        return "signal_present"
    if "INSUFFICIENT_DATA" in codes:
        return "no_signal_linkage"
    return None


def _context_key(row: dict[str, Any]) -> str | None:
    codes = row.get("reason_codes") or []
    if "CONTEXT_POSITIVE" in codes:
        return "context_supportive"
    if "CONTEXT_NEGATIVE" in codes:
        return "context_unsupportive"
    if row.get("context_class"):
        return "context_neutral"
    return None


def _sizing_key(row: dict[str, Any]) -> str | None:
    codes = row.get("reason_codes") or []
    if "OVERSIZED" in codes:
        return "cap_bound"
    if "UNDERSIZED" in codes:
        return "below_intended"
    if row.get("sizing_method"):
        return "at_intended_size"
    return None


def _risk_key(row: dict[str, Any]) -> str | None:
    codes = row.get("reason_codes") or []
    if "HIGH_RISK" in codes:
        return "risk_wider_than_planned"
    if "LOW_RISK" in codes:
        return "risk_tighter_than_planned"
    if row.get("realized_risk_pct") is not None:
        return "risk_as_planned"
    return None


def _execution_key(row: dict[str, Any]) -> str | None:
    quality = row.get("execution_quality")
    if quality:
        return str(quality)
    codes = row.get("reason_codes") or []
    if "HIGH_SLIPPAGE" in codes:
        return "high_slippage"
    if "LOW_SLIPPAGE" in codes:
        return "low_slippage"
    return None


def _exit_key(row: dict[str, Any]) -> str | None:
    codes = row.get("reason_codes") or []
    for code in ("TARGET_DRIVEN", "STOP_DRIVEN", "TIME_EXIT"):
        if code in codes:
            return code.lower()
    if row.get("exit_reason"):
        return "other"
    return None


def _group(
    rows: Sequence[dict[str, Any]], keyer: Any, *, min_sample: int = 1
) -> list[dict[str, Any]]:
    """Bucket rows by a classifier, with the honest summary of each bucket."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    excluded = 0
    for row in rows:
        key = keyer(row)
        if key is None:
            # A row the classifier cannot place is *excluded and counted*, never
            # swept into an "unknown" bucket that then gets compared to the rest
            # as though "we could not tell" were a finding.
            excluded += 1
            continue
        buckets.setdefault(str(key), []).append(row)

    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        summary = overview(bucket)
        out.append(
            {
                "key": key,
                "n": summary["n_trades"],
                "net_pnl": summary["net_pnl"],
                "net_return_pct_mean": summary["net_return_pct_mean"],
                "win_rate": summary["win_rate"],
                "expectancy": summary["expectancy"],
                "profit_factor": summary["profit_factor"],
                "mean_holding_sec": summary["mean_holding_sec"],
                "total_costs": summary["total_costs"],
                # Statistics are suppressed below the floor, but the *counts* are
                # always shown: a bucket of three trades is worth knowing about
                # even when its mean is not worth quoting.
                "suppressed": len(bucket) < min_sample,
                "counts": summary["counts"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# MAE / MFE
# ---------------------------------------------------------------------------


#: The excursion buckets the dashboard and the learning axis both use. Declared
#: once so a bucket boundary cannot differ between the chart and the slice.
MFE_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.0, "never_favourable"),
    (1.0, "under_1R"),
    (2.0, "1R_to_2R"),
    (3.0, "2R_to_3R"),
    (float("inf"), "above_3R"),
)

MAE_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.25, "mild_under_quarter_R"),
    (0.5, "quarter_to_half_R"),
    (1.0, "half_to_1R"),
    (float("inf"), "worse_than_1R"),
)


def bucket_mfe(value: float | None) -> str | None:
    """The MFE/R bucket label. ``None`` when it cannot be computed."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    for upper, label in MFE_BUCKETS:
        if number <= upper:
            return label
    return MFE_BUCKETS[-1][1]


def bucket_mae(value: float | None) -> str | None:
    """The |MAE|/R bucket label. ``None`` when it cannot be computed.

    Takes the magnitude, so a caller may pass either sign — MAE is adverse-negative
    by convention and a caller who forgot that should not get a silent wrong
    bucket.
    """
    if value is None:
        return None
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return None
    for upper, label in MAE_BUCKETS:
        if number <= upper:
            return label
    return MAE_BUCKETS[-1][1]


def mae_mfe(rows: Sequence[dict[str, Any]], *, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """Distributions of the excursions, and their relationship to the outcome.

    The scatter points are returned per-trade rather than binned, because the
    relationship between an excursion and a final P&L is the thing a reader wants
    to *see*, and binning it first would decide the shape for them. The points
    carry their evidence grade so a mixed book renders honestly.
    """
    mfe_r = _clean([row.get("mfe_over_risk") for row in rows])
    mae_r = _clean(
        [
            abs(float(row["mae_amount"])) / float(row["planned_risk_amount"])
            for row in rows
            if row.get("mae_amount") is not None
            and row.get("planned_risk_amount")
            and float(row["planned_risk_amount"]) != 0
        ]
    )
    realized_r = _clean([row.get("realized_over_risk") for row in rows])

    points = []
    for row in rows:
        net = row.get("net_pnl")
        mfe = row.get("mfe_pct")
        mae = row.get("mae_pct")
        r_multiple = row.get("realized_over_risk")
        if net is None and mfe is None and mae is None:
            continue
        points.append(
            {
                "trade_id": row.get("trade_id"),
                "symbol": row.get("symbol"),
                "net_pnl": net,
                "return_pct": row.get("net_return_pct"),
                "mfe_pct": mfe,
                "mae_pct": mae,
                "mfe_over_risk": row.get("mfe_over_risk"),
                "mae_over_risk": (
                    round(abs(float(row["mae_amount"])) / float(row["planned_risk_amount"]), 4)
                    if row.get("mae_amount") is not None
                    and row.get("planned_risk_amount")
                    and float(row["planned_risk_amount"]) != 0
                    else None
                ),
                "realized_over_risk": r_multiple,
                "capped_by_stop": (
                    abs(float(mae)) / 100.0
                    >= (float(row.get("realized_risk_pct") or 0) / 100.0) * 0.9
                    if mae is not None and row.get("realized_risk_pct")
                    else None
                ),
                "evidence_grade": row.get("evidence_grade"),
            }
        )

    return {
        "n": len(rows),
        "counts": evidence_counts(rows),
        "min_sample": min_sample,
        "mfe_over_risk": {
            "mean": _mean(mfe_r),
            "median": _median(mfe_r),
            "p25": _percentile(mfe_r, 0.25),
            "p75": _percentile(mfe_r, 0.75),
            "measured": len(mfe_r),
            "buckets": _bucket_counts(rows, lambda r: bucket_mfe(r.get("mfe_over_risk"))),
        },
        "mae_over_risk": {
            "mean": _mean(mae_r),
            "median": _median(mae_r),
            "p25": _percentile(mae_r, 0.25),
            "p75": _percentile(mae_r, 0.75),
            "measured": len(mae_r),
            "buckets": _bucket_counts(rows, _row_mae_bucket),
        },
        "realized_over_risk": {
            "mean": _mean(realized_r),
            "median": _median(realized_r),
            "measured": len(realized_r),
        },
        "points": points,
    }


def _row_mae_bucket(row: dict[str, Any]) -> str | None:
    amount = row.get("mae_amount")
    risk = row.get("planned_risk_amount")
    if amount is None or not risk or float(risk) == 0:
        return None
    return bucket_mae(abs(float(amount)) / float(risk))


def _bucket_counts(rows: Sequence[dict[str, Any]], keyer: Any) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    excluded = 0
    for row in rows:
        key = keyer(row)
        if key is None:
            excluded += 1
            continue
        counts[key] = counts.get(key, 0) + 1
    return [
        {"key": key, "n": count}
        for key, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ] + ([{"key": "not_measured", "n": excluded}] if excluded else [])


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def execution(rows: Sequence[dict[str, Any]], *, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """Delay, slippage and cost, overall and cut by symbol and by hour.

    Every slippage figure is accompanied by the number of trades it was measured
    over. That count is not decoration: a mean slippage over four trades out of
    forty is a statement about four trades, and a dashboard that showed only the
    mean would be read as a statement about forty.
    """
    slippage = _clean([row.get("total_slippage_bps") for row in rows])
    entry_slip = _clean([row.get("entry_slippage_bps") for row in rows])
    exit_slip = _clean([row.get("exit_slippage_bps") for row in rows])
    signal_to_order = _clean([row.get("signal_to_order_sec") for row in rows])
    order_to_fill = _clean([row.get("order_to_fill_sec") for row in rows])

    return {
        "n": len(rows),
        "counts": evidence_counts(rows),
        "min_sample": min_sample,
        "signal_to_order_sec": {
            "mean": _mean(signal_to_order),
            "median": _median(signal_to_order),
            "p90": _percentile(signal_to_order, 0.9),
            "max": round(max(signal_to_order), 4) if signal_to_order else None,
            "measured": len(signal_to_order),
        },
        "order_to_fill_sec": {
            "mean": _mean(order_to_fill),
            "median": _median(order_to_fill),
            "p90": _percentile(order_to_fill, 0.9),
            "max": round(max(order_to_fill), 4) if order_to_fill else None,
            "measured": len(order_to_fill),
        },
        "slippage_bps": {
            "mean": _mean(slippage),
            "median": _median(slippage),
            "entry_mean": _mean(entry_slip),
            "exit_mean": _mean(exit_slip),
            "measured": len(slippage),
            # Trades where no leg carried a reference price. Named so a reader can
            # see how much of the book the figure above actually covers.
            "unmeasurable": len(rows) - len(slippage),
        },
        "costs": {
            "total": _sum([row.get("transaction_costs") for row in rows]),
            "mean": _mean([row.get("transaction_costs") for row in rows]),
            "mean_pct_of_position": _mean([row.get("cost_pct") for row in rows]),
            "measured": len(_clean([row.get("transaction_costs") for row in rows])),
        },
        "partial_fills": {
            "n": sum(1 for row in rows if row.get("partial_fill")),
            "mean_fill_ratio": _mean(
                [row.get("fill_ratio") for row in rows if row.get("partial_fill")]
            ),
            "measured": len(
                _clean([row.get("fill_ratio") for row in rows if row.get("partial_fill")])
            ),
        },
        "by_symbol": _execution_by(rows, lambda r: r.get("symbol"), min_sample=min_sample),
        "by_time_of_day": _execution_by(rows, _hour_key, min_sample=min_sample),
    }


def _execution_by(
    rows: Sequence[dict[str, Any]], keyer: Any, *, min_sample: int
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = keyer(row)
        if key is None:
            continue
        buckets.setdefault(str(key), []).append(row)

    out: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        slippage = _clean([row.get("total_slippage_bps") for row in bucket])
        out.append(
            {
                "key": key,
                "n": len(bucket),
                "mean_slippage_bps": _mean(slippage),
                "median_slippage_bps": _median(slippage),
                "mean_cost": _mean([row.get("transaction_costs") for row in bucket]),
                "measured": len(slippage),
                "suppressed": len(bucket) < min_sample or len(slippage) < min_sample,
                "counts": evidence_counts(bucket),
            }
        )
    return out


#: The session buckets a slippage-by-time figure is cut into. Indian cash equity
#: hours, and coarse on purpose: an hour of the session has a character (the open
#: is wide, the lunch hour is thin, the close is busy), but five-minute bins would
#: put four trades in each and invite a conclusion from noise.
TIME_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (9, 15, "open_0915_1000"),
    (10, 0, "morning_1000_1130"),
    (11, 30, "midday_1130_1300"),
    (13, 0, "afternoon_1300_1430"),
    (14, 30, "close_1430_1530"),
)


def _hour_key(row: dict[str, Any]) -> str | None:
    """The session bucket of a trade's entry, from its stored timestamp."""
    stamp = row.get("entry_ts")
    if stamp is None:
        return None
    try:
        import pandas as pd

        moment = pd.to_datetime(stamp, errors="coerce")
        if pd.isna(moment):
            return None
        minutes = int(moment.hour) * 60 + int(moment.minute)
    except Exception:  # noqa: BLE001
        return None
    for hour, minute, label in TIME_BUCKETS:
        if minutes < hour * 60 + minute:
            return label
    return "after_hours"


# ---------------------------------------------------------------------------
# strategy diagnostics — comparison, never a ranking
# ---------------------------------------------------------------------------


def diagnostics(
    rows: Sequence[dict[str, Any]], *, min_sample: int = MIN_SAMPLE
) -> dict[str, Any]:
    """A strategy version compared across regime, context, sector, sizing, exit.

    Returns the *dimensions*, each with its buckets. There is deliberately no
    "winner" key and no ordering by performance: the caller renders a comparison
    and lets a reader draw their own conclusion, which is the only defensible
    thing a sample of this size supports. Ordering buckets by P&L would be the
    "best strategy ranking" the specification forbids, wearing a table's clothes.
    """
    dimensions = {
        "market_regime": lambda r: r.get("market_regime"),
        "context_class": lambda r: r.get("context_class"),
        "sector": lambda r: r.get("sector"),
        "sizing_method": lambda r: r.get("sizing_method"),
        "exit_reason": lambda r: (r.get("exit_reason") or _exit_key(r)),
        "execution_quality": lambda r: _execution_key(r),
    }
    return {
        "n": len(rows),
        "counts": evidence_counts(rows),
        "min_sample": min_sample,
        "dimensions": {
            name: _group(rows, keyer, min_sample=min_sample)
            for name, keyer in dimensions.items()
        },
        "note": (
            "A comparison, not a ranking. Buckets are listed in a stable "
            "alphabetical order rather than by performance, because ordering "
            "them by P&L over a sample this size presents noise as a finding."
        ),
    }


# ---------------------------------------------------------------------------
# bucketing the learning axis reads
# ---------------------------------------------------------------------------


def bucket_slippage(value: float | None) -> str | None:
    """Slippage bucket, adverse-positive bps."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 5.0:
        return "low_under_5bps"
    if number <= 15.0:
        return "normal_5_15bps"
    if number <= 30.0:
        return "elevated_15_30bps"
    return "high_above_30bps"


def bucket_holding(seconds: Any) -> str | None:
    """Holding-period bucket, from seconds."""
    if seconds is None:
        return None
    try:
        number = float(seconds)
    except (TypeError, ValueError):
        return None
    days = number / 86_400.0
    if days < 1.0:
        return "intraday"
    if days <= 5.0:
        return "days_1_5"
    if days <= 20.0:
        return "weeks_1_4"
    if days <= 60.0:
        return "months_1_3"
    return "months_3_plus"


__all__ = [
    "MAE_BUCKETS",
    "MFE_BUCKETS",
    "MIN_SAMPLE",
    "TIME_BUCKETS",
    "bucket_holding",
    "bucket_mae",
    "bucket_mfe",
    "bucket_slippage",
    "by_branch",
    "diagnostics",
    "evidence_counts",
    "execution",
    "mae_mfe",
    "overview",
]
