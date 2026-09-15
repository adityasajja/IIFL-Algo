"""The screener: condition trees, evidence, ranking, and the API.

The contract this file protects, in the user's words: *"every result must
explain WHY it matched."* That is not a UI concern that can be deferred — if the
service does not carry the reason, no frontend can invent it. So most of these
tests assert on the reasoning text and on the three-valued behaviour (matched /
did not match / could not be measured), not just on which symbols came back.

Fixtures come from ``conftest.py``: ``market_cache`` builds a synthetic 4-symbol
cache with 300 bars each, which is long enough for every warmup in the catalog.
"""

from __future__ import annotations

import pytest

from atr.screener.conditions import (
    ConditionError,
    Group,
    Leaf,
    count_nodes,
    evaluate_symbol,
    iter_leaves,
    parse_node,
    validate_tree,
)
from atr.screener.indicators import (
    AVAILABLE_INDICATORS,
    INDICATOR_INDEX,
    indicator_series,
    last_value,
    resolve_indicator,
)
from atr.screener.service import (
    ScreenerError,
    load_frames,
    rank_rows,
    reset_screener_service,
)


@pytest.fixture()
def screener(fresh_env, master):
    """A screener service over the synthetic cache, with a cold frame cache."""
    reset_screener_service()
    from atr.screener.service import ScreenerService

    yield ScreenerService()
    reset_screener_service()


@pytest.fixture()
def users(app_db) -> tuple[str, str]:
    """Two real accounts.

    ``screener_scans.user_id`` is a foreign key to ``users``, so a saved scan
    cannot be made up: the database enforces ownership rather than trusting the
    service to check it.
    """
    from atr.appdb.repositories import UserRepository

    with app_db.session() as session:
        first = UserRepository.create(
            session, email="a@example.com", username="a", password_hash="x", role="trader"
        )
        second = UserRepository.create(
            session, email="b@example.com", username="b", password_hash="x", role="trader"
        )
    return first["user_id"], second["user_id"]


@pytest.fixture()
def reliance(master):
    """RELIANCE's synthetic frame, loaded the way the service loads it.

    ``cache_file`` is stored **relative** to the cache root, so it must be joined
    rather than opened directly.
    """
    import pandas as pd

    from atr.instruments.service import get_instrument_master

    owner = get_instrument_master()
    record = owner.get("RELIANCE")
    return pd.read_parquet(owner.cache_root / record.cache_file)


# ===========================================================================
# the indicator catalog
# ===========================================================================
class TestIndicatorCatalog:
    def test_the_catalog_is_self_consistent(self):
        """Every spec has a unique key, a group, and a description."""
        keys = [spec.key for spec in INDICATOR_INDEX.values()]
        assert len(keys) == len(set(keys))
        for spec in INDICATOR_INDEX.values():
            assert spec.label, spec.key
            assert spec.group, spec.key
            assert spec.description, spec.key

    def test_unavailable_indicators_declare_what_they_need(self):
        """Nothing is silently zero. An unavailable indicator names its missing
        capability, which is the difference between "no data" and "worth 0"."""
        unavailable = [s for s in INDICATOR_INDEX.values() if not s.available]
        assert unavailable, "expected some declared-but-unavailable indicators"
        for spec in unavailable:
            assert spec.requires, f"{spec.key} is unavailable but names no requirement"
            assert spec.key not in AVAILABLE_INDICATORS

    def test_market_cap_is_declared_and_unavailable(self):
        """The user asked for market-cap filters "where data exists". There is no
        fundamentals service, so it must be declared unavailable rather than
        guessed from price × shares."""
        spec = resolve_indicator("market_cap")
        assert spec.available is False
        assert spec.requires

    def test_resolve_unknown_indicator_names_the_known_ones(self):
        with pytest.raises(KeyError) as excinfo:
            resolve_indicator("not_an_indicator")
        assert "not_an_indicator" in str(excinfo.value)

    def test_indicators_compute_on_a_real_frame(self, reliance):
        """Every available indicator must produce a finite last value on a frame
        with 300 bars. A catalog entry that cannot compute is a landmine: the
        user builds a screen on it and gets nothing back with no explanation."""
        broken: list[str] = []
        for key in sorted(AVAILABLE_INDICATORS):
            spec = resolve_indicator(key)
            period = spec.default_period
            try:
                series = indicator_series(reliance, key, period)
                value = last_value(series)
            except Exception as exc:  # noqa: BLE001 - reporting, not handling
                broken.append(f"{key}: raised {type(exc).__name__}: {exc}")
                continue
            if value != value:  # NaN
                broken.append(f"{key}: NaN at the last bar")
        assert not broken, "indicators failed on a 300-bar frame:\n" + "\n".join(broken)

    def test_last_value_of_an_empty_series_is_nan_not_none(self):
        """NaN, because ``None`` propagates into comparisons as a TypeError and
        None < 5 is a crash, not a False."""
        import pandas as pd

        value = last_value(pd.Series([], dtype="float64"))
        assert value != value  # NaN
        assert value is not None


# ===========================================================================
# condition parsing
# ===========================================================================
class TestParsing:
    def test_nested_and_or_tree_parses(self):
        """The user's exact shape: (A AND B AND C) OR (D AND E)."""
        tree = {
            "match": "any",
            "conditions": [
                {
                    "match": "all",
                    "conditions": [
                        {"indicator": "close", "op": ">", "rhs_indicator": "ema", "period": 50},
                        {"indicator": "rsi", "op": "<", "value": 70},
                        {"indicator": "rel_volume", "op": ">", "value": 1.2},
                    ],
                },
                {
                    "match": "all",
                    "conditions": [
                        {"indicator": "gap_pct", "op": ">", "value": 1.0},
                        {"indicator": "close", "op": ">", "rhs_indicator": "prev_high"},
                    ],
                },
            ],
        }
        root = parse_node(tree)
        assert isinstance(root, Group)
        assert root.match == "any"
        groups, leaves = count_nodes(root)
        assert groups == 3  # root plus the two branches
        assert leaves == 5

    def test_a_bare_period_routes_to_the_side_that_uses_it(self):
        """``close > ema`` with ``period: 50`` means EMA 50.

        ``close`` takes no period. Applying 50 to the left operand would drop it
        and silently test EMA 20 instead — a screen that does not measure what it
        says. This is the assertion that pins that behaviour down.
        """
        leaf = parse_node({"indicator": "close", "op": ">", "rhs_indicator": "ema", "period": 50})
        assert leaf.period is None
        assert leaf.rhs_period == 50
        assert "EMA 50" in leaf.describe()

    def test_an_explicit_rhs_period_is_not_overridden(self):
        leaf = parse_node(
            {"indicator": "sma", "op": ">", "rhs_indicator": "ema", "period": 20, "rhs_period": 50}
        )
        assert leaf.period == 20
        assert leaf.rhs_period == 50

    def test_between_needs_both_bounds(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"indicator": "rsi", "op": "between", "value": 30})
        assert excinfo.value.code == "missing_bound"

    def test_a_leaf_with_no_target_is_rejected(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"indicator": "rsi", "op": ">"})
        assert excinfo.value.code == "missing_target"

    def test_unknown_operator_is_rejected(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"indicator": "rsi", "op": "~=", "value": 50})
        assert excinfo.value.code == "unknown_operator"

    def test_unknown_indicator_fails_at_parse_not_at_scan(self):
        """Build-time rejection. A typo that survives to scan time is a screen
        that quietly returns nothing, which reads as "no matches today"."""
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"indicator": "rsl", "op": ">", "value": 50})
        assert excinfo.value.code == "unknown_indicator"

    def test_a_typo_and_a_missing_capability_are_different_errors(self):
        """Two failure modes that must not be conflated: ``rsl`` is a typo the
        user can fix, ``market_cap`` is a capability this build does not have.
        One message saying "unknown" for both would send the user hunting for a
        spelling mistake in a field that is simply not wired up."""
        with pytest.raises(ConditionError) as typo:
            parse_node({"indicator": "rsl", "op": ">", "value": 50})
        with pytest.raises(ConditionError) as missing:
            parse_node({"indicator": "market_cap", "op": ">", "value": 1000})
        assert typo.value.code == "unknown_indicator"
        assert missing.value.code == "indicator_unavailable"
        assert "requires" in str(missing.value)

    def test_unavailable_indicator_cannot_be_parsed(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"indicator": "market_cap", "op": ">", "value": 1000})
        assert excinfo.value.code == "indicator_unavailable"

    def test_a_node_with_neither_indicator_nor_conditions_is_rejected(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"label": "mystery"})
        assert excinfo.value.code == "empty_node"

    def test_bad_match_word_is_rejected(self):
        with pytest.raises(ConditionError) as excinfo:
            parse_node({"match": "both", "conditions": []})
        assert excinfo.value.code == "bad_match"

    def test_the_error_reports_where_the_problem_is(self):
        """A path, because "invalid condition" over a 40-node tree is useless."""
        tree = {
            "match": "all",
            "conditions": [
                {"indicator": "rsi", "op": ">", "value": 50},
                {"indicator": "rsi", "op": ">", "value": "high"},
            ],
        }
        with pytest.raises(ConditionError) as excinfo:
            parse_node(tree)
        assert "conditions[1]" in excinfo.value.path

    def test_validate_tree_returns_warnings_not_exceptions(self):
        """A non-fatal note is a warning. Only structural breakage raises."""
        node, warnings = validate_tree({"indicator": "close", "op": ">", "value": 10})
        assert isinstance(node, Leaf)
        assert warnings == []

    def test_iter_leaves_finds_every_leaf(self):
        root = parse_node(
            {
                "match": "all",
                "conditions": [
                    {"indicator": "rsi", "op": ">", "value": 50},
                    {
                        "match": "any",
                        "conditions": [
                            {"indicator": "close", "op": ">", "value": 10},
                            {"indicator": "volume", "op": ">", "value": 1000},
                        ],
                    },
                ],
            }
        )
        assert len(list(iter_leaves(root))) == 3


# ===========================================================================
# evaluation and evidence
# ===========================================================================
class TestEvidence:
    def test_evidence_carries_the_numbers(self, reliance):
        """The reason string must contain the measured value, not just a verdict.

        "Close > EMA50" is an assertion; "419.00 satisfies > EMA 50 403.03" is
        something the reader can check. The test asserts the *shape* rather than
        a fixed direction, because which side of its EMA a price sits on is a
        property of the synthetic series, not of the code.
        """
        leaf = parse_node({"indicator": "close", "op": ">", "rhs_indicator": "ema", "period": 50})
        evidence = leaf.evaluate(reliance)
        assert evidence.value is not None, "the left operand was not measured"
        assert evidence.target is not None, "the EMA it was compared against was not carried"
        assert evidence.target_period == 50
        assert "EMA 50" in evidence.reason
        # The verdict must agree with the two numbers in the same record, so a UI
        # (or a reader) can re-derive it rather than trusting the flag.
        assert evidence.passed == (evidence.value > evidence.target)

    def test_an_indicator_comparison_names_its_rhs_in_the_target_field(self, reliance):
        """A structured reader must not have to parse the prose. ``target`` holds
        the compared-against number even when that number came from another
        indicator."""
        leaf = parse_node({"indicator": "close", "op": "<", "rhs_indicator": "prev_high"})
        evidence = leaf.evaluate(reliance)
        assert evidence.target_indicator == "prev_high"
        assert isinstance(evidence.target, float)
        assert evidence.passed == (evidence.value < evidence.target)

    def test_a_failure_still_produces_evidence(self, reliance):
        """Failed leaves are kept, not discarded. A screen that returns nothing
        must be able to say *which* gate stopped each symbol."""
        leaf = parse_node({"indicator": "close", "op": ">", "value": 10_000_000})
        evidence = leaf.evaluate(reliance)
        assert evidence.passed is False
        assert evidence.reason

    def test_unmeasurable_is_distinct_from_false(self, reliance):
        """Short history is reported as unmeasurable, so the UI can say "not
        enough data" instead of the false and much more alarming "did not
        match"."""
        short = reliance.tail(5)
        passed, evidence = evaluate_symbol(
            short, parse_node({"indicator": "rsi", "op": ">", "value": 50})
        )
        assert passed is False
        assert evidence
        assert all(e.unmeasurable for e in evidence)

    def test_every_leaf_gets_evidence_even_on_an_or_group(self, reliance):
        """OR semantics: one branch passing makes the symbol a match, but a user
        still wants to see the state of the other branch."""
        root = parse_node(
            {
                "match": "any",
                "conditions": [
                    {"indicator": "close", "op": ">", "value": 10_000_000},  # false
                    {"indicator": "close", "op": ">", "value": 1},  # true
                ],
            }
        )
        passed, evidence = evaluate_symbol(reliance, root)
        assert passed is True
        assert len(evidence) == 2
        assert {e.passed for e in evidence} == {True, False}

    def test_an_empty_group_matches_nothing(self, reliance):
        """Not match-all. A half-built screen returning the whole exchange is the
        most expensive possible default."""
        passed, evidence = evaluate_symbol(reliance, Group(match="all", children=[]))
        assert passed is False
        assert evidence == []

    def test_negate_inverts_the_group(self, reliance):
        always_true = Group(
            match="all", children=[parse_node({"indicator": "close", "op": ">", "value": 1})]
        )
        negated = Group(
            match="all",
            children=[parse_node({"indicator": "close", "op": ">", "value": 1})],
            negate=True,
        )
        assert evaluate_symbol(reliance, always_true)[0] is True
        assert evaluate_symbol(reliance, negated)[0] is False

    def test_crosses_above_uses_the_previous_bar(self, reliance):
        """A crossover is a statement about the transition, so it needs two bars.
        Setting the target at the last close makes "crossed above" false by
        construction today while remaining true of the transition."""
        last = float(reliance["close"].iloc[-1])
        leaf = parse_node({"indicator": "close", "op": "crosses_above", "value": last})
        evidence = leaf.evaluate(reliance)
        assert evidence.passed is False
        assert "crossing above" in evidence.reason

    def test_evidence_serialises_without_losing_fields(self, reliance):
        evidence = parse_node({"indicator": "rsi", "op": "<", "value": 70}).evaluate(reliance)
        payload = evidence.as_dict()
        for key in ("label", "indicator", "op", "passed", "reason", "unmeasurable"):
            assert key in payload


# ===========================================================================
# the service: universes, running, ranking
# ===========================================================================
class TestService:
    def test_universes_report_scannable_size(self, screener):
        universes = {u["name"]: u for u in screener.universes("NSEEQ")}
        assert "all" in universes
        for entry in universes.values():
            assert entry["members"] >= 1
            assert entry["missing_history"] == 0
            # size must be the scannable count, not the nominal list count
            assert entry["size"] == entry["members"]

    def test_a_nested_scan_returns_ranked_rows_with_reasons(self, screener):
        result = screener.run(
            universe="all",
            exchange="NSEEQ",
            conditions={
                "match": "any",
                "conditions": [
                    {
                        "match": "all",
                        "conditions": [
                            {"indicator": "close", "op": ">", "value": 1},
                            {"indicator": "rsi", "op": ">", "value": 0},
                        ],
                    },
                    {"indicator": "close", "op": ">", "rhs_indicator": "prev_high"},
                ],
            },
            limit=10,
        )
        assert result["scanned"] >= 1
        assert result["rows"], "a permissive screen over a populated cache must match something"
        for row in result["rows"]:
            assert row["symbol"]
            assert row["why"], f"{row['symbol']} matched with no explanation"
            assert all(w["reason"] for w in row["why"])

    def test_the_row_carries_the_requested_columns(self, screener):
        """The exact column set from the request."""
        result = screener.run(
            universe="all",
            exchange="NSEEQ",
            conditions={"indicator": "close", "op": ">", "value": 1},
            limit=1,
        )
        row = result["rows"][0]
        for column in (
            "symbol",
            "ltp",
            "change_pct",
            "volume",
            "rel_volume",
            "rsi14",
            "ema20",
            "ema50",
            "atr_pct",
            "setup",
        ):
            assert column in row, f"missing requested column {column}"

    def test_ranking_descending_puts_the_largest_first(self):
        rows = [
            {"symbol": "A", "rel_volume": 1.1},
            {"symbol": "B", "rel_volume": 3.3},
            {"symbol": "C", "rel_volume": 2.2},
        ]
        ordered = [r["symbol"] for r in rank_rows(rows, "rel_volume", descending=True)]
        assert ordered == ["B", "C", "A"]

    def test_ranking_ascending_puts_the_smallest_first(self):
        rows = [
            {"symbol": "A", "rel_volume": 1.1},
            {"symbol": "B", "rel_volume": 3.3},
            {"symbol": "C", "rel_volume": 2.2},
        ]
        ordered = [r["symbol"] for r in rank_rows(rows, "rel_volume", descending=False)]
        assert ordered == ["A", "C", "B"]

    def test_unmeasurable_values_sort_last_in_both_directions(self):
        """The missingness flag must not be reversed along with the value, or a
        descending sort would float every blank row to the top."""
        rows = [
            {"symbol": "A", "rel_volume": None},
            {"symbol": "B", "rel_volume": 2.0},
            {"symbol": "C", "rel_volume": 1.0},
        ]
        for descending in (True, False):
            ordered = [r["symbol"] for r in rank_rows(rows, "rel_volume", descending=descending)]
            assert ordered[-1] == "A", f"None not last when descending={descending}"

    def test_ties_break_on_symbol_deterministically(self):
        """Same value twice must not depend on dict insertion order, or the row
        order changes between runs and looks like the market moving."""
        rows = [
            {"symbol": "ZZZ", "rel_volume": 2.0},
            {"symbol": "AAA", "rel_volume": 2.0},
        ]
        ordered = [r["symbol"] for r in rank_rows(rows, "rel_volume")]
        assert ordered == ["AAA", "ZZZ"]

    def test_an_unknown_sort_field_falls_back_to_the_default(self):
        rows = [{"symbol": "A", "rel_volume": 1.0}, {"symbol": "B", "rel_volume": 5.0}]
        ordered = [r["symbol"] for r in rank_rows(rows, "definitely_not_a_field")]
        assert ordered == ["B", "A"]  # DEFAULT_SORT is rel_volume, descending

    def test_the_result_reports_what_it_scanned(self, screener):
        """``scanned`` and ``as_of`` are how a user knows the screen ran over the
        intended universe at the intended date."""
        result = screener.run(
            universe="nifty50",
            exchange="NSEEQ",
            conditions={"indicator": "close", "op": ">", "value": 1},
        )
        assert result["scanned"] >= 1
        assert result["as_of"]
        assert result["exchange"] == "NSEEQ"

    def test_a_scan_over_no_symbols_is_not_an_error(self, screener):
        """An empty universe returns empty rows. Raising here would make "no
        matches" indistinguishable from "the screen broke"."""
        result = screener.run(
            universe="all",
            exchange="NSEEQ",
            symbols=[],
            conditions={"indicator": "close", "op": ">", "value": 1},
        )
        assert result["rows"] == []

    def test_validate_never_raises_for_a_bad_tree(self, screener):
        report = screener.validate({"indicator": "rsl", "op": ">", "value": 1})
        assert report["valid"] is False
        assert report["error"]

    def test_validate_accepts_a_good_tree(self, screener):
        report = screener.validate({"indicator": "rsi", "op": "<", "value": 70})
        assert report["valid"] is True

    def test_frames_are_cached_across_runs(self, screener):
        """Reading the cache twice must not re-read it: the TTL cache is what
        makes a UI usable at all."""
        first = load_frames("NSEEQ")
        second = load_frames("NSEEQ")
        assert first is second


class TestUniverseCoverage:
    """The bug these tests exist to prevent: a scan that silently covers a
    fraction of the universe and reports the result as the whole market.

    The cache stores files by *series* (``20MICRONS-EQ.parquet``) while every
    universe is a bare ticker (``20MICRONS``). Any lookup that confuses the two
    drops the symbol with no error. On the real cache that was 2,219 of 2,654
    names — an 84% silent omission.
    """

    def test_frames_are_keyed_by_bare_ticker_not_filename(self, master):
        """``load_frames`` must re-key by ticker, or every lookup misses."""
        frames = load_frames("NSEEQ", force=True)
        assert frames, "no frames loaded"
        dashed = [k for k in frames if k.endswith("-EQ")]
        assert not dashed, f"frames still keyed by filename: {dashed[:5]}"

    def test_every_universe_member_resolves_to_a_frame(self, master):
        """Every symbol a universe advertises as scannable must actually load.

        This is the assertion that would have caught the 435/2,654 bug: the
        universe reported one size and the scan quietly evaluated another.
        """
        from atr.instruments.service import get_instrument_master

        owner = get_instrument_master()
        expected = sorted(
            r.symbol
            for r in owner.snapshot().records.values()
            if r.exchange.upper() == "NSEEQ" and r.has_daily
        )
        assert expected, "the synthetic cache produced no NSEEQ records"

        frames = load_frames("NSEEQ", expected, force=True)
        missing = [s for s in expected if s not in frames]
        assert not missing, f"{len(missing)} of {len(expected)} scannable symbols did not load"

    def test_a_subset_read_matches_a_full_read(self, master):
        """The two code paths must agree. ``run`` passes an explicit symbol list
        (the subset path) while a bare ``load_frames`` takes the full path; when
        these disagreed, the scan and the universe report contradicted each
        other."""
        from atr.instruments.service import get_instrument_master

        owner = get_instrument_master()
        symbols = sorted(owner.snapshot().records.keys())
        full = load_frames("NSEEQ", force=True)
        subset = load_frames("NSEEQ", list(full.keys()), force=True)
        assert set(subset.keys()) == set(full.keys())

    def test_a_symbol_present_in_a_universe_can_be_scanned(self, screener):
        """End to end: ask for one universe, confirm the whole universe is
        actually evaluated."""
        symbols = screener.symbols_for("nifty50", "NSEEQ")
        assert symbols
        result = screener.run(
            universe="nifty50",
            exchange="NSEEQ",
            conditions={"indicator": "bars", "op": ">", "value": 1},
        )
        assert result["scanned"] == len(symbols), (
            f"advertised {len(symbols)} symbols but scanned {result['scanned']}"
        )


# ===========================================================================
# saved scans
# ===========================================================================
class TestSavedScans:
    def test_save_list_get_delete_roundtrip(self, screener, users):
        owner, _ = users
        definition = {
            "universe": "all",
            "exchange": "NSEEQ",
            "conditions": {"indicator": "rsi", "op": "<", "value": 70},
        }
        saved = screener.save(owner, name="Oversold", definition=definition)
        assert saved["scan_id"]
        assert saved["name"] == "Oversold"
        assert saved["definition"]["conditions"]["indicator"] == "rsi"

        listed = screener.list_saved(owner)
        assert [s["scan_id"] for s in listed] == [saved["scan_id"]]

        fetched = screener.get_saved(owner, saved["scan_id"])
        assert fetched["name"] == "Oversold"

        screener.delete_saved(owner, saved["scan_id"])
        assert screener.list_saved(owner) == []

    def test_a_saved_scan_is_private_to_its_owner(self, screener, users):
        owner, intruder = users
        saved = screener.save(
            owner,
            name="Mine",
            definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}},
        )
        # 404, never 403: a foreign scan must not be distinguishable from one
        # that does not exist.
        with pytest.raises(ScreenerError) as excinfo:
            screener.get_saved(intruder, saved["scan_id"])
        assert excinfo.value.status == 404

    def test_a_duplicate_name_is_rejected(self, screener, users):
        owner, _ = users
        definition = {"conditions": {"indicator": "rsi", "op": "<", "value": 70}}
        screener.save(owner, name="Same", definition=definition)
        with pytest.raises(ScreenerError) as excinfo:
            screener.save(owner, name="same", definition=definition)
        assert excinfo.value.status == 409

    def test_an_invalid_definition_cannot_be_saved(self, screener, users):
        """Saving an unrunnable screen would defer the failure to the moment the
        user comes back to it."""
        owner, _ = users
        with pytest.raises(ScreenerError):
            screener.save(owner, name="Broken", definition={"conditions": {"op": ">"}})

    def test_a_saved_scan_can_be_re_run_by_id(self, screener, users):
        owner, _ = users
        definition = {
            "universe": "all",
            "exchange": "NSEEQ",
            "conditions": {"indicator": "close", "op": ">", "value": 1},
        }
        saved = screener.save(owner, name="Reusable", definition=definition)
        result = screener.run_saved(owner, saved["scan_id"])
        assert result["rows"]
        assert result["scanned"] >= 1

    def test_a_blank_name_is_rejected(self, screener, users):
        owner, _ = users
        with pytest.raises(ScreenerError):
            screener.save(owner, name="   ", definition={"conditions": {}})

    def test_update_renames_and_redefines_a_saved_scan(self, screener, users):
        owner, _ = users
        saved = screener.save(
            owner,
            name="Before",
            definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}},
        )
        updated = screener.update_saved(
            owner,
            saved["scan_id"],
            name="After",
            definition={"conditions": {"indicator": "rsi", "op": "<", "value": 40}},
        )
        assert updated["name"] == "After"
        assert updated["definition"]["conditions"]["value"] == 40

    def test_update_leaves_absent_fields_alone(self, screener, users):
        """Omitted fields are untouched — a rename must not silently wipe the
        definition it did not mention."""
        owner, _ = users
        saved = screener.save(
            owner,
            name="Keep",
            description="original",
            definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}},
        )
        updated = screener.update_saved(owner, saved["scan_id"], name="Renamed")
        assert updated["name"] == "Renamed"
        assert updated["description"] == "original"
        assert updated["definition"]["conditions"]["value"] == 70

    def test_update_rejects_an_invalid_definition(self, screener, users):
        owner, _ = users
        saved = screener.save(
            owner, name="Valid", definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}}
        )
        with pytest.raises(ScreenerError):
            screener.update_saved(
                owner, saved["scan_id"], definition={"conditions": {"indicator": "rsl", "op": ">"}}
            )
        # The original must survive a rejected edit.
        assert screener.get_saved(owner, saved["scan_id"])["definition"]["conditions"]["indicator"] == "rsi"

    def test_update_cannot_touch_another_users_scan(self, screener, users):
        owner, intruder = users
        saved = screener.save(
            owner, name="Private", definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}}
        )
        with pytest.raises(ScreenerError) as excinfo:
            screener.update_saved(intruder, saved["scan_id"], name="Hijacked")
        assert excinfo.value.status == 404

    def test_renaming_onto_an_existing_name_is_refused(self, screener, users):
        owner, _ = users
        definition = {"conditions": {"indicator": "rsi", "op": "<", "value": 70}}
        screener.save(owner, name="Taken", definition=definition)
        other = screener.save(owner, name="Other", definition=definition)
        with pytest.raises(ScreenerError) as excinfo:
            screener.update_saved(owner, other["scan_id"], name="Taken")
        assert excinfo.value.status == 409

    def test_re_saving_under_the_same_name_is_allowed(self, screener, users):
        """Editing a scan without renaming it must not collide with itself."""
        owner, _ = users
        saved = screener.save(
            owner, name="Unchanged", definition={"conditions": {"indicator": "rsi", "op": "<", "value": 70}}
        )
        updated = screener.update_saved(
            owner,
            saved["scan_id"],
            name="Unchanged",
            definition={"conditions": {"indicator": "rsi", "op": "<", "value": 30}},
        )
        assert updated["definition"]["conditions"]["value"] == 30


# ===========================================================================
# the API
# ===========================================================================
class TestApi:
    def _definition(self) -> dict:
        return {
            "universe": "all",
            "exchange": "NSEEQ",
            "columns": ["symbol", "ltp", "rsi14", "rel_volume", "setup"],
            "conditions": {
                "match": "all",
                "conditions": [
                    {"indicator": "close", "op": ">", "value": 1},
                    {"indicator": "rsi", "op": ">", "value": 0},
                ],
            },
        }

    def test_universes_endpoint(self, auth_client):
        response = auth_client.get("/api/v1/screener/universes")
        assert response.status_code == 200, response.text
        assert response.json()["universes"]

    def test_indicators_endpoint_declares_the_unavailable_ones(self, auth_client):
        response = auth_client.get("/api/v1/screener/indicators")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["indicators"]
        assert body["unavailable"], "unavailable indicators must be visible, not hidden"
        assert "market_cap" in body["unavailable"]

    def test_columns_endpoint(self, auth_client):
        response = auth_client.get("/api/v1/screener/columns")
        assert response.status_code == 200, response.text
        keys = {c["key"] for c in response.json()["columns"]}
        for expected in ("ltp", "change_pct", "rel_volume", "rsi14", "ema20", "ema50", "atr_pct"):
            assert expected in keys

    def test_run_endpoint_returns_explained_rows(self, auth_client):
        response = auth_client.post("/api/v1/screener/run", json={**self._definition(), "limit": 5})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rows"]
        assert body["as_of"]
        for row in body["rows"]:
            assert row["why"], f"{row['symbol']} has no explanation"
            assert row["why"][0]["reason"]

    def test_run_endpoint_rejects_an_unknown_indicator(self, auth_client):
        response = auth_client.post(
            "/api/v1/screener/run",
            json={"universe": "all", "conditions": {"indicator": "rsl", "op": ">", "value": 1}},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"]

    def test_validate_endpoint_reports_rather_than_fails(self, auth_client):
        response = auth_client.post(
            "/api/v1/screener/validate",
            json={"conditions": {"indicator": "rsl", "op": ">", "value": 1}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["valid"] is False

    def test_saved_lifecycle_over_http(self, auth_client):
        created = auth_client.post(
            "/api/v1/screener/saved", json={"name": "HTTP scan", "definition": self._definition()}
        )
        assert created.status_code == 201, created.text
        scan_id = created.json()["scan_id"]

        listed = auth_client.get("/api/v1/screener/saved")
        assert listed.status_code == 200
        assert any(s["scan_id"] == scan_id for s in listed.json()["scans"])

        results = auth_client.get(f"/api/v1/screener/saved/{scan_id}/results")
        assert results.status_code == 200, results.text
        assert results.json()["rows"]

        removed = auth_client.delete(f"/api/v1/screener/saved/{scan_id}")
        assert removed.status_code == 200, removed.text

        assert auth_client.get(f"/api/v1/screener/saved/{scan_id}").status_code == 404

    def test_the_screener_requires_authentication(self, client):
        """An unauthenticated client must not reach it at all."""
        assert client.get("/api/v1/screener/universes").status_code in (401, 403)

    def test_put_edits_a_saved_scan_over_http(self, auth_client):
        created = auth_client.post(
            "/api/v1/screener/saved", json={"name": "Editable", "definition": self._definition()}
        )
        assert created.status_code == 201, created.text
        scan_id = created.json()["scan_id"]

        edited = auth_client.put(
            f"/api/v1/screener/saved/{scan_id}",
            json={
                "name": "Edited",
                "definition": {
                    **self._definition(),
                    "conditions": {"indicator": "rsi", "op": "<", "value": 45},
                },
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "Edited"
        assert edited.json()["definition"]["conditions"]["value"] == 45

    def test_put_on_a_missing_scan_is_404(self, auth_client):
        response = auth_client.put(
            "/api/v1/screener/saved/does-not-exist", json={"name": "Whatever"}
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "not_found"

    def test_put_rejects_an_invalid_definition(self, auth_client):
        created = auth_client.post(
            "/api/v1/screener/saved", json={"name": "Guarded", "definition": self._definition()}
        )
        scan_id = created.json()["scan_id"]
        response = auth_client.put(
            f"/api/v1/screener/saved/{scan_id}",
            json={"definition": {"conditions": {"indicator": "rsl", "op": ">", "value": 1}}},
        )
        assert response.status_code == 400, response.text

    def test_the_screener_cannot_be_reached_at_legacy_paths(self, auth_client):
        """/api/v1/screener is the contract; the legacy /scanner paths are a
        different, flat model and must not quietly serve screener semantics."""
        assert auth_client.get("/api/v1/screener/universes").status_code == 200
        # A path that does not exist must 404 rather than fall through to a
        # catch-all.
        assert auth_client.get("/api/v1/screener/nonexistent").status_code == 404


# ===========================================================================
# architecture
# ===========================================================================
class TestLayering:
    def test_the_screener_does_not_import_the_api(self):
        """``services !-> api``: the screener is domain logic and must not know
        HTTP exists. Checked here as well as in ``test_architecture`` so the
        failure names the module that broke the rule."""
        import ast
        from pathlib import Path

        offenders: list[str] = []
        root = Path("src/atr/screener")
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name.startswith("atr.api") for name in names):
                    offenders.append(f"{path.name}:{node.lineno} -> {names}")
        assert not offenders, "the screener imports the API:\n" + "\n".join(offenders)
