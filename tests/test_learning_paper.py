"""The paper ledger reader — the grading rule, and nothing invented.

The module under test turns two append-only JSONL files into dataset rows. Two
properties matter more than the rest, and both are asserted here rather than
described in a docstring:

**Provenance is never upgraded.** A week the producer labelled a backfill is
graded ``in_sample``; a week whose provenance is unknown is *also* graded
``in_sample``, because the failure mode of over-claiming independence is silent
and the failure mode of under-claiming it is merely conservative. A reader who
trusts the label must be able to trust it in the direction that matters.

**Nothing is invented.** The ledger records an equal-weight return and no
position size, so ``net_pnl`` is ``None`` and marked missing — not multiplied by
an assumed capital. A pick with no measured exit produces no row rather than a
0.00% one, because a flat week and an unmeasured week are different findings.

No database and no price cache is touched: the reader is pure file I/O, so these
tests are deterministic and run in milliseconds.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from atr.research import learning_paper as paper

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _picks_record(
    week: str,
    *,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
    backfilled: bool = False,
    expectation: float | None = 0.88,
) -> dict:
    """One picks record, in the shape ``track_momentum_paper.py`` writes."""
    record = {
        "week": week,
        "recorded_at": "2026-09-13T17:22:58",
        "filter": "top_decile_ret_26w",
        "n_picks": len(symbols),
        "picks": [
            {"symbol": symbol, "entry": 100.0 + index}
            for index, symbol in enumerate(symbols)
        ],
        "backtest_expectation": {
            "mean_weekly_pct": None if backfilled else expectation,
            "hit_rate_ge_5pct": None if backfilled else 0.2017,
            "source": (
                "n/a — backfilled week"
                if backfilled
                else "weekly_stock_picks.json filters.top_decile_ret_26w"
            ),
        },
    }
    if backfilled:
        # The marker the producer writes for a backfilled week, and the only
        # thing that distinguishes it from a forward one.
        record["source"] = "backfill (in-sample — harness validation only)"
    return record


def _settlement_record(
    week: str,
    *,
    returns: dict[str, float] | None = None,
    exit_window_end: str | None = None,
) -> dict:
    returns = returns if returns is not None else {"AAA": 0.05, "BBB": -0.02}
    # A deliberately dirty record must still be writable: the producer's own
    # aggregate fields are computed over whatever parsed, and the reader under
    # test is what has to cope with the junk.
    numeric = [float(value) for value in returns.values() if _is_number(value)]
    return {
        "week": week,
        "settled_at": "2026-09-13T17:22:58",
        "exit_window_end": exit_window_end or _plus_days(week, 7),
        "returns": returns,
        "n_settled": len(returns),
        "missing": [],
        "portfolio_return": round(sum(numeric) / len(numeric), 6) if numeric else None,
        "hits_ge_5pct": sum(1 for value in numeric if value >= 0.05),
        "hit_rate_ge_5pct": (
            round(sum(1 for value in numeric if value >= 0.05) / len(numeric), 4)
            if numeric
            else None
        ),
        "best": max(numeric) if numeric else None,
        "worst": min(numeric) if numeric else None,
    }


def _is_number(value: object) -> bool:
    try:
        float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


def _plus_days(week: str, days: int) -> str:
    from datetime import timedelta

    try:
        return str(date.fromisoformat(week) + timedelta(days=days))
    except ValueError:
        return "not-a-date"


def _write(root: Path, picks: list[dict], settlements: list[dict]) -> Path:
    directory = root / paper.LEDGER_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / paper.PICKS_FILE).write_text(
        "\n".join(json.dumps(record) for record in picks) + "\n", encoding="utf-8"
    )
    (directory / paper.SETTLEMENTS_FILE).write_text(
        "\n".join(json.dumps(record) for record in settlements) + "\n", encoding="utf-8"
    )
    return directory


@pytest.fixture()
def ledger_root(tmp_path: Path) -> Path:
    """One forward week and one backfilled week, both settled."""
    root = tmp_path / "data"
    _write(
        root,
        [
            _picks_record("2026-06-05", backfilled=True),
            _picks_record("2026-06-12"),
        ],
        [
            _settlement_record("2026-06-05"),
            _settlement_record("2026-06-12", returns={"AAA": 0.03, "BBB": 0.01}),
        ],
    )
    return root


# ---------------------------------------------------------------------------
# absence
# ---------------------------------------------------------------------------


def test_a_missing_ledger_is_absent_not_empty(tmp_path: Path):
    """No directory is a different statement from a directory with no week."""
    ledger = paper.load_ledger(tmp_path / "data")
    assert ledger.present is False
    assert ledger.weeks == []
    assert ledger.counts()["trades"] == 0
    assert "no paper ledger has been written yet" in ledger.summary()["note"]


def test_an_empty_ledger_is_present_with_no_weeks(tmp_path: Path):
    """Files that exist and hold nothing is 'a ledger with no settled week',
    which is a different statement from 'no ledger has been written'."""
    root = tmp_path / "data"
    directory = root / paper.LEDGER_DIR
    directory.mkdir(parents=True)
    (directory / paper.PICKS_FILE).write_text("", encoding="utf-8")
    (directory / paper.SETTLEMENTS_FILE).write_text("", encoding="utf-8")
    ledger = paper.load_ledger(root)
    assert ledger.present is True
    assert ledger.weeks == []
    assert "holds no settled week" in ledger.summary()["note"]


def test_a_default_root_of_none_reads_the_working_directory():
    """``None`` means the project's own ``data`` directory, not an error."""
    ledger = paper.load_ledger(None)
    assert ledger.present in (True, False)  # depends on the machine, not the logic
    assert isinstance(ledger.summary(), dict)


# ---------------------------------------------------------------------------
# the grading rule — the property the module exists for
# ---------------------------------------------------------------------------


def test_a_backfilled_week_is_graded_in_sample(ledger_root: Path):
    ledger = paper.load_ledger(ledger_root)
    by_week = {week.week: week for week in ledger.weeks}
    assert by_week["2026-06-05"].grade == paper.GRADE_IN_SAMPLE
    assert by_week["2026-06-05"].forward is False


def test_a_forward_week_is_graded_forward(ledger_root: Path):
    ledger = paper.load_ledger(ledger_root)
    by_week = {week.week: week for week in ledger.weeks}
    assert by_week["2026-06-12"].grade == paper.GRADE_FORWARD
    assert by_week["2026-06-12"].forward is True


def test_the_counts_split_forward_from_in_sample(ledger_root: Path):
    counts = paper.load_ledger(ledger_root).counts()
    assert counts["weeks"] == 2
    assert counts["forward_weeks"] == 1
    assert counts["in_sample_weeks"] == 1
    assert counts["forward_trades"] == 2
    assert counts["in_sample_trades"] == 2
    assert counts["trades"] == 4


def test_a_record_with_no_provenance_is_graded_in_sample(tmp_path: Path):
    """The conservative default. Ambiguity must not become a forward claim.

    This is the case a future edit is most likely to get wrong: it is tempting
    to treat "no backfill marker" as "forward", which would silently promote
    every unlabelled record into evidence.
    """
    root = tmp_path / "data"
    record = _picks_record("2026-06-05")
    record.pop("backtest_expectation")
    _write(root, [record], [_settlement_record("2026-06-05")])
    ledger = paper.load_ledger(root)
    assert ledger.weeks[0].grade == paper.GRADE_IN_SAMPLE
    assert "no provenance" in ledger.weeks[0].provenance


def test_an_agreement_between_the_two_markers_is_not_required_but_either_suffices(tmp_path: Path):
    """A record whose expectation names a backfill is graded in-sample even
    when its own ``source`` key is absent."""
    root = tmp_path / "data"
    record = _picks_record("2026-06-05")
    record["backtest_expectation"]["source"] = "n/a — backfilled week"
    _write(root, [record], [_settlement_record("2026-06-05")])
    ledger = paper.load_ledger(root)
    assert ledger.weeks[0].grade == paper.GRADE_IN_SAMPLE


def test_the_ledger_says_when_it_holds_no_forward_week(tmp_path: Path):
    """The finding a reader most needs, stated by the ledger itself."""
    root = tmp_path / "data"
    _write(
        root,
        [_picks_record("2026-06-05", backfilled=True)],
        [_settlement_record("2026-06-05")],
    )
    summary = paper.load_ledger(root).summary()
    assert summary["forward_trades"] == 0
    assert summary["in_sample_trades"] == 2
    assert "none of them is forward" in summary["note"]
    assert "no claim about whether the rule still works" in summary["note"]


# ---------------------------------------------------------------------------
# rows — what a trade looks like, and what is refused
# ---------------------------------------------------------------------------


def test_rows_carry_the_recorded_return_and_nothing_else(ledger_root: Path):
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    assert len(rows) == 4
    row = next(r for r in rows if r["trade_ref"].endswith("#AAA") and "06-12" in r["trade_ref"])
    assert row["source"] == "PAPER"
    assert row["strategy_key"] == paper.STRATEGY_KEY
    assert row["return_pct"] == pytest.approx(3.0)
    assert row["direction"] == "LONG"
    assert row["evidence_grade"] == paper.GRADE_FORWARD


def test_a_paper_row_has_no_rupee_pnl_and_says_why(ledger_root: Path):
    """The ledger records a return and no size. A rupee figure would be invented."""
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    for row in rows:
        assert row["net_pnl"] is None
        assert row["gross_pnl"] is None
        assert row["quantity"] is None
        assert row["commission"] is None
        assert "net_pnl" in row["_missing"]
        assert "quantity" in row["_missing"]
        assert "costs" in row["_missing"]


def test_the_exit_price_is_algebra_on_the_recorded_return(ledger_root: Path):
    """The ledger measured exit/entry - 1, so exit is recoverable exactly."""
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    for row in rows:
        expected = row["entry_price"] * (1.0 + row["return_pct"] / 100.0)
        assert row["exit_price"] == pytest.approx(expected)


def test_the_entry_and_exit_are_stamped_at_the_close(ledger_root: Path):
    """A weekly close placed on the clock, so time_of_day reads 'close' rather
    than the 'open' a midnight stamp would produce."""
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    for row in rows:
        assert row["entry_ts"].hour == paper.CLOSE_HOUR
        assert row["entry_ts"].minute == paper.CLOSE_MINUTE
        assert row["time_of_day"] == "close"


def test_the_day_of_week_is_filled(ledger_root: Path):
    """The builder's enrichment pass does not fill this, so the reader must.

    Left blank, the day-of-week axis reports a coverage of zero rather than an
    error — a whole requested axis silently disappearing.
    """
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    assert all(row["day_of_week"] for row in rows)
    assert all(row["day_of_week"] in {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
               for row in rows)


def test_the_hold_is_the_harness_window_not_a_market_decision(ledger_root: Path):
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    assert all(row["duration_days"] == 7 for row in rows)
    assert all(row["exit_reason"] == paper.EXIT_REASON for row in rows)


def test_no_setup_is_invented_for_a_cross_sectional_rank(ledger_root: Path):
    """None of the four ``signals.rules`` setups describes this rule, so the
    field is left unnamed rather than filed under 'unknown' — an 'unknown'
    bucket would be compared to the rest of the sample as a market condition."""
    rows = paper.build_rows(paper.load_ledger(ledger_root))
    assert all(row["setup"] is None for row in rows)
    assert all("setup" in row["_missing"] for row in rows)


def test_an_unsettled_pick_produces_no_row(tmp_path: Path):
    """A listed pick with no measured exit is unmeasured, not flat."""
    root = tmp_path / "data"
    _write(
        root,
        [_picks_record("2026-06-05", symbols=("AAA", "BBB", "CCC"))],
        [_settlement_record("2026-06-05", returns={"AAA": 0.05})],
    )
    ledger = paper.load_ledger(root)
    week = ledger.weeks[0]
    assert {pick.symbol for pick in week.picks} == {"AAA", "BBB", "CCC"}
    assert {pick.symbol for pick in week.unsettled_picks} == {"BBB", "CCC"}
    rows = paper.build_rows(ledger)
    assert [row["trade_ref"] for row in rows] == ["paper#2026-06-05#AAA"]


def test_a_pick_with_no_entry_price_produces_no_row(tmp_path: Path):
    """No entry price means no return and no entry bar to enrich against."""
    root = tmp_path / "data"
    record = _picks_record("2026-06-05")
    record["picks"][1].pop("entry")
    _write(root, [record], [_settlement_record("2026-06-05")])
    rows = paper.build_rows(paper.load_ledger(root))
    assert [row["trade_ref"] for row in rows] == ["paper#2026-06-05#AAA"]


def test_a_non_numeric_return_is_skipped_with_a_warning(tmp_path: Path):
    root = tmp_path / "data"
    _write(
        root,
        [_picks_record("2026-06-05")],
        [_settlement_record("2026-06-05", returns={"AAA": 0.05, "BBB": "n/a"})],
    )
    ledger = paper.load_ledger(root)
    assert ledger.warnings
    assert len(paper.build_rows(ledger)) == 1


# ---------------------------------------------------------------------------
# the ledger's own integrity
# ---------------------------------------------------------------------------


def test_a_settlement_without_a_picks_record_is_reported_not_guessed(tmp_path: Path):
    """Without an entry price there is nothing to build a row from."""
    root = tmp_path / "data"
    _write(root, [], [_settlement_record("2026-06-05")])
    ledger = paper.load_ledger(root)
    assert ledger.weeks == []
    assert any("no picks record" in warning for warning in ledger.warnings)


def test_a_duplicate_week_keeps_the_first_record(tmp_path: Path):
    """The ledger is append-only so it cannot be revised after the outcome.

    Honouring a later record would let a week be rewritten once its result was
    known, which is the one property the record exists to have.
    """
    root = tmp_path / "data"
    first = _picks_record("2026-06-05", symbols=("AAA",))
    second = _picks_record("2026-06-05", symbols=("ZZZ",))
    _write(root, [first, second], [_settlement_record("2026-06-05", returns={"AAA": 0.05})])
    ledger = paper.load_ledger(root)
    assert len(ledger.weeks) == 1
    assert {pick.symbol for pick in ledger.weeks[0].picks} == {"AAA"}
    assert any("more than once" in warning for warning in ledger.warnings)


def test_a_half_written_line_does_not_discard_the_history(tmp_path: Path):
    """A partial final line is normal for an append-only file being read while
    written. It is skipped — and reported, because a ledger quietly losing a
    week is a ledger whose counts cannot be trusted."""
    root = tmp_path / "data"
    directory = _write(
        root,
        [_picks_record("2026-06-05")],
        [_settlement_record("2026-06-05")],
    )
    with (directory / paper.PICKS_FILE).open("a", encoding="utf-8") as handle:
        handle.write('{"week": "2026-06-1')
    ledger = paper.load_ledger(root)
    assert len(ledger.weeks) == 1
    assert any("not valid JSON" in warning for warning in ledger.warnings)


def test_reading_the_ledger_does_not_modify_it(ledger_root: Path):
    """The record is append-only; a reader must not be able to revise it."""
    directory = ledger_root / paper.LEDGER_DIR
    before = {
        name: (directory / name).read_bytes()
        for name in (paper.PICKS_FILE, paper.SETTLEMENTS_FILE)
    }
    ledger = paper.load_ledger(ledger_root)
    paper.build_rows(ledger)
    paper.load_ledger(ledger_root).summary()
    after = {
        name: (directory / name).read_bytes()
        for name in (paper.PICKS_FILE, paper.SETTLEMENTS_FILE)
    }
    assert before == after


def test_every_paper_reason_names_the_gap_it_explains():
    reasons = paper.reasons()
    assert set(reasons) >= {"net_pnl", "quantity", "costs", "mfe", "mae", "setup"}
    for feature, why in reasons.items():
        assert isinstance(why, str) and len(why) > 20, feature
        assert not why.endswith("."), f"{feature} should read as a clause, not a sentence"


def test_the_strategy_key_is_stable_and_named_for_the_rule():
    assert paper.STRATEGY_KEY == "weekly_momentum_top10"


def test_the_ledger_module_imports_nothing_from_services():
    """``research`` may not import ``services`` — asserted structurally so a
    convenience import cannot quietly invert the layering."""
    import ast

    tree = ast.parse(Path(paper.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.startswith("atr.services") for name in imported), imported
    assert not any(name.startswith("atr.appdb") for name in imported), imported


def test_a_week_that_is_not_a_date_is_skipped(tmp_path: Path):
    root = tmp_path / "data"
    record = _picks_record("not-a-date")
    _write(root, [record], [_settlement_record("not-a-date")])
    ledger = paper.load_ledger(root)
    assert ledger.weeks == []
    assert any("not a usable date" in warning for warning in ledger.warnings)


def test_the_expected_return_is_read_from_the_picks_record(tmp_path: Path):
    """The expectation logged before the outcome — the point of the harness."""
    root = tmp_path / "data"
    _write(
        root,
        [_picks_record("2026-06-05", expectation=0.882)],
        [_settlement_record("2026-06-05")],
    )
    week = paper.load_ledger(root).weeks[0]
    assert week.expected_mean_weekly_pct == pytest.approx(0.882)


def test_a_forward_row_is_stamped_with_the_grade_the_ledger_declares(tmp_path: Path):
    """End to end on the one property that matters: the row carries the grade."""
    root = tmp_path / "data"
    _write(
        root,
        [_picks_record("2026-06-05", backfilled=True), _picks_record("2026-06-12")],
        [
            _settlement_record("2026-06-05"),
            _settlement_record("2026-06-12"),
        ],
    )
    rows = paper.build_rows(paper.load_ledger(root))
    grades = {
        row["trade_ref"].split("#")[1]: row["evidence_grade"] for row in rows
    }
    assert grades["2026-06-05"] == paper.GRADE_IN_SAMPLE
    assert grades["2026-06-12"] == paper.GRADE_FORWARD


def test_a_date_input_is_accepted_as_well_as_a_string():
    assert paper._parse_date("2026-06-05") == date(2026, 6, 5)
    assert paper._parse_date(date(2026, 6, 5)) == date(2026, 6, 5)
    assert paper._parse_date(datetime(2026, 6, 5, 15, 30)) == date(2026, 6, 5)
    assert paper._parse_date(None) is None
    assert paper._parse_date("nonsense") is None
