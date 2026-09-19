"""Does the Context-Aware Signal Engine's score carry information?

This module answers one question and only one: *is a higher context score
followed by a different trade outcome than a lower one?* It does not change the
score, the weights, a strategy, a risk limit or an order. A finding produced here
is a measurement, and the scoring model is left exactly as it was until enough
forward observations exist to justify touching it.

The framework is not rebuilt here. Three existing pieces are reused so this
cannot become a second methodology that disagrees with the first:

* :mod:`atr.research.learning_stats` — every mean, interval, p-value and
  significance label. Nothing statistical is reimplemented.
* :mod:`atr.research.learning_evidence` — the evidence vocabulary. In-sample
  rows and forward rows are never combined, and the conservative default points
  one way.
* :mod:`atr.research.learning_axes` — the declared feature axes (regime,
  breadth, sector strength, stock relative strength, relative volume,
  volatility), reused so a context feature and a dataset feature mean the same
  thing and a bucket count is comparable across both.

The honesty rules specific to this surface
------------------------------------------

* **A score band is compared to its complement, not to zero.** "Do high scores
  make more than low scores?" is the question; "do high scores make money?" is a
  different and much easier one.
* **The multiple-comparisons count is the declared vocabulary size** of every
  axis examined, not the number of buckets that happened to be non-empty. A
  lucky small sample must not be able to shrink its own penalty.
* **A score band that could not be measured is excluded, never bucketed.** No
  ``unknown`` band is compared to the rest as though "we did not know" were a
  context.
* **In-sample (backtest) results are reported, labelled, and kept apart.** They
  are descriptive only and carry no significance claim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from atr.research import learning_stats as stats
from atr.research.learning_axes import (
    AXIS_MARKET_BREADTH,
    AXIS_REGIME,
    AXIS_RVOL,
    AXIS_SECTOR_STRENGTH,
    AXIS_STOCK_RS,
    AXIS_VOLATILITY_REGIME,
    Axis,
)
from atr.research.learning_evidence import (
    CLASS_BACKTEST,
    CLASS_IN_SAMPLE,
    forward_class,
    is_forward_grade,
    row_grade,
)

#: The score bands, declared rather than inferred. Four fixed labels mean the
#: family-wise correction below is honest about how many bands were looked at.
SCORE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("80-100", 80, 100),
    ("60-79", 60, 79),
    ("40-59", 40, 59),
    ("0-39", 0, 39),
)

#: The band a card is compared from, and the band it is compared against. Named
#: constants so the report and a test quote the same two.
HIGH_BAND = "80-100"
LOW_BAND = "0-39"

#: The significance labels that would let a context finding be called supported.
_CLAIMABLE = ("strong", "moderate")


def score_band(score: Any) -> str | None:
    """The declared band for a score, or ``None`` when it cannot be placed.

    ``None`` (or a non-numeric) produces no band at all — see the module
    docstring on why an ``unknown`` band is not allowed.
    """
    try:
        value = int(score)
    except (TypeError, ValueError):
        return None
    for label, low, high in SCORE_BANDS:
        if low <= value <= high:
            return label
    return None


AXIS_CONTEXT_SCORE = Axis(
    name="context_score",
    label="Context score at signal",
    value=lambda row: score_band(row.get("context_score")),
    max_values=len(SCORE_BANDS),
)

#: The context engine's own features, reused from the learning axes. Each reads
#: a point-in-time column the context snapshot supplies; none is recomputed.
CONTEXT_FEATURE_AXES: tuple[Axis, ...] = (
    AXIS_REGIME,
    AXIS_MARKET_BREADTH,
    AXIS_SECTOR_STRENGTH,
    AXIS_STOCK_RS,
    AXIS_RVOL,
    AXIS_VOLATILITY_REGIME,
)

#: What the effectiveness analysis scans by default: the score, then every
#: context feature. The order is stable so a report renders the same way twice.
DEFAULT_CONTEXT_AXES: tuple[Axis, ...] = (AXIS_CONTEXT_SCORE, *CONTEXT_FEATURE_AXES)


def evidence_class_for(signal_source: Any, evidence_kind: Any) -> str:
    """The ``evidence_class`` for a resolved context outcome.

    A ``forward`` outcome from a LIVE deployment is ``LIVE_FORWARD`` and any
    other forward outcome is ``PAPER_FORWARD``; a non-forward outcome from a
    backtest is ``BACKTEST`` and anything else is ``IN_SAMPLE``. The mapping is
    delegated to :mod:`atr.research.learning_evidence` so it cannot drift.
    """
    if str(evidence_kind) == "forward":
        return forward_class(signal_source)
    if str(signal_source).upper() == "BACKTEST":
        return CLASS_BACKTEST
    return CLASS_IN_SAMPLE


def max_drawdown(values: Sequence[float | None]) -> float | None:
    """Largest peak-to-trough fall of the cumulative metric, in its own unit.

    The metric is a per-trade return, so summing it is a cumulative-return
    curve in the unit the metric is already quoted in; the fall is not
    annualised and not scaled, because scaling a number this noisy would imply
    a precision the sample does not have.
    """
    equity = 0.0
    peak = 0.0
    worst = 0.0
    seen = False
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        equity += number
        seen = True
        if equity > peak:
            peak = equity
        worst = max(worst, peak - equity)
    return round(worst, 4) if seen else None


def _ordered(
    labels: Sequence[str], axis: Axis, counts: dict[str, int] | None = None
) -> list[str]:
    """Declared order for the score axis, sample-size order for the rest."""
    if axis.name == AXIS_CONTEXT_SCORE.name:
        declared = [label for label, _, _ in SCORE_BANDS if label in labels]
        return declared + [label for label in labels if label not in declared]
    if counts:
        return sorted(labels, key=lambda label: (-counts.get(label, 0), label))
    return sorted(labels)


@dataclass
class ContextEffectiveness:
    """Measure context bands against their complements, on forward evidence.

    The analysis is a pure function of its rows. The service layer is what turns
    stored contexts and resolved outcomes into those rows; this class never
    reads a database, so a case whose right answer is known can be tested
    directly.
    """

    metric: str = "return_pct"
    min_sample: int = stats.MIN_SAMPLE
    axes: tuple[Axis, ...] = DEFAULT_CONTEXT_AXES
    _comparisons: int = field(default=0, init=False)

    def analyse(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        self._comparisons = sum(axis.max_values or 0 for axis in self.axes)
        forward = [row for row in rows if is_forward_grade(row_grade(row))]
        in_sample = [row for row in rows if not is_forward_grade(row_grade(row))]

        forward_axes = [self._forward_axis(axis, forward) for axis in self.axes]
        in_sample_axes = [self._in_sample_axis(axis, in_sample) for axis in self.axes]
        verdict = self._score_verdict(forward)

        return {
            "metric": self.metric,
            "min_sample": self.min_sample,
            "bonferroni_comparisons": self._comparisons,
            "model_note": (
                "the context score is the sum of met criteria, not a probability; "
                "bands are compared to their complement within the same evidence class"
            ),
            "forward_n": len(forward),
            "forward_with_metric": sum(1 for row in forward if self._value(row) is not None),
            "in_sample_n": len(in_sample),
            "score_verdict": verdict,
            "axes": forward_axes,
            "in_sample_axes": in_sample_axes,
            "caveats": self._caveats(forward, in_sample, verdict),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    # ------------------------------------------------------------------ value
    def _value(self, row: dict[str, Any]) -> float | None:
        value = row.get(self.metric)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------------- forward
    def _forward_axis(self, axis: Axis, rows: list[dict[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        missing_value = 0
        for row in rows:
            label = axis.value(row)
            if label is None:
                continue
            if self._value(row) is None:
                missing_value += 1
                continue
            grouped.setdefault(label, []).append(row)

        counts = {label: len(grows) for label, grows in grouped.items()}
        buckets: list[dict[str, Any]] = []
        for label in _ordered(list(grouped), axis, counts):
            grows = grouped[label]
            values = [value for value in (self._value(r) for r in grows) if value is not None]
            complement = [
                value
                for row in rows
                if axis.value(row) not in (None, label)
                for value in (self._value(row),)
                if value is not None
            ]
            verdict = stats.compare_bucket(
                label,
                values,
                complement,
                comparisons=self._comparisons,
                min_sample=self.min_sample,
            )
            record = verdict.as_dict()
            record["n_forward"] = len(values)
            record["evidence_class_forward"] = self._dominant_class(grows)
            record["max_drawdown"] = max_drawdown([self._value(r) for r in grows])
            buckets.append(record)

        excluded = sum(1 for row in rows if axis.value(row) is None)
        return {
            "axis": axis.name,
            "label": axis.label,
            "max_values": axis.max_values,
            "coverage": {"with_value": len(rows) - excluded, "scanned": len(rows)},
            "excluded": {"axis_missing": excluded, "outcome_missing": missing_value},
            "buckets": buckets,
        }

    # -------------------------------------------------------------- in-sample
    def _in_sample_axis(self, axis: Axis, rows: list[dict[str, Any]]) -> dict[str, Any]:
        grouped: dict[str, list[float]] = {}
        for row in rows:
            label = axis.value(row)
            value = self._value(row)
            if label is None or value is None:
                continue
            grouped.setdefault(label, []).append(value)

        baseline: list[float] = [value for values in grouped.values() for value in values]
        baseline_mean = (sum(baseline) / len(baseline)) if baseline else None

        counts = {label: len(values) for label, values in grouped.items()}
        buckets: list[dict[str, Any]] = []
        for label in _ordered(list(grouped), axis, counts):
            values = grouped[label]
            summary = stats.summarise(values)
            lift = (
                summary.mean - baseline_mean
                if summary.mean is not None and baseline_mean is not None
                else None
            )
            buckets.append(
                {
                    "label": label,
                    "n": summary.n,
                    "stats": summary.as_dict(),
                    "lift": round(lift, 4) if lift is not None else None,
                    "max_drawdown": max_drawdown(values),
                    "suppressed": summary.n < self.min_sample,
                    # Deliberately not a p-value verdict: a backtest is the
                    # history the rule was chosen on, so significance computed
                    # within it would dress a measurement up as a finding.
                    "significance": "in_sample_not_a_claim",
                    "evidence_class": CLASS_IN_SAMPLE,
                }
            )
        return {
            "axis": axis.name,
            "label": axis.label,
            "coverage": {"with_value": len(baseline), "scanned": len(rows)},
            "buckets": buckets,
        }

    # ---------------------------------------------------------- score verdict
    def _score_verdict(self, forward: list[dict[str, Any]]) -> dict[str, Any]:
        by_band: dict[str, list[float]] = {}
        for row in forward:
            band = score_band(row.get("context_score"))
            value = self._value(row)
            if band is None or value is None:
                continue
            by_band.setdefault(band, []).append(value)

        ordered = [label for label, _, _ in SCORE_BANDS if label in by_band]
        high = by_band.get(HIGH_BAND, [])
        low = by_band.get(LOW_BAND, [])
        lower = [value for label, values in by_band.items() if label != HIGH_BAND for value in values]

        high_verdict = (
            stats.compare_bucket(
                HIGH_BAND,
                high,
                lower,
                comparisons=len(SCORE_BANDS),
                min_sample=self.min_sample,
            )
            if high
            else None
        )
        low_verdict = (
            stats.compare_bucket(
                LOW_BAND,
                low,
                [value for label, values in by_band.items() if label != LOW_BAND for value in values],
                comparisons=len(SCORE_BANDS),
                min_sample=self.min_sample,
            )
            if low
            else None
        )

        means = [
            (label, stats.summarise(by_band[label]).mean) for label in ordered
        ]
        # ``ordered`` runs high band to low band, so a score that helps shows
        # means that do not rise as the band falls.
        ranked = [mean for _, mean in means if mean is not None]
        monotonic = len(ranked) < 2 or all(
            earlier >= later for earlier, later in zip(ranked, ranked[1:], strict=False)
        )

        claimable = (
            high_verdict is not None
            and not high_verdict.suppressed
            and high_verdict.significance in _CLAIMABLE
        )
        if not by_band:
            statement = (
                "no forward trade carries both a context score and a resolved "
                "outcome, so the score cannot yet be assessed"
            )
        elif claimable:
            statement = (
                f"the {HIGH_BAND} band averaged {high_verdict.stats.get('mean')} over "
                f"{high_verdict.n} forward trades, {high_verdict.lift} against lower "
                f"bands ({high_verdict.significance}, corrected p="
                f"{high_verdict.p_adjusted})"
            )
        else:
            statement = (
                "the forward sample does not yet distinguish high from low score "
                "bands after the multiple-comparisons correction; no claim is made"
            )

        return {
            "bands": [label for label in ordered],
            "means": {label: mean for label, mean in means},
            "monotonic_high_is_better": bool(monotonic),
            "high_band": high_verdict.as_dict() if high_verdict else None,
            "low_band": low_verdict.as_dict() if low_verdict else None,
            "may_claim": claimable,
            "statement": statement,
        }

    # --------------------------------------------------------------- caveats
    def _caveats(
        self,
        forward: list[dict[str, Any]],
        in_sample: list[dict[str, Any]],
        verdict: dict[str, Any],
    ) -> list[str]:
        out = [
            f"p-values are Bonferroni-corrected for {self._comparisons} bucket "
            "comparisons across the declared axes"
        ]
        if not forward:
            out.append(
                "no forward (paper/live) observations are present; backtest rows "
                "are in-sample by construction and support no claim"
            )
        elif forward and not any(self._value(row) is not None for row in forward):
            out.append("forward contexts exist but none has a resolved outcome yet")
        elif self._below_floor(forward):
            out.append(
                f"no forward bucket has reached the {self.min_sample}-trade "
                "floor; small buckets are shown but their statistics are suppressed"
            )
        if in_sample:
            out.append(
                f"{len(in_sample)} in-sample (backtest) observations are reported "
                "separately and are never combined with forward evidence"
            )
        if not verdict.get("may_claim"):
            out.append(
                "the scoring model is unchanged; this analysis measures the "
                "existing model and does not tune it"
            )
        return out

    def _below_floor(self, forward: list[dict[str, Any]]) -> bool:
        for axis in self.axes:
            counts: dict[str, int] = {}
            for row in forward:
                label = axis.value(row)
                if label is None or self._value(row) is None:
                    continue
                counts[label] = counts.get(label, 0) + 1
            if any(count >= self.min_sample for count in counts.values()):
                return False
        return True

    @staticmethod
    def _dominant_class(rows: list[dict[str, Any]]) -> str:
        classes = [row.get("evidence_class") for row in rows if row.get("evidence_class")]
        return classes[0] if classes else ""


__all__ = [
    "AXIS_CONTEXT_SCORE",
    "CONTEXT_FEATURE_AXES",
    "DEFAULT_CONTEXT_AXES",
    "HIGH_BAND",
    "LOW_BAND",
    "SCORE_BANDS",
    "ContextEffectiveness",
    "evidence_class_for",
    "max_drawdown",
    "score_band",
]
