"""The deployment monitoring surface — one payload for one deployment's screen.

Why this is its own module
--------------------------

``DeploymentService`` (``services/paper.py``) owns the *account*: it folds the
order log into positions, cash, P&L and exposure. That is the right home for
arithmetic that must agree with the venue's own view of the book.

This module owns the *narrative*: what the deployment is doing, why it is or is
not trading, and the causal chain from a signal to a position update. Those are
different questions with different failure modes. The account can be perfectly
correct while the deployment is silently inert — which is exactly the state the
runner's ``blocked_reason`` exists to expose, and it has no place on a P&L
snapshot.

The timeline is the piece worth reading. A monitoring screen that shows
positions, orders and fills as three separate lists asks the operator to
reconstruct the causal chain in their head, and a chain assembled by eye is one
that gets assembled wrongly. ``timeline()`` reads the append-only event log and
emits one ordered list where each entry says what caused it.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any


#: Steps in the causal chain, in the order they occur. The UI renders these as
#: the pipeline rail, so the order here is load-bearing.
TIMELINE_STAGES = ("signal", "risk", "order", "fill", "position")

#: What each order status means for the timeline. The runner writes its reasons
#: into the event's ``raw`` payload; this maps them to a stage and a verdict.
_STAGE_BY_STATUS = {
    "NEW": "order",
    "VALIDATING": "risk",
    "RISK_APPROVED": "risk",
    "SUBMITTED": "order",
    "ACKNOWLEDGED": "order",
    "PARTIALLY_FILLED": "fill",
    "FILLED": "fill",
    "CANCEL_PENDING": "order",
    "CANCELLED": "order",
    "REJECTED": "risk",
    "EXPIRED": "order",
}


class MonitoringError(Exception):
    """A monitoring read that cannot be served."""

    def __init__(self, message: str, *, code: str = "monitoring_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class TimelineEntry:
    """One step in the Signal → Risk → Order → Fill → Position chain."""

    ts: datetime
    stage: str
    deployment_id: str
    symbol: str | None
    #: One line an operator can read without a legend.
    summary: str
    #: approved | rejected | filled | placed | observed | recorded
    outcome: str
    #: Why, when the step was a decision rather than a fact.
    reason: str | None
    order_id: str | None
    #: The raw event payload, narrowed to keys the UI actually shows.
    detail: dict[str, Any]


def _loads(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _jsonable(value: Any) -> Any:
    """A value the JSON encoder can emit, without changing what it means.

    ``RiskLimits`` is a dataclass holding an ``inf`` sentinel for "no limit". A
    repr string would be unreadable in the UI and a bare ``inf`` is not valid
    JSON, so the sentinel becomes ``None`` — which in this envelope means
    *unbounded*, the same thing the limit does. Anything else is passed through
    untouched and left to the framework's encoder.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def _read_log(
    db: Any, user_id: str, deployment_id: str, *, limit: int = 500
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """This deployment's orders and their events, in one or two queries.

    ``OrderEventRepository.for_user`` returns the user's whole event log ordered
    by ``(ts, seq)``; grouping it here means the six read surfaces below cost two
    queries instead of one per order. ``seq`` is kept as the tiebreaker because a
    submit and its fill can share a timestamp, and the timeline's whole value is
    being in the right order.
    """
    from atr.appdb.repositories import OrderEventRepository, OrderRepository

    with db.session() as session:
        orders, _ = OrderRepository.list_for_user(
            session, user_id, deployment_id=deployment_id, limit=limit
        )
        events = OrderEventRepository.for_user(session, user_id, limit=limit * 10)

    by_order: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_order.setdefault(str(event.get("order_id")), []).append(event)
    for rows in by_order.values():
        rows.sort(key=lambda e: (str(e.get("ts") or ""), int(e.get("seq") or 0)))

    orders.sort(key=lambda o: (str(o.get("created_at") or ""), str(o.get("order_id"))))
    return orders, by_order


class DeploymentMonitor:
    """Read-only composition over the deployment, its orders and its events.

    Deliberately read-only: every method here is a query. Nothing in this module
    can change a deployment's state, so a monitoring bug can misreport but cannot
    mis-trade.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        ledger: Any = None,
        runner: Any = None,
    ) -> None:
        from atr.appdb.engine import get_app_db
        from atr.services.paper import PaperLedger

        self.db = db or get_app_db()
        self.ledger = ledger or PaperLedger(db=self.db)
        #: The runner whose live loops this monitor reports on. Defaults to the
        #: process-wide one, which is the real answer in production; injected so
        #: a caller can point the monitor at the runner it actually started,
        #: rather than at a second one that knows nothing about its deployments.
        self._runner = runner

    def _the_runner(self) -> Any:
        if self._runner is not None:
            return self._runner
        from atr.services.runner import get_runner

        return get_runner()

    # ------------------------------------------------------------------
    # deployment resolution
    # ------------------------------------------------------------------
    def _require(self, user_id: str, deployment_id: str) -> dict[str, Any]:
        """The deployment row, or a 404. Owner-scoped, always."""
        from atr.appdb.repositories import DeploymentRepository

        with self.db.session() as session:
            row = DeploymentRepository.get(session, deployment_id, user_id)
        if row is None:
            raise MonitoringError(
                "deployment not found", code="not_found", status=404
            )
        return row

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def status(
        self,
        user_id: str,
        deployment_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Everything the header of the monitoring screen shows.

        ``trading`` is the field that matters and the one that is easy to get
        wrong. A deployment can be ``RUNNING`` and still place no orders — because
        its rules could not be resolved, because the market is shut, or because it
        has no universe. Reporting only the row's ``status`` would show "running"
        for a deployment that is not running, so ``blocked_reason`` and
        ``in_market_hours`` are returned alongside it and the UI is expected to
        show all three.
        """
        from atr.services.runner import in_market_hours

        row = self._require(user_id, deployment_id)
        config = _loads(row.get("config")) or {}
        if not isinstance(config, dict):
            config = {}

        status = row.get("status")
        runner = self._the_runner()
        loop = runner._loops.get(deployment_id)

        blocked_reason = getattr(loop, "blocked_reason", None) if loop is not None else None
        open_now = in_market_hours(now)

        runner_state = getattr(loop, "state", None) if loop else None
        last_tick_dt = getattr(loop, "last_tick_time", None) if loop else None
        last_tick_age = getattr(loop, "last_tick_age_seconds", None) if loop else None
        max_tick_age = getattr(getattr(loop, "venue", None), "max_tick_age_seconds", 60.0) if loop else 60.0
        skipped_reason = getattr(loop, "skipped_reason", None) if loop else None
        last_risk = getattr(loop, "last_risk_decision", None) if loop else None
        last_order = getattr(loop, "last_order", None) if loop else None
        last_fill = getattr(loop, "last_fill", None) if loop else None

        if status != "RUNNING":
            trading = False
            why = f"deployment is {status}"
            diagnostic_state = status.lower()
        elif loop is None:
            trading = False
            why = "the runner has not attached a loop yet"
            diagnostic_state = "waiting_for_loop"
        elif blocked_reason:
            trading = False
            why = blocked_reason
            diagnostic_state = "blocked"
        elif not open_now:
            trading = False
            why = skipped_reason or "the cash session is closed"
            diagnostic_state = "market_closed"
        elif runner_state == "ERROR":
            trading = False
            why = skipped_reason or "system error"
            diagnostic_state = "system_error"
        elif runner_state == "WAITING_FOR_TICKS":
            trading = False
            if last_tick_age is not None and last_tick_age > max_tick_age:
                diagnostic_state = "stale_tick"
                why = f"stale tick received ({last_tick_age:.1f}s old, max allowed {max_tick_age:.0f}s)"
            else:
                diagnostic_state = "no_live_tick"
                why = "no live tick received"
        elif last_tick_dt is None:
            trading = False
            diagnostic_state = "no_live_tick"
            why = "no live tick received yet"
        elif last_tick_age is not None and last_tick_age > max_tick_age:
            trading = False
            diagnostic_state = "stale_tick"
            why = f"tick is stale ({last_tick_age:.1f}s old)"
        else:
            trading = True
            why = None
            if last_risk and not last_risk.get("approved"):
                diagnostic_state = "risk_rejected"
                why = last_risk.get("reason") or "risk rejected signal"
            elif last_order and last_order.get("status") == "REJECTED":
                diagnostic_state = "order_rejected"
                why = last_order.get("reason") or "order rejected"
            elif last_order and last_order.get("status") in ("OPEN", "SUBMITTED", "ACCEPTED"):
                diagnostic_state = "order_waiting_for_fill"
            elif last_fill and (not last_order or last_order.get("order_id") == last_fill.get("order_id")):
                diagnostic_state = "paper_fill_completed"
            elif skipped_reason and "no signal" in skipped_reason.lower():
                diagnostic_state = "no_signal"
            else:
                diagnostic_state = "running"

        trades_info = self.trades(user_id, deployment_id)
        pnl_info = self.pnl(user_id, deployment_id)

        return {
            "deployment_id": deployment_id,
            "status": status,
            "mode": row.get("mode"),
            "strategy_id": row.get("strategy_id"),
            "strategy_version": row.get("strategy_version"),
            "capital": row.get("capital"),
            "symbols": config.get("symbols") or [],
            "exchange": config.get("exchange") or "NSEEQ",
            "timeframe": config.get("timeframe") or "1d",
            "started_at": row.get("started_at"),
            "stopped_at": row.get("stopped_at"),
            "stop_reason": row.get("stop_reason"),
            "created_at": row.get("created_at"),
            # The fields that together answer "is it trading?" and why.
            "trading": trading,
            "not_trading_because": why,
            "diagnostic_state": diagnostic_state,
            "runner_state": runner_state,
            "blocked_reason": blocked_reason,
            "skipped_reason": skipped_reason,
            "in_market_hours": open_now,
            "market_open": open_now,
            "runner_running": runner.running,
            "loop_attached": loop is not None,
            "last_tick_time": loop.last_tick_time.isoformat() if (loop and getattr(loop, "last_tick_time", None)) else None,
            "last_tick_age_seconds": getattr(loop, "last_tick_age_seconds", None) if loop else None,
            "tick_age_seconds": getattr(loop, "last_tick_age_seconds", None) if loop else None,
            "last_strategy_evaluation": loop.last_strategy_evaluation.isoformat() if (loop and getattr(loop, "last_strategy_evaluation", None)) else None,
            "last_signal": getattr(loop, "last_signal", None) if loop else None,
            "last_risk_decision": last_risk,
            "last_order": getattr(loop, "last_order", None) if loop else None,
            "last_fill": getattr(loop, "last_fill", None) if loop else None,
            "skipped_evaluations_count": getattr(loop, "skipped_evaluations_count", 0) if loop else 0,
            "skipped_fills_count": getattr(loop, "skipped_fills_count", 0) if loop else 0,
            "last_pass": (getattr(loop, "last_pass", None) or {}) if loop else None,
            "trade_count": trades_info.get("total", 0),
            "open_trades_count": trades_info.get("open_count", 0),
            "today_pnl": pnl_info.get("today_pnl"),
            "cumulative_pnl": pnl_info.get("total_pnl"),
            "equity": pnl_info.get("equity"),
        }

    # ------------------------------------------------------------------
    # pnl, with today separated from the lifetime figure
    # ------------------------------------------------------------------
    def pnl(self, user_id: str, deployment_id: str, *, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Lifetime and *today's* P&L, kept distinct.

        Today's P&L cannot be read off a stored equity curve, because the paper
        engine has no curve — it has a log. It is the change in account equity
        since the session boundary, which is the only definition that answers
        "how is it doing today" without inventing a baseline.

        The boundary is IST midnight, matching the exchange the deployment trades
        on. When the log holds no fill before today, today's figure *is* the
        lifetime figure and ``today_since`` is ``None``: there is no earlier
        equity to subtract, and reporting 0 as the day's P&L would understate a
        first-day gain as nothing. ``today_pnl`` is returned as ``None`` in that
        case, never ``0.0`` — the two mean different things and the UI must be
        able to tell them apart.
        """
        from atr.services.runner import IST

        row = self._require(user_id, deployment_id)
        snapshot = self.ledger.snapshot(
            user_id, deployment_id=deployment_id, prices=prices
        )

        day_start = datetime.combine(
            datetime.now(IST).date(), datetime.min.time(), tzinfo=IST
        )
        equity_then = self._equity_at(user_id, deployment_id, day_start)

        if equity_then is None:
            today_pnl = None
            today_pct = None
            today_since = None
        else:
            today_pnl = snapshot["equity"] - equity_then
            today_pct = (
                (snapshot["equity"] / equity_then - 1.0) * 100.0
                if equity_then
                else None
            )
            today_since = day_start.isoformat()

        capital = row.get("capital") or 0.0
        return {
            **snapshot,
            "deployment_id": deployment_id,
            "capital": capital,
            "today_pnl": today_pnl,
            "today_pct": today_pct,
            "today_since": today_since,
            # Both figures are against the *allocated* capital, which is what a
            # deployment is judged on — not against the initial_cash the ledger
            # folded to, and not against whatever cash remained.
            "total_pnl": snapshot["equity"] - capital if capital else None,
            "total_pct": (
                (snapshot["equity"] / capital - 1.0) * 100.0 if capital else None
            ),
            "exposure_pct": (
                snapshot["gross_exposure"] / snapshot["equity"] * 100.0
                if snapshot["equity"]
                else None
            ),
        }

    def _equity_at(
        self, user_id: str, deployment_id: str, moment: datetime
    ) -> float | None:
        """Account equity at a past instant, reconstructed from the fill log.

        Returns ``None`` when nothing was filled before ``moment`` — distinct
        from returning the capital, which would claim a flat day that was not
        lived, and distinct from returning the current equity, which would make
        today's P&L identically zero.

        The fold is the *same* ``Portfolio.apply_fill`` the ledger uses, so this
        cannot drift from the live figure by using different arithmetic. What it
        does not do is mark to a past price it does not have: ``apply_fill``
        leaves ``last_price`` untouched, so each position is marked at its own
        last fill price at or before the boundary. That is the only price the log
        actually records for that instant — using *today's* mark instead would
        fold today's move into yesterday's equity and then cancel it back out of
        today's figure, an error that is invisible precisely because it is
        zero-sum.
        """
        from atr.backtest.portfolio import Portfolio

        fills = [
            f
            for f in self.ledger.fills(user_id, deployment_id=deployment_id)
            if (ts := _as_dt(getattr(f, "ts", None))) is not None and ts < moment
        ]
        if not fills:
            return None

        portfolio = Portfolio(
            initial_cash=float(self.ledger._deployment_cash(user_id, deployment_id))
        )
        marks: dict[str, float] = {}
        for fill in fills:
            portfolio.apply_fill(fill)
            # A fill is a trade at that price, so it is the mark as of then.
            marks[fill.instrument.symbol] = fill.price

        for symbol, price in marks.items():
            position = portfolio.positions.get(symbol)
            if position is not None and not position.is_flat:
                position.mark(price)
        return portfolio.equity

    # ------------------------------------------------------------------
    # orders and fills
    # ------------------------------------------------------------------
    def orders(
        self, user_id: str, deployment_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Orders for this deployment, newest first, with their event count."""
        self._require(user_id, deployment_id)
        rows, by_order = _read_log(self.db, user_id, deployment_id, limit=limit)
        out = [
            {
                "order_id": row["order_id"],
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "quantity": row.get("quantity"),
                "order_type": row.get("order_type"),
                "limit_price": row.get("limit_price"),
                "status": row.get("status"),
                # The column is ``filled_quantity``; the event payload calls the
                # same thing ``filled_qty``. Renaming it here to the event's
                # vocabulary would make this read disagree with the row it came
                # from, so the row's own name is used.
                "filled_quantity": row.get("filled_quantity"),
                "avg_fill_price": row.get("avg_fill_price"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "strategy_id": row.get("strategy_id"),
                "strategy_version": row.get("strategy_version"),
                "signal_reason": row.get("signal_reason"),
                "events": len(by_order.get(str(row["order_id"]), [])),
            }
            for row in rows
        ]
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return out

    def fills(self, user_id: str, deployment_id: str) -> list[dict[str, Any]]:
        """Execution events, newest first."""
        self._require(user_id, deployment_id)
        fills = self.ledger.fills(user_id, deployment_id=deployment_id)
        out = [
            {
                "order_id": getattr(f, "order_id", None),
                "symbol": getattr(f, "symbol", None),
                "side": getattr(f, "side", None),
                "quantity": getattr(f, "quantity", None),
                "price": getattr(f, "price", None),
                "commission": getattr(f, "commission", None),
                "ts": getattr(f, "ts", None),
                "value": (
                    float(getattr(f, "quantity", 0) or 0)
                    * float(getattr(f, "price", 0) or 0)
                ),
            }
            for f in fills
        ]
        out.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
        return out

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------
    def signals(
        self, user_id: str, deployment_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Signals this deployment generated, derived from the order log.

        There is no separate signal table, and there should not be: a signal that
        did not become an order is not actionable and recording it would fill a
        screen with rows nothing acted on. What is recorded is the signal that
        *did* produce an order, on the order's creation event, together with the
        rule and reason the runner stamped on it.
        """
        self._require(user_id, deployment_id)
        rows, by_order = _read_log(self.db, user_id, deployment_id, limit=limit)
        out: list[dict[str, Any]] = []
        for row in rows:
            for event in by_order.get(str(row["order_id"]), []):
                if event.get("to_status") != "NEW":
                    continue
                raw = _loads(event.get("raw")) or {}
                if not isinstance(raw, dict):
                    raw = {}
                out.append(
                    {
                        "order_id": row["order_id"],
                        "symbol": row.get("symbol"),
                        "side": row.get("side"),
                        "quantity": row.get("quantity"),
                        "rule": raw.get("rule") or row.get("signal_rule"),
                        "reason": raw.get("reason")
                        or raw.get("signal_reason")
                        or row.get("signal_reason"),
                        "price": raw.get("price"),
                        "strategy_id": row.get("strategy_id"),
                        "strategy_version": row.get("strategy_version"),
                        "ts": event.get("ts") or row.get("created_at"),
                    }
                )
        out.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
        return out

    # ------------------------------------------------------------------
    # trades
    # ------------------------------------------------------------------
    def trades(self, user_id: str, deployment_id: str, *, limit: int = 200) -> dict[str, Any]:
        """Closed and open trades from the journal.

        The journal is the only place a trade's *duration*, MFE and MAE are
        recorded — the order log holds fills, not episodes. So this read is
        honest about being empty when nothing has journalled yet, rather than
        reconstructing episodes from fills and presenting a guess as a record.
        """
        from atr.appdb.repositories import TradeJournalRepository

        self._require(user_id, deployment_id)
        with self.db.session() as session:
            rows, total = TradeJournalRepository.list_for_user(
                session,
                user_id,
                deployment_id=deployment_id,
                closed_only=True,
                limit=limit,
            )
            open_rows, open_total = TradeJournalRepository.list_for_user(
                session,
                user_id,
                deployment_id=deployment_id,
                closed_only=False,
                limit=limit,
            )
        return {
            "trades": rows,
            "total": total,
            # An open trade has no realised P&L, so it is reported separately: a
            # win rate computed from a list that mixed the two would be dragged
            # toward zero by rows that have not resolved yet.
            "open": [r for r in open_rows if r.get("exit_ts") is None],
            "open_count": sum(1 for r in open_rows if r.get("exit_ts") is None),
            "all_count": open_total,
        }

    # ------------------------------------------------------------------
    # risk
    # ------------------------------------------------------------------
    def risk(self, user_id: str, deployment_id: str) -> dict[str, Any]:
        """The risk state as it applies to *this* deployment.

        The kill switch and the limits are platform-wide (they live in
        ``system_state``), so they are reported as-is alongside the deployment's
        own folded book — the view count, the exposure and the symbols — because
        a limit is only meaningful next to the number it is compared against.
        """
        from atr.services.risk import RiskStateService

        self._require(user_id, deployment_id)
        snapshot = self.ledger.snapshot(user_id, deployment_id=deployment_id)
        state = RiskStateService(db=self.db).snapshot()

        limits = state.limits
        if dataclasses.is_dataclass(limits) and not isinstance(limits, type):
            limits = _jsonable(limits)

        return {
            "kill_switch": state.kill_switch,
            "execution_mode": state.execution_mode,
            "limits": limits,
            "changed_at": state.changed_at,
            "changed_by": state.changed_by,
            "reason": state.reason,
            # The deployment's live numbers, so a limit can be read against the
            # figure it constrains rather than in the abstract.
            "observed": {
                "open_positions": len(snapshot["positions"]),
                "gross_exposure": snapshot["gross_exposure"],
                "net_exposure": snapshot["net_exposure"],
                "equity": snapshot["equity"],
                "cash": snapshot["cash"],
                "unpriced_symbols": snapshot["unpriced_symbols"],
            },
        }

    # ------------------------------------------------------------------
    # the timeline
    # ------------------------------------------------------------------
    def timeline(
        self, user_id: str, deployment_id: str, *, limit: int = 300
    ) -> list[TimelineEntry]:
        """Signal → Risk → Order → Fill → Position, as one ordered list.

        Built from ``order_events``, which is append-only and therefore the only
        source that can answer "what happened, in what order". Each entry carries
        the stage it belongs to so the UI can render the chain without inferring
        it from a status string, and the reason when the step was a decision.

        Position updates are emitted per fill, because that is when the position
        changes. The quantity shown is the cumulative filled quantity on that
        order, which is what the ledger had applied at that instant.
        """
        self._require(user_id, deployment_id)
        rows, by_order = _read_log(self.db, user_id, deployment_id, limit=limit)
        entries: list[TimelineEntry] = []

        for row in rows:
            symbol = row.get("symbol")
            side = row.get("side")
            order_id = row["order_id"]

            for event in by_order.get(str(order_id), []):
                to_status = str(event.get("to_status") or "")
                stage = _STAGE_BY_STATUS.get(to_status)
                if stage is None:
                    continue
                ts = _as_dt(event.get("ts")) or _as_dt(row.get("created_at"))
                if ts is None:
                    continue
                raw = _loads(event.get("raw")) or {}
                if not isinstance(raw, dict):
                    raw = {}

                reason = event.get("reject_reason") or raw.get("reason")
                summary, outcome = self._describe(
                    to_status=to_status,
                    symbol=symbol,
                    side=side,
                    row=row,
                    event=event,
                    raw=raw,
                )

                entries.append(
                    TimelineEntry(
                        ts=ts,
                        stage=stage,
                        deployment_id=deployment_id,
                        symbol=symbol,
                        summary=summary,
                        outcome=outcome,
                        reason=reason,
                        order_id=order_id,
                        detail={
                            k: v
                            for k, v in {
                                "to_status": to_status,
                                "rule": raw.get("rule"),
                                "reason": raw.get("reason"),
                                "filled_qty": event.get("filled_qty"),
                                "filled_price": event.get("filled_price"),
                                "commission": event.get("commission"),
                                "slippage_bps": event.get("slippage_bps"),
                                "quantity": row.get("quantity"),
                                "strategy_version": row.get("strategy_version"),
                            }.items()
                            if v is not None
                        },
                    )
                )

                # A fill moves the position, so the chain gets an explicit
                # position step rather than leaving the reader to infer it.
                if stage == "fill" and event.get("filled_qty"):
                    entries.append(
                        TimelineEntry(
                            ts=ts,
                            stage="position",
                            deployment_id=deployment_id,
                            symbol=symbol,
                            summary=(
                                f"{symbol} position updated to "
                                f"{float(event['filled_qty']):g} from this order"
                            ),
                            outcome="recorded",
                            reason=None,
                            order_id=order_id,
                            detail={
                                k: v
                                for k, v in {
                                    "filled_qty": event.get("filled_qty"),
                                    "filled_price": event.get("filled_price"),
                                    "side": side,
                                }.items()
                                if v is not None
                            },
                        )
                    )

        # ``seq`` is not carried onto the entry, so a submit and its fill in the
        # same second would reorder arbitrarily. Sorting by the status's place in
        # the chain as a tiebreaker keeps the causal order stable and true.
        entries.sort(
            key=lambda e: (e.ts, TIMELINE_STAGES.index(e.stage), e.order_id or "")
        )
        return entries

    @staticmethod
    def _describe(
        *,
        to_status: str,
        symbol: Any,
        side: Any,
        row: dict[str, Any],
        event: dict[str, Any],
        raw: dict[str, Any],
    ) -> tuple[str, str]:
        """One readable line and an outcome word for a timeline entry.

        Branches on the *status*, which is what carries meaning; the stage is
        derived from it upstream and passing it in as well would invite the two
        to disagree. Kept as explicit branches rather than an f-string built from
        the status name: a timeline whose wording comes from an enum is one that
        says ``RISK_APPROVED`` to a human and needs a legend.
        """
        name = str(symbol or "?")
        quantity = row.get("quantity")

        if to_status == "NEW":
            rule = raw.get("rule")
            reason = raw.get("reason")
            lead = f"{name} {side} signal"
            if rule:
                lead += f" from {rule}"
            if reason:
                return f"{lead} — {reason}", "observed"
            return lead, "observed"

        if to_status == "VALIDATING":
            return f"{name} sent to the risk gate", "observed"

        if to_status == "RISK_APPROVED":
            return f"{name} approved by risk", "approved"

        if to_status == "REJECTED":
            why = event.get("reject_reason") or raw.get("reason") or "no reason given"
            return f"{name} rejected — {why}", "rejected"

        if to_status in {"SUBMITTED", "ACKNOWLEDGED"}:
            return f"{name} order placed ({quantity:g})" if quantity else f"{name} order placed", "placed"

        if to_status == "PARTIALLY_FILLED":
            filled = event.get("filled_qty")
            return (
                f"{name} partially filled — {float(filled):g} so far",
                "filled" if filled is not None else "observed",
            )

        if to_status == "FILLED":
            filled = event.get("filled_qty")
            price = event.get("filled_price")
            if filled is not None and price is not None:
                return f"{name} filled {float(filled):g} @ {float(price):,.2f}", "filled"
            return f"{name} filled", "filled"

        if to_status in {"CANCELLED", "CANCEL_PENDING"}:
            return f"{name} order cancelled", "observed"

        if to_status == "EXPIRED":
            return f"{name} order expired unfilled", "observed"

        return f"{name} {to_status.lower().replace('_', ' ')}", "observed"

    # ------------------------------------------------------------------
    # the whole screen
    # ------------------------------------------------------------------
    def overview(
        self,
        user_id: str,
        deployment_id: str,
        *,
        prices: dict[str, float] | None = None,
        timeline_limit: int = 300,
    ) -> dict[str, Any]:
        """Everything the monitoring screen renders, in one read.

        One request rather than six, because the six views are of one state: a
        screen that fetches P&L, then positions, then orders can render a position
        from before a fill next to a P&L from after it, and the two disagree for
        as long as the operator is looking at them.
        """
        return {
            "status": self.status(user_id, deployment_id),
            "pnl": self.pnl(user_id, deployment_id, prices=prices),
            "positions": self.ledger.snapshot(
                user_id, deployment_id=deployment_id, prices=prices
            )["positions"],
            "orders": self.orders(user_id, deployment_id, limit=timeline_limit),
            "fills": self.fills(user_id, deployment_id),
            "signals": self.signals(user_id, deployment_id, limit=timeline_limit),
            "trades": self.trades(user_id, deployment_id),
            "risk": self.risk(user_id, deployment_id),
            "timeline": [
                {
                    "ts": e.ts.isoformat(),
                    "stage": e.stage,
                    "symbol": e.symbol,
                    "summary": e.summary,
                    "outcome": e.outcome,
                    "reason": e.reason,
                    "order_id": e.order_id,
                    "detail": e.detail,
                }
                for e in self.timeline(user_id, deployment_id, limit=timeline_limit)
            ],
        }
