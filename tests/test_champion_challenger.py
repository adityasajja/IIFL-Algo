"""Champion vs challenger, at the pure layer.

Pinned here because each would be a silent-corruption bug if it drifted:

* **The thinner arm decides.** A comparison with one arm below the floor is
  INSUFFICIENT whatever the other holds; below fifty it is EARLY.
* **No winner.** Deltas are challenger-minus-champion descriptions with both
  sample sizes beside them. There is no significance test on the difference
  and no function that could promote.
* **The diff is exact.** Changed parameters render as dotted paths with both
  values; identical definitions report identical, not an empty table that
  could mean "could not compare".
* **The timeline never infers roles.** A version with no forward row reports
  zero, not absence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atr.research.champion_challenger import (
    compare_arms,
    comparison_verdict,
    definition_diff,
    version_timeline,
)

NOW = datetime(2026, 9, 17, tzinfo=UTC)


def _row(**over: object) -> dict:
    base: dict = {
        "evidence_class": "PAPER_FORWARD",
        "evidence_grade": "forward",
        "is_forward": True,
        "trade_ref": "t-1",
        "strategy_id": "strat-1",
        "strategy_version": 1,
        "symbol": "RELIANCE",
        "sector": "Energy",
        "direction": "LONG",
        "entry_ts": NOW - timedelta(days=5),
        "exit_ts": NOW - timedelta(days=4),
        "quantity": 10,
        "entry_price": 100.0,
        "exit_price": 102.0,
        "net_pnl": 20.0,
        "return_pct": 2.0,
        "context_score": 85,
        "market_regime": "BULLISH_TREND",
        "missing_features": [],
    }
    base.update(over)
    return base


def _arm(n: int, *, ret: float, start: int = 0) -> list[dict]:
    return [
        _row(
            trade_ref=f"t-{start + i}",
            return_pct=ret,
            net_pnl=ret * 10.0,
            exit_ts=NOW - timedelta(days=start + i),
            entry_ts=NOW - timedelta(days=start + i, hours=1),
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


def test_the_thinner_arm_decides_readiness():
    verdict, reasons = comparison_verdict(3, 200)
    assert verdict == "INSUFFICIENT EVIDENCE"
    assert any("3" in r for r in reasons)

    verdict, _ = comparison_verdict(47, 39)
    assert verdict == "EARLY EVIDENCE"

    verdict, reasons = comparison_verdict(52, 61)
    assert verdict == "COMPARISON READY"
    assert any("not promoting" in r for r in reasons)


def test_compare_arms_reports_descriptive_deltas_with_both_sizes():
    result = compare_arms(_arm(12, ret=1.0), _arm(12, ret=3.0), metric="return_pct")
    assert result["metric"] == "return_pct"
    assert result["champion"]["n"] == 12
    assert result["challenger"]["n"] == 12
    assert result["deltas"]["mean"] == 2.0
    assert result["verdict"] == "EARLY EVIDENCE"
    assert result["champion"]["evidence_status"] == "SMALL_SAMPLE"
    assert "p_value" not in result["deltas"], "no test on the difference, ever"


def test_in_sample_rows_must_be_excluded_by_the_caller_not_here():
    mixed = _arm(12, ret=2.0) + [
        _row(
            trade_ref="bt-1",
            evidence_class="BACKTEST",
            evidence_grade="in_sample",
            is_forward=False,
            return_pct=50.0,
        )
    ]
    result = compare_arms(mixed, _arm(12, ret=2.0), metric="return_pct")
    # The function counts what it is given: selection is the caller's duty,
    # and the service layer selects forward-closed-deduped rows only.
    assert result["champion"]["n"] == 13


# ---------------------------------------------------------------------------
# definition diff
# ---------------------------------------------------------------------------


def test_changed_parameters_render_as_dotted_paths_with_both_values():
    champion = {"rules": {"entry": {"volume_multiple": 1.5, "lookback": 20}}}
    challenger = {"rules": {"entry": {"volume_multiple": 1.9, "lookback": 20}}}
    diff = definition_diff(champion, challenger)
    assert diff["identical"] is False
    assert diff["changes"] == [
        {
            "parameter": "rules.entry.volume_multiple",
            "champion": 1.5,
            "challenger": 1.9,
        }
    ]


def test_identical_definitions_report_identical():
    definition = {"rules": {"entry": {"volume_multiple": 1.5}}}
    diff = definition_diff(definition, {"rules": {"entry": {"volume_multiple": 1.5}}})
    assert diff == {"changes": [], "identical": True}


def test_added_and_removed_keys_are_changes_not_silence():
    diff = definition_diff({"a": 1}, {"a": 1, "b": 2})
    assert diff["changes"] == [{"parameter": "b", "champion": None, "challenger": 2}]


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def test_timeline_assigns_roles_and_counts_without_inferring():
    timeline = version_timeline(
        [{"version": 1}, {"version": 2}, {"version": 3}],
        champion_version=1,
        challenger_version=2,
        forward_counts={1: 47, 2: 39},
    )
    assert [(t["version"], t["role"]) for t in timeline] == [
        (1, "CHAMPION"),
        (2, "CHALLENGER"),
        (3, None),
    ]
    assert [t["forward_observations"] for t in timeline] == [47, 39, 0]
