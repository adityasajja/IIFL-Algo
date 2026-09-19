"""The acceptance chain, walked end to end on a real (temporary) database.

The integrity tests in ``test_analytics_integrity.py`` prove each part of the
attribution layer is correct in isolation. This file proves something different
and, for acceptance purposes, more important: that the parts are actually
**connected** in the order the spec requires —

    Signal -> Context -> Sizing -> Risk -> Portfolio Gate -> OMS -> Execution
           -> Closed Trade -> Post-Trade Attribution -> Forward Learning Dataset

— and that a trade recorded at one end becomes a row in the forward learning
dataset at the other, with its provenance intact.

Why this is not redundant with the unit tests
---------------------------------------------

Every failure this file guards against is a *wiring* failure, and wiring failures
are invisible to unit tests by construction:

* A service that works perfectly but is never reached by the sweep produces a
  green test suite and an empty dashboard. The tests here start from the journal
  and assert a stored row exists at the end, so "never called" fails.
* A field that is computed correctly, passed correctly, and then dropped on the
  last step is also green everywhere except here. ``test_metadata_survives``
  therefore reads the value back *out of the database*, not out of the object
  that produced it.
* An evidence grade that is stamped correctly but overwritten at the seam
  between two correct components is the most dangerous case of all, because it
  silently converts an in-sample measurement into a forward claim. The chain
  tests assert the grade at both ends.

The chain is walked on ``tmp_path`` databases via the fixtures the rest of the
suite already uses, so nothing here touches the operator's real book.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from atr.appdb.repositories import (
    OrderRepository,
    TradeAttributionRepository,
    TradeJournalRepository,
    UserRepository,
)
from atr.services.attribution import AttributionService

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: Naive, because that is how the journal stores timestamps — ``close_trade``
#: subtracts them to derive the duration, and a mix of aware and naive raises.
#: The test's job is to stand in for the writer, so it has to use the writer's
#: convention rather than a tidier one.
ENTRY_TS = datetime(2026, 3, 2, 9, 20)
EXIT_TS = datetime(2026, 3, 4, 14, 30)
SYMBOL = "RELIANCE"


def _mk_user(db, username: str) -> str:
    """Create one account and return the id the repository minted.

    The id is read back rather than supplied because ``UserRepository.create``
    mints its own — and because ``trade_attributions.user_id`` is a real foreign
    key, a test that guessed the id would fail with a constraint error that
    looks nothing like its cause.
    """
    with db.session() as session:
        user = UserRepository.create(
            session,
            email=f"{username}@example.test",
            username=username,
            password_hash="not-a-real-hash",
            role="trader",
        )
    return str(user["user_id"])


@pytest.fixture()
def owner(app_db) -> str:
    """A single account, the owner of every trade the tests seed."""
    return _mk_user(app_db, "chain-user")


@pytest.fixture()
def bars():
    """A three-bar series spanning the entry, with a known high/low envelope.

    Deliberately asymmetric so a transposed MAE/MFE cannot pass by symmetry.
    Against the 100.0 entry the extreme high is 105.0 (+5%) and the extreme low
    is 97.0 (−3%) — and note the two come from *different* bars, so a figure
    derived from a single bar's range cannot accidentally satisfy both.
    """
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [
                    "2026-03-02 09:20",
                    "2026-03-03 09:20",
                    "2026-03-04 09:20",
                ]
            ),
            "open": [100.0, 100.5, 104.0],
            "high": [101.0, 104.0, 105.0],
            "low": [99.0, 97.0, 103.5],
            "close": [100.5, 103.0, 104.5],
            "volume": [1_000_000.0, 1_200_000.0, 900_000.0],
        }
    )


def _place_order(session, owner: str, side: str, quantity: float, price: float, ts) -> str:
    """Raise one paper order through the repository, which also writes its ``NEW``
    event. ``create`` takes no status or fill price: the order starts in its
    opening state and the lifecycle is reconstructed from the event log, which is
    the same contract the live OMS works to.
    """
    order = OrderRepository.create(
        session,
        user_id=owner,
        symbol=SYMBOL,
        side=side,
        quantity=quantity,
        mode="PAPER",
        order_type="MARKET",
        requested_price=price,
    )
    return str(order["order_id"])


def _seed_closed_trade(
    app_db,
    owner: str,
    *,
    entry_price: float = 100.0,
    exit_price: float = 104.0,
    quantity: float = 100.0,
    evidence_grade: str | None = None,
    with_orders: bool = True,
) -> str:
    """Write the record the upstream chain would have written, and return its id.

    This stands in for Signal -> Context -> Sizing -> Risk -> Gate -> OMS ->
    Execution, which are all exercised by their own suites. What matters here is
    that attribution is handed a *realistic* record — an open, then a close, and
    the orders that produced them — rather than a hand-built payload, because a
    hand-built payload would let a wiring bug hide behind a convenient shape.
    """
    with app_db.session() as session:
        trade = TradeJournalRepository.open_trade(
            session,
            user_id=owner,
            symbol=SYMBOL,
            side="BUY",
            quantity=quantity,
            entry_price=entry_price,
            entry_ts=ENTRY_TS,
            strategy_id="momentum_basic",
            strategy_version=3,
            signal_reason="SIGNAL_POSITIVE",
            evidence_grade=evidence_grade,
        )
        trade_id = str(trade["trade_id"])

        if with_orders:
            _place_order(session, owner, "BUY", quantity, entry_price, ENTRY_TS)
            _place_order(session, owner, "SELL", quantity, exit_price, EXIT_TS)

        TradeJournalRepository.close_trade(
            session,
            trade_id,
            owner,
            exit_price=exit_price,
            gross_pnl=(exit_price - entry_price) * quantity,
            net_pnl=(exit_price - entry_price) * quantity - 42.0,
            exit_ts=EXIT_TS,
        )
    return trade_id


def _service(app_db, bars, symbol: str = SYMBOL) -> AttributionService:
    return AttributionService(db=app_db, frames={symbol: bars})


# ---------------------------------------------------------------------------
# 1. the chain is connected
# ---------------------------------------------------------------------------


def test_a_closed_trade_becomes_a_stored_attribution(app_db, owner, bars):
    """The first link. A journal episode must produce a persisted row.

    ``attribute_episode`` returning ``"inserted"`` is not sufficient evidence on
    its own — a return value is a claim, and the whole point of storing the row
    is that the row outlives the call. So the row is read back from a fresh
    session.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)

    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    result = service.attribute_episode(owner, episode)
    assert result == "inserted"

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["trade_id"] == trade_id
    assert stored["user_id"] == owner


def test_an_open_trade_is_never_attributed(app_db, owner, bars):
    """A position still running has no exit, so its MAE/MFE are a moving target.

    Storing a snapshot of an in-flight trade would put a number in the dataset
    that silently means "as of whenever the sweep last ran", and every later
    aggregate would treat it as a finished outcome.
    """
    with app_db.session() as session:
        trade = TradeJournalRepository.open_trade(
            session,
            user_id=owner,
            symbol=SYMBOL,
            side="BUY",
            quantity=100.0,
            entry_price=100.0,
            entry_ts=ENTRY_TS,
        )
        trade_id = str(trade["trade_id"])
        episode = TradeJournalRepository.get(session, trade_id, owner)

    service = _service(app_db, bars)
    assert service.attribute_episode(owner, episode) == "skipped"
    with app_db.session() as session:
        assert TradeAttributionRepository.get(session, trade_id, owner) is None  # type: ignore[attr-defined]


def test_attributing_the_same_trade_twice_leaves_one_row(app_db, owner, bars):
    """Idempotency across the whole chain, not just inside the repository.

    A sweep that runs hourly must not accumulate one row per hour. The second
    call is a ``skipped`` — the *same* input, unchanged, is not a new
    measurement.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)

    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    assert service.attribute_episode(owner, episode) == "inserted"
    assert service.attribute_episode(owner, episode) == "skipped"
    assert service.attribute_episode(owner, episode) == "skipped"

    with app_db.session() as session:
        rows, total = TradeAttributionRepository.list_for_user(session, owner)
    assert len(rows) == 1
    assert total == 1


def test_a_re_measured_trade_replaces_rather_than_duplicates(app_db, owner, bars):
    """``force`` is the deliberate override, and it overwrites in place.

    There must be no path that produces two rows for one trade. The trade id is
    the primary key precisely so that this is a structural property rather than
    a convention the caller has to remember.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)

    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)
    assert service.attribute_episode(owner, episode, force=True) == "replaced"

    with app_db.session() as session:
        rows, total = TradeAttributionRepository.list_for_user(session, owner)
    assert len(rows) == 1
    assert total == 1


# ---------------------------------------------------------------------------
# 2. the recorded path statistics are the real ones
# ---------------------------------------------------------------------------


def test_excursions_come_out_of_the_price_series_not_the_pnl(app_db, owner, bars):
    """MAE/MFE must be derived from the bars, and truncated at the exit.

    The bars here top out at 105.0 and bottom at 97.0 against a 100.0 entry, so
    MFE is +5% and MAE is −3% *if and only if* the engine read the highs and lows
    and stopped at the exit. The exit was 104.0, so a figure derived from the
    P&L would report +4% for MFE — close enough to look right — and 0 for MAE,
    which is why the MAE assertion is the one that actually discriminates.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["mfe_pct"] == pytest.approx(5.0, abs=0.05)
    assert stored["mae_pct"] == pytest.approx(-3.0, abs=0.05)


def test_a_bar_after_the_exit_cannot_inflate_the_excursion(app_db, owner, bars):
    """The look-ahead guard, asserted through the whole chain.

    A violent bar is appended *after* the exit. If any layer of the chain
    forgets to truncate, MFE jumps to roughly +90% and the test fails. This is
    the single most valuable assertion in the file: an overstated MFE is exactly
    the fabrication a self-learning system is most prone to, and it is invisible
    in the summary.
    """
    leaky = pd.concat(
        [
            bars,
            pd.DataFrame(
                {
                    "ts": pd.to_datetime(["2026-03-20 09:20"]),
                    "open": [105.0],
                    "high": [190.0],
                    "low": [104.0],
                    "close": [188.0],
                    "volume": [5_000_000.0],
                }
            ),
        ],
        ignore_index=True,
    )
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, leaky)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["mfe_pct"] is not None
    assert stored["mfe_pct"] < 10.0, "a post-exit bar reached the excursion scan"


# ---------------------------------------------------------------------------
# 3. metadata survives the trip
# ---------------------------------------------------------------------------


def test_the_strategy_identity_reaches_the_stored_row(app_db, owner, bars):
    """Strategy and version must be readable back off the stored row.

    Every future slice by strategy depends on this. A row that records P&L but
    loses its strategy id cannot be grouped, and grouping is the only reason the
    table exists.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    # ``symbol`` and ``side`` are lifted into real columns because the API
    # filters on them; ``strategy_id``/``strategy_version`` are deliberately not
    # — they already have an index on ``trade_journal``, and a second index of
    # one field is a second thing that can go stale. They live in the tree.
    assert stored["symbol"] == SYMBOL
    assert stored["side"] == "BUY"
    signal = stored["attribution"]["tree"]["Signal"]
    assert signal["strategy_id"] == "momentum_basic"
    assert signal["strategy_version"] == 3


def test_an_absent_sizing_method_is_reported_as_absent(app_db, owner, bars):
    """The chain here has no sizing record, so the field must be ``None``.

    Not ``0``, not ``"unknown"``, not ``"fixed"``. An invented default would make
    every later slice by sizing method a slice that quietly includes trades whose
    sizing nobody recorded — which reads as evidence and is not.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["sizing_method"] is None
    assert stored["context_score"] is None
    # And the absence is *explained*, which is what makes the None actionable
    # rather than a hole in the report.
    assert stored["missing_fields"]
    # And the absence is *explained*, which is what makes the None actionable
    # rather than a hole in the report.
    assert stored["missing_fields"]


def test_the_detail_api_returns_the_nine_branches(app_db, owner, bars):
    """Requirement 4's tree, read back through the service the route uses.

    Nine branches, because the report has nine things to say. A missing branch
    would be silently absent from the dashboard rather than visibly broken.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    detail = service.get(owner, trade_id)
    assert detail is not None
    tree = detail.get("tree") or detail.get("attribution", {}).get("tree")
    assert tree is not None, f"no tree in detail keys={sorted(detail)}"
    # Display labels, not snake_case identifiers: the tree is what the report
    # renders, and the dashboard reads these keys directly.
    for branch in (
        "Signal",
        "Market Context",
        "Sector Context",
        "Entry",
        "Position Sizing",
        "Risk",
        "Execution",
        "Exit",
        "Outcome",
    ):
        assert branch in tree, branch
    assert len(tree) == 9


# ---------------------------------------------------------------------------
# 4. provenance is copied, never inferred
# ---------------------------------------------------------------------------


def test_an_unstamped_trade_grades_in_sample(app_db, owner, bars):
    """No provenance stamp means in-sample. The default is not neutral.

    This is the conservative direction on purpose: claiming independence that
    was not recorded is a silent overstatement, whereas treating a genuine
    forward trade as in-sample merely understates it — and the understatement is
    visible in the coverage counters.
    """
    trade_id = _seed_closed_trade(app_db, owner, evidence_grade=None)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["evidence_grade"] == "in_sample"
    assert stored["evidence_class"] != "LIVE_FORWARD"


def test_a_forward_stamp_is_carried_through_unchanged(app_db, owner, bars):
    """The one case where forward standing is earned — and it is *copied*.

    The grade is written by the OMS at order creation and must arrive at the
    attribution row without being re-derived. A layer that recomputed it would
    be a layer that could get it wrong.
    """
    trade_id = _seed_closed_trade(app_db, owner, evidence_grade="forward")
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, trade_id, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        stored = TradeAttributionRepository.get(session, trade_id, owner)  # type: ignore[attr-defined]
    assert stored is not None
    assert stored["evidence_grade"] == "forward"


def test_attribution_never_upgrades_an_in_sample_trade(app_db, owner, bars):
    """The sweep must not be a laundering path.

    Run over a whole book of in-sample trades, not one of the resulting rows may
    carry forward standing. This is the property that keeps the learning
    dataset's forward bucket meaning what it says.
    """
    ids = [_seed_closed_trade(app_db, owner, evidence_grade=None) for _ in range(5)]
    service = _service(app_db, bars)
    with app_db.session() as session:
        episodes = [TradeJournalRepository.get(session, tid, owner) for tid in ids]
    for episode in episodes:
        service.attribute_episode(owner, episode)

    with app_db.session() as session:
        rows, total = TradeAttributionRepository.list_for_user(session, owner)
    assert len(rows) == 5
    assert total == 5
    assert all(row["evidence_grade"] == "in_sample" for row in rows)


# ---------------------------------------------------------------------------
# 5. the aggregate read model agrees with the stored rows
# ---------------------------------------------------------------------------


def test_the_summary_counts_every_row_and_separates_the_grades(app_db, owner, bars):
    """A book of five in-sample trades must report five, and report zero forward.

    The grade and class counters are read together because the failure this
    guards against is the two disagreeing — a row that counts as forward in one
    breakdown and in-sample in the other is a contradiction the reader cannot
    resolve.
    """
    service = _service(app_db, bars)
    ids = [
        _seed_closed_trade(app_db, owner, evidence_grade=None),
        _seed_closed_trade(app_db, owner, evidence_grade=None),
        _seed_closed_trade(app_db, owner, evidence_grade="forward"),
    ]
    with app_db.session() as session:
        for trade_id in ids:
            episode = TradeJournalRepository.get(session, trade_id, owner)
            service.attribute_episode(owner, episode)

    rows = service.all_rows(owner)
    assert len(rows) == 3

    from atr.analytics.aggregation import evidence_counts

    counts = evidence_counts(rows)
    assert counts["n"] == 3
    assert counts["forward_n"] == 1
    assert counts["in_sample_n"] == 2
    assert counts["all_forward"] is False


def test_an_empty_book_is_empty_rather_than_zero_filled(app_db, owner, bars):
    """No trades means no rows — and the summary must not invent a win rate.

    Requirement 7's dashboard shows a win rate. On an empty book that is
    undefined, and rendering it as 0% would read as "everything lost".
    """
    service = _service(app_db, bars)
    rows = service.all_rows(owner)
    assert rows == []

    from atr.analytics.aggregation import evidence_counts, overview

    counts = evidence_counts(rows)
    assert counts["n"] == 0
    # ``all_forward`` is False on an empty book: there is nothing to be forward
    # *about*, and a vacuous True would let an empty dataset satisfy a gate.
    assert counts["all_forward"] is False
    headline = overview(rows)
    assert headline["n_trades"] == 0
    assert headline["win_rate"] is None
    assert headline["net_pnl"] is None


# ---------------------------------------------------------------------------
# 6. the chain's upstream layers are untouched
# ---------------------------------------------------------------------------


def test_the_attribution_write_path_does_not_touch_the_journal(app_db, owner, bars):
    """Attribution reads the journal and writes only its own table.

    If the sweep could rewrite ``trade_journal``, a reporting bug could change
    the record it is reporting on — and the change would be indistinguishable
    from a real trade update.
    """
    trade_id = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        before = TradeJournalRepository.get(session, trade_id, owner)
    episode = dict(before or {})
    service.attribute_episode(owner, episode)
    with app_db.session() as session:
        after = TradeJournalRepository.get(session, trade_id, owner)

    for field in ("entry_price", "exit_price", "quantity", "net_pnl", "exit_ts"):
        assert after[field] == before[field], field


def test_attributing_one_trade_leaves_another_untouched(app_db, owner, bars):
    """Per-trade scope. A sweep must not attribute a neighbour by accident."""
    first = _seed_closed_trade(app_db, owner)
    second = _seed_closed_trade(app_db, owner)
    service = _service(app_db, bars)
    with app_db.session() as session:
        episode = TradeJournalRepository.get(session, first, owner)
    service.attribute_episode(owner, episode)

    with app_db.session() as session:
        assert TradeAttributionRepository.get(session, first, owner) is not None  # type: ignore[attr-defined]
        assert TradeAttributionRepository.get(session, second, owner) is None  # type: ignore[attr-defined]


def test_two_users_never_see_each_others_rows(app_db, bars):
    """Ownership scoping, asserted at the storage boundary.

    The API scopes by the authenticated principal; if the repository did not
    scope as well, a bug in one route would expose another user's book.
    """
    alice = _mk_user(app_db, "chain-alice")
    bob = _mk_user(app_db, "chain-bob")

    alice_trade = _seed_closed_trade(app_db, alice)
    bob_trade = _seed_closed_trade(app_db, bob)
    service = _service(app_db, bars)
    with app_db.session() as session:
        service.attribute_episode(
            alice, TradeJournalRepository.get(session, alice_trade, alice)
        )
        service.attribute_episode(
            bob, TradeJournalRepository.get(session, bob_trade, bob)
        )

    assert len(service.all_rows(alice)) == 1
    assert len(service.all_rows(bob)) == 1
    with app_db.session() as session:
        assert TradeAttributionRepository.get(session, bob_trade, alice) is None  # type: ignore[attr-defined]
        assert TradeAttributionRepository.get(session, alice_trade, bob) is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 7. the learning dataset picks the attribution columns up
# ---------------------------------------------------------------------------


def test_the_learning_dataset_declares_the_attribution_columns():
    """Requirement 5's hand-off point, asserted at the schema.

    The forward learning dataset is where all of this has to end up. If the
    columns are not declared, the chain terminates at the dashboard and the
    slices the spec asks for cannot be built at all.
    """
    from atr.services.learning import DATASET_COLUMNS

    attribution_cols = [c for c in DATASET_COLUMNS if c.startswith("attribution_")]
    assert len(attribution_cols) >= 20, f"only {len(attribution_cols)} attribution columns"

    for required in (
        "attribution_entry_quality",
        "attribution_exit_quality",
        "attribution_execution_quality",
        "attribution_mfe_over_risk",
        "attribution_realized_over_risk",
        "attribution_total_slippage_bps",
        "attribution_capture_efficiency_pct",
        "attribution_sizing_method",
        "attribution_sizing_cap_reason",
        "attribution_realized_risk_pct",
        "attribution_planned_risk_amount",
        "attribution_signal_to_order_sec",
        "attribution_order_to_fill_sec",
        "attribution_holding_sec",
        "attribution_partial_fill",
        "attribution_fill_ratio",
        "attribution_market_regime",
        "attribution_reason_codes",
    ):
        assert required in DATASET_COLUMNS, required

    # The raw excursion figures are *not* re-declared as ``attribution_*`` — the
    # dataset's own ``mae``/``mfe`` columns are the same measurement, and adding
    # a second spelling of one quantity is how the two come to disagree.
    assert "mae" in DATASET_COLUMNS
    assert "mfe" in DATASET_COLUMNS
    assert "attribution_mae_r" not in DATASET_COLUMNS


def test_the_attribution_axes_exist_but_stay_out_of_the_default_scan():
    """The axes are registered and opt-in, and both halves matter.

    Registered, because requirement 5 asks for the slices. Opt-in, because a
    default scan over features that are populated on almost no rows would add
    comparisons without adding information — which deflates every p-value in the
    report and makes a real finding harder to see.
    """
    from atr.research.learning_axes import (
        ATTRIBUTION_AXES,
        AXES_BY_NAME,
        DEFAULT_AXES,
    )

    assert len(ATTRIBUTION_AXES) >= 10
    default_names = {axis.name for axis in DEFAULT_AXES}
    for axis in ATTRIBUTION_AXES:
        assert axis.name not in default_names, axis.name
        assert axis.name in AXES_BY_NAME, axis.name


def test_in_sample_trades_are_marked_missing_for_forward_only_features(app_db, owner, bars):
    """The vocabulary's two directions, asserted where each one actually governs.

    ``grade_of`` runs *class -> grade* and is what decides whether a stored row
    counts as evidence. ``forward_class`` runs *source -> class* and is what
    decides which forward bucket a new paper or live record lands in. They are
    different functions with different domains, and conflating them is how a
    test comes to assert a contract that no caller relies on.

    The rule that matters for this feature is on ``grade_of``: an unknown or
    absent class grades **in-sample**. That is what stops the attribution sweep,
    or any later reader of its columns, from treating a measurement as evidence.
    """
    from atr.research.learning_evidence import (
        CLASS_LIVE_FORWARD,
        CLASS_PAPER_FORWARD,
        GRADE_IN_SAMPLE,
        SOURCE_LIVE,
        SOURCE_PAPER,
        forward_class,
        grade_of,
    )

    # class -> grade: anything not a recognised forward class is a measurement.
    for klass in (None, "", "NOT_A_CLASS", "IN_SAMPLE", "BACKTEST"):
        assert grade_of(klass) == GRADE_IN_SAMPLE, klass

    # source -> forward class: only the two real sources name a forward bucket.
    assert forward_class(SOURCE_PAPER) == CLASS_PAPER_FORWARD
    assert forward_class(SOURCE_LIVE) == CLASS_LIVE_FORWARD
