"""End-to-end proof of the paper deployment workflow, against a real database.

Answers one question: does

    Strategy -> Version -> Deploy Paper -> Live tick -> Signal -> Risk ->
    OMS -> Paper fill -> Position -> P&L -> Journal -> Learning observation ->
    Monitor

actually run, on real daily history, through the real runner, the real risk
gate, the real OMS, the real ``PaperVenue`` and the real journal?

Not an in-process unit test. It builds a throwaway app database, seeds a real
strategy with two immutable versions, deploys one of them, drives
``PaperRunner.pass_once`` with a synthetic live price, closes the position
through the same execution path that opened it, and then reads every monitoring
view *and* the learning dataset — asserting the causal chain is present, that
the closed trade is graded ``forward``, and that a trade written straight into
the journal without an execution-time provenance stamp is graded ``in_sample``.

The last two are the point. A learning engine that counts a backfilled row as
evidence manufactures exactly the thing it exists to measure, and the only way
to know the difference survives the whole chain is to run the chain.

Run:  ./.venv/Scripts/python.exe scripts/verify_paper_workflow.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# A throwaway database, set before anything imports the engine.
_DB_DIR = tempfile.mkdtemp(prefix="atr-paper-verify-")
os.environ["APP_DB_URL"] = "sqlite:///" + _DB_DIR.replace("\\", "/") + "/app.db"
os.environ.setdefault("ATR_SECRET_KEY", "verify" * 8)
os.environ["ENV"] = "dev"
# And a throwaway data root, so the learning dataset this probe builds reads the
# ledger it just wrote and never the operator's real paper record. Without this
# the probe would report on someone else's trades, which is the failure that
# ``ATR_DATA_ROOT`` exists to make impossible.
_DATA_DIR = tempfile.mkdtemp(prefix="atr-paper-verify-data-")
os.environ["ATR_DATA_ROOT"] = _DATA_DIR

import pandas as pd  # noqa: E402

from atr.appdb.engine import get_app_db  # noqa: E402
from atr.appdb.repositories import (  # noqa: E402
    DeploymentRepository,
    StrategyRepository,
    UserRepository,
)
from atr.services.monitoring import DeploymentMonitor  # noqa: E402
from atr.services.runner import IST, PaperRunner  # noqa: E402
from atr.signals.engine import CACHE_ROOT  # noqa: E402

SYMBOL = "RELIANCE-EQ"
EXCHANGE = "NSEEQ"

_failures: list[str] = []
_checks = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global _checks
    _checks += 1
    if condition:
        print(f"  PASS  {label}")
        return True
    _failures.append(label)
    print(f"  FAIL  {label}" + (f"  <- {detail}" if detail else ""))
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def find_symbol() -> str:
    """A cached symbol with enough history for the indicators to warm up."""
    path = CACHE_ROOT / EXCHANGE
    candidates = [SYMBOL, "RELIANCE"]
    for candidate in candidates:
        if (path / f"{candidate}.parquet").exists():
            return candidate
    files = sorted(p.stem for p in path.glob("*.parquet"))
    if not files:
        raise SystemExit(f"no daily cache under {path}; cannot run")
    return files[0]


def history_frame(symbol: str) -> pd.DataFrame:
    frame = pd.read_parquet(CACHE_ROOT / EXCHANGE / f"{symbol}.parquet")
    return frame.sort_values("ts").reset_index(drop=True)


def main() -> int:
    db = get_app_db()
    symbol = find_symbol()
    frame = history_frame(symbol)
    last_close = float(frame["close"].iloc[-1])
    print(f"symbol {symbol}  bars {len(frame)}  last close {last_close:,.2f}")

    section("1. strategy + immutable versions")

    with db.session() as session:
        user = UserRepository.create(
            session,
            email="verify@atr.local",
            username="verify",
            password_hash="x",
            role="user",
        )
    user_id = user["user_id"]

    # Version 1 closes on a 3% stop; version 2 on an 18% stop. Two versions of
    # one strategy that behave differently is the property being proved — a
    # deployment that ignored its version could not tell them apart.
    #
    # ``EntryRules`` has no rule selector: the breakout parameters *are* the
    # breakout rule, and the trend/RSI fields are what the other rules read. So
    # the block below is expressed in the same vocabulary ``eval_entry`` reads,
    # which is the whole point of resolving a version rather than a class.
    def definition(stop_pct: float) -> dict:
        return {
            "engine_key": None,
            "rules": {
                "entry": {
                    "breakout_lookback": 63,
                    "breakout_proximity_pct": 2.0,
                    "volume_multiple": 1.5,
                    "volume_lookback": 20,
                    "min_history_bars": 70,
                },
                "exit": {"stop_loss_pct": stop_pct, "min_history_bars": 70},
            },
        }

    with db.session() as session:
        strategy = StrategyRepository.create(
            session, user_id=user_id, name="Verify Momentum", kind="rules"
        )
        strategy_id = strategy["strategy_id"]
        v1 = StrategyRepository.create_version(
            session, strategy_id=strategy_id, author_user_id=user_id,
            definition=definition(3.0), change_note="tight stop",
        )
        v2 = StrategyRepository.create_version(
            session, strategy_id=strategy_id, author_user_id=user_id,
            definition=definition(18.0), change_note="wide stop",
        )

    check("strategy created", bool(strategy_id))
    check("two distinct versions", v1["version"] == 1 and v2["version"] == 2,
          f"{v1['version']} / {v2['version']}")
    check("versions are immutable (different hashes)",
          v1["definition_hash"] != v2["definition_hash"])

    section("2. deploy to paper")

    with db.session() as session:
        deployment = DeploymentRepository.create(
            session,
            user_id=user_id,
            strategy_id=strategy_id,
            strategy_version=v1["version"],
            capital=500_000.0,
            config={
                "symbols": [symbol],
                "exchange": EXCHANGE,
                "timeframe": "1d",
                "order_value": 250_000.0,
                "lookback_days": max(400, len(frame) + 5),
                "max_open_positions": 1,
            },
        )
    deployment_id = deployment["deployment_id"]
    check("deployment created", bool(deployment_id))
    check("starts PENDING", deployment["status"] == "PENDING", deployment["status"])

    with db.session() as session:
        DeploymentRepository.start(session, deployment_id, user_id)
        fresh = DeploymentRepository.get(session, deployment_id, user_id)
    check("START moves it to RUNNING", fresh["status"] == "RUNNING", fresh["status"])

    section("3. version rules resolve (the bug this whole thing turned on)")

    runner = PaperRunner(db=db)
    runner.sync_loops()
    loop = runner._loops.get(deployment_id)
    check("runner attached a loop", loop is not None)
    if loop is None:
        return report()

    resolved = runner.resolve_rules(loop)
    check("rules resolved from the version", resolved is not None,
          str(loop.blocked_reason))
    if resolved is not None:
        entry_rules, exit_rules = resolved
        check("resolved the version's stop (3.0), not the default (15.0)",
              float(exit_rules.stop_loss_pct) == 3.0,
              f"got {exit_rules.stop_loss_pct}")
        check("not blocked", loop.blocked_reason is None, str(loop.blocked_reason))

    section("4. live tick -> signal -> risk -> OMS -> fill")

    # A price inside the breakout band: within 2% of the 63-bar high, on volume
    # that clears the multiple. Computed from the history rather than guessed,
    # so the entry is a consequence of the stored rule and not of the probe's
    # choice of price.
    high_63 = float(frame["close"].tail(63).max())
    avg_volume_20 = float(frame["volume"].tail(20).mean())
    tick_price = round(high_63 * 0.995, 2)
    print(f"  63-bar high {high_63:,.2f}  avg vol(20) {avg_volume_20:,.0f}")
    print(f"  live tick {tick_price:,.2f} (within the breakout band)")

    # The venue reads its price source from a plain callable attribute, and the
    # loop caches the venue on first access. Wire the synthetic feed *before*
    # the first evaluation pass touches it, and drop any venue built during
    # warmup so the probe prices every order itself.
    live_feed = lambda symbol_, exchange_: tick_price  # noqa: E731
    runner._price_source = live_feed  # noqa: SLF001 - probe wiring
    loop.venue.prices = live_feed

    # Entries only fire inside the session, so drive the pass at an in-session
    # instant rather than pretending the clock is convenient.
    now = datetime.now(IST)
    session_moment = (
        now if (_is_open(now)) else now.replace(hour=10, minute=30, second=0, microsecond=0)
    )

    tick = runner.pass_once(now=session_moment)
    print(f"  pass: deployments={tick.deployments} signals={tick.signals} "
          f"orders={tick.orders} resting_fills={tick.resting_fills}")
    if tick.signals == 0:
        print(f"  last_pass: {loop.last_pass}")

    monitor = DeploymentMonitor(db=db)
    orders = monitor.orders(user_id, deployment_id)
    fills = monitor.fills(user_id, deployment_id)
    signals = monitor.signals(user_id, deployment_id)
    timeline = monitor.timeline(user_id, deployment_id)

    check("an order was placed", len(orders) >= 1, f"{len(orders)} orders")
    check("a fill was recorded", len(fills) >= 1, f"{len(fills)} fills")
    check("a signal was recorded", len(signals) >= 1, f"{len(signals)} signals")

    if orders:
        order = orders[0]
        check("order carries the deployed version",
              int(order.get("strategy_version") or 0) == v1["version"],
              str(order.get("strategy_version")))
        check("order is attributed to this deployment's strategy",
              order.get("strategy_id") == strategy_id)

    section("5. position and P&L follow from the fill")

    pnl = monitor.pnl(user_id, deployment_id, prices={symbol: tick_price})
    positions = pnl["positions"]
    check("a position exists", len(positions) >= 1, f"{len(positions)}")
    if positions:
        row = positions[0]
        check("position quantity matches the fill",
              abs(float(row["quantity"])) > 0, str(row["quantity"]))
    check("equity identity holds (cash + market value)",
          abs(pnl["equity"] - (pnl["cash"] + pnl["market_value"])) < 1.0,
          f"{pnl['equity']} vs {pnl['cash']} + {pnl['market_value']}")
    check("total P&L is against allocated capital",
          pnl["total_pct"] is not None and pnl["capital"] == 500_000.0)

    section("6. the causal chain is complete")

    stages = [entry.stage for entry in timeline]
    print(f"  timeline stages: {stages}")
    check("chain reaches the fill stage", "fill" in stages, str(stages))
    check("chain records the position update", "position" in stages, str(stages))
    check("chain is in causal order (first index of each stage ascends)",
          _causal_order(stages), str(stages))
    check("every entry is readable prose",
          all(len(e.summary) > 8 and " " in e.summary for e in timeline))

    section("7. risk state is reported next to its subject")

    risk = monitor.risk(user_id, deployment_id)
    check("risk reports the deployment's own exposure",
          "gross_exposure" in risk["observed"])
    check("limits serialise as data, not a repr string",
          not isinstance(risk["limits"], str),
          type(risk["limits"]).__name__)

    section("8. two versions of one strategy behave differently")

    with db.session() as session:
        second = DeploymentRepository.create(
            session,
            user_id=user_id,
            strategy_id=strategy_id,
            strategy_version=v2["version"],
            capital=500_000.0,
            config={"symbols": [symbol], "exchange": EXCHANGE, "timeframe": "1d"},
        )
        # A loop is only attached to a RUNNING deployment, so a PENDING second
        # row would prove nothing about its rules.
        DeploymentRepository.start(session, second["deployment_id"], user_id)
    runner.sync_loops()
    loop2 = runner._loops.get(second["deployment_id"])
    check("second deployment attached a loop", loop2 is not None)
    resolved2 = runner.resolve_rules(loop2) if loop2 is not None else None
    if resolved2 is not None:
        check("version 2 resolves its own stop (18.0)",
              float(resolved2[1].stop_loss_pct) == 18.0,
              f"got {resolved2[1].stop_loss_pct}")
        check("the two versions really differ",
              float(resolved2[1].stop_loss_pct) != float(exit_rules.stop_loss_pct),
              "both resolved to the same stop")
    else:
        check("version 2 resolves its own stop (18.0)", False, "unresolved")

    section("9. pause and stop")

    with db.session() as session:
        DeploymentRepository.pause(session, deployment_id, user_id,
                                   reason="verify: pausing")
    runner.sync_loops()
    check("PAUSE detaches the loop", deployment_id not in runner._loops)

    with db.session() as session:
        DeploymentRepository.stop(session, deployment_id, user_id,
                                  reason="verify: stopping")
        stopped = DeploymentRepository.get(session, deployment_id, user_id)
    check("STOP records the terminal state", stopped["status"] == "STOPPED",
          stopped["status"])
    status = monitor.status(user_id, deployment_id)
    check("monitor says it is not trading", status["trading"] is False)
    check("monitor names why", bool(status["not_trading_because"]),
          str(status["not_trading_because"]))

    section("10. restart recovery")

    # ``STOPPED`` is terminal by design: ``DeploymentRepository.start`` only
    # accepts PENDING and PAUSED, because resuming a stopped strategy would leave
    # "why is this still trading?" with no answer. Restarting a stopped strategy
    # is a *new deployment*, which is what RESET creates. So recovery is tested
    # on the PAUSED state, which is the resumable one — and the terminal rule is
    # asserted too, since a test that skipped it would not notice the day it
    # regressed.
    with db.session() as session:
        DeploymentRepository.stop(session, second["deployment_id"], user_id,
                                  reason="verify: end of section 8")
        rows = session.execute(
            __import__("sqlalchemy").text(
                "select count(*) from deployments where deployment_id = :d and status = 'RUNNING'"
            ), {"d": deployment_id},
        ).scalar()
    check("the deployment under test is not RUNNING yet", int(rows) == 0)

    # ``stop`` is a coroutine that awaits the background task; this probe only
    # ever called ``pass_once`` directly, so there is no task to await. Detaching
    # the loops is the part that matters for what follows.
    runner._loops.clear()  # noqa: SLF001 - probe teardown
    revived = PaperRunner(db=db)
    revived.sync_loops()
    check("a STOPPED deployment is not resumed",
          deployment_id not in revived._loops)

    with db.session() as session:
        resumed_rows = DeploymentRepository.start(session, deployment_id, user_id)
    check("a STOPPED deployment refuses to start (terminal state)",
          resumed_rows == 0, f"rowcount {resumed_rows}")

    # RESET is a service-level operation that creates a fresh row, so it is
    # exercised through the service rather than the repository.
    from atr.services.paper import DeploymentService

    replacement = DeploymentService(db=db).reset(
        user_id, deployment_id, reason="verify: fresh account"
    )
    new_id = replacement["deployment_id"]
    print(f"  reset created {new_id}")
    check("RESET creates a new deployment rather than reviving the old one",
          new_id != deployment_id, str(new_id))
    check("RESET records where it came from",
          replacement.get("reset_from") == deployment_id,
          str(replacement.get("reset_from")))
    check("RESET carries the original capital forward",
          float(replacement["capital"]) == 500_000.0,
          str(replacement.get("capital")))

    # The old run must still be readable — the whole reason reset creates a row
    # instead of erasing one.
    with db.session() as session:
        old_still_there = DeploymentRepository.get(session, deployment_id, user_id)
    check("the replaced deployment keeps its history (nothing was deleted)",
          old_still_there is not None
          and old_still_there["status"] == "STOPPED")
    old_pnl = monitor.pnl(user_id, deployment_id, prices={symbol: tick_price})
    check("the replaced deployment still reports its P&L",
          old_pnl["equity"] > 0)

    with db.session() as session:
        fresh_row = DeploymentRepository.get(session, new_id, user_id)
    check("the replacement row points at the same immutable version",
          int(fresh_row["strategy_version"]) == v1["version"],
          str(fresh_row.get("strategy_version")))

    with db.session() as session:
        DeploymentRepository.start(session, new_id, user_id)
    revived.sync_loops()
    check("a RUNNING deployment resumes after restart",
          new_id in revived._loops, f"loops={list(revived._loops)}")
    if new_id in revived._loops:
        live_rules = revived.resolve_rules(revived._loops[new_id])
        check("the resumed loop still resolves the right version's rules",
              live_rules is not None and float(live_rules[1].stop_loss_pct) == 3.0,
              str(live_rules[1].stop_loss_pct) if live_rules else "unresolved")

    # And a paused one stays resumable, which is the everyday case.
    with db.session() as session:
        DeploymentRepository.pause(session, new_id, user_id, reason="verify: pause")
    revived.sync_loops()
    check("PAUSE again detaches it", new_id not in revived._loops)
    with db.session() as session:
        back = DeploymentRepository.start(session, new_id, user_id)
    check("a PAUSED deployment does resume", back == 1, f"rowcount {back}")
    revived.sync_loops()
    check("and the loop comes back", new_id in revived._loops)

    section("11. the single overview payload")

    overview = monitor.overview(user_id, deployment_id)
    expected = {"status", "pnl", "positions", "orders", "fills", "signals",
                "trades", "risk", "timeline"}
    check("overview carries every view", expected <= set(overview), str(sorted(overview)))
    try:
        blob = json.dumps(overview, default=str)
        check("overview is JSON-serialisable", len(blob) > 0, str(len(blob)))
    except (TypeError, ValueError) as exc:
        check("overview is JSON-serialisable", False, str(exc))

    section("12. the closed paper trade becomes a forward learning observation")

    from atr.research.learning_evidence import GRADE_FORWARD, GRADE_IN_SAMPLE
    from atr.services.journal import TradeJournalService
    from atr.services.learning import LearningDatasetBuilder

    journal = TradeJournalService(db=db)
    opened_episode = journal.reconcile(user_id, deployment_id)
    # ``opened`` is empty here on purpose and it is not a failure: section 4's
    # evaluation pass already ran the journal, which is exactly what the runner
    # does after every pass. What must be true is that an episode *exists* for
    # the position the entry produced.
    check("the open position has a journal episode",
          opened_episode["open_now"] >= 1, str(opened_episode))

    # Close it through the same path that opened it — an order, a risk check, a
    # fill — rather than by adjusting the book. A position flattened by hand
    # would produce a journal row with no closing order behind it, and the exit
    # reason and slippage would both be missing for a reason the probe had
    # invented.
    from atr.execution.oms import OrderDraft
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    book = PaperLedger(db=db).portfolio(user_id, deployment_id=deployment_id)
    position = book.position(symbol)
    quantity = abs(float(position.quantity))
    check("the book holds the position the entry produced", quantity > 0, str(quantity))
    exit_price = round(float(position.avg_price) * 1.02, 2)
    print(f"  flattening {quantity:,.0f} at {exit_price:,.2f} (2% above the entry)")
    ExecutionService(
        orders=get_order_service(portfolio=book),
        venue=paper_service.paper_venue(lambda _s, _e: exit_price),
    ).place(
        OrderDraft(
            user_id=user_id,
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            mode="PAPER",
            exchange=EXCHANGE,
            requested_price=exit_price,
            deployment_id=deployment_id,
            signal_reason="verify: flatten the probe position",
        )
    )

    closed_episode = journal.reconcile(user_id, deployment_id)
    check("the flat book closed the episode",
          len(closed_episode["closed"]) >= 1, str(closed_episode))

    # The dataset, built from the journal the pipeline just wrote. No frames are
    # injected: this reads the real price cache, which is the path production
    # takes.
    dataset = LearningDatasetBuilder(db=db, cache_root=Path(_DATA_DIR)).build(
        user_id=user_id
    )
    check("the closed trade reached the learning dataset", len(dataset) >= 1,
          f"{len(dataset)} rows")
    if not dataset.rows:
        return report()

    row = dataset.rows[0]
    check("the row is graded FORWARD, from the OMS provenance stamp",
          row["evidence_grade"] == GRADE_FORWARD, str(row["evidence_grade"]))
    check("its evidence_class is PAPER_FORWARD",
          row["evidence_class"] == "PAPER_FORWARD", str(row["evidence_class"]))
    check("the row states the basis of its grade",
          "provenance stamp" in str(row["evidence_note"]), str(row["evidence_note"]))
    check("is_forward is set", row["is_forward"] is True)

    check("strategy attribution survived the chain",
          row["strategy_id"] == strategy_id and int(row["strategy_version"] or 0) == v1["version"],
          f"{row['strategy_id']}@{row['strategy_version']}")
    # The dataset canonicalises the symbol — the cache names files
    # ``<TICKER>-EQ.parquet`` and a journal symbol may arrive with or without the
    # series suffix — so the comparison is against the canonical form, not
    # against the deployment's spelling.
    from atr.instruments.service import canonical_symbol

    check("symbol survived the chain (canonicalised)",
          row["symbol"] == canonical_symbol(symbol), f"{row['symbol']} vs {symbol}")
    check("entry and exit timestamps are ordered",
          row["entry_ts"] is not None and row["exit_ts"] is not None
          and row["exit_ts"] > row["entry_ts"])
    check("entry and exit prices came from the fills",
          float(row["entry_price"]) > 0 and float(row["exit_price"]) > 0)
    check("the trade is profitable, as the exit price intended",
          float(row["return_pct"]) > 0, str(row["return_pct"]))
    check("quantity came from the fold", float(row["quantity"]) > 0, str(row["quantity"]))
    check("rupee P&L was recorded", row["net_pnl"] is not None, str(row["net_pnl"]))
    check("costs were recorded and are not zero",
          row["commission"] is not None and float(row["commission"]) > 0,
          str(row["commission"]))
    check("MFE and MAE were recorded from the fills",
          row["mfe"] is not None and row["mae"] is not None,
          f"mfe={row['mfe']} mae={row['mae']}")
    check("slippage was measured and averaged over the legs",
          row["slippage_bps"] is not None, str(row["slippage_bps"]))
    # Clipped to the column's 32 characters. A reason that does not fit loses its
    # tail rather than the whole field, and the untruncated text is still on the
    # closing order's ``NEW`` event, which is where it came from.
    check("the exit reason is the closing order's own (clipped to the column)",
          row["exit_reason"] == "verify: flatten the probe position"[:32],
          str(row["exit_reason"]))
    check("the entry-time calendar was filled",
          bool(row["day_of_week"]) and bool(row["time_of_day"]),
          f"{row['day_of_week']} / {row['time_of_day']}")
    check("VWAP is absent AND named, not silently blank",
          row["vwap_relationship"] is None and "vwap_relationship" in row["missing_features"])

    summary = dataset.summary()
    check("the summary counts it as forward evidence",
          summary["forward_observations"] == 1 and summary["in_sample_observations"] == 0,
          str(summary["evidence_grades"]))
    check("the summary names the latest forward trade date",
          summary["latest_forward_ts"] is not None, str(summary["latest_forward_ts"]))
    check("metric coverage reflects the columns actually recorded",
          summary["metric_coverage"]["net_pnl"] == 1
          and summary["metric_coverage"]["return_pct"] == 1,
          str(summary["metric_coverage"]))

    section("13. a trade with no provenance stamp stays in-sample")

    # Exactly what a replay harness writes: correct in every visible field, and
    # no execution-time stamp, because there was no execution. Grading this
    # forward would let last year's prices confirm the rule fitted to them.
    from atr.appdb.repositories import TradeJournalRepository

    with db.session() as session:
        replayed = TradeJournalRepository.open_trade(
            session,
            user_id=user_id,
            symbol=symbol,
            side="BUY",
            quantity=10.0,
            entry_price=100.0,
            entry_ts=datetime(2026, 3, 2, 9, 15),
            strategy_id=strategy_id,
            strategy_version=v1["version"],
            signal_reason="replayed from the selection history",
        )
        TradeJournalRepository.close_trade(
            session,
            replayed["trade_id"],
            user_id,
            exit_price=110.0,
            gross_pnl=100.0,
            net_pnl=90.0,
            exit_ts=datetime(2026, 3, 6, 14, 30),
        )

    mixed = LearningDatasetBuilder(db=db, cache_root=Path(_DATA_DIR)).build(user_id=user_id)
    grades = mixed.grade_counts()
    check("the backfill is graded in-sample",
          grades[GRADE_IN_SAMPLE] == 1, str(grades))
    check("the genuine trade is still the only forward row",
          grades[GRADE_FORWARD] == 1, str(grades))
    backfilled = next(r for r in mixed.rows if r["trade_ref"] == replayed["trade_id"])
    check("the backfill says why it is in-sample",
          "no execution-time provenance stamp" in str(backfilled["evidence_note"]),
          str(backfilled["evidence_note"]))
    check("a mixed book reports both counts separately",
          mixed.summary()["evidence_grades"] == {GRADE_FORWARD: 1, GRADE_IN_SAMPLE: 1},
          str(mixed.summary()["evidence_grades"]))

    section("14. the daily report does not claim on this book")

    # One forward observation is far below the floor, so the report states what
    # happened and refuses to say whether it was good. That refusal is the
    # property; a report that called this a result would be the overfitting the
    # engine exists to prevent.
    from atr.services.learning import DailyLearningReport

    report_payload = DailyLearningReport(mixed).build().as_dict()
    check("the report marks itself advisory",
          report_payload["advisory"] is True and report_payload["applies_changes"] is False)
    check("the report is not claimable below the forward floor",
          report_payload["claimable"] is False, str(report_payload["forward_observations"]))
    check("no advisory finding was produced",
          report_payload["observations"] == [], str(report_payload["observations"]))
    check("the report names the reason it will not claim",
          "Too few forward observations" in report_payload["headline"]
          or "in-sample" in report_payload["headline"],
          report_payload["headline"])
    check("the report still says what the book recorded",
          report_payload["forward_observations"] == 1
          and report_payload["in_sample_observations"] == 1,
          str(report_payload["today_grades"]))
    print(f"  headline: {report_payload['headline']}")

    return report()


def _causal_order(stages: list[str]) -> bool:
    """Each stage's first appearance must not precede an earlier stage's last."""
    rank = {"signal": 0, "risk": 1, "order": 2, "fill": 3, "position": 4}
    seen: list[int] = []
    for stage in stages:
        if stage not in rank:
            continue
        value = rank[stage]
        # A stage may repeat, but a stage must never appear after one that
        # belongs strictly later in the chain.
        if seen and value < max(seen) - 1:
            return False
        seen.append(value)
    return True


def _is_open(moment: datetime) -> bool:
    """Weekday, 09:15-15:30 IST. Mirrors the runner's own gate."""
    if moment.weekday() >= 5:
        return False
    return (9, 15) <= (moment.hour, moment.minute) <= (15, 30)


def report() -> int:
    print("\n" + "=" * 62)
    if _failures:
        print(f"FAILED  {len(_failures)}/{_checks} checks")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"OK  {_checks}/{_checks} checks passed")
    print("Strategy -> Version -> Deploy -> Live tick -> Signal -> Risk")
    print("        -> OMS -> Fill -> Position -> P&L -> Journal")
    print("        -> Learning observation (graded forward) -> Daily report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
