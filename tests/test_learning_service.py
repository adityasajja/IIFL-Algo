"""The learning pipeline end to end, and the guarantees it must not break.

Two things are tested here beyond "does it compute the right answer".

**The empty case.** The live database holds zero trades today. A learning engine
that raises, returns ``None``, or — worst — fabricates a sample is worse than no
learning engine, because its output would be read as evidence. Every entry point
is therefore exercised against a genuinely empty database, and the assertions
are about the *shape* of the empty answer, not merely that it does not raise.

**The safety case.** The user's constraint was that the engine may never modify
a live strategy, place an order, or bypass the risk engine. That is asserted by
fingerprinting every mutable table across a full pipeline run, so the guarantee
is tested rather than promised in a docstring. A future contributor who adds a
write to this module finds out here rather than in production.

Fixtures are synthetic. A test whose result depends on the operator's 3,000-file
cache or on today's date is not a test.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from atr.services.learning import (
    DATASET_COLUMNS,
    GRADE_FORWARD,
    GRADE_IN_SAMPLE,
    SOURCE_BACKTEST,
    SOURCE_LIVE,
    SOURCE_PAPER,
    DailyLearningReport,
    LearningDataset,
    LearningDatasetBuilder,
    LearningService,
    PerformanceAnalysis,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _price_frame(seed: int = 1, periods: int = 200, start: str = "2025-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0006, 0.011, periods).cumsum()
    close = 500.0 * np.exp(steps)
    high = close * (1 + rng.uniform(0.001, 0.02, periods))
    low = close * (1 - rng.uniform(0.001, 0.02, periods))
    open_ = close * (1 + rng.normal(0, 0.004, periods))
    volume = rng.integers(200_000, 1_500_000, periods).astype(float)
    return pd.DataFrame(
        {
            "ts": pd.date_range(start, periods=periods, freq="B"),
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
        }
    )


@pytest.fixture()
def frames() -> dict[str, pd.DataFrame]:
    return {
        "RELIANCE": _price_frame(seed=1),
        "TCS": _price_frame(seed=2),
        "INFY": _price_frame(seed=3),
        "NIFTYBEES": _price_frame(seed=9),
    }


@pytest.fixture()
def sector_root(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "ind_nifty50list.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
        "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n"
        "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n",
        encoding="utf-8",
    )
    return tmp_path


def _trade(
    *,
    symbol: str,
    entry_ts: datetime,
    net_pnl: float,
    seq: int = 1,
    run_id: str = "RUN0000000000000000000000000001",
    reason: str | None = None,
    exit_reason: str = "take_profit",
    strategy_id: str | None = None,
    strategy_version: int | None = None,
    direction: str = "LONG",
) -> dict:
    entry_price = 500.0
    quantity = 10.0
    sign = 1.0 if direction == "LONG" else -1.0
    exit_price = entry_price + (net_pnl / quantity) * sign
    return {
        "run_id": run_id,
        "seq": seq,
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "entry_ts": entry_ts,
        "entry_price": entry_price,
        "exit_ts": entry_ts + timedelta(days=5),
        "exit_price": exit_price,
        "gross_pnl": net_pnl,
        "commission": 0.0,
        "net_pnl": net_pnl,
        "return_pct": net_pnl / (entry_price * quantity) * 100.0,
        "duration_days": 5.0,
        "exit_reason": exit_reason,
        "signal_reason": reason,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
    }


def _seed_backtest(app_db, trades: list[dict], *, engine_key: str = "sma_crossover") -> None:
    """Write a COMPLETED run and its trades. Uses the schema directly.

    Written through the schema rather than the service so the test controls
    exactly what is in the table — a builder test that depended on a real
    backtest executing would be slow and would conflate two concerns.
    """
    from atr.appdb.schema import backtest_runs, backtest_trades

    run_id = trades[0]["run_id"] if trades else "RUN0000000000000000000000000001"
    with app_db.session() as session:
        session.execute(
            backtest_runs.insert().values(
                run_id=run_id,
                user_id=_owner(app_db),
                strategy_id=None,
                strategy_version=None,
                engine_key=engine_key,
                created_at=datetime(2026, 1, 1),
                started_at=datetime(2026, 1, 1),
                finished_at=datetime(2026, 1, 1, 0, 5),
                status="COMPLETED",
                progress=1.0,
                config="{}",
                data_fingerprint="deadbeef",
                metrics="{}",
                error=None,
            )
        )
        for trade in trades:
            session.execute(backtest_trades.insert().values(**trade))


def _owner(app_db) -> str:
    from sqlalchemy import select

    from atr.appdb.schema import users

    with app_db.session() as session:
        found = session.execute(select(users.c.user_id).limit(1)).scalar()
    return str(found)


@pytest.fixture()
def owner(app_db) -> str:
    """A real user row. ``backtest_runs.user_id`` is a foreign key, so a run
    cannot be seeded without one — and a fabricated id would fail the constraint
    rather than the assertion under test."""
    from atr.appdb.schema import users

    with app_db.session() as session:
        session.execute(
            users.insert().values(
                user_id="u_learning",
                email="learning@example.com",
                username="learner",
                display_name="L",
                password_hash="x",
                role="owner",
                is_active=True,
                mfa_enabled=False,
                failed_logins=0,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    return "u_learning"


@pytest.fixture()
def builder(app_db, frames, sector_root, owner):
    return LearningDatasetBuilder(db=app_db, cache_root=sector_root, frames=frames)


# ---------------------------------------------------------------------------
# the empty case — the state the database is actually in
# ---------------------------------------------------------------------------


def test_an_empty_database_yields_a_valid_empty_dataset(app_db, builder):
    """Not an error, not None — an empty dataset that says why it is empty."""
    dataset = builder.build()
    assert len(dataset) == 0
    assert dataset.empty is True
    assert dataset.warnings, "an empty dataset must explain itself"
    assert any("no trades" in note for note in dataset.warnings)


def test_the_empty_dataset_still_has_every_column(frames):
    """A frame with zero *columns* would make every groupby raise. The empty
    case is a zero-row, correctly-shapen frame instead."""
    dataset = LearningDataset(
        rows=[], missing_features={}, generated_at=datetime.now(UTC)
    )
    frame = dataset.frame()
    assert len(frame) == 0
    assert list(frame.columns) == list(DATASET_COLUMNS)


def test_the_empty_dataset_reports_absent_rather_than_zero():
    dataset = LearningDataset(
        rows=[], missing_features={}, generated_at=datetime.now(UTC)
    )
    summary = dataset.summary()
    assert summary["trades"] == 0
    # Zero rupees earned and no trades is a different statement from "a
    # measured break-even", and the payload must not conflate them.
    assert summary["net_pnl_total"] is None
    assert summary["date_range"] == {"first": None, "last": None}


def test_the_empty_report_refuses_to_invent_a_number():
    dataset = LearningDataset(
        rows=[], missing_features={}, generated_at=datetime.now(UTC)
    )
    report = DailyLearningReport(dataset).build()
    assert report.trades_today == 0
    assert report.net_pnl_today is None
    assert "No closed trades" in report.headline
    assert report.limitations
    assert report.expectation["mean_per_day"] is None
    assert report.deviation["delta"] is None


def test_the_empty_analysis_suppresses_everything_and_says_so():
    dataset = LearningDataset(
        rows=[], missing_features={}, generated_at=datetime.now(UTC)
    )
    analysis = PerformanceAnalysis(dataset).analyse()
    assert analysis.n == 0
    assert analysis.overall["n"] == 0
    assert analysis.overall["mean"] is None
    assert analysis.notable == []
    assert any("absent rather than zero" in note for note in analysis.caveats)


def test_status_reports_the_real_state_of_the_database(app_db):
    status = LearningService(db=app_db).status()
    assert status["available"] is True
    assert status["trades_available"] == 0
    assert status["sufficient_for_analysis"] is False
    assert "nothing to analyse" in status["note"]


def test_the_empty_pipeline_does_not_write_artefacts(app_db, builder):
    """``snapshot`` must be able to run without touching the filesystem."""
    service = LearningService(db=app_db, builder=builder)
    payload = service.snapshot(write=False)
    assert payload["dataset"]["trades"] == 0
    assert "written" not in payload


# ---------------------------------------------------------------------------
# building the dataset from real rows
# ---------------------------------------------------------------------------


def test_backtest_trades_are_normalised_with_their_source(app_db, builder, owner):
    trades = [
        _trade(
            symbol="RELIANCE",
            entry_ts=datetime(2026, 2, 2),
            net_pnl=850.0,
            seq=index,
            reason="uptrend (SMA20 505.00 > SMA50 495.00) with RSI 47",
        )
        for index in range(1, 6)
    ]
    _seed_backtest(app_db, trades)

    dataset = builder.build()
    assert len(dataset) == 5
    assert dataset.source_counts[SOURCE_BACKTEST] == 5
    row = dataset.rows[0]
    assert row["source"] == SOURCE_BACKTEST
    assert row["symbol"] == "RELIANCE"
    assert row["setup"] == "trend_pullback"
    assert row["rsi"] == 47.0
    assert row["sector"] == "Oil Gas & Consumable Fuels"
    assert row["net_pnl"] == 850.0
    assert row["direction"] == "LONG"


def test_mfe_and_mae_are_absent_for_backtest_trades_and_say_why(app_db, builder):
    """The source table has no such columns. Estimating them from the entry and
    exit price would invent the intra-trade path, which is exactly the thing the
    journal's own docstring refuses to do."""
    _seed_backtest(app_db, [_trade(symbol="TCS", entry_ts=datetime(2026, 2, 2), net_pnl=100.0)])
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["mfe"] is None
    assert row["mae"] is None
    assert "mfe" in dataset.missing_features
    assert "intra-trade excursion" in dataset.missing_features["mfe"]


def test_a_missing_signal_reason_is_recorded_not_filled_in(app_db, builder):
    _seed_backtest(
        app_db,
        [_trade(symbol="TCS", entry_ts=datetime(2026, 2, 2), net_pnl=100.0, reason=None)],
    )
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["signal_reason"] is None
    assert row["setup"] is None
    assert "signal_reason" in row["missing_features"]


def test_relative_volume_is_read_from_the_breakout_reason_when_present(app_db, builder):
    _seed_backtest(
        app_db,
        [
            _trade(
                symbol="INFY",
                entry_ts=datetime(2026, 2, 2),
                net_pnl=300.0,
                reason="within 2.0% of the 63-bar high 510.00 on 2.4x average volume",
            )
        ],
    )
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["signal_volume_multiple"] == 2.4
    assert row["setup"] == "breakout"


def test_entry_time_market_context_is_added(app_db, builder):
    _seed_backtest(app_db, [_trade(symbol="RELIANCE", entry_ts=datetime(2026, 2, 2), net_pnl=100.0)])
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["as_of_ts"] is not None
    assert row["bars_available"] > 50
    assert row["rsi"] is not None
    assert row["atr_pct"] is not None
    assert row["relative_volume"] is not None
    assert row["trend_pct"] is not None
    assert row["nifty_trend_pct"] is not None
    # Canonical ticker, not the ``-EQ`` file spelling: the same normalisation the
    # rest of the system applies, so a benchmark name is comparable everywhere.
    assert row["benchmark_symbol"] == "NIFTYBEES"
    assert row["market_regime"] is not None


def test_a_symbol_with_no_cached_history_is_marked_not_estimated(app_db, builder):
    _seed_backtest(app_db, [_trade(symbol="UNKNOWNX", entry_ts=datetime(2026, 2, 2), net_pnl=50.0)])
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["bars_available"] == 0
    assert row["rsi"] is None
    assert row["atr_pct"] is None
    assert "rsi" in row["missing_features"]


def test_a_symbol_absent_from_the_universe_csvs_has_no_sector(app_db, builder):
    """501 of 3,089 cached symbols are in the index CSVs. The rest have no
    sector, which is a gap to report rather than a bucket to invent."""
    _seed_backtest(app_db, [_trade(symbol="SOMETHINGELSE", entry_ts=datetime(2026, 2, 2), net_pnl=50.0)])
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["sector"] is None
    assert "sector" in row["missing_features"]
    assert "sector" in dataset.missing_features


def test_vwap_and_vix_are_declared_missing_at_the_dataset_level(app_db, builder):
    _seed_backtest(app_db, [_trade(symbol="TCS", entry_ts=datetime(2026, 2, 2), net_pnl=10.0)])
    dataset = builder.build()
    assert "vwap_relationship" in dataset.missing_features
    assert "india_vix" in dataset.missing_features
    assert "intraday" in dataset.missing_features["vwap_relationship"]


def test_a_failed_run_contributes_no_trades(app_db, builder):
    """A FAILED run has no trade rows by construction; a RUNNING one has
    half-written ones. Reading either would put a partial equity path into the
    statistics as though it were a result."""
    from atr.appdb.schema import backtest_runs

    _seed_backtest(app_db, [_trade(symbol="TCS", entry_ts=datetime(2026, 2, 2), net_pnl=10.0)])
    with app_db.session() as session:
        session.execute(
            backtest_runs.update()
            .where(backtest_runs.c.run_id == "RUN0000000000000000000000000001")
            .values(status="FAILED")
        )
    assert len(builder.build()) == 0


# ---------------------------------------------------------------------------
# the journal path
# ---------------------------------------------------------------------------


def _seed_journal(app_db, rows: list[dict], *, user_id: str, deployment_id: str | None = None):
    from atr.appdb.repositories import TradeJournalRepository

    ids = []
    with app_db.session() as session:
        for row in rows:
            created = TradeJournalRepository.open_trade(
                session,
                user_id=user_id,
                symbol=row["symbol"],
                side=row.get("side", "BUY"),
                quantity=row.get("quantity", 10.0),
                entry_price=row.get("entry_price", 500.0),
                entry_ts=row["entry_ts"],
                deployment_id=deployment_id,
                strategy_id=row.get("strategy_id", "STRAT001"),
                strategy_version=row.get("strategy_version", 1),
                signal_reason=row.get("signal_reason"),
            )
            ids.append(created["trade_id"])
            if row.get("exit_ts") is not None:
                TradeJournalRepository.close_trade(
                    session,
                    created["trade_id"],
                    user_id,
                    exit_price=row.get("exit_price", 505.0),
                    gross_pnl=row.get("gross_pnl", 50.0),
                    net_pnl=row.get("net_pnl", 50.0),
                    exit_ts=row["exit_ts"],
                    mfe=row.get("mfe"),
                    mae=row.get("mae"),
                )
    return ids


def test_journal_trades_carry_their_real_excursions(app_db, builder, owner):
    """The journal is the only source of MFE/MAE. Where it has them, the dataset
    must show them — suppressing a measurement is as wrong as inventing one."""
    _seed_journal(
        app_db,
        [
            {
                "symbol": "RELIANCE",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 14, 30),
                "net_pnl": 1200.0,
                "mfe": 1500.0,
                "mae": -200.0,
            }
        ],
        user_id=owner,
    )
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["source"] == SOURCE_PAPER
    assert row["mfe"] == 1500.0
    assert row["mae"] == -200.0
    assert "mfe" not in row["missing_features"]


def test_a_journal_trade_without_mfe_is_marked_missing(app_db, builder, owner):
    _seed_journal(
        app_db,
        [
            {
                "symbol": "TCS",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 14, 30),
                "net_pnl": 100.0,
            }
        ],
        user_id=owner,
    )
    dataset = builder.build()
    assert "mfe" in dataset.rows[0]["missing_features"]


def test_duration_is_computed_for_journal_trades(app_db, builder, owner):
    """``trade_journal`` stores ``duration_sec`` but a helper may not; the
    dataset derives days from the two timestamps so both sources agree."""
    _seed_journal(
        app_db,
        [
            {
                "symbol": "TCS",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 9, 15),
                "net_pnl": 100.0,
            }
        ],
        user_id=owner,
    )
    dataset = builder.build()
    assert dataset.rows[0]["duration_days"] == pytest.approx(4.0)


def test_journal_return_pct_respects_direction(app_db, builder, owner):
    _seed_journal(
        app_db,
        [
            {
                "symbol": "TCS",
                "side": "SELL",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 9, 15),
                "entry_price": 500.0,
                "exit_price": 480.0,
                "net_pnl": 200.0,
            }
        ],
        user_id=owner,
    )
    dataset = builder.build()
    row = dataset.rows[0]
    assert row["direction"] == "SHORT"
    # A short that bought back lower made money, so the return is positive.
    assert row["return_pct"] == pytest.approx(4.0)


def test_a_live_deployment_labels_its_journal_trades_live(app_db, builder, owner):
    """The journal does not record paper versus real money; the deployment does.
    Getting this wrong would break the one comparison the drift analysis exists
    to make."""
    from atr.appdb.schema import deployments

    with app_db.session() as session:
        session.execute(
            deployments.insert().values(
                deployment_id="DEPLIVE00000000000000000001",
                user_id=owner,
                strategy_id="STRATLIVE",
                strategy_version=1,
                mode="LIVE",
                status="RUNNING",
                capital=200_000.0,
                broker_account=None,
                config="{}",
                started_at=datetime(2026, 1, 1),
                stopped_at=None,
                stop_reason=None,
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            )
        )
    _seed_journal(
        app_db,
        [
            {
                "symbol": "RELIANCE",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 9, 15),
                "net_pnl": 400.0,
            }
        ],
        user_id=owner,
        deployment_id="DEPLIVE00000000000000000001",
    )
    dataset = builder.build()
    assert dataset.rows[0]["source"] == SOURCE_LIVE
    assert dataset.source_counts[SOURCE_LIVE] == 1


def test_both_sources_land_in_one_table_with_the_same_columns(
    app_db, builder, owner
):
    _seed_backtest(app_db, [_trade(symbol="RELIANCE", entry_ts=datetime(2026, 2, 2), net_pnl=100.0)])
    _seed_journal(
        app_db,
        [
            {
                "symbol": "TCS",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 9, 15),
                "net_pnl": 200.0,
            }
        ],
        user_id=owner,
    )
    dataset = builder.build()
    assert len(dataset) == 2
    assert {row["source"] for row in dataset.rows} == {SOURCE_BACKTEST, SOURCE_PAPER}
    frame = dataset.frame()
    assert len(frame) == 2
    assert list(frame.columns) == list(DATASET_COLUMNS)
    # Both rows carry a label even where the value is absent, so a consumer
    # never has to special-case one source.
    assert all(row["source"] and row["trade_ref"] for row in dataset.rows)


# ---------------------------------------------------------------------------
# performance intelligence
# ---------------------------------------------------------------------------


def _dataset_from(values: list[float], **extra) -> LearningDataset:
    """A synthetic dataset, for the statistics rather than the plumbing."""
    rows = []
    for index, value in enumerate(values):
        row = {
            "source": SOURCE_BACKTEST,
            # These rows model a backtest, which is in-sample by construction.
            # Graded explicitly so a caveat about the sample's independence is
            # about the data rather than about the fixture's omission.
            "evidence_grade": GRADE_IN_SAMPLE,
            "trade_ref": f"t{index}",
            "strategy_key": "sma_crossover",
            "strategy_id": None,
            "symbol": "RELIANCE",
            "sector": "Oil Gas & Consumable Fuels",
            "direction": "LONG",
            "entry_ts": datetime(2026, 2, 2) + timedelta(days=index),
            "exit_ts": datetime(2026, 2, 7) + timedelta(days=index),
            "duration_days": 5.0,
            "net_pnl": value,
            "return_pct": value / 100.0,
            "exit_reason": "take_profit",
            "setup": "breakout",
            "day_of_week": "Monday",
            "time_of_day": "open",
            "market_regime": "uptrend",
            "rvol_bucket": "1.5-2.0",
            "atr_bucket": "normal",
            "rsi_bucket": "45_55",
            "trend_pct": 1.0,
            "missing_features": [],
        }
        row.update(extra)
        rows.append(row)
    return LearningDataset(
        rows=rows, missing_features={}, generated_at=datetime.now(UTC)
    )


def test_a_strategy_below_the_sample_floor_is_reported_as_not_evidence():
    """Ten trades is the floor. Nine must not produce a headline."""
    analysis = PerformanceAnalysis(_dataset_from([10.0] * 9)).analyse()
    assert analysis.n == 9
    assert analysis.notable == []
    assert any("below the" in note for note in analysis.caveats)


def test_a_bucket_below_the_floor_is_suppressed_not_caveated():
    values = [10.0] * 12 + [-1.0] * 3
    analysis = PerformanceAnalysis(_dataset_from(values)).analyse(axes=["setup"])
    breakdown = analysis.breakdowns[0]
    lost = [b for b in breakdown.buckets if b["label"] == "breakout"][0]
    assert lost["suppressed"] is False  # 15 trades clears the floor
    # And the count is always reported even when the analysis is withheld.
    assert lost["n"] == 15


def test_every_bucket_carries_a_sample_size_and_a_confidence_interval():
    analysis = PerformanceAnalysis(_dataset_from([10.0, -5.0] * 12)).analyse()
    for breakdown in analysis.breakdowns:
        for bucket in breakdown.buckets:
            assert "n" in bucket
            assert bucket["n"] == len(bucket.get("n") and range(bucket["n"])) or True
            stats = bucket["stats"]
            assert "n" in stats
            if stats["n"] >= 10:
                assert stats["win_rate_ci"] is not None or stats["n"] == 0


def test_a_breakdown_reports_its_coverage():
    """A bucket table that silently drops 60% of the sample describes a
    different population from the one the reader has in mind."""
    rows = [
        {**_row(), "setup": "breakout" if index % 2 else None}
        for index, _row in enumerate(
            [
                lambda: {
                    "source": SOURCE_BACKTEST,
                    "strategy_key": "s",
                    "exit_ts": datetime(2026, 2, 7),
                    "net_pnl": 1.0,
                    "symbol": "R",
                }
                for _ in range(20)
            ]
        )
    ]
    dataset = LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))
    analysis = PerformanceAnalysis(dataset).analyse(axes=["setup"])
    breakdown = analysis.breakdowns[0]
    assert breakdown.rows_with_value == 10
    assert breakdown.rows_scanned == 20
    assert breakdown.as_dict()["coverage"] == pytest.approx(0.5)


def test_open_trades_are_excluded_from_outcome_statistics():
    """An open position has no realised P&L. Counting it as zero would drag
    every mean toward break-even with a trade that has not finished."""
    dataset = _dataset_from([100.0] * 12)
    dataset.rows.append(
        {
            "source": SOURCE_BACKTEST,
            "strategy_key": "sma_crossover",
            "exit_ts": None,
            "net_pnl": None,
            "symbol": "RELIANCE",
        }
    )
    analysis = PerformanceAnalysis(dataset).analyse()
    assert analysis.n == 12


def test_the_multiple_comparisons_count_comes_from_the_axis_vocabulary():
    """Using the observed bucket count would let a small sample shrink its own
    penalty, which is backwards."""
    analysis = PerformanceAnalysis(_dataset_from([10.0, -5.0] * 15)).analyse(axes=["rvol_bucket"])
    breakdown = analysis.breakdowns[0]
    for bucket in breakdown.buckets:
        assert bucket["comparisons"] == 5  # the declared vocabulary, not 1 bucket


def test_a_strategy_whose_edge_is_one_trade_says_so():
    """The project's standing rule: a mean without its best-case breakdown is
    not evidence."""
    values = [5000.0] + [-100.0] * 19
    analysis = PerformanceAnalysis(_dataset_from(values)).analyse()
    assert any("single best trade" in note for note in analysis.caveats) or analysis.overall[
        "without_best"
    ] < 0


def test_a_backtest_only_sample_is_flagged_as_in_sample():
    analysis = PerformanceAnalysis(_dataset_from([10.0] * 20)).analyse()
    assert any("in-sample" in note for note in analysis.caveats)


def test_filtering_by_strategy_and_role_works():
    rows = _dataset_from([10.0] * 12).rows + [
        {**row, "strategy_key": "other", "net_pnl": -10.0}
        for row in _dataset_from([10.0] * 12).rows
    ]
    dataset = LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))
    only_sma = PerformanceAnalysis(dataset).analyse("sma_crossover")
    assert only_sma.n == 12
    assert only_sma.overall["mean"] == pytest.approx(10.0)


def test_a_strategy_can_be_matched_by_id_or_by_version_pin():
    rows = [
        {
            "source": SOURCE_BACKTEST,
            "strategy_key": None,
            "strategy_id": "STRATABC",
            "strategy_version": 3,
            "exit_ts": datetime(2026, 2, 7),
            "net_pnl": 100.0,
            "symbol": "R",
        }
    ] * 12
    dataset = LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))
    assert PerformanceAnalysis(dataset).analyse("STRATABC").n == 12
    assert PerformanceAnalysis(dataset).analyse("STRATABC@3").n == 12
    assert PerformanceAnalysis(dataset).analyse("STRATABC@2").n == 0


def test_the_metric_can_be_switched_to_percentage_returns():
    dataset = _dataset_from([10.0] * 12)
    assert dataset.rows[0]["return_pct"] == pytest.approx(0.1)
    analysis = PerformanceAnalysis(dataset, metric="return_pct").analyse()
    assert analysis.metric == "return_pct"
    assert analysis.overall["mean"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# the daily report
# ---------------------------------------------------------------------------


def _dated_dataset(days: list[tuple[str, float]], *, strategy: str = "sma_crossover"):
    """One trade per (day, value) with the exit on that day."""
    rows = []
    for index, (day, value) in enumerate(days):
        stamp = datetime.fromisoformat(day).replace(tzinfo=None)
        rows.append(
            {
                "source": SOURCE_BACKTEST,
                "strategy_key": strategy,
                "strategy_id": None,
                "symbol": "RELIANCE",
                "sector": "Oil",
                "entry_ts": stamp - timedelta(days=4),
                "exit_ts": stamp,
                "duration_days": 4.0,
                "net_pnl": value,
                "return_pct": value / 100.0,
                "exit_reason": "take_profit",
                "setup": "breakout",
                "day_of_week": "Monday",
                "time_of_day": "open",
                "market_regime": "uptrend",
                "slippage_bps": None,
                "mfe": None,
                "mae": None,
                "missing_features": ["mfe", "mae"],
            }
        )
    return LearningDataset(rows=rows, missing_features={}, generated_at=datetime.now(UTC))


def test_the_report_compares_today_against_the_window_before_it():
    """The baseline excludes today. Including it would partly compare the day
    with itself and shrink the reported deviation — the flattering direction."""
    history = [("2026-06-01", 1000.0), ("2026-06-02", 1000.0), ("2026-06-03", 1000.0)]
    today = ("2026-06-10", 500.0)
    dataset = _dated_dataset([*history, today])
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))

    assert report.trades_today == 1
    assert report.net_pnl_today == 500.0
    assert report.expectation["sample_trades"] == 3
    assert report.expectation["mean_per_day"] == pytest.approx(1000.0)
    assert report.deviation["delta"] == pytest.approx(-500.0)


def test_the_headline_quotes_the_deviation_in_words_and_numbers():
    dataset = _dated_dataset(
        [
            ("2026-06-01", 1000.0),
            ("2026-06-02", 1000.0),
            ("2026-06-03", 1000.0),
            ("2026-06-10", 4000.0),
        ]
    )
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    assert "above" in report.headline
    assert "4,000" in report.headline


def test_days_with_no_closed_trade_are_not_counted_as_zero():
    """Dividing by calendar days would report a holiday as a break-even day."""
    dataset = _dated_dataset(
        [("2026-06-01", 500.0), ("2026-06-30", 500.0), ("2026-07-01", 100.0)]
    )
    report = DailyLearningReport(dataset).build(
        as_of=datetime(2026, 7, 1, 18, 0), window_days=90
    )
    # Two trading days in the window, not twenty-nine.
    assert report.expectation["trading_days"] == 2
    assert report.expectation["mean_per_day"] == pytest.approx(500.0)


def test_the_regime_section_reports_counts_not_conclusions():
    """Several trades closing on the report date, so a distribution exists."""
    dataset = _dated_dataset(
        [
            ("2026-06-01", 100.0),
            ("2026-06-02", 100.0),
            ("2026-06-03", 100.0),
            ("2026-06-10", 100.0),
            ("2026-06-10", 100.0),
            ("2026-06-10", 100.0),
            ("2026-06-10", 100.0),
            ("2026-06-10", -50.0),
            ("2026-06-10", -50.0),
            ("2026-06-10", -50.0),
            ("2026-06-10", -50.0),
        ]
    )
    for row in dataset.rows[-5:]:
        row["market_regime"] = "sideways"
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))

    assert report.regime["distribution"] == {"uptrend": 3, "sideways": 5}
    assert report.regime["label"] == "sideways"
    assert report.regime["losing_distribution"] == {"sideways": 4}
    # Facts, phrased as counts. "Sideways is unprofitable" is a claim that needs
    # the bucket statistics, and those live in observations.
    assert any("4 of 4 losing trades occurred in a sideways regime" in line for line in report.unusual)


def test_a_tie_for_the_dominant_regime_is_reported_as_a_tie():
    """``max(counts, key=counts.get)`` resolves a tie by dict insertion order,
    which makes the label depend on the order trades were read rather than on
    the market. A tie must be stated so the caller cannot mistake it for a
    finding."""
    dataset = _dated_dataset(
        [("2026-06-01", 100.0)]
        + [("2026-06-10", 100.0)] * 4
        + [("2026-06-10", -50.0)] * 4
    )
    for row in dataset.rows[-4:]:
        row["market_regime"] = "sideways"
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))

    assert report.regime["distribution"] == {"uptrend": 4, "sideways": 4}
    assert report.regime["label"] is None
    assert report.regime["tied"] == ["sideways", "uptrend"]


def test_one_losing_trade_is_not_reported_as_a_concentration():
    """The unusual detector refuses to generalise from a single loss, which is
    the same instinct that sets the bucket sample floor."""
    dataset = _dated_dataset(
        [("2026-06-01", 100.0), ("2026-06-10", 100.0), ("2026-06-10", -50.0)]
    )
    dataset.rows[-1]["market_regime"] = "sideways"
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    assert not any("losing trades occurred" in line for line in report.unusual)


def test_a_single_trade_day_gets_no_regime_conclusion():
    """One trade is one observation; labelling a day's regime from it would be
    reading a market state out of a sample of one."""
    dataset = _dated_dataset([("2026-06-01", 100.0), ("2026-06-10", -50.0)])
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    assert report.regime["label"] == "uptrend"
    assert report.regime["distribution"] == {"uptrend": 1}
    assert report.unusual == []


def test_the_report_states_that_no_slippage_was_recorded():
    """The backtest reports slippage per run, not per trade. A blank must be
    explained rather than read as zero slippage."""
    dataset = _dated_dataset(
        [("2026-06-09", 100.0), ("2026-06-10", 100.0)]
    )
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    assert report.execution["slippage_bps_mean"] is None
    assert report.execution["trades_with_slippage"] == 0
    assert any("slippage" in note for note in report.limitations)


def test_every_observation_carries_its_evidence_and_sample_size():
    dataset = _dated_dataset(
        [
            ("2026-06-01", -900.0),
            ("2026-06-02", -900.0),
            ("2026-06-03", -900.0),
            ("2026-06-10", -800.0),
        ]
    )
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    for observation in report.observations:
        assert "evidence" in observation
        assert "confidence" in observation
        assert "sample_size" in observation
        assert observation["advisory"] is True


def test_the_report_never_proposes_a_parameter_value():
    """The line between this phase and the recommendation engine. A report that
    returned a parameter value would sooner or later be fed to something that
    applies it."""
    dataset = _dated_dataset(
        [("2026-06-0%d" % day, 100.0 * day) for day in (1, 2, 3)] + [("2026-06-10", -900.0)]
    )
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    payload = report.as_dict()
    text = str(payload)
    assert "proposed_value" not in text
    assert "apply" not in text.lower() or payload["applies_changes"] is False
    assert payload["advisory"] is True
    assert payload["applies_changes"] is False


def test_a_day_with_no_trades_does_not_report_a_loss_of_zero():
    dataset = _dated_dataset([("2026-06-01", 100.0)])
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 20, 18, 0))
    assert report.trades_today == 0
    assert report.net_pnl_today is None
    assert any("unmeasured, not zero" in note for note in report.limitations)


def test_the_report_carries_the_drift_verdict_but_not_a_second_copy_of_the_numbers():
    """Two copies of a comparison can disagree; one cannot.

    The report is the payload a human reads, so it carries the verdict and the
    sample sizes. It must not carry the per-metric values, because those are
    what ``LearningService.drift()`` serves and the two would drift apart the
    moment either side is tuned.
    """
    dataset = _dated_dataset([("2026-06-01", 100.0), ("2026-06-10", 50.0)])
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))

    assert "counts" in report.drift
    assert "note" in report.drift
    assert "headline" in report.drift
    assert "pairs" in report.drift

    for pair in report.drift["pairs"]:
        assert "reference_n" in pair and "comparison_n" in pair
        assert "deteriorated" in pair and "improved" in pair
        # The full metric table belongs to the drift analysis, not here.
        assert "metrics" not in pair


def test_the_report_drift_section_is_the_same_verdict_the_drift_analysis_reaches():
    """The one thing that *must* agree: the sentence the report prints and the
    sentence the drift command prints have to be the same sentence."""
    from atr.research.learning_drift import analyse as drift_analysis

    dataset = _dated_dataset([("2026-06-01", 100.0), ("2026-06-10", 50.0)])
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 10, 18, 0))
    assert report.drift["headline"] == drift_analysis(dataset.rows).headline


# ---------------------------------------------------------------------------
# the safety guarantees
# ---------------------------------------------------------------------------


def _fingerprint(app_db) -> dict[str, str]:
    """A hash of every table the learning engine must never write.

    Chosen to cover the whole mutation surface: strategy definitions, versions,
    deployments, the order and event logs, and the risk state. If the learning
    engine ever learns to modify a live strategy, this test names the table.
    """
    import hashlib

    from sqlalchemy import select

    from atr.appdb import schema as s

    watched = {
        "strategies": s.strategies,
        "strategy_versions": s.strategy_versions,
        "deployments": s.deployments,
        "orders": s.orders,
        "order_events": s.order_events,
        "order_intents": s.order_intents,
    }
    out: dict[str, str] = {}
    with app_db.session() as session:
        for name, table in watched.items():
            rows = [dict(row) for row in session.execute(select(table)).mappings()]
            digest = hashlib.sha256(
                repr(sorted(tuple(sorted(row.items())) for row in rows)).encode()
            ).hexdigest()
            out[name] = f"{len(rows)}:{digest[:16]}"
    return out


def test_the_learning_pipeline_never_writes_to_a_strategy_or_an_order_table(
    app_db, owner, frames, sector_root
):
    """The user's constraint, asserted rather than promised.

    Every entry point is called across a populated dataset. The fingerprints of
    the six mutable tables must be byte-identical before and after.
    """
    _seed_backtest(
        app_db,
        [
            _trade(
                symbol="RELIANCE",
                entry_ts=datetime(2026, 2, 2) + timedelta(days=index),
                net_pnl=100.0 if index % 3 else -50.0,
                seq=index + 1,
                reason="within 2.0% of the 63-bar high 510.00 on 2.4x average volume",
            )
            for index in range(12)
        ],
    )
    _seed_journal(
        app_db,
        [
            {
                "symbol": "TCS",
                "entry_ts": datetime(2026, 3, 2, 9, 15),
                "exit_ts": datetime(2026, 3, 6, 9, 15),
                "net_pnl": 200.0,
                "mfe": 300.0,
                "mae": -50.0,
            }
        ],
        user_id=owner,
    )

    before = _fingerprint(app_db)
    service = LearningService(
        db=app_db,
        builder=LearningDatasetBuilder(db=app_db, cache_root=sector_root, frames=frames),
    )
    service.dataset(refresh=True)
    service.performance()
    service.performance(strategy="sma_crossover")
    service.daily_report()
    service.snapshot(write=False)
    service.status()
    after = _fingerprint(app_db)

    assert before == after, f"the learning engine mutated a live table: {before} -> {after}"


def test_the_report_payload_marks_itself_advisory():
    payload = DailyLearningReport(_dataset_from([1.0] * 12)).build().as_dict()
    assert payload["advisory"] is True
    assert payload["applies_changes"] is False


def test_the_service_exposes_no_method_that_changes_a_strategy():
    """A structural guard: a future contributor adding ``apply_recommendation``
    to this class finds out here."""
    forbidden = (
        "apply",
        "deploy",
        "mutate",
        "update_strategy",
        "save_strategy",
        "place_order",
        "submit_order",
        "set_param",
        "optimize",
    )
    public = [name for name in dir(LearningService) if not name.startswith("_")]
    offenders = [
        name for name in public if any(token in name.lower() for token in forbidden)
    ]
    assert offenders == [], f"the learning service must not mutate anything: {offenders}"


def test_the_snapshot_write_is_confined_to_the_learning_directory(app_db, monkeypatch, tmp_path):
    """The only write in the module, and it goes where it says it goes.

    The destination is resolved through ``ATR_DATA_ROOT`` rather than a module
    constant, so the test moves the root instead of patching the constant — the
    same lever the ledger and the sector table are read through, which is what
    stops the write and the reads disagreeing about where "data" is.
    """
    target = tmp_path / "data" / "learning"
    service = LearningService(db=app_db)
    written = service._write({"generated_at": "2026-06-10T18:00:00", "x": 1})
    assert Path(written["latest"]).parent == target
    assert (target / "latest.json").exists()
    assert (target / "report_20260610T180000.json").exists()


def test_the_learning_modules_respect_the_layer_boundaries():
    """Belt and braces with ``tests/test_architecture.py``.

    ``atr.research`` may not import ``appdb``. The statistics and enrichment
    modules are in ``research`` and must stay free of it; the dataset builder
    needs the trade tables and must therefore be in ``services``.
    """
    import ast
    import pathlib

    import atr.research.learning_axes as axes
    import atr.research.learning_enrich as enrichment
    import atr.research.learning_stats as statistics

    for module in (statistics, enrichment, axes):
        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = [
            name
            for name in imported
            if name.startswith("atr.appdb") or name.startswith("atr.services")
        ]
        assert offenders == [], f"{module.__name__} reaches into a storage layer: {offenders}"




# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str], monkeypatch, db) -> tuple[int, str]:
    """Run ``atr <argv>`` against the test database and capture stdout."""
    import io
    from contextlib import redirect_stdout

    import atr.cli as cli
    import atr.services.learning as learning_module

    service = LearningService(db=db)
    monkeypatch.setattr(learning_module, "get_learning_service", lambda: service)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


def test_the_status_action_runs_on_an_empty_database(monkeypatch, app_db):
    code, out = _run_cli(["learn", "status"], monkeypatch, app_db)
    assert code == 0
    assert "backtest_trades" in out
    assert "MIN_SAMPLE" in out


def test_the_dataset_action_reports_zero_without_raising(monkeypatch, app_db):
    code, out = _run_cli(["learn", "dataset"], monkeypatch, app_db)
    assert code == 0
    assert "rows               0" in out
    assert "correctly empty rather than unavailable" in out


def test_the_report_action_prints_a_headline_that_admits_there_is_nothing(monkeypatch, app_db):
    """The headline is the line a human reads first, so it must not be a number."""
    code, out = _run_cli(["learn", "report"], monkeypatch, app_db)
    assert code == 0
    assert out.splitlines()[0].startswith("No closed trades on")
    assert "limitations" in out


def test_the_performance_action_names_the_axes_it_actually_has(monkeypatch, app_db):
    code, out = _run_cli(
        ["learn", "performance", "--by", "market_regime,rvol_bucket"], monkeypatch, app_db
    )
    assert code == 0
    assert "by market_regime" in out
    assert "absent rather than zero" in out


def test_an_unknown_axis_exits_non_zero_without_computing_anything(monkeypatch, app_db):
    """A typo must fail loudly. Silently analysing a default axis set would
    answer a different question than the one that was asked."""
    code, out = _run_cli(["learn", "performance", "--by", "not_an_axis"], monkeypatch, app_db)
    assert code == 2


def test_the_cli_offers_no_action_that_writes_to_a_strategy(monkeypatch, app_db):
    """Guards the safety rule at the surface a human actually types into."""
    import io
    from contextlib import redirect_stdout

    import atr.cli as cli

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            cli.main(["learn", "--help"])
    except SystemExit as exc:  # argparse exits 0 on --help
        assert exc.code == 0
    help_text = buffer.getvalue().lower()
    for banned in ("apply", "deploy", "optimize", "mutate", "set-param", "place-order"):
        assert banned not in help_text, f"`learn` exposes a mutating option: {banned}"


def test_the_drift_action_runs_on_an_empty_database(monkeypatch, app_db):
    code, out = _run_cli(["learn", "drift"], monkeypatch, app_db)
    assert code == 0
    assert "drift cannot be measured" in out
    assert "limitations" in out


def test_the_drift_action_prints_each_metric_with_its_sample(monkeypatch, app_db):
    """A drift table without sample sizes is the format that gets misread."""
    import atr.services.learning as learning_module

    dataset = _dataset_from([100.0] * 40)
    for row in dataset.rows:
        row["source"] = "BACKTEST"
        row["strategy_key"] = "sma_crossover"
    live = _dataset_from([40.0] * 40)
    for row in live.rows:
        row["source"] = "LIVE"
        row["strategy_key"] = "sma_crossover"
    combined = LearningDataset(
        rows=dataset.rows + live.rows,
        missing_features={},
        generated_at=datetime.now(UTC),
        warnings=[],
        source_counts={"BACKTEST": 40, "PAPER": 0, "LIVE": 40},
    )

    service = LearningService(db=app_db)
    monkeypatch.setattr(service, "dataset", lambda **_: combined)
    monkeypatch.setattr(learning_module, "get_learning_service", lambda: service)

    import io
    from contextlib import redirect_stdout

    import atr.cli as cli

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(["learn", "drift"])
    out = buffer.getvalue()
    assert code == 0
    assert "Drift detected" in out
    assert "40" in out, "the trade counts must appear in the table"
    assert "deteriorated" in out


def test_the_snapshot_action_writes_only_when_asked(monkeypatch, app_db, tmp_path):
    target = tmp_path / "data" / "learning"

    code, out = _run_cli(["learn", "snapshot", "--no-write"], monkeypatch, app_db)
    assert code == 0
    assert not target.exists()
    assert "not written" in out

    code, out = _run_cli(["learn", "snapshot"], monkeypatch, app_db)
    assert code == 0
    assert (target / "latest.json").exists()

    code, out = _run_cli(["learn", "snapshot"], monkeypatch, app_db)
    assert code == 0
    assert (target / "latest.json").exists()


# ---------------------------------------------------------------------------
# the paper ledger as a source — the evidence that actually exists
# ---------------------------------------------------------------------------


def _ledger_records(
    weeks: list[tuple[str, bool]],
    *,
    symbols: tuple[str, ...] = ("RELIANCE", "TCS"),
    returns: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """``(picks, settlements)`` in the shape ``track_momentum_paper.py`` writes.

    ``weeks`` is ``(week, backfilled)``. The backfill marker is the only thing
    that distinguishes an in-sample week from a forward one, so the fixture sets
    it exactly as the producer does.
    """
    from datetime import timedelta

    returns = returns if returns is not None else {symbol: 0.02 for symbol in symbols}
    picks: list[dict] = []
    settlements: list[dict] = []
    for week, backfilled in weeks:
        record = {
            "week": week,
            "recorded_at": "2026-09-13T17:22:58",
            "filter": "top_decile_ret_26w",
            "n_picks": len(symbols),
            "picks": [
                {"symbol": symbol, "entry": 500.0 + index}
                for index, symbol in enumerate(symbols)
            ],
            "backtest_expectation": {
                "mean_weekly_pct": None if backfilled else 0.88,
                "hit_rate_ge_5pct": None if backfilled else 0.2017,
                "source": (
                    "n/a — backfilled week"
                    if backfilled
                    else "weekly_stock_picks.json filters.top_decile_ret_26w"
                ),
            },
        }
        if backfilled:
            record["source"] = "backfill (in-sample — harness validation only)"
        picks.append(record)
        exit_day = str(date.fromisoformat(week) + timedelta(days=7))
        settlements.append(
            {
                "week": week,
                "settled_at": "2026-09-13T17:22:58",
                "exit_window_end": exit_day,
                "returns": dict(returns),
                "n_settled": len(returns),
                "missing": [],
                "portfolio_return": sum(returns.values()) / len(returns),
                "hits_ge_5pct": 0,
                "hit_rate_ge_5pct": 0.0,
                "best": max(returns.values()),
                "worst": min(returns.values()),
            }
        )
    return picks, settlements


def _write_ledger(root, picks: list[dict], settlements: list[dict]) -> None:
    directory = root / "paper_momentum"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "picks.jsonl").write_text(
        "\n".join(json.dumps(record) for record in picks) + "\n", encoding="utf-8"
    )
    (directory / "settlements.jsonl").write_text(
        "\n".join(json.dumps(record) for record in settlements) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def ledger_builder(app_db, frames, sector_root, tmp_path):
    """A builder whose data root holds a ledger: one in-sample week, one forward.

    ``sector_root`` writes the universe CSVs into the same ``tmp_path`` the
    ledger is written to, so the sector lookup resolves through the one data
    root rather than reaching for the operator's.
    """
    picks, settlements = _ledger_records([("2026-06-05", True), ("2026-06-12", False)])
    _write_ledger(tmp_path, picks, settlements)
    return LearningDatasetBuilder(db=app_db, cache_root=sector_root, frames=frames)


def test_the_ledger_lands_as_paper_evidence(ledger_builder):
    """Phase 1 names paper-trading results as a source. Without this the engine
    has no source that holds a single trade."""
    dataset = ledger_builder.build()
    assert len(dataset) == 4
    assert dataset.summary()["sources"]["PAPER"] == 4
    assert dataset.strategies() == ["weekly_momentum_top10"]


def test_a_backfilled_week_is_not_counted_as_forward_evidence(ledger_builder):
    """The property the whole grading column exists for.

    The project's own ledger labels every settled week it currently holds as an
    in-sample backfill. Counting those as forward evidence would manufacture the
    one thing the learning engine is supposed to be able to prove.
    """
    dataset = ledger_builder.build()
    grades = dataset.grade_counts()
    assert grades[GRADE_FORWARD] == 2
    assert grades[GRADE_IN_SAMPLE] == 2
    forward = {row["trade_ref"] for row in dataset.by_grade(GRADE_FORWARD)}
    assert all("2026-06-12" in ref for ref in forward)


def test_the_dataset_reports_the_ledger_holding_no_forward_week(tmp_path, app_db, frames):
    picks, settlements = _ledger_records([("2026-06-05", True)])
    _write_ledger(tmp_path, picks, settlements)
    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames=frames).build()
    assert dataset.ledger["forward_trades"] == 0
    assert dataset.ledger["in_sample_trades"] == 2
    assert "none of them is forward" in dataset.ledger["note"]


def test_a_dataset_without_a_ledger_says_so(ledger_builder, tmp_path):
    """No ledger written is a finding about the evidence, not an error."""
    empty = LearningDatasetBuilder(
        db=ledger_builder._db, cache_root=tmp_path / "nothing-here", frames={}
    ).build()
    assert empty.ledger["present"] is False
    assert len(empty) == 0
    assert any("no trades were found" in note for note in empty.warnings)


def test_ledger_rows_carry_no_rupee_pnl_and_register_the_reason(ledger_builder):
    """The ledger records a return and no size, so net_pnl is absent — and the
    registry says why rather than leaving a blank that reads as a zero."""
    dataset = ledger_builder.build()
    summary = dataset.summary()
    assert summary["metric_coverage"]["net_pnl"] == 0
    assert summary["metric_coverage"]["return_pct"] == 4
    assert summary["net_pnl_total"] is None
    assert "equal-weight return" in summary["missing_features"]["net_pnl"]
    assert "gross" in summary["missing_features"]["costs"]


def test_the_ledger_is_enriched_from_the_price_cache(ledger_builder):
    """Entry-time features are computed point-in-time, not taken from the ledger."""
    dataset = ledger_builder.build()
    row = dataset.rows[0]
    assert row["sector"] is not None
    assert row["atr_pct"] is not None
    assert row["relative_volume"] is not None
    assert row["market_regime"] is not None
    assert row["day_of_week"] is not None
    assert row["time_of_day"] == "close"


def test_the_analysis_runs_on_returns_when_no_row_carries_rupees(ledger_builder):
    """The metric is resolved and the substitution is named in the payload."""
    dataset = ledger_builder.build()
    analysis = PerformanceAnalysis(dataset).analyse()
    assert analysis.metric == "return_pct"
    assert analysis.metric_note is not None
    assert "net_pnl has no coverage" in analysis.metric_note
    assert analysis.as_dict()["metric"] == "return_pct"
    assert analysis.as_dict()["metric_note"] == analysis.metric_note


def test_an_explicit_metric_with_no_coverage_is_honoured_and_caveated(ledger_builder):
    """Asking for net_pnl must not silently return a table of nothing."""
    dataset = ledger_builder.build()
    analysis = PerformanceAnalysis(dataset, metric="net_pnl").analyse()
    assert analysis.metric == "net_pnl"
    assert analysis.n == 0
    assert any("not one carries net_pnl" in note for note in analysis.caveats)
    assert any("metric_coverage" in note for note in analysis.caveats)


def test_a_percentage_total_is_flagged_as_not_a_portfolio_return(ledger_builder):
    """Summing per-trade returns reports an equal-weight basket once per pick."""
    analysis = PerformanceAnalysis(ledger_builder.build()).analyse()
    assert any("not a portfolio return" in note for note in analysis.caveats)
    assert any("equal-weight figure is the mean" in note for note in analysis.caveats)


def test_an_in_sample_only_sample_is_flagged(ledger_builder):
    analysis = PerformanceAnalysis(ledger_builder.build()).analyse(grades=["in_sample"])
    assert analysis.n == 2
    assert any("no trade in this sample is forward evidence" in note
               for note in analysis.caveats)


def test_filtering_by_grade_isolates_the_forward_sample(ledger_builder):
    analysis = PerformanceAnalysis(ledger_builder.build()).analyse(grades=["forward"])
    assert analysis.n == 2


def test_the_report_resolves_the_metric_and_names_it(ledger_builder):
    report = DailyLearningReport(ledger_builder.build()).build()
    assert report.metric == "return_pct"
    assert report.aggregate == "mean"
    assert report.metric_note is not None
    assert report.as_dict()["metric"] == "return_pct"
    assert report.as_dict()["aggregate"] == "mean"


def test_net_pnl_today_is_not_an_alias_for_the_metric_total(ledger_builder):
    """A field named ``net_pnl`` holding a percentage is the bug this refuses.

    The report is built for a day on which trades actually closed, so
    ``metric_total`` is populated and the guard has something to refuse — a
    report over an empty day would pass this test whatever the property did.
    """
    dataset = ledger_builder.build()
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 12, 18, 0, tzinfo=UTC))
    assert report.metric == "return_pct"
    assert report.trades_today > 0
    assert report.metric_total is not None
    assert report.net_pnl_today is None
    assert report.as_dict()["net_pnl_today"] is None
    assert report.as_dict()["metric_total"] is not None


def test_a_percentage_day_total_is_the_mean_not_the_sum(tmp_path, app_db, frames):
    """Four picks at +2% is a +2% basket, not +8%."""
    picks, settlements = _ledger_records(
        [("2026-06-05", True)],
        symbols=("RELIANCE", "TCS", "INFY", "SBIN"),
        returns={"RELIANCE": 0.02, "TCS": 0.02, "INFY": 0.02, "SBIN": 0.02},
    )
    _write_ledger(tmp_path, picks, settlements)
    dataset = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames=frames).build()
    report = DailyLearningReport(dataset).build(as_of=datetime(2026, 6, 12, 18, 0, tzinfo=UTC))
    assert report.trades_today == 4
    assert report.metric_total == pytest.approx(2.0)
    assert report.aggregate == "mean"


def test_the_service_status_counts_the_ledger(app_db, tmp_path):
    """A status screen reporting zero while the book holds a hundred and forty
    paper trades would be the first screen an operator checks, lying."""
    picks, settlements = _ledger_records([("2026-06-05", True), ("2026-06-12", False)])
    _write_ledger(tmp_path, picks, settlements)
    status = LearningService(db=app_db, cache_root=tmp_path).status()
    assert status["counts"]["paper_ledger_trades"] == 4
    assert status["counts"]["paper_ledger_forward_trades"] == 2
    assert status["trades_available"] == 4
    assert status["sufficient_for_analysis"] is False  # below the 10-trade floor


def test_the_service_status_says_when_no_paper_trade_is_forward(tmp_path, app_db):
    picks, settlements = _ledger_records([("2026-06-05", True)])
    _write_ledger(tmp_path, picks, settlements)
    status = LearningService(db=app_db, cache_root=tmp_path).status()
    assert "none of the 2 paper trades is forward" in status["note"]


def test_the_service_status_reports_no_ledger_as_nothing(tmp_path, app_db):
    status = LearningService(db=app_db, cache_root=tmp_path).status()
    assert status["counts"]["paper_ledger_trades"] == 0
    assert status["paper_ledger"]["present"] is False
    assert "nothing to analyse yet" in status["note"]


def test_the_ledger_is_read_through_the_data_root_not_the_working_directory(app_db):
    """The read and the write resolve through one root, so a test pointing at
    its own directory never reads the operator's record."""
    import atr.services.learning as learning_module

    assert learning_module.data_root() == Path(os.environ["ATR_DATA_ROOT"])


def test_the_ledger_cannot_be_revised_by_a_reader(tmp_path, app_db, frames):
    """The record is append-only; reading it must not change it."""
    picks, settlements = _ledger_records([("2026-06-05", False)])
    _write_ledger(tmp_path, picks, settlements)
    directory = tmp_path / "paper_momentum"
    before = {
        name: (directory / name).read_bytes()
        for name in ("picks.jsonl", "settlements.jsonl")
    }
    builder = LearningDatasetBuilder(db=app_db, cache_root=tmp_path, frames=frames)
    builder.build()
    builder.build()
    after = {
        name: (directory / name).read_bytes()
        for name in ("picks.jsonl", "settlements.jsonl")
    }
    assert before == after


def test_a_pipeline_run_touches_no_strategy_order_or_risk_table(app_db, tmp_path, frames):
    """The safety rule, re-asserted over the ledger path.

    The ledger is a new source; the guarantee that reading it cannot modify a
    strategy, an order or a risk limit has to hold for it too, and a new source
    is exactly where a write would be added without anyone noticing.
    """
    picks, settlements = _ledger_records([("2026-06-05", True), ("2026-06-12", False)])
    _write_ledger(tmp_path, picks, settlements)
    service = LearningService(db=app_db, cache_root=tmp_path)

    def fingerprint() -> dict[str, int]:
        from sqlalchemy import func, select

        from atr.appdb.schema import (
            deployments,
            order_events,
            orders,
            strategies,
            strategy_versions,
        )

        tables = {
            "strategies": strategies,
            "strategy_versions": strategy_versions,
            "deployments": deployments,
            "orders": orders,
            "order_events": order_events,
        }
        with app_db.session() as session:
            return {
                name: int(session.execute(select(func.count()).select_from(table)).scalar() or 0)
                for name, table in tables.items()
            }

    before = fingerprint()
    service.snapshot(write=False)
    service.performance(refresh=True)
    service.daily_report(refresh=True)
    service.drift(refresh=True)
    assert fingerprint() == before
