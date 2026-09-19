"""Forward-learning operations: is the book accumulating trustworthy evidence?

This module answers the operator's question — "does the learning system have
enough real data yet?" — and nothing else. It does not change a strategy, pause
trading, touch a risk limit, deploy anything, or tune a single weight. Every
state it emits is a research label; the optimisation system reads its own
inputs and makes its own decisions.

What is reused, not rebuilt
----------------------------
* :mod:`atr.research.learning_stats` — ``MIN_SAMPLE`` (10) and
  ``SMALL_SAMPLE`` (30) are the first two evidence gates, and every mean,
  interval and win rate comes from :func:`summarise`. No statistic is
  reimplemented here.
* :mod:`atr.research.learning_evidence` — what counts as forward is decided by
  ``row_grade`` / ``is_forward_grade``. An in-sample row can never clear a gate,
  because it never enters the count.
* :mod:`atr.research.learning_context` — the context-score bands and the
  cumulative-drawdown helper, so the readiness view and the effectiveness view
  cannot disagree about what "80–100" means.

The one number this module owns
-------------------------------
``ANALYSIS_READY_N = 50`` is an *operational policy floor*, not a statistical
finding: bucketed claims need every comparator arm above the small-sample
line, and the team chose fifty forward trades as the point where per-strategy
analysis stops being a small-sample exercise. It is stated here, in one place,
so a reader can argue with it rather than discover it.

Honesty rules specific to this surface
--------------------------------------
* **Duplicates never inflate a sample.** Rows are deduplicated by ``trade_ref``
  (keep first) before anything is counted, and every dropped reference is
  reported — in the payload, not a log nobody reads.
* **Missing is missing.** A row without an outcome metric contributes no number
  to any average; a row without a recorded context contributes no band to any
  distribution. Absent is carried as ``None`` and surfaced as *unrecorded*,
  never bucketed as zero.
* **Quality problems are flagged, not deleted.** A suspect row stays in the
  book with its grade intact; the issue list says what is wrong with it and
  which trades are affected.
* **Readiness caps on gaps.** A trade count alone never yields "ready": with no
  measurable outcome, or with no recorded context at all, a strategy is held at
  ``MINIMUM SAMPLE`` however many rows it holds, and the reason is stated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from atr.research import learning_stats as stats
from atr.research.learning_context import SCORE_BANDS, max_drawdown, score_band
from atr.research.learning_evidence import (
    NOTE_NO_STAMP,
    is_forward_grade,
    row_grade,
)

#: Operational policy floor for per-strategy forward analysis. See the module
#: docstring for why this number lives here and what it is (policy, not proof).
ANALYSIS_READY_N = 50

#: The three evidence gates, ascending. The first two are the statistics
#: module's own floors; the third is the policy floor above.
EVIDENCE_GATES: tuple[int, ...] = (stats.MIN_SAMPLE, stats.SMALL_SAMPLE, ANALYSIS_READY_N)

#: Per-bucket evidence vocabulary. ``SMALL_SAMPLE`` covers everything from the
#: minimum to the analysis floor: a bucket of 24 has cleared the minimum and
#: is still too small to analyse, and calling that anything but small-sample
#: would overstate it.
STATUS_INSUFFICIENT = "INSUFFICIENT"
STATUS_SMALL_SAMPLE = "SMALL_SAMPLE"
STATUS_ANALYSIS_READY = "ANALYSIS_READY"

#: Strategy research states, in order. Research labels only — see the module
#: docstring.
STATE_NOT_READY = "NOT READY"
STATE_MINIMUM_SAMPLE = "MINIMUM SAMPLE"
STATE_ANALYSIS_READY = "ANALYSIS READY"
STATE_OPTIMIZATION_ELIGIBLE = "OPTIMIZATION ELIGIBLE"

#: A row counts as recent when its exit falls inside this window. The split is
#: descriptive (two means side by side), never a test.
RECENT_DAYS = 30

#: |return_pct| above this on a cash-equity trade is flagged for review. It is
#: not discarded: a genuine 100%+ runner exists, but so does a misplaced
#: decimal point, and the reader should know which rows to look at.
ABNORMAL_RETURN_PCT = 100.0

#: An order and a journal entry belong to the same signal when the entry falls
#: inside this window around the order's creation. This is the same rule
#: ``SignalContextService._match_episode`` applies from the other direction
#: (context → outcome); it lives here so the two directions cannot drift apart.
SIGNAL_MATCH_WINDOW_SEC = 12 * 3600


# ---------------------------------------------------------------------------
# gates and states
# ---------------------------------------------------------------------------


def evidence_status(n_forward: int) -> str:
    """The per-bucket status for a forward count.

    In-sample rows must never reach this function — count forward rows only.
    """
    n = int(n_forward)
    if n < stats.MIN_SAMPLE:
        return STATUS_INSUFFICIENT
    if n < ANALYSIS_READY_N:
        return STATUS_SMALL_SAMPLE
    return STATUS_ANALYSIS_READY


def next_gate(n_forward: int) -> int | None:
    """The next evidence gate the count has not cleared, if any."""
    n = int(n_forward)
    for gate in EVIDENCE_GATES:
        if n < gate:
            return gate
    return None


def research_state(
    n_forward: int,
    *,
    recorded_forward_observations: int = 0,
    blocking_gaps: list[str] | None = None,
) -> tuple[str, list[str]]:
    """The strategy research state, with the reasons stated.

    ``recorded_forward_observations`` is how many persisted forward learning
    observations the strategy holds — the actual input the optimisation system
    consumes. A strategy becomes ``OPTIMIZATION ELIGIBLE`` only when the book
    is analysis-ready *and* that pipeline has produced something for the
    optimiser to read; a trade count alone never suffices, and neither does a
    count beside a gap that blocks measurement.
    """
    n = int(n_forward)
    gaps = list(blocking_gaps or [])
    recorded = int(recorded_forward_observations)

    if n < stats.MIN_SAMPLE:
        reasons = [
            f"{n} forward trades are below the {stats.MIN_SAMPLE}-trade minimum; "
            "no finding of any kind is supported"
        ]
        reasons.extend(gaps)
        return STATE_NOT_READY, reasons

    if gaps:
        reasons = [
            f"{n} forward trades clear the minimum, but the strategy is held here: "
            + "; ".join(gaps)
        ]
        return STATE_MINIMUM_SAMPLE, reasons

    if n < ANALYSIS_READY_N:
        return (
            STATE_MINIMUM_SAMPLE,
            [
                f"{n} forward trades clear the {stats.MIN_SAMPLE}-trade minimum; "
                f"the {ANALYSIS_READY_N}-trade analysis floor is not yet reached"
            ],
        )

    if recorded <= 0:
        return (
            STATE_ANALYSIS_READY,
            [
                f"{n} forward trades clear the {ANALYSIS_READY_N}-trade analysis floor; "
                "no recorded forward observation feeds optimisation yet"
            ],
        )
    return (
        STATE_OPTIMIZATION_ELIGIBLE,
        [
            f"{n} forward trades clear the {ANALYSIS_READY_N}-trade analysis floor; "
            f"{recorded} recorded forward observation(s) are available to optimisation"
        ],
    )


# ---------------------------------------------------------------------------
# rows in, evidence out
# ---------------------------------------------------------------------------


def forward_closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed forward rows only: open trades have no outcome, in-sample rows
    are not evidence. Both are excluded, not zeroed."""
    return [
        row
        for row in rows
        if row.get("exit_ts") is not None and is_forward_grade(row_grade(row))
    ]


def dedupe_forward(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deduplicate by ``trade_ref``, keep-first.

    Returns the unique rows and the dropped references. A row with no
    ``trade_ref`` cannot be shown to be a duplicate of anything, so it is kept
    — dropping an unkeyed row would discard evidence on a suspicion.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    dropped: list[str] = []
    for row in rows:
        ref = row.get("trade_ref")
        if ref is None or ref not in seen:
            unique.append(row)
            if ref is not None:
                seen.add(str(ref))
        else:
            dropped.append(str(ref))
    return unique, dropped


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _exit_ts(row: dict[str, Any]) -> datetime | None:
    return _as_utc(row.get("exit_ts")) or _as_utc(row.get("entry_ts"))


def summarise_forward(
    rows: list[dict[str, Any]],
    *,
    metric: str = "return_pct",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The per-strategy forward-evidence summary. Purely descriptive.

    ``rows`` should already be closed, forward and deduplicated —
    :func:`forward_closed` and :func:`dedupe_forward` do that — but the
    function tolerates anything: rows without the metric contribute nothing to
    the figures, rows without a context score contribute nothing to the score
    distribution, and every such exclusion is counted, not hidden.
    """
    moment = now or datetime.now(UTC)
    ordered = sorted(rows, key=lambda r: (_exit_ts(r) is None, _exit_ts(r)))

    values = [v for v in (_metric_value(r, metric) for r in ordered) if v is not None]
    distribution = stats.summarise(values).as_dict()
    drawdown = max_drawdown(values)

    with_score = [r for r in ordered if r.get("context_score") is not None]
    bands: dict[str, int] = {}
    for label, _low, _high in SCORE_BANDS:
        bands[label] = sum(1 for r in with_score if score_band(r.get("context_score")) == label)

    regimes: dict[str, int] = {}
    for row in ordered:
        key = str(row.get("market_regime") or "unrecorded")
        regimes[key] = regimes.get(key, 0) + 1

    sectors: dict[str, int] = {}
    for row in ordered:
        key = str(row.get("sector") or "unrecorded")
        sectors[key] = sectors.get(key, 0) + 1

    cutoff = moment - timedelta(days=RECENT_DAYS)
    recent = [v for r, v in ((_exit_ts(r), _metric_value(r, metric)) for r in ordered) if r is not None and r >= cutoff and v is not None]
    historical = [v for r, v in ((_exit_ts(r), _metric_value(r, metric)) for r in ordered) if r is not None and r < cutoff and v is not None]

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "n": len(ordered),
        "n_with_metric": len(values),
        "metric": metric,
        "stats": distribution,
        "max_drawdown": drawdown,
        "score_bands": [
            {
                "band": label,
                "forward_trades": bands[label],
                "required": next_gate(bands[label]),
                "status": evidence_status(bands[label]),
            }
            for label, _low, _high in SCORE_BANDS
        ],
        "score_coverage": {"with_score": len(with_score), "scanned": len(ordered)},
        "regime_distribution": regimes,
        "sector_distribution": sectors,
        "recent_vs_historical": {
            "window_days": RECENT_DAYS,
            "recent_n": len(recent),
            "recent_mean": _mean(recent),
            "historical_n": len(historical),
            "historical_mean": _mean(historical),
        },
    }


def _metric_value(row: dict[str, Any], metric: str) -> float | None:
    value = row.get(metric)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def weekly_progression(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trades per ISO week with a running total: how evidence accumulates.

    Keyed by exit week (entry week when no exit is recorded). Weeks with no
    trades are not shown — a gap in the list *is* the gap in trading, and
    filling it with zeros would read as measured inactivity.
    """
    per_week: dict[str, int] = {}
    for row in rows:
        stamp = _exit_ts(row)
        if stamp is None:
            continue
        monday = (stamp - timedelta(days=stamp.weekday())).date().isoformat()
        per_week[monday] = per_week.get(monday, 0) + 1
    weeks = sorted(per_week)
    running = 0
    out = []
    for week in weeks:
        running += per_week[week]
        out.append({"week": week, "trades": per_week[week], "cumulative": running})
    return out


# ---------------------------------------------------------------------------
# data quality: flag, never discard
# ---------------------------------------------------------------------------


def data_quality_issues(
    rows: list[dict[str, Any]],
    *,
    metric: str = "return_pct",
    now: datetime | None = None,
    duplicate_refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """What is wrong with the forward book, stated per problem, not per row.

    Every issue carries the affected trade references (up to five) and an
    explanation. Nothing here removes a row: the rows stay in the book with
    their grades intact, and the reader decides what to trust.
    """
    moment = now or datetime.now(UTC)
    issues: list[dict[str, Any]] = []

    def _add(code: str, severity: str, refs: list[str], explanation: str) -> None:
        if not refs:
            return
        issues.append(
            {
                "code": code,
                "severity": severity,
                "count": len(refs),
                "sample_refs": refs[:5],
                "explanation": explanation,
            }
        )

    def _refs(predicate: Any) -> list[str]:
        out = []
        for row in rows:
            if predicate(row):
                ref = row.get("trade_ref")
                out.append(str(ref) if ref is not None else "<unkeyed>")
        return out

    _add(
        "missing_context",
        "watch",
        _refs(lambda r: r.get("context_score") is None),
        "no recorded context score: the signal fired before context retention "
        "existed, enrichment missed it, or no order links the trade to a signal",
    )
    _add(
        "missing_sector",
        "watch",
        _refs(lambda r: r.get("sector") is None),
        "no sector recorded: the symbol is outside the sector table or the "
        "entry could not be enriched",
    )
    _add(
        "missing_benchmark",
        "watch",
        _refs(lambda r: r.get("benchmark_symbol") is None),
        "no benchmark at entry: benchmark-relative features on these rows are "
        "unmeasured, not zero",
    )
    _add(
        "missing_execution",
        "watch",
        _refs(lambda r: r.get("signal_id") is None and r.get("opening_order_id") is None),
        "no linked opening order: the trade cannot be traced to the order that "
        "raised it, so its execution trail is unverifiable",
    )
    _add(
        "stale_price_lookup",
        "watch",
        _refs(lambda r: r.get("close_at_entry") is None and (r.get("bars_available") or 0) >= 50),
        "price history existed at entry but no close was recorded: the lookup "
        "failed rather than the market being thin",
    )
    _add(
        "future_dated",
        "watch",
        _refs(lambda r: (_as_utc(r.get("entry_ts")) or moment) > moment or (_as_utc(r.get("exit_ts")) or moment) > moment),
        "a timestamp lies after the moment of analysis: clock skew, a bad feed, "
        "or a backfill wearing live timestamps",
    )
    _add(
        "exit_before_entry",
        "watch",
        _refs(
            lambda r: _as_utc(r.get("entry_ts")) is not None
            and _as_utc(r.get("exit_ts")) is not None
            and _as_utc(r.get("exit_ts")) < _as_utc(r.get("entry_ts"))
        ),
        "exit precedes entry: the holding period and any path statistic on "
        "these rows are meaningless",
    )
    _add(
        "abnormal_price",
        "watch",
        _refs(
            lambda r: (r.get("entry_price") is not None and float(r.get("entry_price") or 0) <= 0)
            or (r.get("exit_price") is not None and float(r.get("exit_price") or 0) <= 0)
        ),
        "a non-positive entry or exit price: splits and bonuses are adjusted "
        "upstream, so this is a data fault until proven otherwise",
    )
    _add(
        "abnormal_quantity",
        "watch",
        _refs(lambda r: r.get("quantity") is not None and float(r.get("quantity") or 0) <= 0),
        "a non-positive quantity on a closed trade",
    )
    _add(
        "abnormal_return",
        "watch",
        _refs(
            lambda r: _metric_value(r, "return_pct") is not None
            and abs(_metric_value(r, "return_pct") or 0.0) > ABNORMAL_RETURN_PCT
        ),
        f"|return| above {ABNORMAL_RETURN_PCT}% on a cash-equity trade: possible "
        "genuine runner, possible misplaced decimal — review before trusting",
    )
    _add(
        "negative_commission",
        "watch",
        _refs(lambda r: r.get("commission") is not None and float(r.get("commission") or 0) < 0),
        "negative commission: costs recovered as gross-minus-net should never "
        "go below zero",
    )
    _add(
        "missing_outcome",
        "watch",
        _refs(lambda r: r.get("exit_ts") is not None and _metric_value(r, metric) is None),
        f"a closed trade with no {metric}: it counts as evidence collected but "
        "contributes no number to any average",
    )
    def _provenance_broken(row: dict[str, Any]) -> bool:
        if bool(row.get("is_forward")) != is_forward_grade(row_grade(row)):
            return True
        return bool(
            is_forward_grade(row_grade(row))
            and NOTE_NO_STAMP in str(row.get("evidence_note") or "")
        )

    _add(
        "inconsistent_provenance",
        "blocking",
        _refs(_provenance_broken),
        "the row's forward flag disagrees with its grade, or a forward grade "
        "rests on no execution-time stamp: the provenance of these rows cannot "
        "be trusted until resolved",
    )
    if duplicate_refs:
        issues.append(
            {
                "code": "duplicate_trade_ref",
                "severity": "watch",
                "count": len(duplicate_refs),
                "sample_refs": [str(r) for r in duplicate_refs[:5]],
                "explanation": "the same trade reference appeared more than once: "
                "counted once, reported here so the double-write can be found",
            }
        )
    return issues


def blocking_gaps(
    *,
    n_with_metric: int,
    n_with_context: int,
    n_inconsistent_provenance: int = 0,
) -> list[str]:
    """Why a strategy must be held back no matter what its trade count says."""
    gaps: list[str] = []
    if n_with_metric <= 0:
        gaps.append("no forward row carries a measurable outcome: there is nothing to average")
    if n_with_context <= 0:
        gaps.append(
            "no forward row carries a recorded context score: context evidence "
            "has not started accumulating"
        )
    if n_inconsistent_provenance > 0:
        gaps.append(
            f"{n_inconsistent_provenance} row(s) carry inconsistent provenance: "
            "their evidence direction cannot be trusted until resolved"
        )
    return gaps


__all__ = [
    "ABNORMAL_RETURN_PCT",
    "ANALYSIS_READY_N",
    "EVIDENCE_GATES",
    "RECENT_DAYS",
    "SIGNAL_MATCH_WINDOW_SEC",
    "STATE_ANALYSIS_READY",
    "STATE_MINIMUM_SAMPLE",
    "STATE_NOT_READY",
    "STATE_OPTIMIZATION_ELIGIBLE",
    "STATUS_ANALYSIS_READY",
    "STATUS_INSUFFICIENT",
    "STATUS_SMALL_SAMPLE",
    "blocking_gaps",
    "data_quality_issues",
    "dedupe_forward",
    "evidence_status",
    "forward_closed",
    "next_gate",
    "research_state",
    "summarise_forward",
    "weekly_progression",
]
