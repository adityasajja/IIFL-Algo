"""Backtest vs paper vs live drift detection.

The question this answers is: *is the strategy doing live what it did in the
backtest?* That question is easy to answer badly, and the bad answers are all
flattering, so most of this module is about refusing to answer rather than
producing a number.

Four ways it could lie, and what is done instead:

**Comparing against nothing.** If there are no paper or live trades, a "drift"
of zero delta is not a finding — it is an empty comparison. Absent sources are
reported as ``insufficient`` with a reason, and the overall verdict is
``unknown`` rather than ``stable``. "Stable" is a claim about the live system;
it must not be reachable from a sample that contains no live trades.

**Comparing different strategies.** A backtest of ``sma_crossover`` says nothing
about live ``trend_pullback``. When the two sources share no strategy, the
comparison is refused by name rather than run on whatever happened to be there.

**Comparing different market conditions.** A live period that was flat while the
backtest period trended will show drift that is regime, not decay. The regime
distribution of each side is measured and reported, and a mismatch is raised as
its own finding.

**Comparing a handful of trades.** Drift in expectancy over 8 live trades is not
detectable. Every metric carries its sample size, and anything below the floor
is reported as ``insufficient`` with the count, never as a difference.

The module is pure: it takes rows in, gives a structured verdict out. It does not
know what a database is, so the whole thing is testable against known answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from atr.research import learning_stats as stats

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: The sources, in the order they should be compared. The first is the
#: reference: a strategy is expected to *degrade* relative to its backtest,
#: never the other way round.
SOURCE_ORDER: tuple[str, ...] = ("BACKTEST", "PAPER", "LIVE")

#: A metric needs at least this many observations on **both** sides before a
#: comparison is attempted. Below it the verdict is ``insufficient``.
MIN_DRIFT_SAMPLE = stats.MIN_SAMPLE

#: Below this, a difference is described as "within noise" even when the
#: arithmetic says otherwise.
SMALL_EFFECT = 0.10

#: What each metric's direction of concern is. ``higher_is_better`` decides
#: which sign of a delta is deterioration, so the module does not have to be
#: trusted to get every sign right by hand.
METRIC_DIRECTIONS: dict[str, bool] = {
    "expectancy_per_trade": True,
    "mean_return_pct": True,
    "win_rate": True,
    "profit_factor": True,
    "payoff_ratio": True,
    "max_drawdown_pct": False,
    "slippage_bps": False,
    # Duration is deliberately **not** scored. A longer hold is not worse than
    # a shorter one — it means something changed, and which way that cuts
    # depends on the strategy. Reporting it as "deteriorated" would state a
    # judgement the data does not carry.
}

#: Metrics measured and printed but never given an improved/deteriorated
#: verdict. They are context, not findings.
NEUTRAL_METRICS: frozenset[str] = frozenset({"duration_days"})

#: Adverbs for each magnitude, so ``f"{magnitude}ly"`` cannot produce
#: "largely"/"materiallyly" style nonsense. "large" → "sharply".
MAGNITUDE_ADVERB: dict[str, str] = {
    "modest": "modestly",
    "material": "materially",
    "large": "sharply",
    "within noise": "marginally",
}


class DriftError(Exception):
    """Raised only for programming errors — a bad metric name, not bad data."""


# ---------------------------------------------------------------------------
# result shapes
# ---------------------------------------------------------------------------


@dataclass
class MetricComparison:
    """One metric, measured on two sources.

    ``status`` is the field that matters. It is one of:

    * ``ok``            — both sides cleared the floor and the delta is reported
    * ``insufficient``  — one side is too small; ``reason`` says which
    * ``absent``        — one side has no observations of this metric at all
    * ``not_comparable``— the two sides share nothing to compare

    A caller that only reads ``delta`` will see ``None`` in every non-``ok``
    case, so the honest path is also the default path.
    """

    metric: str
    label: str
    higher_is_better: bool
    reference: str
    comparison: str
    status: str
    reference_value: float | None = None
    comparison_value: float | None = None
    reference_n: int = 0
    comparison_n: int = 0
    delta: float | None = None
    relative: float | None = None
    magnitude: str = "unknown"
    direction: str = "unknown"
    reason: str | None = None
    p_value: float | None = None
    significant: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "higher_is_better": self.higher_is_better,
            "reference": self.reference,
            "comparison": self.comparison,
            "status": self.status,
            "reference_value": _round(self.reference_value),
            "comparison_value": _round(self.comparison_value),
            "reference_n": self.reference_n,
            "comparison_n": self.comparison_n,
            "delta": _round(self.delta, 4),
            "relative": _round(self.relative, 4),
            "magnitude": self.magnitude,
            "direction": self.direction,
            "reason": self.reason,
            "p_value": _round(self.p_value, 5),
            "significant": self.significant,
        }


@dataclass
class SourceDrift:
    """Everything one source-vs-source comparison found."""

    reference: str
    comparison: str
    reference_n: int
    comparison_n: int
    status: str
    metrics: list[MetricComparison] = field(default_factory=list)
    distribution: dict[str, Any] = field(default_factory=dict)
    regime: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def metric(self, name: str) -> MetricComparison | None:
        for item in self.metrics:
            if item.metric == name:
                return item
        return None

    @property
    def deteriorated(self) -> list[str]:
        """The metrics that got *worse*, in the direction that matters."""
        return [
            item.metric
            for item in self.metrics
            if item.status == "ok" and item.direction == "deteriorated"
        ]

    @property
    def improved(self) -> list[str]:
        return [
            item.metric
            for item in self.metrics
            if item.status == "ok" and item.direction == "improved"
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "comparison": self.comparison,
            "reference_n": self.reference_n,
            "comparison_n": self.comparison_n,
            "status": self.status,
            "metrics": [m.as_dict() for m in self.metrics],
            "distribution": self.distribution,
            "regime": self.regime,
            "findings": self.findings,
            "limitations": self.limitations,
            "deteriorated": self.deteriorated,
            "improved": self.improved,
        }


@dataclass
class DriftAnalysis:
    """The whole picture, across every source pair that can be formed."""

    strategy: str
    available_sources: dict[str, int]
    pairs: list[SourceDrift] = field(default_factory=list)
    headline: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    advisory: bool = True
    applies_changes: bool = False

    def pair(self, reference: str, comparison: str) -> SourceDrift | None:
        for item in self.pairs:
            if item.reference == reference and item.comparison == comparison:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "available_sources": self.available_sources,
            "headline": self.headline,
            "findings": self.findings,
            "limitations": self.limitations,
            "pairs": [p.as_dict() for p in self.pairs],
            "advisory": self.advisory,
            "applies_changes": self.applies_changes,
        }


# ---------------------------------------------------------------------------
# per-source measurement
# ---------------------------------------------------------------------------


def _values(rows: Sequence[dict[str, Any]], column: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _closed(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only closed trades can be scored. An open position has no outcome."""
    return [row for row in rows if row.get("exit_ts") is not None]


def _max_drawdown_pct(rows: Sequence[dict[str, Any]]) -> float | None:
    """Peak-to-trough on the trade-by-trade equity path, in percent.

    Deliberately computed from the realised trade sequence rather than an
    equity curve: the curve is a backtest artefact and the journal has no
    equivalent, so using it would make the two sides incomparable — the exact
    failure this module exists to avoid.
    """
    ordered = sorted(
        (row for row in rows if row.get("net_pnl") is not None),
        key=lambda row: str(row.get("exit_ts") or row.get("entry_ts") or ""),
    )
    if not ordered:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for row in ordered:
        equity += float(row["net_pnl"])
        peak = max(peak, equity)
        base = abs(peak) if peak > 0 else None
        if base is None:
            # No peak to fall from yet: the drawdown is the loss itself, and
            # there is no capital base to express it against.
            continue
        worst = max(worst, (peak - equity) / base)
    return worst * 100.0 if worst else 0.0


def measure(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Every drift metric for one source, with its sample size.

    Returns ``None`` for any metric the sample cannot support, so a caller
    cannot accidentally plot a zero that means "unmeasured".
    """
    closed = _closed(rows)
    pnl = _values(closed, "net_pnl")
    returns = _values(closed, "return_pct")
    duration = _values(closed, "duration_days")
    slippage = _values(closed, "slippage_bps")

    summary = stats.summarise(pnl) if pnl else None

    return {
        "n": len(closed),
        "metrics": {
            "expectancy_per_trade": (
                summary.mean if summary is not None else None
            ),
            "mean_return_pct": (
                sum(returns) / len(returns) if returns else None
            ),
            "win_rate": (
                # ``win_rate_outcomes`` returns (wins, total, rate); the rate is
                # None on an empty sample, which is the behaviour we want here.
                stats.win_rate_outcomes([1.0 if value > 0 else 0.0 for value in pnl])[2]
                if pnl
                else None
            ),
            "profit_factor": summary.profit_factor if summary is not None else None,
            "payoff_ratio": summary.payoff_ratio if summary is not None else None,
            "max_drawdown_pct": _max_drawdown_pct(closed),
            "slippage_bps": (
                sum(slippage) / len(slippage) if slippage else None
            ),
            "duration_days": (
                sum(duration) / len(duration) if duration else None
            ),
        },
        "counts": {
            "closed": len(closed),
            "with_return_pct": len(returns),
            "with_slippage": len(slippage),
            "with_duration": len(duration),
        },
        "pnl": pnl,
    }


# ---------------------------------------------------------------------------
# distribution and regime
# ---------------------------------------------------------------------------


def _distribution(
    reference: Sequence[dict[str, Any]],
    comparison: Sequence[dict[str, Any]],
    column: str,
) -> dict[str, Any]:
    """How the *mix* of trades changed, not just their average.

    A strategy can hold its expectancy while quietly moving from twenty trades
    to four, or from one symbol to another. Neither shows up in a mean; both
    are what a drift check is for.
    """
    ref_counts = _counts(reference, column)
    cmp_counts = _counts(comparison, column)
    keys = sorted(set(ref_counts) | set(cmp_counts))
    if not keys:
        return {"column": column, "status": "absent", "buckets": {}}

    ref_total = sum(ref_counts.values()) or 1
    cmp_total = sum(cmp_counts.values()) or 1
    buckets = {}
    for key in keys:
        ref_share = ref_counts.get(key, 0) / ref_total
        cmp_share = cmp_counts.get(key, 0) / cmp_total
        buckets[key] = {
            "reference_n": ref_counts.get(key, 0),
            "comparison_n": cmp_counts.get(key, 0),
            "reference_share": round(ref_share, 4),
            "comparison_share": round(cmp_share, 4),
            "share_delta": round(cmp_share - ref_share, 4),
        }
    return {
        "column": column,
        "status": "ok",
        "buckets": buckets,
        # The largest single move in the mix, which is the line worth printing.
        "largest_shift": _largest_shift(buckets),
    }


def _counts(rows: Sequence[dict[str, Any]], column: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        out[str(value)] = out.get(str(value), 0) + 1
    return out


def _largest_shift(buckets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    for key, item in buckets.items():
        magnitude = abs(item["share_delta"])
        if best is None or magnitude > abs(best["share_delta"]):
            best = {"bucket": key, **item}
    return best


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------


def _compare_metric(
    metric: str,
    reference: dict[str, Any],
    comparison: dict[str, Any],
    *,
    reference_name: str,
    comparison_name: str,
    min_sample: int,
) -> MetricComparison:
    higher_is_better = METRIC_DIRECTIONS.get(metric, True)
    label = metric.replace("_", " ")

    ref_value = reference["metrics"].get(metric)
    cmp_value = comparison["metrics"].get(metric)

    base = MetricComparison(
        metric=metric,
        label=label,
        higher_is_better=higher_is_better,
        reference=reference_name,
        comparison=comparison_name,
        status="ok",
        reference_value=ref_value,
        comparison_value=cmp_value,
        reference_n=reference["n"],
        comparison_n=comparison["n"],
    )

    if ref_value is None and cmp_value is None:
        base.status = "absent"
        base.reason = f"neither source recorded {label}"
        return base
    if ref_value is None:
        base.status = "absent"
        base.reason = f"{reference_name} recorded no {label}"
        return base
    if cmp_value is None:
        base.status = "absent"
        base.reason = f"{comparison_name} recorded no {label}"
        return base

    if reference["n"] < min_sample or comparison["n"] < min_sample:
        base.status = "insufficient"
        short = reference_name if reference["n"] < min_sample else comparison_name
        count = min(reference["n"], comparison["n"])
        base.reason = (
            f"{short} has {count} closed trade(s); a difference needs at least "
            f"{min_sample} on both sides to be worth reporting"
        )
        return base

    delta = cmp_value - ref_value
    base.delta = delta
    if ref_value != 0:
        base.relative = delta / abs(ref_value)
    elif delta == 0:
        base.relative = 0.0

    # The sign tells us whether it moved; ``higher_is_better`` tells us whether
    # that movement is good news. Keeping them separate is what stops a rising
    # drawdown from being reported as an improvement.
    if metric in NEUTRAL_METRICS:
        base.direction = "changed" if delta != 0 else "unchanged"
    elif higher_is_better:
        base.direction = "improved" if delta > 0 else "deteriorated" if delta < 0 else "unchanged"
    else:
        base.direction = "improved" if delta < 0 else "deteriorated" if delta > 0 else "unchanged"

    relative = abs(base.relative) if base.relative is not None else 0.0
    if base.direction in {"unchanged", "changed"} or relative < SMALL_EFFECT:
        base.magnitude = "within noise"
    elif relative < 0.35:
        base.magnitude = "modest"
    elif relative < 0.75:
        base.magnitude = "material"
    else:
        base.magnitude = "large"

    # Is the gap bigger than the scatter on either side? Only available when
    # both sides kept a per-trade series, which expectancy does.
    if metric == "expectancy_per_trade":
        ref_pnl = reference.get("pnl") or []
        cmp_pnl = comparison.get("pnl") or []
        test = stats.welch_t(ref_pnl, cmp_pnl)
        if test is not None:
            # ``welch_t`` returns (t_statistic, degrees_of_freedom). Feeding the
            # df into a p-value would print "p=39.0000" — a number that looks
            # like a p-value and is not one. Same conversion the bucket
            # comparison uses, so the two cannot disagree.
            t_statistic, _df = test
            base.p_value = stats.normal_two_sided_p(t_statistic)
            base.significant = (
                base.p_value < 1.0 - stats.CONFIDENCE_95
                and base.magnitude != "within noise"
            )

    return base


def compare_sources(
    reference_rows: Sequence[dict[str, Any]],
    comparison_rows: Sequence[dict[str, Any]],
    *,
    reference: str,
    comparison: str,
    min_sample: int = MIN_DRIFT_SAMPLE,
    distribution_columns: Sequence[str] = (
        "symbol",
        "setup",
        "exit_reason",
        "market_regime",
        "direction",
        "strategy_key",
    ),
) -> SourceDrift:
    """One source against another, on every metric both can support."""
    ref = measure(reference_rows)
    cmp = measure(comparison_rows)

    drift = SourceDrift(
        reference=reference,
        comparison=comparison,
        reference_n=ref["n"],
        comparison_n=cmp["n"],
        status="ok",
    )

    if ref["n"] == 0 and cmp["n"] == 0:
        drift.status = "insufficient"
        drift.limitations.append(
            f"neither {reference} nor {comparison} has a closed trade, so there "
            "is nothing to compare"
        )
    elif ref["n"] == 0:
        drift.status = "insufficient"
        drift.limitations.append(
            f"{reference} has no closed trades; {comparison} cannot be judged "
            "against a baseline that does not exist"
        )
    elif cmp["n"] == 0:
        drift.status = "insufficient"
        drift.limitations.append(
            f"{comparison} has no closed trades; the absence of drift is not "
            "evidence that the strategy is behaving as backtested"
        )

    for metric in METRIC_DIRECTIONS:
        drift.metrics.append(
            _compare_metric(
                metric, ref, cmp,
                reference_name=reference, comparison_name=comparison,
                min_sample=min_sample,
            )
        )
    # Neutral metrics are measured and shown, but never judged.
    for metric in sorted(NEUTRAL_METRICS):
        drift.metrics.append(
            _compare_metric(
                metric, ref, cmp,
                reference_name=reference, comparison_name=comparison,
                min_sample=min_sample,
            )
        )

    for column in distribution_columns:
        result = _distribution(reference_rows, comparison_rows, column)
        if result["status"] == "ok":
            drift.distribution[column] = result

    drift.regime = _regime_mix(reference_rows, comparison_rows)
    if (
        drift.regime.get("status") == "ok"
        and drift.regime.get("mismatch")
        and drift.reference_n >= min_sample
        and drift.comparison_n >= min_sample
    ):
        drift.findings.append(
            {
                "kind": "regime_mismatch",
                "severity": "warning",
                "statement": (
                    f"{comparison} traded under a different regime mix than "
                    f"{reference} — any difference below is partly conditions, "
                    "not decay"
                ),
                "evidence": drift.regime["summary"],
            }
        )

    drift.findings.extend(_drift_findings(drift, ref, cmp))
    return drift


def _regime_mix(
    reference: Sequence[dict[str, Any]], comparison: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Whether the two periods were even comparable conditions."""
    ref_rows = [row for row in _closed(reference) if row.get("market_regime")]
    cmp_rows = [row for row in _closed(comparison) if row.get("market_regime")]
    if not ref_rows and not cmp_rows:
        return {
            "status": "absent",
            "summary": "no trade in either source carries a market-regime label",
        }

    ref_counts = _counts(ref_rows, "market_regime")
    cmp_counts = _counts(cmp_rows, "market_regime")
    ref_total = sum(ref_counts.values()) or 1
    cmp_total = sum(cmp_counts.values()) or 1

    keys = sorted(set(ref_counts) | set(cmp_counts))
    shares = {
        key: {
            "reference_share": round(ref_counts.get(key, 0) / ref_total, 4),
            "comparison_share": round(cmp_counts.get(key, 0) / cmp_total, 4),
        }
        for key in keys
    }
    largest = max(
        keys,
        key=lambda key: abs(
            shares[key]["comparison_share"] - shares[key]["reference_share"]
        ),
    )
    shift = abs(shares[largest]["comparison_share"] - shares[largest]["reference_share"])

    coverage = len(ref_rows) / (len(_closed(reference)) or 1)
    return {
        "status": "ok",
        "reference_counts": ref_counts,
        "comparison_counts": cmp_counts,
        "shares": shares,
        "mismatch": shift >= 0.25,
        "largest_shift": {
            "regime": largest,
            "share_delta": round(shift, 4),
        },
        "coverage": round(coverage, 4),
        "summary": (
            f"{largest} is {shift:.0%} of the mix on one side and not the other"
            if shift
            else "the regime mix is the same on both sides"
        ),
    }


def _drift_findings(
    drift: SourceDrift, ref: dict[str, Any], cmp: dict[str, Any]
) -> list[dict[str, Any]]:
    """Plain statements about what got worse, each with its numbers."""
    out: list[dict[str, Any]] = []
    if drift.status != "ok":
        return out

    for item in drift.metrics:
        if item.status != "ok" or item.direction != "deteriorated":
            continue
        if item.magnitude == "within noise":
            continue
        severity = "warning" if item.magnitude in {"material", "large"} else "info"
        adverb = MAGNITUDE_ADVERB.get(item.magnitude, "materially")
        # "below" is only correct when the value actually fell. A slippage rise
        # is a deterioration *and* an increase, and printing "11.00 is below
        # 2.00" would be read as a data error by anyone who notices.
        word = "below" if item.delta < 0 else "above"
        out.append(
            {
                "kind": "metric_drift",
                "metric": item.metric,
                "severity": severity,
                "statement": (
                    f"{item.label} is {adverb} worse than the {drift.reference} "
                    f"baseline: {item.comparison_value:,.2f} vs "
                    f"{item.reference_value:,.2f} ({word} by "
                    f"{abs(item.delta):,.2f})"
                ),
                "evidence": (
                    f"{item.comparison_n} {drift.comparison} trades against "
                    f"{item.reference_n} {drift.reference} trades"
                    + (
                        f"; p={item.p_value:.4f}"
                        if item.p_value is not None
                        else ""
                    )
                ),
                "sample_size": item.comparison_n,
                "confidence": (
                    "high" if item.significant
                    else "medium" if item.magnitude in {"material", "large"}
                    else "low"
                ),
            }
        )

    # Falling trade count is its own finding: it changes what every other
    # number above means, and it is invisible in a per-trade average.
    if ref["n"] and drift.comparison_n:
        ratio = drift.comparison_n / ref["n"]
        if ratio < 0.5:
            out.append(
                {
                    "kind": "trade_frequency",
                    "severity": "info",
                    "statement": (
                        f"{drift.comparison} produced {drift.comparison_n} closed "
                        f"trades against {ref['n']} in the {drift.reference}; the "
                        "strategy is trading less often"
                    ),
                    "evidence": f"ratio {ratio:.2f}",
                    "sample_size": drift.comparison_n,
                    "confidence": "medium",
                }
            )
    return out


# ---------------------------------------------------------------------------
# the whole analysis
# ---------------------------------------------------------------------------


def _pairs_for(available: dict[str, int], reference: str | None) -> list[tuple[str, str]]:
    """Which source pairs to compare.

    Default is each non-empty source against the backtest, because that is the
    pairing the question implies: *did it do live what it did in testing?*
    Backtest-against-live is not the same question and is not asked by default.
    """
    present = [source for source in SOURCE_ORDER if available.get(source, 0) > 0]
    if reference is not None:
        if reference not in available:
            raise DriftError(f"unknown source {reference!r}; expected one of {SOURCE_ORDER}")
        return [(reference, other) for other in present if other != reference]
    if "BACKTEST" not in present:
        # No baseline: report the pairs that can still be formed rather than
        # silently returning nothing.
        return [
            (present[i], present[j])
            for i in range(len(present))
            for j in range(i + 1, len(present))
        ]
    return [("BACKTEST", other) for other in present if other != "BACKTEST"]


def analyse(
    rows: Sequence[dict[str, Any]],
    *,
    strategy: str | None = None,
    reference: str | None = None,
    min_sample: int = MIN_DRIFT_SAMPLE,
) -> DriftAnalysis:
    """Compare the sources present in ``rows``, strategy by strategy.

    Grouping by strategy is not optional. A backtest of one strategy and live
    trades from another share an account and nothing else; averaging them
    together produces a drift number for a strategy that does not exist.
    """
    closed = _closed(rows)
    if strategy:
        closed = [row for row in closed if _matches(row, strategy)]
        if not closed:
            return DriftAnalysis(
                strategy=strategy,
                available_sources={source: 0 for source in SOURCE_ORDER},
                headline=f"no closed trades for {strategy}, so drift cannot be measured.",
                limitations=[
                    f"nothing in the dataset matches {strategy!r}; drift is "
                    "unmeasured rather than zero"
                ],
            )

    available = {
        source: sum(1 for row in closed if row.get("source") == source)
        for source in SOURCE_ORDER
    }

    result = DriftAnalysis(strategy=strategy or "ALL", available_sources=available)

    groups = _group_by_strategy(closed)
    if len(groups) > 1 and strategy is None:
        result.limitations.append(
            f"{len(groups)} strategies are present ({', '.join(sorted(groups))}); "
            "each is compared separately because a backtest of one says nothing "
            "about live trades from another"
        )
        result.findings.append(
            {
                "kind": "multiple_strategies",
                "severity": "info",
                "statement": (
                    "drift is measured per strategy; a whole-book average is not "
                    "reported because it would describe no strategy in particular"
                ),
                "evidence": ", ".join(sorted(groups)),
            }
        )

    for name, group in sorted(groups.items()):
        group_available = {
            source: sum(1 for row in group if row.get("source") == source)
            for source in SOURCE_ORDER
        }
        for ref_name, cmp_name in _pairs_for(group_available, reference):
            pair = compare_sources(
                [row for row in group if row.get("source") == ref_name],
                [row for row in group if row.get("source") == cmp_name],
                reference=ref_name,
                comparison=cmp_name,
                min_sample=min_sample,
            )
            result.pairs.append(pair)
            result.findings.extend(pair.findings)

    if not result.pairs:
        result.limitations.append(
            "no two sources have trades, so no comparison could be formed"
        )
    # One owner for the verdict. Previously ``_empty_headline`` overwrote this
    # for the no-pairs case, which left ``_headline``'s own empty-comparison
    # branch unreachable — so the honest message and the reassuring one lived
    # in different functions and only one of them was ever on the path.
    result.headline = _headline(result)

    if available.get("PAPER", 0) == 0 and available.get("LIVE", 0) == 0:
        result.limitations.append(
            "there are no paper or live trades at all, so this is a comparison "
            "of the backtest with itself or with nothing"
            if available.get("BACKTEST", 0)
            else "the dataset contains no closed trades from any source"
        )

    return result


def _matches(row: dict[str, Any], strategy: str) -> bool:
    """``id`` or ``id@version``, matching the service's own rule."""
    if "@" in strategy:
        key, _, version = strategy.partition("@")
        return str(row.get("strategy_key") or row.get("strategy_id")) == key and str(
            row.get("strategy_version")
        ) == version
    for column in ("strategy_key", "strategy_id"):
        if str(row.get(column)) == strategy:
            return True
    return False


def _group_by_strategy(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get("strategy_key") or row.get("strategy_id") or "unknown"
        groups.setdefault(str(key), []).append(row)
    return groups


def _empty_headline(available: dict[str, int]) -> str:
    """Why no pair could be formed, said precisely.

    These read as similar failures and are not: two sources holding trades for
    *different strategies* is a data-shape problem, not an absence of data, and
    a reader told "only one source has trades" would go looking for trades that
    are already there.
    """
    total = sum(available.values())
    if total == 0:
        return "No closed trades, so drift cannot be measured."
    present = [source for source, count in available.items() if count]
    if len(present) == 1:
        counts = ", ".join(f"{k} {v}" for k, v in available.items() if v)
        return f"Only one source has trades ({counts}); drift needs two to compare."
    if len(present) > 1:
        counts = ", ".join(f"{k} {v}" for k, v in available.items() if v)
        return (
            f"{len(present)} sources hold trades ({counts}) but no strategy "
            "appears in more than one of them, so there is nothing to compare."
        )
    return "No closed trades, so drift cannot be measured."


def _headline(result: DriftAnalysis) -> str:
    """One sentence, and it does not overclaim.

    The verdict word is chosen from what was actually testable. ``stable`` is
    unreachable when no comparison could be made — and, more importantly,
    "no drift detected" is unreachable when a metric *was* measured on both
    sides and the only reason it was not scored is that the sample was too
    small. That case looks identical to a clean bill of health in the numbers
    and is the opposite of one in meaning.
    """
    comparable = [pair for pair in result.pairs if pair.status == "ok"]
    if not comparable:
        # No pair could be scored. Say which of the three reasons applies,
        # because they mean different things: nothing recorded at all, one
        # source with no counterpart, or two sources that disagree about which
        # strategy they are talking about.
        if not result.pairs:
            return _empty_headline(result.available_sources)
        pair = result.pairs[0]
        return (
            "Drift could not be measured: "
            + (pair.limitations[0] if pair.limitations else f"{pair.comparison} has too few trades")
            + "."
        )

    deteriorated = [
        finding
        for pair in comparable
        for finding in pair.findings
        if finding["kind"] == "metric_drift" and finding["severity"] == "warning"
    ]
    if deteriorated:
        worst = deteriorated[0]
        return (
            f"Drift detected — {len(deteriorated)} metric(s) below baseline. "
            f"{worst['statement']}."
        )

    soft = [
        finding
        for pair in comparable
        for finding in pair.findings
        if finding["kind"] == "metric_drift"
    ]
    if soft:
        return (
            f"No material drift in the {len(soft)} metric(s) that moved; every "
            "change is within noise."
        )

    # Nothing was scored. Before calling that "no drift", check whether any
    # metric had real values on both sides and was only blocked by sample size
    # — a near-miss, not a clean result.
    blocked = [
        item
        for pair in comparable
        for item in pair.metrics
        if item.status == "insufficient"
        and item.reference_value is not None
        and item.comparison_value is not None
    ]
    if blocked:
        names = ", ".join(item.metric.replace("_", " ") for item in blocked[:3])
        return (
            f"Drift not yet measurable — {len(blocked)} metric(s) ({names}) were "
            "recorded on both sides but the samples are too small to score. "
            "This is not evidence that the strategy is behaving as backtested."
        )

    if any(item.status == "absent" for pair in comparable for item in pair.metrics):
        return (
            "No drift detected in the metrics both sources could support, but "
            "some metrics were never recorded on one side — see the per-metric "
            "reasons rather than reading this as stability."
        )

    return (
        "No drift detected across the metrics that both sources could support. "
        "This is not proof of stability — it is the absence of a measurable "
        "difference in this sample."
    )


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
