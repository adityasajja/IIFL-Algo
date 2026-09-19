"""The Context-Aware Signal Engine: scoring, recording, enrichment, analytics.

Three things are being pinned here, each of which would be a silent correctness
bug if it drifted:

* **Determinism.** The same context must produce the same score, or a backtest
  comparison is meaningless.
* **Look-ahead safety.** A context at time *t* must be computed only from prices
  up to *t*. The test tampers with prices *after* the signal and asserts the
  score does not move.
* **Evidence honesty.** In-sample (backtest) outcomes are never presented as
  forward evidence; statistics are suppressed until the sample supports them.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from atr.appdb.engine import utcnow
from atr.appdb.repositories import (
    BacktestArtefactRepository,
    BacktestRunRepository,
    OrderRepository,
    TradeJournalRepository,
    UserRepository,
)
from atr.signal_context.engine import SignalContextEngine
from atr.signal_context.service import SignalContextService

CRITERIA = {
    "market_bullish_trend",
    "market_breadth",
    "strong_sector",
    "stock_rs",
    "rvol_confirmation",
    "trend_confirmation",
}


def _frame(seed: int, periods: int = 300, up: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(up, 0.012, periods).cumsum()
    close = 100.0 * np.exp(steps)
    high = close * (1 + rng.uniform(0.001, 0.02, periods))
    low = close * (1 - rng.uniform(0.001, 0.02, periods))
    open_ = close * (1 + rng.normal(0, 0.004, periods))
    volume = rng.integers(100_000, 900_000, periods).astype(float)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=periods, freq="B"),
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
        }
    )


def _context(
    engine: SignalContextEngine,
    *,
    seed: int = 2,
    signal_id: str = "SIG",
    source: str = "PAPER",
    idx: int = 250,
    sector: bool = True,
    stock: pd.DataFrame | None = None,
):
    stock = stock if stock is not None else _frame(seed, up=0.002)
    bench = _frame(seed + 1)
    when = stock["ts"].iloc[idx]
    market = engine.build_market_snapshot(
        bench,
        when,
        breadth_above_ema50_pct=62.0,
        regime="BULLISH_TREND",
    )
    sector_snap = (
        engine.build_sector_snapshot("IT", when, relative_strength_1m=2.0)
        if sector
        else None
    )
    return engine.enrich(
        signal_id=signal_id,
        symbol="TCS",
        action="BUY",
        signal_source=source,
        signal_ts=str(when),
        strategy_id="strat-1",
        strategy_version=1,
        stock_frame=stock,
        bench_frame=bench,
        market_hint=market,
        sector_hint=sector_snap,
        when=when,
    )


# ---------------------------------------------------------------------------
# Engine: deterministic, bounded, honest about missing data
# ---------------------------------------------------------------------------

def test_score_is_deterministic_and_reports_every_criterion():
    engine = SignalContextEngine()
    first = _context(engine, signal_id="A")
    second = _context(engine, signal_id="A")

    assert first.context_score == second.context_score
    assert [c.as_dict() for c in first.score_breakdown] == [
        c.as_dict() for c in second.score_breakdown
    ]
    assert {c.key for c in first.score_breakdown} == CRITERIA
    assert 0 <= first.context_score <= first.max_possible_score == 100
    assert first.missing_fields == []


def test_a_missing_measurement_is_insufficient_data_not_zero():
    """A criterion that cannot be measured must not be scored as if it failed."""
    engine = SignalContextEngine()
    context = _context(engine, signal_id="A", sector=False)

    assert context.has_insufficient_data is True
    assert context.context_class.value == "INSUFFICIENT_DATA"
    assert context.missing_fields, "an unmeasurable criterion must be named"


def test_context_is_computed_only_from_prices_up_to_the_signal():
    """Tamper with post-signal prices; the score at the signal must not move."""
    engine = SignalContextEngine()
    cut = 120
    base = _frame(5, up=0.002)

    tampered = base.copy()
    tampered.loc[cut + 1 :, "close"] *= 5.0
    tampered.loc[cut + 1 :, "high"] *= 5.0
    tampered.loc[cut + 1 :, "low"] *= 5.0
    tampered.loc[cut + 1 :, "volume"] *= 10.0

    honest = _context(engine, signal_id="A", idx=cut, stock=base)
    lookahead = _context(engine, signal_id="A", idx=cut, stock=tampered)

    assert honest.context_score == lookahead.context_score
    assert honest.stock_context.close == pytest.approx(lookahead.stock_context.close)
    # And the honest value really is the price *at* the signal, not the last bar.
    assert honest.stock_context.close == pytest.approx(float(base["close"].iloc[cut]))


# ---------------------------------------------------------------------------
# Service: persistence, point-in-time enrichment, outcome resolution
# ---------------------------------------------------------------------------

def _make_user(app_db, *, username: str = "owner") -> str:
    with app_db.session() as session:
        user = UserRepository.create(
            session,
            email=f"{username}@example.com",
            username=username,
            password_hash="x",
            role="owner",
        )
    return user["user_id"]


def test_record_list_get_roundtrip(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)
    context = _context(service.engine, signal_id="TCS:2024-06-03")

    with app_db.session() as session:
        assert service.record(session, context, user_id=user_id) == 1

    listing = service.list_signals(user_id)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["signal_id"] == "TCS:2024-06-03"
    assert item["market_context"]["regime"] == "BULLISH_TREND"  # JSON decoded
    assert item["score_breakdown"]

    detail = service.get_signal(user_id, "TCS:2024-06-03")
    assert detail is not None and detail["context_model_version"]
    assert service.get_signal(user_id, "nope") is None


def test_a_repeated_signal_does_not_double_record(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)
    context = _context(service.engine, signal_id="TCS:2024-06-03")

    with app_db.session() as session:
        assert service.record(session, context, user_id=user_id) == 1
        assert service.record(session, context, user_id=user_id) == 0

    assert service.list_signals(user_id)["total"] == 1


def test_backtest_outcomes_are_in_sample_and_suppressed(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session,
            user_id=user_id,
            config={"strategy_id": "strat-1"},
            strategy_id="strat-1",
            strategy_version=1,
        )
        run_id = run["run_id"]
        BacktestArtefactRepository.save_trades(
            session,
            run_id,
            [
                {
                    "symbol": "TCS",
                    "direction": "LONG",
                    "quantity": 10,
                    "entry_ts": utcnow() - timedelta(days=5),
                    "entry_price": 100.0,
                    "exit_ts": utcnow() - timedelta(days=1),
                    "exit_price": 110.0,
                    "gross_pnl": 100.0,
                    "net_pnl": 90.0,
                    "return_pct": 10.0,
                    "strategy_id": "strat-1",
                    "strategy_version": 1,
                }
            ],
        )
        trade_id = BacktestArtefactRepository.trades(session, run_id)[0]["trade_id"]

    context = _context(service.engine, signal_id=f"{run_id}:0", source="BACKTEST")
    with app_db.session() as session:
        service.record(session, context, user_id=user_id, run_id=run_id, trade_id=trade_id)

    data = service.analytics(user_id, dimension="score_band")
    buckets = data["buckets"]
    assert sum(b["n"] for b in buckets) == 1
    assert sum(b["n_in_sample"] for b in buckets) == 1
    assert sum(b["n_forward"] for b in buckets) == 0
    # A backtest outcome is real but in-sample: shown, and labelled as such.
    populated = [b for b in buckets if b["n"] > 0]
    assert all(b["evidence_note"] == "in_sample_only" for b in populated)
    assert all(b["suppressed"] is False for b in populated)


def test_live_outcomes_resolve_to_forward_evidence(app_db):
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)
    signal_id = "TCS:2024-06-03"

    with app_db.session() as session:
        OrderRepository.create(
            session,
            user_id=user_id,
            symbol="TCS",
            side="BUY",
            quantity=10,
            strategy_id="strat-1",
            strategy_version=1,
            signal_id=signal_id,
        )
        episode = TradeJournalRepository.open_trade(
            session,
            user_id=user_id,
            symbol="TCS",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            entry_ts=utcnow(),
            strategy_id="strat-1",
        )
        TradeJournalRepository.close_trade(
            session,
            episode["trade_id"],
            user_id,
            exit_price=110.0,
            gross_pnl=100.0,
            net_pnl=90.0,
            exit_ts=utcnow(),
        )

    context = _context(service.engine, signal_id=signal_id, source="PAPER")
    with app_db.session() as session:
        service.record(session, context, user_id=user_id)

    data = service.analytics(user_id, dimension="context_class")
    buckets = data["buckets"]
    assert sum(b["n_forward"] for b in buckets) == 1
    assert sum(b["n_in_sample"] for b in buckets) == 0
    # One forward observation is real evidence but too little to publish stats.
    populated = [b for b in buckets if b["n"] > 0]
    assert all(b["evidence_note"] == "insufficient_forward_observations" for b in populated)
    assert all(b["suppressed"] for b in populated)


def test_enrich_run_computes_point_in_time_contexts(fresh_env, market_cache, app_db):
    """A completed run's trades get contexts computed from data up to each entry."""
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db, data_root=market_cache)

    daily = market_cache / "iifl_daily" / "NSEEQ"
    frame = pd.read_parquet(daily / "RELIANCE-EQ.parquet")
    entry_ts = frame["ts"].iloc[200]
    expected_close = float(frame["close"].iloc[200])

    with app_db.session() as session:
        run = BacktestRunRepository.create(
            session,
            user_id=user_id,
            config={"strategy_id": "strat-1"},
            strategy_id="strat-1",
            strategy_version=1,
        )
        run_id = run["run_id"]
        BacktestArtefactRepository.save_trades(
            session,
            run_id,
            [
                {
                    "symbol": "RELIANCE",
                    "direction": "LONG",
                    "quantity": 10,
                    "entry_ts": entry_ts,
                    "entry_price": expected_close,
                    "net_pnl": 5.0,
                    "return_pct": 5.0,
                    "strategy_id": "strat-1",
                    "strategy_version": 1,
                }
            ],
        )

    result = service.enrich_run(run_id, user_id)
    assert result["enriched"] == 1

    listing = service.list_signals(user_id, source="BACKTEST")
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["run_id"] == run_id
    assert item["trade_id"] is not None
    assert item["stock_context"]["close"] == pytest.approx(expected_close, rel=1e-6)


def test_enrichment_failure_never_propagates(app_db, monkeypatch):
    """A broken annotation is a logged miss, not an exception in the caller."""
    user_id = _make_user(app_db)
    service = SignalContextService(db=app_db)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("scan unavailable")

    monkeypatch.setattr(service.engine, "enrich", _boom)
    assert (
        service.enrich_live(
            user_id=user_id,
            symbol="TCS",
            action="BUY",
            signal_ts="2024-06-03T09:15:00",
            signal_id="TCS:2024-06-03",
        )
        is None
    )
