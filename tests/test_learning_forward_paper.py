"""The forward paper pipeline: a real trade becomes evidence, a backfill does not.

What this suite protects
------------------------

The learning engine's only claim to being worth anything is that it can tell
**evidence** from **measurement**. Everything else it does — the axes, the
confidence intervals, the multiple-comparisons correction — is machinery for
reading a sample honestly, and all of it is worthless if the sample is
mislabelled. So the five properties below are not features; they are the
premises the rest of the engine stands on.

1. **A genuinely executed paper trade becomes a forward observation.** The chain
   is driven end to end through the real components — live tick, rule, risk gate,
   OMS order, paper fill, position fold, journal, dataset — and the row that comes
   out the far end must be graded ``forward``. A test that seeded the journal row
   directly would prove nothing about the pipeline, which is the thing that has to
   work.
2. **An in-sample backfill stays in-sample.** A harness that replays historical
   prices writes fills with historical market timestamps and no execution-time
   provenance. That is the shape of a backfill, and it must never be promoted.
3. **A mixed dataset cannot be labelled forward.** When both kinds of record are
   in one table, the counts must separate them and nothing may roll the book up
   into a single "forward" figure.
4. **A missing P&L is not a zero.** The paper ledger records a return and no
   position size, so ``net_pnl`` is absent on those rows. Absent and zero are
   different measurements, and the engine reports the difference rather than
   quietly averaging a zero in.
5. **A daily report does not claim without forward observations.** The report
   states what happened — that is a record — and withholds the judgement until
   the forward book clears the sample floor.

Why the provenance has to be a stamp rather than a timestamp
------------------------------------------------------------

It is tempting to decide "forward or not" from the gap between an order's
``created_at`` and the market time it claims. That does not work: a backfill
harness writes both, so it can write them consistently, and a reader has no way
to tell a genuine record from a well-forged one. So the marker is something only
the live path *can* write — the OMS stamps the order's ``NEW`` event at the
instant it creates the order, and anything inserted into the table by another
route simply does not have it. Absence is then the safe reading, which is why an
unstamped record grades in-sample.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from atr.research.learning_evidence import (
    CLASS_IN_SAMPLE,
    CLASS_PAPER_FORWARD,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
    has_grade,
    row_grade,
)
from atr.services.learning import (
    DATASET_COLUMNS,
    DailyLearningReport,
    LearningDataset,
    LearningDatasetBuilder,
    LearningService,
    PerformanceAnalysis,
    resolve_metric,
)

SYMBOL = "RELIANCE"
EXCHANGE = "NSEEQ"
#: A Monday, 10:00 IST — inside the cash session. Aware on purpose: a naive
#: datetime is interpreted as the machine's local zone, which would make the
#: session gate depend on where the test happens to run.
SESSION = datetime(2026, 9, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

#: A breakout entry, so a live tick can be chosen that actually triggers it.
BREAKOUT = {
    "engine_key": None,
    "rules": {
        "entry": {
            "breakout_lookback": 63,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.5,
            "volume_lookback": 20,
            "min_history_bars": 70,
        },
        "exit": {"stop_loss_pct": 3.0, "min_history_bars": 70},
    },
}


def _daily(n: int = 300, start: float = 1000.0) -> pd.DataFrame:
    """A rising series with a volume surge on the last bar.

    Both conditions are needed to trigger the breakout rule, so the entry in
    these tests is a consequence of the stored rule rather than of a number the
    test liked.
    """
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = [start + i for i in range(n)]
    volumes = [500_000.0] * n
    volumes[-1] = 5_000_000.0
    return pd.DataFrame(
        {
            "ts": index,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


# ---------------------------------------------------------------------------
# fixtures — the real paper chain
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner(app_db) -> str:
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u1",
                email="forward@example.com",
                username="forward",
                display_name="F",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    return "u1"


@pytest.fixture()
def paper_runner(app_db, owner, monkeypatch):
    """A runner over a rising series, with the live price wired to a breakout.

    Returns ``(runner, deployment_id, tick)``.
    """
    monkeypatch.setattr(
        "atr.signals.engine.load_daily",
        lambda symbol, exchange="NSEEQ", **kw: _daily(),
    )
    from atr.appdb.repositories import DeploymentRepository, StrategyRepository
    from atr.services.runner import PaperRunner

    with app_db.session() as session:
        strategy = StrategyRepository.create(
            session, user_id="u1", name="Forward rules", kind="rules"
        )
        version = StrategyRepository.create_version(
            session,
            strategy_id=strategy["strategy_id"],
            author_user_id="u1",
            definition=BREAKOUT,
        )
        deployment = DeploymentRepository.create(
            session,
            user_id="u1",
            strategy_id=strategy["strategy_id"],
            strategy_version=int(version["version"]),
            mode="PAPER",
            capital=500_000.0,
            status="RUNNING",
            config={
                "symbols": [SYMBOL],
                "exchange": EXCHANGE,
                "timeframe": "1d",
                "order_value": 250_000.0,
                "lookback_days": 400,
                "max_open_positions": 1,
            },
        )

    frame = _daily()
    high_63 = float(frame["close"].tail(63).max())
    tick = round(high_63 * 0.995, 2)

    runner = PaperRunner(db=app_db)
    runner.sync_loops()
    runner._loops[deployment["deployment_id"]].venue.prices = lambda s, e: tick
    return runner, deployment["deployment_id"], tick


def _flatten(app_db, deployment_id: str, *, multiplier: float) -> None:
    """Sell the whole position at a chosen multiple of the entry price."""
    from atr.execution.oms import OrderDraft
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    ledger = PaperLedger(db=app_db)
    portfolio = ledger.portfolio("u1", deployment_id=deployment_id)
    position = portfolio.position(SYMBOL)
    quantity = abs(float(position.quantity))
    assert quantity > 0, "the entry must have filled before the exit is placed"
    exit_price = round(float(position.avg_price) * multiplier, 2)
    ExecutionService(
        orders=get_order_service(portfolio=portfolio),
        venue=paper_service.paper_venue(lambda s, e: exit_price),
    ).place(
        OrderDraft(
            user_id="u1",
            symbol=SYMBOL,
            side="SELL",
            quantity=quantity,
            mode="PAPER",
            exchange=EXCHANGE,
            requested_price=exit_price,
            deployment_id=deployment_id,
        )
    )


@pytest.fixture()
def closed_paper_trade(app_db, paper_runner, tmp_path):
    """One paper trade opened and closed through the real chain, and its dataset.

    Returns ``(dataset, builder, deployment_id)``. The dataset is built with an
    empty frame map so nothing depends on the operator's price cache; the row's
    *evidence* is what these tests are about, and enrichment is covered by the
    service suite.
    """
    from atr.services.journal import TradeJournalService

    runner, deployment_id, _tick = paper_runner
    runner.pass_once(now=SESSION)

    journal = TradeJournalService(db=app_db)
    journal.reconcile("u1", deployment_id)

    _flatten(app_db, deployment_id, multiplier=1.02)
    journal.reconcile("u1", deployment_id)

    builder = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={})
    return builder.build(), builder, deployment_id


# ---------------------------------------------------------------------------
# 1. the stamp — what makes "forward" checkable at all
# ---------------------------------------------------------------------------


def _new_event_raw(app_db, order_id: str) -> dict:
    import json

    from atr.appdb.repositories import OrderEventRepository

    with app_db.session() as session:
        for event in OrderEventRepository.history(session, order_id):
            if event.get("to_status") != "NEW":
                continue
            raw = event.get("raw")
            return json.loads(raw) if isinstance(raw, str) else (raw or {})
    raise AssertionError(f"order {order_id} has no NEW event")


def test_the_oms_stamps_provenance_on_every_order_it_raises(app_db, owner):
    """The stamp is written where the order is created, so no caller can forget.

    It is on the ``NEW`` event rather than on the order row because the row is
    current state and the claim — "this was raised live, at this instant" — is
    history, which is what an append-only log is for.
    """
    from atr.execution.oms import PROVENANCE_FORWARD, PROVENANCE_KEY
    from atr.services.orders import get_order_service

    service = get_order_service()
    opened = service.open_order(
        _draft(symbol=SYMBOL, side="BUY", quantity=10.0, requested_price=100.0)
    )
    raw = _new_event_raw(app_db, opened.order_id)
    assert raw.get(PROVENANCE_KEY) == PROVENANCE_FORWARD
    assert raw.get("recorded_at"), "the instant the claim was made must be on the record"


def _draft(*, symbol: str, side: str, quantity: float, requested_price: float | None):
    from atr.execution.oms import OrderDraft

    return OrderDraft(
        user_id="u1",
        symbol=symbol,
        side=side,
        quantity=quantity,
        mode="PAPER",
        exchange=EXCHANGE,
        requested_price=requested_price,
    )


def test_a_real_paper_trade_becomes_a_forward_learning_observation(closed_paper_trade):
    """The whole point of the task, driven through the real chain.

    Live tick → rule → risk gate → OMS order → paper fill → position fold →
    journal episode → learning dataset. Nothing here is seeded by hand: if any
    link in the chain stopped carrying the provenance, this row would come out
    in-sample and the test would fail.
    """
    dataset, _builder, _deployment_id = closed_paper_trade
    assert len(dataset) == 1, "the chain produced one closed trade"

    row = dataset.rows[0]
    assert row["evidence_grade"] == GRADE_FORWARD
    assert row["evidence_class"] == CLASS_PAPER_FORWARD
    assert row["is_forward"] is True
    assert row["evidence_note"], "the row must state the basis of its grade"
    assert dataset.grade_counts() == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 0}


def test_the_forward_row_carries_the_context_the_trade_actually_had(closed_paper_trade):
    """Requirement: record as much real trade context as is available.

    Each field asserted here is one the chain genuinely produced. The ones it
    could not are asserted absent *and* listed in ``missing_features`` — which is
    the difference between a gap and a zero.
    """
    dataset, _builder, deployment_id = closed_paper_trade
    row = dataset.rows[0]

    # identity and attribution
    assert row["strategy_id"]
    assert row["strategy_version"] == 1
    assert row["symbol"] == SYMBOL
    assert row["source"] == "PAPER"
    assert row["source_ref"] == deployment_id
    assert row["trade_ref"]

    # timing — stamped when the order executed, not when the session began, so
    # the weekday is the real one rather than a date the fixture chose.
    assert row["entry_ts"] is not None
    assert row["exit_ts"] is not None
    assert row["exit_ts"] > row["entry_ts"]
    assert row["day_of_week"] in {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
    assert row["time_of_day"] is not None

    # outcome — rupees, size and costs all come from the fold
    assert row["entry_price"] > 0
    assert row["exit_price"] > row["entry_price"]
    assert row["quantity"] > 0
    assert row["return_pct"] > 0
    assert row["gross_pnl"] is not None
    assert row["net_pnl"] is not None
    assert row["commission"] is not None
    assert row["commission"] > 0, "NSE delivery costs are never zero"

    # excursions, from the fills the log holds
    assert row["mfe"] is not None
    assert row["mae"] is not None
    assert "mfe" not in row["missing_features"]
    assert "mae" not in row["missing_features"]

    # slippage, measured per leg and averaged
    assert row["slippage_bps"] is not None
    assert "slippage_bps" not in row["missing_features"]

    # what the chain cannot supply is absent and named, not filled in
    assert row["vwap_relationship"] is None
    assert "vwap_relationship" in row["missing_features"]


def test_the_dataset_summary_shows_the_evidence_figures(closed_paper_trade):
    """Requirement: the dashboard leads with these, so they are on the payload."""
    dataset, _builder, _deployment_id = closed_paper_trade
    summary = dataset.summary()
    assert summary["observations"] == 1
    assert summary["forward_observations"] == 1
    assert summary["in_sample_observations"] == 0
    assert summary["evidence_grades"] == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 0}
    assert summary["evidence_classes"][CLASS_PAPER_FORWARD] == 1
    assert summary["latest_forward_ts"] is not None
    assert summary["metric_coverage"]["net_pnl"] == 1
    assert summary["metric_coverage"]["return_pct"] == 1


# ---------------------------------------------------------------------------
# 2. a backfill stays in-sample
# ---------------------------------------------------------------------------


def _seed_backfilled_episode(app_db, *, user_id: str = "u1", symbol: str = "TCS") -> str:
    """A closed episode written straight into the journal, with no order behind it.

    This is what a backfill harness produces: historical market timestamps, a
    plausible trade, and no execution-time provenance — because there was no
    execution. It is the shape the grading rule exists to catch.
    """
    from atr.appdb.repositories import TradeJournalRepository

    with app_db.session() as session:
        created = TradeJournalRepository.open_trade(
            session,
            user_id=user_id,
            symbol=symbol,
            side="BUY",
            quantity=10.0,
            entry_price=500.0,
            entry_ts=datetime(2026, 3, 2, 9, 15),
            strategy_id="STRAT_BACKFILL",
            strategy_version=1,
            signal_reason="replayed from the 2026 selection history",
        )
        TradeJournalRepository.close_trade(
            session,
            created["trade_id"],
            user_id,
            exit_price=505.0,
            gross_pnl=50.0,
            net_pnl=40.0,
            exit_ts=datetime(2026, 3, 6, 14, 30),
            mfe=60.0,
            mae=-10.0,
        )
    return created["trade_id"]


def test_a_backfilled_episode_stays_in_sample(app_db, owner, tmp_path):
    """The property the whole grading column exists for.

    The record is real in every visible respect — a symbol, prices, timestamps,
    a P&L — and it is still not evidence, because nothing about it demonstrates
    it was written before its own outcome.
    """
    _seed_backfilled_episode(app_db)
    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={}).build()
    assert len(dataset) == 1

    row = dataset.rows[0]
    assert row["evidence_grade"] == GRADE_IN_SAMPLE
    assert row["evidence_class"] == CLASS_IN_SAMPLE
    assert row["is_forward"] is False
    assert "no execution-time provenance stamp" in row["evidence_note"]
    assert dataset.grade_counts()[GRADE_FORWARD] == 0


def test_building_the_dataset_again_does_not_promote_a_backfill(app_db, owner, tmp_path):
    """Grading is a property of the record, not of the pass that read it.

    A rule that graded on the first build and something else on the second would
    make every count depend on when it was asked for.
    """
    _seed_backfilled_episode(app_db)
    builder = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={})
    first = builder.build().grade_counts()
    second = builder.build().grade_counts()
    assert first == second == {GRADE_FORWARD: 0, GRADE_IN_SAMPLE: 1}


def test_an_order_row_inserted_without_a_stamp_is_not_forward(app_db, owner, tmp_path):
    """Absence of the stamp is the *safe* reading, and that is deliberate.

    This inserts an order and a fill exactly as a replay harness would: correct
    in every field, correct foreign keys, no provenance. Grading it forward would
    let last year's prices confirm the rule that was fitted to them.
    """
    from sqlalchemy import insert

    from atr.appdb.repositories import TradeJournalRepository
    from atr.appdb.schema import order_events, orders

    now = datetime(2026, 3, 2, 9, 15)
    with app_db.session() as session:
        session.execute(
            insert(orders).values(
                order_id="ORDREPLAY0000000000000000001",
                user_id="u1",
                deployment_id=None,
                strategy_id="STRAT_REPLAY",
                strategy_version=1,
                symbol="INFY",
                exchange=EXCHANGE,
                asset_class="EQUITY",
                side="BUY",
                quantity=10.0,
                order_type="MARKET",
                tif="DAY",
                mode="PAPER",
                status="FILLED",
                filled_quantity=10.0,
                avg_fill_price=500.0,
                requested_price=500.0,
                created_at=now,
                updated_at=now,
                submitted_at=now,
                completed_at=now,
            )
        )
        session.execute(
            insert(order_events).values(
                order_event_id="EVREPLAY00000000000000000001",
                order_id="ORDREPLAY0000000000000000001",
                seq=1,
                from_status=None,
                to_status="NEW",
                ts=now,
                source="oms",
                # No provenance key. This is the whole point of the fixture.
                raw='{"reason": "replayed"}',
            )
        )
        created = TradeJournalRepository.open_trade(
            session,
            user_id="u1",
            symbol="INFY",
            side="BUY",
            quantity=10.0,
            entry_price=500.0,
            entry_ts=now,
        )
        TradeJournalRepository.close_trade(
            session,
            created["trade_id"],
            "u1",
            exit_price=510.0,
            gross_pnl=100.0,
            exit_ts=datetime(2026, 3, 6, 14, 30),
        )

    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={}).build()
    assert dataset.rows[0]["evidence_grade"] == GRADE_IN_SAMPLE


# ---------------------------------------------------------------------------
# 3. a mixed book cannot be rolled up into "forward"
# ---------------------------------------------------------------------------


def test_a_mixed_book_keeps_the_two_kinds_apart(closed_paper_trade, app_db, owner, tmp_path):
    """One genuine forward trade and one backfill, in one table.

    The failure this guards against is a reader that takes "the book" as one
    thing: a mean over the two rows is a mean over evidence and non-evidence
    mixed, and no count derived from it means anything.
    """
    _seed_backfilled_episode(app_db)
    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames={}).build()

    assert len(dataset) == 2
    grades = dataset.grade_counts()
    assert grades == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 1}

    forward_refs = {row["trade_ref"] for row in dataset.by_grade(GRADE_FORWARD)}
    in_sample_refs = {row["trade_ref"] for row in dataset.by_grade(GRADE_IN_SAMPLE)}
    assert forward_refs and in_sample_refs
    assert not (forward_refs & in_sample_refs), "a trade cannot be both"

    # The analysis can be run on the forward sample alone, which is the only
    # version of it that is a finding rather than a restatement.
    forward_only = PerformanceAnalysis(dataset).analyse(grades=[GRADE_FORWARD])
    assert forward_only.n == 1
    everything = PerformanceAnalysis(dataset).analyse()
    assert everything.n == 2
    assert any("graded in-sample" in note for note in everything.caveats)


def test_the_summary_of_a_mixed_book_states_both_counts(closed_paper_trade, app_db, owner, tmp_path):
    _seed_backfilled_episode(app_db)
    summary = LearningDatasetBuilder(
        db=app_db, cache_root=tmp_path, frames={}
    ).build().summary()
    assert summary["observations"] == 2
    assert summary["forward_observations"] == 1
    assert summary["in_sample_observations"] == 1
    assert summary["evidence_grades"] == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 1}
    # The latest *forward* trade date, which is the number that answers "is
    # anything still being measured?" — not the latest trade of any kind.
    assert summary["latest_forward_ts"] is not None


def test_an_ungraded_row_is_not_silently_counted_as_forward():
    """The conservative default, asserted directly on the reader.

    A row carrying no verdict at all is the case a future edit is most likely to
    get wrong, because "no marker" reads as "nothing to say" rather than as
    "cannot be shown".
    """
    assert row_grade({}) == GRADE_IN_SAMPLE
    assert row_grade({"evidence_class": "NONSENSE"}) == GRADE_IN_SAMPLE
    assert row_grade({"is_forward": False}) == GRADE_IN_SAMPLE
    assert has_grade({}) is False
    assert has_grade({"evidence_grade": GRADE_IN_SAMPLE}) is True
    # The explicit grade wins, then the class is translated, then the boolean.
    assert row_grade({"evidence_grade": GRADE_FORWARD}) == GRADE_FORWARD
    assert row_grade({"evidence_class": CLASS_PAPER_FORWARD}) == GRADE_FORWARD
    assert row_grade({"is_forward": True}) == GRADE_FORWARD
    # And a row that says both, contradicting itself, resolves to the explicit
    # grade rather than to whichever the reader happened to look at.
    assert row_grade({"evidence_grade": GRADE_IN_SAMPLE, "is_forward": True}) == GRADE_IN_SAMPLE


# ---------------------------------------------------------------------------
# 4. a missing P&L is not a zero
# ---------------------------------------------------------------------------


def _row(*, net_pnl, return_pct, grade: str = GRADE_FORWARD, day: str = "2026-06-10", **extra):
    stamp = datetime.fromisoformat(day)
    row = {
        "source": "PAPER",
        "evidence_class": CLASS_PAPER_FORWARD if grade == GRADE_FORWARD else CLASS_IN_SAMPLE,
        "evidence_grade": grade,
        "evidence_note": "fixture",
        "is_forward": grade == GRADE_FORWARD,
        "trade_ref": f"t{day}-{net_pnl}-{return_pct}",
        "strategy_key": "weekly_momentum_top10",
        "strategy_id": None,
        "strategy_version": None,
        "symbol": SYMBOL,
        "sector": "Oil",
        "direction": "LONG",
        "entry_ts": stamp - timedelta(days=5),
        "exit_ts": stamp,
        "duration_days": 5.0,
        "day_of_week": "Wednesday",
        "time_of_day": "close",
        "quantity": None,
        "entry_price": 100.0,
        "exit_price": 102.0,
        "gross_pnl": None,
        "commission": None,
        "net_pnl": net_pnl,
        "return_pct": return_pct,
        "exit_reason": "fixed_hold_7d",
        "mfe": None,
        "mae": None,
        "slippage_bps": None,
        "signal_reason": "weekly momentum",
        "setup": None,
        "rsi": None,
        "sma_fast": None,
        "sma_slow": None,
        "sma_long": None,
        "prior_high": None,
        "signal_volume_multiple": None,
        "breakout_proximity_pct": None,
        "market_regime": "uptrend",
        "vwap_relationship": None,
        "missing_features": ["net_pnl"] if net_pnl is None else [],
    }
    row.update(extra)
    return row


def _dataset(rows: list[dict]) -> LearningDataset:
    return LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))


def test_a_missing_pnl_is_absent_rather_than_zero():
    """The paper ledger records a return and no size, so ``net_pnl`` is absent.

    Reporting it as zero would say the trades broke even. Reporting the coverage
    and switching the metric says what actually happened.
    """
    dataset = _dataset([_row(net_pnl=None, return_pct=2.0) for _ in range(4)])
    assert dataset.metric_coverage() == {"net_pnl": 0, "return_pct": 4}

    metric, note = resolve_metric(dataset)
    assert metric == "return_pct"
    assert note and "net_pnl has no coverage" in note

    summary = dataset.summary()
    assert summary["net_pnl_total"] is None, "no rupees were recorded, so there is no total"
    assert summary["metric_coverage"]["net_pnl"] == 0
    assert summary["metric_coverage"]["return_pct"] == 4


def test_a_zero_pnl_is_a_measurement_and_is_counted_as_coverage():
    """The contrast, without which the rule above could be satisfied by treating
    every row as unmeasured."""
    dataset = _dataset([_row(net_pnl=0.0, return_pct=0.0) for _ in range(3)])
    assert dataset.metric_coverage() == {"net_pnl": 3, "return_pct": 3}
    metric, note = resolve_metric(dataset)
    assert metric == "net_pnl" and note is None
    assert dataset.summary()["net_pnl_total"] == 0.0


def test_the_analysis_refuses_to_report_an_unmeasured_column_as_flat():
    """Asking for the absent column must not return a table of nothing that reads
    like a break-even book."""
    dataset = _dataset([_row(net_pnl=None, return_pct=2.0) for _ in range(12)])
    analysis = PerformanceAnalysis(dataset, metric="net_pnl").analyse()
    assert analysis.n == 0
    assert any("not one carries net_pnl" in note for note in analysis.caveats)


# ---------------------------------------------------------------------------
# 5. the report states facts and withholds claims
# ---------------------------------------------------------------------------


def _book(
    *,
    forward: int,
    in_sample: int,
    value: float = 100.0,
    grade_value: float | None = None,
    history: int = 0,
    history_value: float = 500.0,
):
    """A book that closes ``forward`` forward and ``in_sample`` in-sample rows today.

    ``history`` adds that many closed rows in the preceding window, which is what
    the report compares the day against. Without them there is no baseline, and
    the report correctly declines to compute a deviation.
    """
    rows = [
        _row(
            net_pnl=grade_value if grade_value is not None else value,
            return_pct=1.0,
            grade=GRADE_FORWARD,
            trade_ref=f"f{i}",
        )
        for i in range(forward)
    ]
    rows += [
        _row(net_pnl=value, return_pct=1.0, grade=GRADE_IN_SAMPLE, trade_ref=f"s{i}")
        for i in range(in_sample)
    ]
    for index in range(history):
        rows.append(
            _row(
                net_pnl=history_value,
                return_pct=1.0,
                grade=GRADE_FORWARD,
                # April, so a history row can never land on the June report date
                # and be counted as one of *today's* trades. A fixture that
                # overlapped the two would make the day's grade counts depend on
                # how many history rows the test happened to ask for.
                day=f"2026-04-{index + 1:02d}",
                trade_ref=f"h{index}",
            )
        )
    return _dataset(rows)


def test_the_report_does_not_claim_below_the_forward_floor():
    """Requirement 9. Nine forward observations is one short of the floor.

    The day's figure is still stated — it is a fact — but the report says nothing
    about whether it was good, and it names the reason.
    """
    report = DailyLearningReport(_book(forward=3, in_sample=0)).build(
        as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    )
    assert report.forward_observations == 3
    assert report.claimable is False
    assert report.observations == [], "a claim below the floor is overfitting"
    assert "Too few forward observations" in report.headline
    assert report.metric_total is not None, "the day's figure is a fact and is stated"
    assert any("below the" in note for note in report.limitations)


def test_a_book_of_in_sample_rows_supports_no_claim_however_large_it_is():
    """The count that gates a claim is forward observations, not rows.

    This is the distinction the whole grade column exists for. A hundred
    in-sample rows are a hundred measurements of the history the rule was chosen
    from; they are not a hundred tests of it.
    """
    report = DailyLearningReport(_book(forward=0, in_sample=100)).build(
        as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    )
    assert report.trades_today == 100
    assert report.forward_observations == 0
    assert report.in_sample_observations == 100
    assert report.claimable is False
    assert report.observations == []
    assert any("graded in-sample" in note for note in report.limitations)
    assert "in-sample" in report.headline


def test_the_report_states_the_day_and_names_the_grade_of_its_trades():
    """The day is a record; the grade of the day is what says whether it is
    evidence. Both are on the payload so a screen can render them side by side."""
    payload = DailyLearningReport(_book(forward=2, in_sample=3)).build(
        as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    ).as_dict()
    assert payload["trades_today"] == 5
    assert payload["today_grades"] == {GRADE_FORWARD: 2, GRADE_IN_SAMPLE: 3}
    assert payload["forward_observations"] == 2
    assert payload["in_sample_observations"] == 3
    assert payload["claimable"] is False
    assert payload["advisory"] is True
    assert payload["applies_changes"] is False


def test_the_report_claims_once_the_forward_book_clears_the_floor():
    """The gate must open, or it would be a mute button rather than a safeguard.

    Twelve forward observations clear the floor and the preceding window supplies
    a baseline, so the advisory findings are produced — and the report stops
    telling the reader it has too little to go on.
    """
    report = DailyLearningReport(
        _book(forward=12, in_sample=0, value=-500.0, history=6, history_value=500.0)
    ).build(as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC))
    assert report.claimable is True
    assert report.expectation["mean_per_day"] is not None, "the baseline must exist"
    assert "Too few forward observations" not in report.headline
    assert report.observations, "the gate opened, so findings are produced"
    for observation in report.observations:
        assert observation["advisory"] is True
        assert "evidence" in observation and "sample_size" in observation


def test_an_in_sample_day_is_reported_as_a_record_and_not_as_a_result():
    """A day whose trades are all in-sample produced a figure and no evidence.

    The headline says so in words rather than leaving the reader to notice that
    the grade column was empty.
    """
    report = DailyLearningReport(_book(forward=0, in_sample=4)).build(
        as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    )
    assert report.metric_total is not None
    assert "in-sample" in report.headline
    assert "not a forward test" in report.headline


def test_a_claimable_book_does_not_license_a_finding_about_a_backfilled_day():
    """The subtlest version of the mistake, and the one a grade filter alone misses.

    The book here holds twelve forward observations in the preceding window, so
    it is claimable and the sample floor is cleared. Today's trades, however, are
    all in-sample. Computing the findings over the day would publish a
    restatement of the selection history — and it would look like a finding,
    because the book that licensed it was real.
    """
    report = DailyLearningReport(
        _book(forward=0, in_sample=6, value=-500.0, history=12, history_value=500.0)
    ).build(as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC))

    assert report.forward_observations == 12
    assert report.claimable is True, "the book clears the floor"
    assert report.today_grades == {GRADE_FORWARD: 0, GRADE_IN_SAMPLE: 6}
    assert report.observations == [], "today is not evidence, so it carries no finding"
    assert any("no finding is drawn from it" in note for note in report.limitations)


def test_a_mixed_day_draws_its_findings_from_the_forward_rows_only():
    """And says so, so a reader knows which half of the day was analysed."""
    report = DailyLearningReport(
        _book(forward=6, in_sample=6, value=-500.0, history=6, history_value=500.0)
    ).build(as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC))

    assert report.claimable is True
    assert report.today_grades == {GRADE_FORWARD: 6, GRADE_IN_SAMPLE: 6}
    assert any(
        "6 of today's 12 trades are forward" in note for note in report.limitations
    ), report.limitations
    for observation in report.observations:
        # Every finding names the grade it was computed from.
        assert observation["evidence"]["evidence_grade"] == GRADE_FORWARD


def test_the_report_never_carries_a_parameter_value():
    """The line between this phase and autonomous optimisation, held in the
    payload rather than in a docstring."""
    payload = DailyLearningReport(_book(forward=12, in_sample=0, value=-500.0)).build(
        as_of=datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
    ).as_dict()
    assert "proposed_value" not in str(payload)
    assert payload["applies_changes"] is False


# ---------------------------------------------------------------------------
# the dataset's own shape
# ---------------------------------------------------------------------------


def test_every_row_states_a_grade_and_the_columns_are_fixed(closed_paper_trade):
    """A row with no grade is the hole this column closes, so the builder must
    never leave one — and the frame must stay the declared shape."""
    dataset, _builder, _deployment_id = closed_paper_trade
    assert list(dataset.frame().columns) == list(DATASET_COLUMNS)
    assert "evidence_grade" in DATASET_COLUMNS
    assert "evidence_note" in DATASET_COLUMNS
    for row in dataset.rows:
        assert has_grade(row), row.get("trade_ref")
        assert row["evidence_note"]


# ---------------------------------------------------------------------------
# the status screen — the first thing an operator reads
# ---------------------------------------------------------------------------


def test_status_says_a_book_of_journal_trades_holds_no_forward_evidence(app_db, owner, tmp_path):
    """Three trades and no forward row is not "3 trades available".

    The status screen is the one an operator checks first, and a total that
    counts everything while saying nothing about the grade is the same lie as an
    empty screen — it says there is plenty to learn from when there is nothing
    to claim.
    """
    for _ in range(3):
        _seed_backfilled_episode(app_db, symbol=f"SYM{_}")
    status = LearningService(db=app_db, cache_root=tmp_path).status()
    assert status["counts"]["trade_journal"] == 3
    assert status["counts"]["trade_journal_forward"] == 0
    assert status["counts"]["trade_journal_in_sample"] == 3
    assert status["forward_available"] == 0
    assert status["sufficient_for_a_claim"] is False
    assert "not one is forward evidence" in status["note"]


def test_status_counts_a_forward_trade_as_evidence(closed_paper_trade, app_db, tmp_path):
    status = LearningService(db=app_db, cache_root=tmp_path).status()
    assert status["counts"]["trade_journal_forward"] == 1
    assert status["counts"]["trade_journal_in_sample"] == 0
    assert status["forward_available"] == 1
    assert "1 of them forward evidence" in status["note"]
    # One forward observation is not a claim, and the status says so separately
    # from whether there is enough to analyse.
    assert status["sufficient_for_analysis"] is False
    assert status["sufficient_for_a_claim"] is False
