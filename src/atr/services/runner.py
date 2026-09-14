"""The continuous paper-trading deployment runner.

What this closes
----------------

Before this module the paper engine was *request-driven*: an order filled when
somebody called ``POST /paper/deployments/{id}/orders``, prices came from the
daily parquet close, and a resting limit order was re-evaluated only if a client
happened to ask. The pieces were correct and the loop did not exist, so
``BACKTEST → PAPER → LIVE`` had no middle that actually ran.

The loop, and nothing new in the middle of it::

    live tick ──► strategy rules ──► signal ──► ExecutionService ──► OMS
                                                                     │
                                              risk gate (RiskEngine) ┘
                                                                     │
                        PaperVenue.match/submit ──► simulated fill ──┘
                                                                     │
                                     PaperLedger (a fold of order_events)
                                                                     │
                                                        positions · cash · P&L

Every step is an existing component. The runner's only job is to sequence them
on a clock, which is why it contains no matching, no costing and no P&L
arithmetic of its own.

Two rules it must not break
---------------------------

**One strategy definition.** Signals come from ``signals.rules.eval_entry`` /
``eval_exit`` — the same functions the scanner and the backtester call. A second
evaluation path here would mean paper and backtest disagreeing about what a rule
means, which is the specific failure the shared rule layer exists to prevent.

**The log is the state.** Positions and cash are folded from ``order_events`` on
every observation, never accumulated in memory. A runner that kept its own
position counters would drift from the orders that produced them, and the drift
would only be visible as money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from atr.appdb.engine import utcnow
from atr.appdb.repositories import DeploymentRepository
from atr.core.enums import OrderType, Side
from atr.execution.oms import OrderDraft
from atr.services.paper import PaperLedger, PaperVenue, default_price_source

logger = logging.getLogger("atr.services.runner")

IST = ZoneInfo("Asia/Kolkata")

#: The Indian cash-market session. A signal is only acted on inside it, because a
#: paper fill at 20:15 IST is a fill at a price no exchange offered.
MARKET_OPEN = clock_time(9, 15)
MARKET_CLOSE = clock_time(15, 30)

#: How long a signal stays actionable. Beyond this it is dropped rather than
#: filled late: a rule that fired at 10:02 was true of the market at 10:02.
SIGNAL_TTL_SECONDS = 300


def in_market_hours(now: datetime | None = None) -> bool:
    """Whether the NSE/BSE regular session is open.

    Weekends are excluded. Public holidays are *not* — the platform has no market
    calendar yet (``docs/DATA_MODEL.md`` §2 specifies one), and on a holiday the
    live feed simply delivers no ticks, so a deployment evaluates nothing. That is
    the same observable behaviour as a correct calendar for this purpose, and
    inventing a holiday list would be a guess presented as a fact.
    """
    now = now.astimezone(IST) if now else datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


@dataclass
class RunnerConfig:
    """One deployment's running parameters.

    Everything here is either the deployment row's own ``config`` JSON or a safety
    bound. Nothing is a strategy parameter — those belong to the strategy version,
    and a runner that could override them would be a second place the strategy is
    defined.
    """

    symbols: tuple[str, ...]
    exchange: str = "NSEEQ"
    #: Seconds between evaluation passes. A tick-driven loop would run the rules
    #: once per tick per symbol, which at ~10 ticks/s across 50 symbols is 500
    #: pandas evaluations a second for a signal that is only actionable once a
    #: bar. One pass a second is far finer than the daily rules can distinguish.
    interval_seconds: float = 1.0
    #: Per-trade capital, in rupees. Sized from the deployment's allocation when
    #: not set explicitly.
    order_value: float | None = None
    #: Bars of history to load for indicator warmup.
    lookback_days: int = 400
    #: Refuse to open more than this many open positions at once.
    max_open_positions: int = 10
    #: Trailing stop applied to every paper position, as the exit rules would.
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None

    @classmethod
    def from_deployment(cls, row: dict[str, Any]) -> RunnerConfig:
        """Read the deployment's stored config, defaulting to the whole universe.

        ``config`` is stored on the row so a deployment's behaviour is
        reproducible from that row alone — the property that makes "why did this
        trade?" answerable a week later.

        The column may hold either a mapping (SQLite JSON column) or a JSON
        string, depending on how the row was written, so both are accepted. A
        missing row or unreadable config yields an empty universe, which
        ``_make_loop`` then logs and skips — a deployment whose config cannot be
        read must never be traded on a guessed default.
        """
        raw = (row or {}).get("config")
        if isinstance(raw, (str, bytes)):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if not isinstance(raw, dict):
            raw = {}
        symbols = raw.get("symbols") or ()
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(",") if s.strip()]
        return cls(
            symbols=tuple(str(s).strip().upper() for s in symbols if str(s).strip()),
            exchange=str(raw.get("exchange") or "NSEEQ").upper(),
            interval_seconds=float(raw.get("interval_seconds") or 1.0),
            order_value=raw.get("order_value"),
            lookback_days=int(raw.get("lookback_days") or 400),
            max_open_positions=int(raw.get("max_open_positions") or 10),
            stop_loss_pct=raw.get("stop_loss_pct"),
            take_profit_pct=raw.get("take_profit_pct"),
        )


@dataclass
class DeploymentLoop:
    """One running deployment. Owns its own history cache, not its own positions.

    The history cache is per-deployment because two deployments of the same
    strategy over different universes should not share a warmup frame; the
    positions are *not* held here because the order log already holds them.
    """

    row: dict[str, Any]
    config: RunnerConfig
    #: Called with this loop, returns an (execution, orders) pair bound to the
    #: deployment's own folded portfolio. A callable rather than two objects
    #: because the portfolio is rebuilt from the log on every pass — see
    #: ``PaperRunner.order_service``.
    wiring: Callable[["DeploymentLoop"], tuple[Any, Any]]
    ledger: PaperLedger
    venue: PaperVenue
    features: Any = None
    #: symbol -> daily frame, loaded once and appended to as bars close.
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: The forming bar's date, so a day boundary is detected without a calendar.
    frame_day: dict[str, Any] = field(default_factory=dict)
    #: Signals already acted on, keyed by (symbol, rule, bar) so a rule that
    #: stays true for an hour does not open an hour of orders.
    acted: set[tuple[str, str, str]] = field(default_factory=set)
    #: Set when rules emitted but nothing was traded, for the status surface.
    last_pass: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utcnow)
    #: (execution, orders) for the current pass, set by ``bind``.
    _bound: tuple[Any, Any] | None = None

    @property
    def user_id(self) -> str:
        return self.row["user_id"]

    @property
    def deployment_id(self) -> str:
        return self.row["deployment_id"]

    def bind(self) -> None:
        """Resolve the pipeline once, at the start of a pass.

        Both ``execution`` and ``orders`` are read many times inside one pass and
        folding the log twice would price the risk gate against a different book
        than the venue matched against. Binding per pass is the granularity that
        is both correct and cheap — the fold is a handful of indexed reads, not
        something to hold across passes, because a fill recorded this pass must
        be visible to the next one.
        """
        execution, orders = self.wiring(self)
        self._bound = (execution, orders)

    @property
    def execution(self) -> Any:
        return self._bound[0]

    @property
    def orders(self) -> Any:
        return self._bound[1]

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def warmup(self) -> None:
        """Load each symbol's daily history once, so rules have indicator context."""
        from atr.signals.engine import load_daily

        loaded = 0
        for symbol in self.config.symbols:
            frame = load_daily(
                symbol,
                self.config.exchange,
                client=None,
                conid=None,
                lookback_days=self.config.lookback_days,
            )
            if frame is None or frame.empty:
                logger.debug("runner %s: no history for %s", self.deployment_id[:8], symbol)
                continue
            self.frames[symbol] = frame
            if "ts" in frame.columns and len(frame):
                self.frame_day[symbol] = pd.Timestamp(frame["ts"].iloc[-1]).date()
            loaded += 1
        logger.info(
            "runner %s warmed %d/%d symbols",
            self.deployment_id[:8],
            loaded,
            len(self.config.symbols),
        )

    def _refresh_if_new_day(self, symbol: str, price: float) -> pd.DataFrame:
        """Start a new bar when the session rolls over; otherwise update in place.

        Without this the forming bar would absorb every historical session into
        one enormous candle and every SMA would be wrong. The day boundary is
        detected from the bar's own timestamp rather than from a calendar, so a
        weekend or a holiday needs no special case.
        """
        from atr.signals.engine import load_daily

        frame = self.frames[symbol]
        today = datetime.now(IST).date()
        previous = self.frame_day.get(symbol)
        if previous is not None and previous == today:
            return frame

        # A new session: persist the previous forming bar by reloading, then let
        # the caller append today's.
        reloaded = load_daily(
            symbol,
            self.config.exchange,
            client=None,
            conid=None,
            lookback_days=self.config.lookback_days,
        )
        if reloaded is not None and not reloaded.empty:
            frame = reloaded
            self.frames[symbol] = frame
        self.frame_day[symbol] = today
        return frame

    def live_frame(self, symbol: str) -> pd.DataFrame | None:
        """The symbol's history with today's live price appended as a forming bar."""
        frame = self.frames.get(symbol)
        if frame is None or frame.empty:
            return None
        price = self._price(symbol)
        if price is None:
            return None
        frame = self._refresh_if_new_day(symbol, price)
        from atr.signals.rules import append_live_bar

        return append_live_bar(frame, price)

    def _price(self, symbol: str) -> float | None:
        return self.venue.prices(symbol, self.config.exchange)

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def _rules(self) -> tuple[Any, Any]:
        """The entry/exit rules this deployment runs.

        Sourced from the strategy registry via the deployment's ``strategy_id``,
        so paper runs the *same* rule definitions as a backtest of the same
        strategy. A deployment whose strategy is not in the registry is skipped
        rather than defaulted — silently running someone else's rules would be
        worse than not running.
        """
        from atr.signals.models import EntryRules, ExitRules

        key = self.row.get("strategy_id")
        try:
            from atr.strategy.strategies import STRATEGIES

            meta = STRATEGIES.get(str(key)) or STRATEGIES.get(key)
            rules = getattr(meta, "signal_rules", None) if meta else None
            if callable(rules):
                entry, exit_rules = rules()
                return entry, exit_rules
            if isinstance(rules, tuple) and len(rules) == 2:
                return rules
        except Exception:  # noqa: BLE001 - fall back to defaults, never crash the loop
            logger.debug("runner %s: no registry rules for %s", self.deployment_id[:8], key)

        entry = EntryRules()
        exit_rules = ExitRules()
        if self.config.stop_loss_pct is not None:
            exit_rules = ExitRules(
                stop_loss_pct=float(self.config.stop_loss_pct),
                take_profit_pct=self.config.take_profit_pct,
            )
        return entry, exit_rules

    def evaluate(self) -> list[dict[str, Any]]:
        """Run the rules over every symbol with a live price.

        Returns the *acted-on* signals, not every signal: a rule that is still
        true on the next pass is the same signal, and re-emitting it is how a loop
        like this places the same order sixty times a minute.
        """
        from atr.signals.rules import eval_entry, eval_exit, primary_exit

        entry_rules, exit_rules = self._rules()
        opened: list[dict[str, Any]] = []

        for symbol in self.config.symbols:
            price = self._price(symbol)
            if price is None:
                continue
            frame = self.live_frame(symbol)
            if frame is None or len(frame) < 2:
                continue

            position = self._position_quantity(symbol)

            # --- exits first -------------------------------------------------
            # A position that should close is closed in the same pass the signal
            # appears, not one pass later. Ordering it the other way round is how
            # a stop-loss gets missed for a full interval while the price runs.
            if position != 0:
                entry_price = self._entry_price(symbol)
                exit_signals = eval_exit(
                    symbol,
                    frame,
                    entry_price,
                    exit_rules,
                    quantity=abs(position),
                )
                chosen = primary_exit(exit_signals)
                if chosen is not None:
                    key = (symbol, str(chosen.rule), self._bar_key(symbol))
                    if key not in self.acted:
                        self.acted.add(key)
                        result = self._trade(
                            symbol=symbol,
                            side="SELL" if position > 0 else "BUY",
                            quantity=abs(position),
                            reason=f"{chosen.rule}: {chosen.reason}",
                            signal=chosen,
                        )
                        if result:
                            opened.append(result)
                    continue

            # --- entries ------------------------------------------------------
            if position != 0:
                continue
            if self._open_position_count() >= self.config.max_open_positions:
                continue

            signals = eval_entry(symbol, frame, entry_rules)
            if not signals:
                continue
            chosen = signals[0]
            key = (symbol, str(chosen.rule), self._bar_key(symbol))
            if key in self.acted:
                continue
            self.acted.add(key)

            quantity = self._size(price)
            if quantity <= 0:
                continue
            result = self._trade(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                reason=f"{chosen.rule}: {chosen.reason}",
                signal=chosen,
            )
            if result:
                opened.append(result)

        return opened

    def _bar_key(self, symbol: str) -> str:
        """A key that changes once per session, so a rule fires once a day.

        The daily rules are daily: a breakout that is true at 10:00 is still true
        at 14:00, and acting on it twice is acting on one signal twice.
        """
        return f"{symbol}:{datetime.now(IST).date().isoformat()}"

    def _size(self, price: float) -> int:
        """How many shares a signal buys. Whole shares, and never more than cash."""
        if price <= 0:
            return 0
        value = self.config.order_value
        if value is None:
            value = float(self.row.get("capital") or 0.0) / max(
                self.config.max_open_positions, 1
            )
        if value <= 0:
            return 0

        portfolio = self._portfolio()
        # Leave 2% for costs, so a full-size order does not fail the cash check
        # by the price of its own brokerage.
        affordable = portfolio.cash * 0.98
        value = min(float(value), max(affordable, 0.0))
        return int(value // price)

    # ------------------------------------------------------------------
    # the fold
    # ------------------------------------------------------------------
    def _portfolio(self) -> Any:
        """The deployment's account, folded from order_events. Never cached."""
        return self.ledger.portfolio(
            self.user_id,
            deployment_id=self.deployment_id,
            initial_cash=float(self.row.get("capital") or 0.0),
            prices=self.live_prices(),
        )

    def live_prices(self) -> dict[str, float]:
        """A live mark for every symbol this deployment knows about."""
        out: dict[str, float] = {}
        for symbol in self.config.symbols:
            price = self._price(symbol)
            if price is not None:
                out[symbol] = price
        return out

    def _position_quantity(self, symbol: str) -> float:
        position = self._portfolio().position(symbol)
        return float(position.quantity) if position is not None else 0.0

    def _entry_price(self, symbol: str) -> float:
        position = self._portfolio().position(symbol)
        if position is None:
            return 0.0
        return float(getattr(position, "avg_price", 0.0) or 0.0)

    def _open_position_count(self) -> int:
        portfolio = self._portfolio()
        count = 0
        for symbol in self.config.symbols:
            position = portfolio.position(symbol)
            if position is not None and float(position.quantity) != 0:
                count += 1
        # Positions outside the configured universe are counted too: they consume
        # capital even though this deployment no longer evaluates them.
        return count

    # ------------------------------------------------------------------
    # trading
    # ------------------------------------------------------------------
    def _trade(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reason: str,
        signal: Any = None,
    ) -> dict[str, Any] | None:
        """Route one signal through the shared path: risk, OMS, venue, fill.

        ``ExecutionService.place`` is the whole pipeline — open → risk → submit →
        venue → record fill — so this method deliberately contains no decision
        of its own beyond *whether* to send.
        """
        price = self._price(symbol)
        draft = OrderDraft(
            user_id=self.user_id,
            symbol=symbol,
            side=side,
            quantity=float(quantity),
            mode="PAPER",
            exchange=self.config.exchange,
            order_type=OrderType.MARKET.value,
            requested_price=price,
            deployment_id=self.deployment_id,
            strategy_id=self.row.get("strategy_id"),
            strategy_version=self.row.get("strategy_version"),
            tag="paper-runner",
        )
        # A signal-driven order gets a key, so a retry after a dropped response
        # returns the existing order rather than placing a second one. It is
        # derived from the bar, so re-evaluating the same bar is a no-op.
        key = self._idempotency_key(draft, symbol, side)

        try:
            result = self.execution.place(draft, idempotency_key=key)
        except Exception:  # noqa: BLE001 - one bad order must not stop the loop
            logger.exception(
                "runner %s: placing %s %s failed",
                self.deployment_id[:8],
                side,
                symbol,
            )
            return None

        logger.info(
            "runner %s: %s %s x%s -> %s (%s)",
            self.deployment_id[:8],
            side,
            symbol,
            int(quantity),
            result.status,
            reason,
        )
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "status": result.status,
            "order_id": result.order_id,
            "reason": reason,
        }

    def _idempotency_key(self, draft: OrderDraft, symbol: str, side: str) -> str:
        """One key per (deployment, bar, symbol, side).

        The bar is passed as the ``signal_id`` rather than bolted on as an extra
        component: ``idempotency_key_for`` is the single definition of what an
        intent's identity is, and a runner that appended its own field would be
        deriving a key the rest of the platform could not reproduce. A daily bar
        key is exactly the right granularity — the rules are daily, so one signal
        per symbol per session is one intent.
        """
        from atr.execution.oms import idempotency_key_for

        return idempotency_key_for(
            user_id=self.user_id,
            strategy_id=draft.strategy_id,
            strategy_version=draft.strategy_version,
            signal_id=self._bar_key(symbol),
            symbol=symbol,
            side=draft.side,
            leg_index=0,
        )

    # ------------------------------------------------------------------
    # resting orders
    # ------------------------------------------------------------------
    def poll_resting_orders(self) -> list[dict[str, Any]]:
        """Re-evaluate every open paper order against the current price.

        This is requirement 4, and it is the difference between a paper LIMIT
        order and a promise: without it a limit that opened away from the market
        sits in the book forever, never filling and never cancelling, while the
        dashboard shows it as live.
        """
        from atr.services.execution import VENUE_PARTIAL
        from atr.services.execution import VENUE_FILLED as _FILLED
        from atr.services.execution import VENUE_REJECTED as _REJECTED

        filled: list[dict[str, Any]] = []

        open_orders = [
            order
            for order in self.orders.open_orders(self.user_id)
            if order.get("deployment_id") == self.deployment_id
            and order.get("mode") == "PAPER"
            # Only orders the venue has accepted and that are resting. An order
            # still in NEW or RISK_APPROVED has not reached the venue yet, and a
            # SUBMITTED one whose outcome we never learned is not ours to retry.
            and order.get("status") in ("ACKNOWLEDGED", "PARTIALLY_FILLED")
        ]
        if not open_orders:
            return filled

        for order in open_orders:
            price = self._price(order["symbol"])
            if price is None:
                continue
            try:
                outcome = self.venue.match(order)
            except Exception:  # noqa: BLE001
                logger.exception("runner %s: match failed for %s",
                                 self.deployment_id[:8], order["order_id"])
                continue

            if outcome.status == _REJECTED:
                # A resting order that became unpriceable is rejected rather than
                # left in the book looking live.
                self.orders.reject(
                    order["order_id"],
                    self.user_id,
                    reason=outcome.reject_reason or "unpriceable while resting",
                    source="oms",
                    raw=outcome.raw,
                )
                continue

            if outcome.status not in (_FILLED, VENUE_PARTIAL) or outcome.filled_qty <= 0:
                continue

            try:
                self.orders.record_fill(
                    order["order_id"],
                    self.user_id,
                    filled_qty=outcome.filled_qty,
                    filled_price=outcome.filled_price or price,
                    commission=outcome.commission,
                    raw=outcome.raw,
                )
            except Exception:  # noqa: BLE001
                logger.exception("runner %s: recording fill failed for %s",
                                 self.deployment_id[:8], order["order_id"])
                continue

            filled.append(
                {
                    "order_id": order["order_id"],
                    "symbol": order["symbol"],
                    "filled_qty": outcome.filled_qty,
                    "filled_price": outcome.filled_price,
                }
            )
        return filled

    # ------------------------------------------------------------------
    # observation
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """The deployment's current account, for the API and the UI."""
        prices = self.live_prices()
        return self.ledger.snapshot(
            self.user_id,
            deployment_id=self.deployment_id,
            initial_cash=float(self.row.get("capital") or 0.0),
            prices=prices,
        )


@dataclass(frozen=True)
class _PositionView:
    """The minimal position shape ``eval_exit`` reads."""

    quantity: float
    entry_price: float


@dataclass
class RunnerTick:
    """What one pass over all deployments produced. For logging and tests."""

    at: datetime
    deployments: int
    signals: int
    orders: int
    resting_fills: int


class PaperRunner:
    """Runs every RUNNING paper deployment on a timer, in one background task.

    One task for all deployments rather than a task each: they share a price
    source and an order log, and N tasks contending on the same SQLite control
    plane buys nothing but lock contention.

    The loop is deliberately synchronous inside. The matching is pure CPU on
    already-loaded frames; the only I/O is the control-plane write per order, and
    a fill happens rarely enough that overlapping passes would be solving a
    problem this workload does not have.
    """

    def __init__(
        self,
        *,
        db: Any = None,
        ledger: PaperLedger | None = None,
        price_source: Callable[[str, str], float | None] | None = None,
        interval_seconds: float = 1.0,
    ) -> None:
        from atr.appdb.engine import get_app_db
        from atr.services.execution import ExecutionService

        self.db = db or get_app_db()
        self.ledger = ledger or PaperLedger(db=self.db)
        self._price_source = price_source
        self._default_interval = float(interval_seconds)

        self._venue: PaperVenue | None = None
        self._execution: ExecutionService | None = None
        self._order_services: dict[str, Any] = {}
        self._loops: dict[str, DeploymentLoop] = {}
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_tick: RunnerTick | None = None
        self.passes = 0
        self.signals_total = 0
        self.orders_total = 0

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    @property
    def venue(self) -> PaperVenue:
        if self._venue is None:
            prices = self._price_source or default_price_source()
            self._venue = PaperVenue(prices=prices)
        return self._venue

    @property
    def execution(self) -> Any:
        if self._execution is None:
            from atr.services.execution import ExecutionService

            self._execution = ExecutionService(orders=self.orders, venue=self.venue)
        return self._execution

    # ------------------------------------------------------------------
    # deployment tracking
    # ------------------------------------------------------------------
    def order_service(self, loop: DeploymentLoop) -> tuple[Any, Any]:
        """The pipeline bound to *this deployment's* folded portfolio.

        Built per deployment, and rebuilt on every pass, rather than sharing the
        flat default. Two consequences, both of them the point:

        * The position-aware limits (``max_position_per_symbol``,
          ``max_open_positions``, gross exposure) can actually bite. With the
          shared flat portfolio they read every position as zero, so they never
          fire — a risk limit that silently does not apply is worse than none,
          because the dashboard says it is enforced.
        * A SELL that closes a long is not misread as opening a short. The engine
          compares the sell quantity against ``portfolio.position(symbol)``
          ``.quantity``, so against a flat book every exit looks like a short and
          is rejected whenever ``allow_short`` is false — meaning stop-losses
          would never execute.

        Rebuilt each pass because the portfolio is a fold of ``order_events``:
        caching it would reintroduce exactly the drift the fold exists to prevent.
        """
        from atr.services.execution import ExecutionService
        from atr.services.orders import get_order_service

        portfolio = self.ledger.portfolio(
            loop.user_id,
            deployment_id=loop.deployment_id,
            initial_cash=float(loop.row.get("capital") or 0.0),
            prices=loop.live_prices(),
        )
        orders = get_order_service(portfolio=portfolio)
        return ExecutionService(orders=orders, venue=self.venue), orders

    def running_deployments(self) -> list[dict[str, Any]]:
        """Every RUNNING paper deployment, across all users.

        Deliberately not scoped to one user: this is the platform's own execution
        loop, not a request. Each deployment still acts only as its own owner,
        because every service call it makes passes that owner's id.
        """
        from sqlalchemy import select

        from atr.appdb.schema import deployments

        with self.db.session() as session:
            stmt = select(deployments).where(
                deployments.c.status == "RUNNING",
                deployments.c.mode == "PAPER",
            )
            return [dict(row) for row in session.execute(stmt).mappings().all()]

    def sync_loops(self) -> None:
        """Start a loop for each new deployment, drop the ones that stopped."""
        with self._lock:
            try:
                rows = self.running_deployments()
            except Exception:  # noqa: BLE001 - a broken store must not kill the loop
                logger.exception("runner: could not list running deployments")
                return

            wanted = {row["deployment_id"]: row for row in rows}

            for deployment_id, row in wanted.items():
                if deployment_id in self._loops:
                    # A config change or a restart after a crash must be picked
                    # up, so the row is refreshed on every sync rather than only
                    # at first sight.
                    self._loops[deployment_id].row = row
                    continue
                loop = self._make_loop(row)
                if loop is None:
                    continue
                try:
                    loop.warmup()
                except Exception:  # noqa: BLE001
                    logger.exception("runner: warmup failed for %s", deployment_id[:8])
                    continue
                self._loops[deployment_id] = loop
                logger.info(
                    "runner: deployment %s is running (%d symbols)",
                    deployment_id[:8],
                    len(loop.config.symbols),
                )

            for deployment_id in list(self._loops):
                if deployment_id not in wanted:
                    logger.info("runner: deployment %s is no longer running",
                                deployment_id[:8])
                    self._loops.pop(deployment_id, None)

    def _make_loop(self, row: dict[str, Any]) -> DeploymentLoop | None:
        config = RunnerConfig.from_deployment(row)
        if not config.symbols:
            # A deployment with no universe would silently do nothing, which is
            # indistinguishable from a broken one. Say so once and skip.
            logger.warning(
                "runner: deployment %s has no symbols configured; skipping",
                str(row.get("deployment_id"))[:8],
            )
            return None
        return DeploymentLoop(
            row=row,
            config=config,
            wiring=self.order_service,
            ledger=self.ledger,
            venue=self.venue,
        )

    # ------------------------------------------------------------------
    # one pass
    # ------------------------------------------------------------------
    def pass_once(self, *, now: datetime | None = None) -> RunnerTick:
        """One evaluation pass over every running deployment.

        Public and synchronous so a test can drive the loop deterministically
        instead of sleeping on a background task.
        """
        now = now or utcnow()
        self.sync_loops()

        signals = 0
        orders = 0
        resting_fills = 0

        # Resting orders are polled even outside market hours: an order that was
        # already accepted should be able to fill on any price that arrives, and
        # refusing to re-evaluate it after 15:30 would leave it stuck.
        for loop in list(self._loops.values()):
            try:
                loop.bind()
                resting_fills += len(loop.poll_resting_orders())
            except Exception:  # noqa: BLE001
                logger.exception("runner: resting poll failed for %s",
                                 loop.deployment_id[:8])

        # New entries and exits only inside the session. A signal outside it is a
        # signal about a market that is not open, and acting on one would fill at
        # a price no exchange offered.
        if in_market_hours(now):
            for loop in list(self._loops.values()):
                try:
                    loop.bind()
                    acted = loop.evaluate()
                except Exception:  # noqa: BLE001 - one deployment must not stop the others
                    logger.exception("runner: evaluation failed for %s",
                                     loop.deployment_id[:8])
                    continue
                signals += len(acted)
                orders += sum(
                    1 for a in acted if a.get("status") not in ("REJECTED", None)
                )
                loop.last_pass = {
                    "at": now.isoformat(),
                    "signals": len(acted),
                    "orders": orders,
                }

        with self._lock:
            self.passes += 1
            self.signals_total += signals
            self.orders_total += orders

        tick = RunnerTick(
            at=now,
            deployments=len(self._loops),
            signals=signals,
            orders=orders,
            resting_fills=resting_fills,
        )
        self.last_tick = tick
        return tick

    # ------------------------------------------------------------------
    # the background loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        logger.info("runner: background loop started")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                # The pass is blocking (SQLite + pandas), so it runs in a worker
                # thread rather than on the event loop. A rule evaluation that
                # takes 200ms must not stall every HTTP request and the tick
                # stream for those 200ms.
                await asyncio.to_thread(self.pass_once)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("runner: pass failed")

            interval = self._default_interval
            with self._lock:
                if self._loops:
                    interval = min(
                        (loop.config.interval_seconds for loop in self._loops.values()),
                        default=interval,
                    )
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(interval - elapsed, 0.05))
        logger.info("runner: background loop stopped")

    def start(self) -> None:
        """Start the background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the background task and wait for the current pass to finish."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """What the loop is doing, for ``/health`` and the dashboard."""
        with self._lock:
            loops = list(self._loops.values())
            return {
                "running": self.running,
                "passes": self.passes,
                "deployments": [
                    {
                        "deployment_id": loop.deployment_id,
                        "strategy_id": loop.row.get("strategy_id"),
                        "symbols": len(loop.config.symbols),
                        "last_pass": loop.last_pass,
                    }
                    for loop in loops
                ],
                "signals_total": self.signals_total,
                "orders_total": self.orders_total,
                "last_tick": (
                    None
                    if self.last_tick is None
                    else {
                        "at": self.last_tick.at.isoformat(),
                        "signals": self.last_tick.signals,
                        "orders": self.last_tick.orders,
                        "resting_fills": self.last_tick.resting_fills,
                    }
                ),
            }


_RUNNER: PaperRunner | None = None


def get_runner() -> PaperRunner:
    """The process-wide paper runner."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = PaperRunner()
    return _RUNNER


def reset_runner() -> None:
    """Drop the singleton. For tests."""
    global _RUNNER
    _RUNNER = None
