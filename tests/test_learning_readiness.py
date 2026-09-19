"""Forward-learning operations, at the pure layer.

What this suite pins — each a silent-corruption bug if it drifted:

* **Gates.** INSUFFICIENT below 10, SMALL_SAMPLE below 50, ANALYSIS_READY at
  and above; the next gate is always the smallest uncleared one.
* **States.** The NOT READY → MINIMUM SAMPLE → ANALYSIS READY → OPTIMIZATION
  ELIGIBLE ladder, including the two caps that keep a bare trade count from
  reading as ready: blocking gaps, and an empty observation pipeline.
* **In-sample never counts.** Only closed forward rows enter a summary; open
  trades and backtests are excluded, not zeroed.
* **Duplicates never inflate.** ``trade_ref`` decides identity; the dropped
  references are returned, not swallowed.
* **Missing is missing.** A row without the metric contributes no number; a
  row without a score contributes no band. Both are counted, neither is zero.
* **Quality flags, never deletes.** Every issue names its trades and explains
  itself, and the rows stay in the book.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atr.research.learning_readiness import (
    ANALYSIS_READY_N,
    blocking_gaps,
    data_quality_issues,
    dedupe_forward,
    evidence_status,
    forward_closed,
    next_gate,
    research_state,
    summarise_forward,
    weekly_progression,
)

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def _row(**over: object) -> dict:
    base: dict = {
        "source": "PAPER",
        "evidence_class": "PAPER_FORWARD",
        "evidence_grade": "forward",
        "evidence_note": "the opening order carries an execution-time provenance stamp",
        "is_forward": True,
        "source_ref": "dep-1",
        "trade_ref": "t-1",
        "strategy_id": "strat-1",
        "strategy_version": 1,
        "symbol": "RELIANCE",
        "sector": "Energy",
        "direction": "LONG",
        "entry_ts": NOW - timedelta(days=40),
        "exit_ts": NOW - timedelta(days=39),
        "quantity": 10,
        "entry_price": 100.0,
        "exit_price": 102.0,
        "net_pnl": 20.0,
        "return_pct": 2.0,
        "signal_id": "RELIANCE:2026-08-08",
        "opening_order_id": "ord-1",
        "context_score": 85,
        "context_model_version": "v1.0.0",
        "context_class": "STRONG_CONTEXT",
        "market_regime": "BULLISH_TREND",
        "benchmark_symbol": "NIFTY50",
        "close_at_entry": 100.0,
        "bars_available": 200,
        "commission": 1.0,
        "missing_features": [],
    }
    base.update(over)
    return base


def _forward_set(n: int, **over: object) -> list[dict]:
    return [
        _row(
            trade_ref=f"t-{i}",
            signal_id=f"SYM:{i}",
            opening_order_id=f"ord-{i}",
            exit_ts=NOW - timedelta(days=i),
            entry_ts=NOW - timedelta(days=i, hours=1),
        ) | dict(over)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_evidence_status_and_next_gate_follow_the_three_floors():
    assert ANALYSIS_READY_N == 50
    assert evidence_status(0) == "INSUFFICIENT"
    assert evidence_status(6) == "INSUFFICIENT"
    assert evidence_status(9) == "INSUFFICIENT"
    assert evidence_status(10) == "SMALL_SAMPLE"
    assert evidence_status(24) == "SMALL_SAMPLE"
    assert evidence_status(49) == "SMALL_SAMPLE"
    assert evidence_status(50) == "ANALYSIS_READY"
    assert evidence_status(52) == "ANALYSIS_READY"

    assert next_gate(6) == 10
    assert next_gate(10) == 30
    assert next_gate(24) == 30
    assert next_gate(30) == 50
    assert next_gate(52) is None


def test_research_state_ladder_with_reasons():
    state, reasons = research_state(6)
    assert state == "NOT READY"
    assert any("6" in r and "10" in r for r in reasons)

    state, reasons = research_state(24)
    assert state == "MINIMUM SAMPLE"
    assert any("50" in r for r in reasons)

    state, reasons = research_state(52)
    assert state == "ANALYSIS READY"
    assert any("no recorded forward observation" in r for r in reasons)

    state, reasons = research_state(52, recorded_forward_observations=2)
    assert state == "OPTIMIZATION ELIGIBLE"
    assert any("2 recorded" in r for r in reasons)


def test_a_gap_holds_the_state_back_whatever_the_count():
    gaps = blocking_gaps(n_with_metric=0, n_with_context=0)
    assert len(gaps) == 2
    state, reasons = research_state(52, recorded_forward_observations=3, blocking_gaps=gaps)
    assert state == "MINIMUM SAMPLE"
    assert any("held here" in r for r in reasons)

    assert blocking_gaps(n_with_metric=5, n_with_context=5) == []
    gaps = blocking_gaps(n_with_metric=5, n_with_context=5, n_inconsistent_provenance=1)
    assert len(gaps) == 1 and "provenance" in gaps[0]


# ---------------------------------------------------------------------------
# selection and dedup
# ---------------------------------------------------------------------------


def test_only_closed_forward_rows_enter_a_summary():
    rows = [
        _row(trade_ref="fwd-closed"),
        _row(trade_ref="fwd-open", exit_ts=None, return_pct=None, net_pnl=None),
        _row(
            trade_ref="backtest",
            source="BACKTEST",
            evidence_class="BACKTEST",
            evidence_grade="in_sample",
            is_forward=False,
        ),
    ]
    selected = forward_closed(rows)
    assert [r["trade_ref"] for r in selected] == ["fwd-closed"]


def test_duplicates_are_counted_once_and_reported():
    rows = [_row(trade_ref="t-1"), _row(trade_ref="t-1"), _row(trade_ref="t-2")]
    unique, dropped = dedupe_forward(rows)
    assert [r["trade_ref"] for r in unique] == ["t-1", "t-2"]
    assert dropped == ["t-1"]


def test_unkeyed_rows_are_kept_not_dropped_on_suspicion():
    rows = [_row(trade_ref=None), _row(trade_ref=None)]
    unique, dropped = dedupe_forward(rows)
    assert len(unique) == 2 and dropped == []


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


def test_summary_reports_figures_and_distributions():
    rows = _forward_set(4)
    summary = summarise_forward(rows, metric="return_pct", now=NOW)
    assert summary["n"] == 4
    assert summary["n_with_metric"] == 4
    assert summary["stats"]["mean"] == 2.0
    assert summary["stats"]["median"] == 2.0
    assert summary["max_drawdown"] == 0.0
    assert summary["score_coverage"] == {"with_score": 4, "scanned": 4}
    bands = {b["band"]: b["forward_trades"] for b in summary["score_bands"]}
    assert bands == {"80-100": 4, "60-79": 0, "40-59": 0, "0-39": 0}
    assert summary["regime_distribution"] == {"BULLISH_TREND": 4}
    assert summary["sector_distribution"] == {"Energy": 4}


def test_missing_values_contribute_nothing_and_are_counted():
    rows = [
        _row(trade_ref="a", return_pct=4.0, net_pnl=40.0),
        _row(trade_ref="b", return_pct=None, net_pnl=None, context_score=None),
    ]
    summary = summarise_forward(rows, metric="return_pct", now=NOW)
    assert summary["n"] == 2
    assert summary["n_with_metric"] == 1
    # The absent return is not averaged in as zero.
    assert summary["stats"]["mean"] == 4.0
    assert summary["score_coverage"] == {"with_score": 1, "scanned": 2}


def test_recent_vs_historical_splits_on_thirty_days_descriptively():
    rows = [
        _row(trade_ref="recent", return_pct=6.0, exit_ts=NOW - timedelta(days=2)),
        _row(trade_ref="old", return_pct=2.0, exit_ts=NOW - timedelta(days=60)),
    ]
    split = summarise_forward(rows, now=NOW)["recent_vs_historical"]
    assert split["window_days"] == 30
    assert (split["recent_n"], split["recent_mean"]) == (1, 6.0)
    assert (split["historical_n"], split["historical_mean"]) == (1, 2.0)


def test_weekly_progression_accumulates_by_exit_week():
    monday = NOW - timedelta(days=NOW.weekday())
    rows = [
        _row(trade_ref="a", exit_ts=monday - timedelta(weeks=2)),
        _row(trade_ref="b", exit_ts=monday - timedelta(weeks=2)),
        _row(trade_ref="c", exit_ts=monday),
    ]
    weeks = weekly_progression(rows)
    assert [w["trades"] for w in weeks] == [2, 1]
    assert [w["cumulative"] for w in weeks] == [2, 3]
    assert weeks[-1]["week"] == monday.date().isoformat()


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------


def test_a_clean_book_raises_no_issue():
    assert data_quality_issues(_forward_set(3), now=NOW) == []


def test_each_gap_is_named_with_its_trades_and_an_explanation():
    rows = [
        _row(trade_ref="no-context", context_score=None),
        _row(trade_ref="no-sector", sector=None),
        _row(trade_ref="no-bench", benchmark_symbol=None),
        _row(
            trade_ref="no-exec",
            signal_id=None,
            opening_order_id=None,
        ),
        _row(trade_ref="stale-price", close_at_entry=None, bars_available=120),
        _row(trade_ref="inverted", entry_ts=NOW, exit_ts=NOW - timedelta(days=1)),
        _row(trade_ref="bad-price", entry_price=0.0),
        _row(trade_ref="bad-qty", quantity=-5),
        _row(trade_ref="moon", return_pct=250.0),
        _row(trade_ref="neg-cost", commission=-2.0),
        _row(trade_ref="no-outcome", return_pct=None),
        _row(
            trade_ref="liar",
            is_forward=False,
            evidence_grade="forward",
        ),
    ]
    by_code = {i["code"]: i for i in data_quality_issues(rows, now=NOW)}
    for code in (
        "missing_context",
        "missing_sector",
        "missing_benchmark",
        "missing_execution",
        "stale_price_lookup",
        "exit_before_entry",
        "abnormal_price",
        "abnormal_quantity",
        "abnormal_return",
        "negative_commission",
        "missing_outcome",
        "inconsistent_provenance",
    ):
        assert code in by_code, f"{code} was not flagged"
        assert by_code[code]["count"] >= 1
        assert by_code[code]["sample_refs"], f"{code} names no trade"
        assert by_code[code]["explanation"], f"{code} explains nothing"
    assert by_code["inconsistent_provenance"]["severity"] == "blocking"
    assert by_code["missing_context"]["sample_refs"] == ["no-context"]


def test_duplicate_refs_are_reported_alongside_the_other_issues():
    issues = data_quality_issues(_forward_set(2), now=NOW, duplicate_refs=["t-9", "t-9"])
    by_code = {i["code"]: i for i in issues}
    assert by_code["duplicate_trade_ref"]["count"] == 2
    assert by_code["duplicate_trade_ref"]["sample_refs"] == ["t-9", "t-9"]
