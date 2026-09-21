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
import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from atr.appdb.engine import utcnow
from atr.core.enums import OrderType
from atr.execution.oms import OrderDraft
from atr.market_calendar import (
    get_market_calendar,
)
from atr.services.paper import (
    PaperLedger,
    PaperVenue,
    TickPrice,
    default_price_source,
)

from atr.signals.session import MarketWeek, build_context
from atr.strategy.definition import parse_definition, resolve_rules

logger = logging.getLogger("atr.services.runner")

#: One reading of the market's prior week, shared by every deployment that asks for it.
_MARKET_WEEK = MarketWeek()

IST = ZoneInfo("Asia/Kolkata")

#: How long a signal stays actionable. Beyond this it is dropped rather than
#: filled late: a rule that fired at 10:02 was true of the market at 10:02.
SIGNAL_TTL_SECONDS = 300


class RunnerDeploymentState:
    WAITING_FOR_MARKET = "WAITING_FOR_MARKET"
    WAITING_FOR_TICKS = "WAITING_FOR_TICKS"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


def _now_ist() -> datetime:
    """The clock the market calendar is asked about.

    Naive datetimes mean Indian time to the calendar. The runner used to default to
    naive UTC, which put the "open" window at 14:45 to 21:00 Indian time and left a run
    waiting for a market that was already trading.
    """
    return datetime.now(IST)


def in_market_hours(now: datetime | None = None) -> bool:
    """Whether the NSE/BSE regular session is open.

    Backed by the market calendar abstraction: respects weekends, holidays,
    and exchange session bounds (09:15 to 15:30 IST).
    """
    return get_market_calendar().is_market_open(now)


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
    sizing: dict[str, Any] | None = None

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
            sizing=raw.get("sizing") if isinstance(raw.get("sizing"), dict) else None,
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
    wiring: Callable[[DeploymentLoop], tuple[Any, Any]]
    ledger: PaperLedger
    venue: PaperVenue
    #: Resolves this deployment's (entry, exit) rules, or ``None`` when they
    #: cannot be resolved. Injected rather than imported so the runner does not
    #: reach into the strategy-version store itself.
    rules_for: Callable[[DeploymentLoop], tuple[Any, Any] | None] | None = None
    features: Any = None
    #: symbol -> daily frame, loaded once and appended to as bars close.
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: The forming bar's date, so a day boundary is detected without a calendar.
    frame_day: dict[str, Any] = field(default_factory=dict)
    #: Signals already acted on, keyed by (symbol, rule, bar) so a rule that
    #: stays true for an hour does not open an hour of orders.
    acted: set[tuple[str, str, str]] = field(default_factory=set)
    #: The first fresh price seen per symbol each day: (day, price, minutes after the open).
    _first_price: dict[str, tuple[Any, float, float]] = field(default_factory=dict)
    #: The account as folded during the current pass; see ``_portfolio``.
    _in_pass: bool = False
    _fold: Any = None
    #: Set when rules emitted but nothing was traded, for the status surface.
    last_pass: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utcnow)
    #: Why this deployment is not trading, when it is not. ``None`` means it
    #: resolved its rules and is evaluating normally.
    blocked_reason: str | None = None
    #: (execution, orders) for the current pass, set by ``bind``.
    _bound: tuple[Any, Any] | None = None

    # Observability state and metrics
    state: str = field(default=RunnerDeploymentState.RUNNING)
    last_tick_time: datetime | None = None
    last_tick_age_seconds: float | None = None
    last_strategy_evaluation: datetime | None = None
    last_signal: dict[str, Any] | None = None
    last_risk_decision: dict[str, Any] | None = None
    last_order: dict[str, Any] | None = None
    last_fill: dict[str, Any] | None = None
    last_sizing: dict[str, Any] | None = None
    skipped_evaluations_count: int = 0
    skipped_fills_count: int = 0
    skipped_reason: str | None = None

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

    def _price_with_meta(
        self, symbol: str, now: datetime | None = None
    ) -> tuple[float | None, datetime | None, float | None]:
        """Return (price, timestamp, age_seconds) for a symbol."""
        try:
            val = self.venue.prices(symbol, self.config.exchange)
        except Exception:
            return None, None, None

        if val is None:
            return None, None, None
        if isinstance(val, TickPrice):
            clock_time = now or self.venue.clock()
            age = val.get_age_seconds(now=clock_time)
            return val.price, val.timestamp, age
        try:
            p = float(val)
            if p == p and p > 0:
                clock_time = now or self.venue.clock()
                return p, clock_time, 0.0
            return None, None, None
        except (ValueError, TypeError):
            return None, None, None

    def _price(self, symbol: str, now: datetime | None = None) -> float | None:
        p, _, _ = self._price_with_meta(symbol, now=now)
        return p

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def _rules(self) -> tuple[Any, Any] | None:
        """The entry/exit rules this deployment runs, or ``None`` if unresolvable."""
        if self.rules_for is not None:
            try:
                resolved = self.rules_for(self)
            except Exception as exc:  # noqa: BLE001 — a broken store must not trade
                self.blocked_reason = f"could not resolve strategy rules: {exc}"
                self.state = RunnerDeploymentState.ERROR
                logger.exception(
                    "runner %s: rule resolution failed; not trading",
                    self.deployment_id[:8],
                )
                return None
            if resolved is not None:
                self.blocked_reason = None
                return resolved
            if self.blocked_reason is None:
                self.blocked_reason = (
                    f"strategy {self.row.get('strategy_id')!r} version "
                    f"{self.row.get('strategy_version')!r} could not be resolved"
                )
            self.state = RunnerDeploymentState.ERROR
            return None

        # No resolver injected (a bare loop in a unit test).
        key = self.row.get("strategy_id")
        try:
            from atr.strategy.strategies import STRATEGIES

            meta = STRATEGIES.get(str(key)) or STRATEGIES.get(key)
            rules = getattr(meta, "signal_rules", None) if meta else None
            if callable(rules):
                entry, exit_rules = rules()
                self.blocked_reason = None
                return entry, exit_rules
            if isinstance(rules, tuple) and len(rules) == 2:
                self.blocked_reason = None
                return rules
        except Exception:  # noqa: BLE001
            logger.debug("runner %s: no registry rules for %s", self.deployment_id[:8], key)

        self.blocked_reason = (
            f"no rule definition for strategy {key!r}; refusing to trade on "
            "unrelated defaults"
        )
        self.state = RunnerDeploymentState.ERROR
        logger.warning(
            "runner %s: %s", self.deployment_id[:8], self.blocked_reason
        )
        return None

    def evaluate(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run one evaluation pass. The account is folded once for the pass, not per stock."""
        self._in_pass, self._fold = True, None
        try:
            return self._evaluate(now)
        finally:
            self._in_pass, self._fold = False, None

    def _evaluate(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Run the rules over every symbol with a live price.

        Observability and freshness rules:
        - Verifies the market calendar.
        - Verifies live tick existence and age against max_tick_age_seconds.
        - Skips evaluation if ticks are stale or absent.
        - Updates deployment state (WAITING_FOR_MARKET, WAITING_FOR_TICKS, RUNNING, ERROR).
        - Records last_strategy_evaluation, last_signal, last_order, skipped counts.
        """
        from atr.market_calendar import get_market_calendar
        from atr.signals.rules import eval_entry, eval_exit, primary_exit

        eval_time = now or _now_ist()
        cal = get_market_calendar()

        resolved = self._rules()
        if resolved is None:
            self.state = RunnerDeploymentState.ERROR
            self.skipped_reason = self.blocked_reason or "rule resolution error"
            self.skipped_evaluations_count += 1
            self.last_pass = {
                "at": eval_time.isoformat(),
                "signals": 0,
                "orders": 0,
                "state": self.state,
                "blocked": self.blocked_reason,
            }
            return []

        if not cal.is_market_open(eval_time):
            self.state = RunnerDeploymentState.WAITING_FOR_MARKET
            reason = cal.reason_closed(eval_time) or "market is closed"
            self.skipped_reason = reason
            self.skipped_evaluations_count += 1
            self.last_pass = {
                "at": eval_time.isoformat(),
                "signals": 0,
                "orders": 0,
                "state": self.state,
                "skipped_reason": reason,
            }
            return []

        entry_rules, exit_rules = resolved
        opened: list[dict[str, Any]] = []
        max_age = getattr(self.venue, "max_tick_age_seconds", 60.0)

        # Check universe prices and freshness
        valid_symbols_count = 0
        latest_tick_dt: datetime | None = None
        min_age: float | None = None

        for symbol in self.config.symbols:
            price, ts, age = self._price_with_meta(symbol, now=eval_time)
            if ts is not None:
                if latest_tick_dt is None or ts > latest_tick_dt:
                    latest_tick_dt = ts
            if age is not None:
                if min_age is None or age < min_age:
                    min_age = age

            if price is None:
                continue

            # Stale tick check
            if max_age is not None and age is not None and age > max_age:
                continue

            valid_symbols_count += 1

        self.last_tick_time = latest_tick_dt
        self.last_tick_age_seconds = min_age

        if valid_symbols_count == 0:
            self.state = RunnerDeploymentState.WAITING_FOR_TICKS
            why = "no fresh live ticks available" if min_age is not None else "no live ticks received"
            self.skipped_reason = why
            self.skipped_evaluations_count += 1
            self.last_pass = {
                "at": eval_time.isoformat(),
                "signals": 0,
                "orders": 0,
                "state": self.state,
                "skipped_reason": why,
            }
            return []

        self.state = RunnerDeploymentState.RUNNING

        # Fresh ticks available and rules resolved: deployment is RUNNING
        self.state = RunnerDeploymentState.RUNNING
        self.skipped_reason = None
        self.last_strategy_evaluation = eval_time
        base_context = self._session_context(eval_time, cal, entry_rules)
        candidates: list[tuple[str, float, pd.DataFrame, Any, tuple[str, str, str]]] = []

        for symbol in self.config.symbols:
            price, ts, age = self._price_with_meta(symbol, now=eval_time)
            if price is None:
                self.skipped_fills_count += 1
                continue
            if max_age is not None and age is not None and age > max_age:
                self.skipped_fills_count += 1
                continue

            frame = self.live_frame(symbol)
            if frame is None or len(frame) < 2:
                continue

            opened_at, minutes = self._session_open(symbol, price, eval_time, cal)
            context = replace(base_context, open_price=opened_at, minutes_since_open=minutes)
            position = self._position_quantity(symbol)

            # --- exits first -------------------------------------------------
            if position != 0:
                entry_price = self._entry_price(symbol)
                exit_signals = eval_exit(
                    symbol,
                    frame,
                    entry_price,
                    exit_rules,
                    quantity=abs(position),
                    context=context,
                )
                chosen = primary_exit(exit_signals)
                if chosen is not None:
                    self.last_signal = {
                        "symbol": symbol,
                        "rule": str(chosen.rule),
                        "reason": chosen.reason,
                        "side": "SELL" if position > 0 else "BUY",
                        "at": eval_time.isoformat(),
                    }
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

            signals = eval_entry(symbol, frame, entry_rules, context)
            if not signals:
                continue
            chosen = signals[0]
            self.last_signal = {
                "symbol": symbol,
                "rule": str(chosen.rule),
                "reason": chosen.reason,
                "side": "BUY",
                "at": eval_time.isoformat(),
            }
            key = (symbol, str(chosen.rule), self._bar_key(symbol))
            if key in self.acted:
                continue
            candidates.append((symbol, price, frame, chosen, key))

        # Entries are placed after the whole universe has been looked at, best first, so
        # a full book is filled by the strongest candidates rather than the first ones
        # the loop happened to reach. Exits above have already freed their slots.
        candidates.sort(key=lambda c: c[3].detail.get("gap_pct", 0.0))
        for symbol, price, frame, chosen, key in candidates:
            if self._open_position_count() >= self.config.max_open_positions:
                break
            self.acted.add(key)
            quantity = self._size(
                price, symbol=symbol, frame=frame, entry_rules=entry_rules
            )
            if quantity <= 0:
                continue
            result = self._trade(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                reason=f"{chosen.rule}: {chosen.reason}",
                signal=chosen,
                sizing=self.last_sizing,
            )
            if result:
                opened.append(result)

        if not opened and self.skipped_reason is None:
            self.skipped_reason = "Strategy produced no signal"

        return opened

    def _session_context(self, when: datetime, cal: Any, entry_rules: Any) -> Any:
        """What today's session adds to the rules. The market reading is read only when asked for."""
        wants_market = getattr(entry_rules, "gap_market_min_pct", None) is not None
        return build_context(
            when, cal, _MARKET_WEEK if wants_market else None, open_price=None, minutes_since_open=None
        )

    def _session_open(
        self, symbol: str, price: float, when: datetime, cal: Any
    ) -> tuple[float | None, float | None]:
        """The session's first fresh price for a symbol, and how long after 09:15 it was seen.

        The tick feed carries the last traded price and nothing else, so the open is taken
        as the first price this loop sees each day. A loop that starts late sees a later
        price and a large delay; the gap rule then declines rather than call it the open.
        """
        local = when.astimezone(IST) if when.tzinfo else when.replace(tzinfo=IST)
        today = local.date()
        seen = self._first_price.get(symbol)
        if seen is None or seen[0] != today:
            start, _ = cal.session_bounds(today)
            minutes = max(0.0, (local - start).total_seconds() / 60.0)
            seen = (today, price, minutes)
            self._first_price[symbol] = seen
        return seen[1], seen[2]

    def _bar_key(self, symbol: str) -> str:
        """A key that changes once per session, so a rule fires once a day.

        The daily rules are daily: a breakout that is true at 10:00 is still true
        at 14:00, and acting on it twice is acting on one signal twice.
        """
        return f"{symbol}:{datetime.now(IST).date().isoformat()}"

    def _size(
        self,
        price: float,
        symbol: str | None = None,
        frame: pd.DataFrame | None = None,
        entry_rules: Any = None,
    ) -> int:
        """How many shares a signal buys, calculated via PositionSizingEngine."""
        if price <= 0:
            self.last_sizing = None
            return 0

        portfolio = self._portfolio()
        capital = float(self.row.get("capital") or 0.0)
        available_capital = float(portfolio.cash)

        from atr.strategy.sizing import PositionSizingEngine, SizingConfig, SizingMethod

        raw_sizing = self.config.sizing
        if not raw_sizing and isinstance(self.row.get("definition"), dict):
            raw_sizing = self.row["definition"].get("sizing")

        if raw_sizing and isinstance(raw_sizing, dict):
            sizing_cfg = SizingConfig.from_dict(raw_sizing)
        elif self.config.order_value is not None:
            sizing_cfg = SizingConfig(
                method=SizingMethod.FIXED_RUPEE_VALUE,
                fixed_rupee_value=float(self.config.order_value),
            )
        else:
            fraction = 1.0 / max(self.config.max_open_positions, 1)
            sizing_cfg = SizingConfig(
                method=SizingMethod.PERCENT_OF_CAPITAL,
                capital_fraction=fraction,
                risk_per_trade_pct=self.config.stop_loss_pct or 1.0,
            )

        # Extract stop loss
        stop_loss_pct = self.config.stop_loss_pct
        if stop_loss_pct is None and entry_rules is not None:
            stop_loss_pct = getattr(entry_rules, "stop_loss_pct", None)

        # Extract ATR
        atr_val: float | None = None
        if frame is not None and not frame.empty and len(frame) >= 15:
            try:
                from atr.strategy.indicators import atr as calc_atr
                high = frame["high"] if "high" in frame.columns else frame["close"]
                low = frame["low"] if "low" in frame.columns else frame["close"]
                close = frame["close"]
                atr_series = calc_atr(high, low, close)
                val = float(atr_series.dropna().iloc[-1])
                if math.isfinite(val) and val > 0:
                    atr_val = val
            except Exception:
                atr_val = None

        current_stock_exposure = 0.0
        if symbol:
            pos = portfolio.position(symbol)
            if pos and not pos.is_flat:
                current_stock_exposure = abs(float(pos.quantity) * price)

        result = PositionSizingEngine.calculate(
            sizing_cfg,
            entry_price=price,
            capital=capital,
            available_capital=available_capital,
            stop_loss_pct=stop_loss_pct,
            atr=atr_val,
            current_stock_exposure=current_stock_exposure,
            current_portfolio_exposure=float(portfolio.gross_exposure),
        )

        res_dict = result.as_dict()
        self.last_sizing = res_dict
        return result.final_quantity

    # ------------------------------------------------------------------
    # the fold
    # ------------------------------------------------------------------
    def _portfolio(self) -> Any:
        """The deployment's account, folded from order_events.

        Rebuilt on every call, except inside one evaluation pass: there it is built once
        and rebuilt only after a trade, because a pass asks for it twice per stock and a
        thousand stocks made that the slowest part of the loop.
        """
        if self._in_pass:
            if self._fold is None:
                self._fold = self._fold_now()
            return self._fold
        return self._fold_now()

    def _fold_now(self) -> Any:
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
        """How many positions this deployment holds, across its whole book.

        **Every non-flat position, not only the configured universe.** The
        previous version iterated ``config.symbols``, which made the count depend
        on a list that is editable while the deployment runs: drop a symbol from
        the universe while holding it and the position becomes invisible to
        ``max_open_positions``, so the deployment can open its way past the cap
        while the risk surface reports the limit as enforced. A position consumes
        capital whether or not the loop still evaluates its symbol, so it is
        counted.

        The fold is the source, not a counter held here — see the module
        docstring.
        """
        portfolio = self._portfolio()
        return sum(
            1
            for position in portfolio.positions.values()
            if not position.is_flat
        )

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
        sizing: dict[str, Any] | None = None,
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
            # The bar this signal belongs to, so the order is traceable back to
            # the context recorded against the same bar (see
            # ``_record_signal_context``). Idempotency is unchanged: the key is
            # still derived independently in ``_idempotency_key``.
            signal_id=self._bar_key(symbol),
            tag="paper-runner",
            # The rule's own words, carried onto the order's NEW event. This is
            # what lets the monitoring timeline answer "why did this order
            # exist?" instead of showing a fill with no antecedent.
            signal_reason=reason,
        )
        # A signal-driven order gets a key, so a retry after a dropped response
        # returns the existing order rather than placing a second one. It is
        # derived from the bar, so re-evaluating the same bar is a no-op.
        key = self._idempotency_key(draft, symbol, side)

        now_iso = utcnow().isoformat()
        try:
            result = self.execution.place(draft, idempotency_key=key)
            self._fold = None  # the account changed: the next look must not use the old fold
        except Exception as exc:  # noqa: BLE001 - one bad order must not stop the loop
            self._fold = None
            logger.exception(
                "runner %s: placing %s %s failed",
                self.deployment_id[:8],
                side,
                symbol,
            )
            self.skipped_fills_count += 1
            self.skipped_reason = f"System error: {exc}"
            self.last_risk_decision = {
                "symbol": symbol,
                "side": side,
                "quantity": float(quantity),
                "approved": False,
                "reason": f"System error placing order: {exc}",
                "rule": reason,
                "at": now_iso,
            }
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
        order_info = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "status": result.status,
            "order_id": result.order_id,
            "reason": reason,
            "sizing": sizing or self.last_sizing,
        }
        self.last_order = order_info

        # Track risk decision and fill state
        is_risk_rejected = (result.status == "REJECTED") and (
            not getattr(result, "broker_order_id", None)
            or "risk" in (result.reject_reason or "").lower()
            or "limit" in (result.reject_reason or "").lower()
            or "exposure" in (result.reject_reason or "").lower()
        )
        if is_risk_rejected:
            self.last_risk_decision = {
                "symbol": symbol,
                "side": side,
                "quantity": float(quantity),
                "approved": False,
                "reason": result.reject_reason or "Rejected by risk engine",
                "rule": reason,
                "at": now_iso,
            }
            self.skipped_fills_count += 1
            self.skipped_reason = f"Risk rejected signal: {result.reject_reason or 'Risk limit exceeded'}"
        else:
            self.last_risk_decision = {
                "symbol": symbol,
                "side": side,
                "quantity": float(quantity),
                "approved": True,
                "reason": "Approved by risk engine",
                "rule": reason,
                "at": now_iso,
            }
            if result.status == "REJECTED":
                self.skipped_fills_count += 1
                self.skipped_reason = f"Order rejected: {result.reject_reason or 'Venue rejected order'}"
            elif result.status == "FILLED":
                self.last_fill = order_info
                self.skipped_reason = "Paper fill completed"
            else:
                self.skipped_reason = "Order waiting for fill"
        self._record_signal_context(symbol=symbol, side=side)
        return order_info

    def _record_signal_context(self, *, symbol: str, side: str) -> None:
        """Annotate the just-raised signal with its market/sector/stock context.

        Best-effort and strictly post-hoc: it runs *after* the order has been
        placed (so a slow market scan can never delay or block a signal), and any
        failure is swallowed — the context record is an annotation, never a gate.

        The ``signal_id`` is the bar key, matching ``OrderDraft.signal_id``, so
        the recorded context and the order agree on the signal they describe and
        the analytics layer can resolve one against the other.
        """
        try:
            from atr.signal_context.service import get_signal_context_service

            get_signal_context_service().enrich_live(
                user_id=self.user_id,
                symbol=symbol,
                action="SELL" if str(side).upper() in ("SELL", "S") else "BUY",
                signal_ts=utcnow().isoformat(),
                signal_id=self._bar_key(symbol),
                strategy_id=self.row.get("strategy_id"),
                strategy_version=self.row.get("strategy_version"),
                signal_source="PAPER",
            )
        except Exception as exc:  # noqa: BLE001 — context is not a gate
            logger.debug(
                "runner %s: signal context for %s skipped: %s",
                self.deployment_id[:8],
                symbol,
                exc,
            )

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
        #: (strategy_id, version) -> resolved (entry, exit) rules or the
        #: ``_UNRESOLVED`` sentinel. A version is immutable, so once read it
        #: cannot change; caching is correct and keeps the pass off the database.
        self._rules_cache: dict[tuple[str, int], Any] = {}
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
    def resolve_rules(self, loop: DeploymentLoop) -> tuple[Any, Any] | None:
        """Read the deployment's *version* and turn it into entry/exit rules.

        This is the seam that makes "select an immutable version" mean something.
        A deployment stores ``(strategy_id, strategy_version)``; the version's
        canonical ``definition`` is the subject, and it is read by exact number
        — never "latest" — for the same reason the backtest service does it: a
        run whose subject can change after the fact is not reproducible, and a
        deployment is a run that lasts months.

        Accepted definition shapes, in the order they are tried:

        * ``{"entry": {...}, "exit": {...}}`` — flat rule blocks, the natural
          shape for a version authored from the rule layer.
        * ``{"rules": {"entry": ..., "exit": ...}}`` — the same, nested, which is
          what ``DATA_MODEL.md`` describes when it calls the column
          "canonical JSON of rules / params / code".
        * ``engine_key`` naming a registry strategy with a ``signal_rules()``
          classmethod — resolved through the registry.

        Returns ``None`` when none of those yield a rule pair. ``None`` is not
        "use the defaults": see ``DeploymentLoop._rules`` for why a named
        strategy must never silently run somebody else's rules.
        """
        strategy_id = loop.row.get("strategy_id")
        version = loop.row.get("strategy_version")
        if not strategy_id or version is None:
            return None

        key = (str(strategy_id), int(version))
        if key in self._rules_cache:
            return self._rules_cache[key]

        resolved = self._read_version_rules(key)
        # A version is immutable, so a resolved pair is safe to keep forever; an
        # unresolved one is cached too, because re-reading the store every pass
        # to reach the same refusal is pure cost.
        self._rules_cache[key] = resolved
        return resolved

    def _read_version_rules(self, key: tuple[str, int]) -> tuple[Any, Any] | None:
        """Load one version definition and coerce it to an (entry, exit) pair."""
        from atr.appdb.repositories import StrategyRepository

        strategy_id, version = key
        try:
            with self.db.session() as session:
                row = StrategyRepository.version(session, strategy_id, version)
        except Exception:  # noqa: BLE001 — a broken store must not trade
            logger.exception(
                "runner: could not read strategy version %s#%s", strategy_id[:8], version
            )
            return None
        if row is None:
            logger.warning(
                "runner: strategy version %s#%s does not exist; not trading",
                strategy_id[:8],
                version,
            )
            return None

        raw = row.get("definition")
        definition, reason = parse_definition(raw)
        if definition is None:
            logger.warning(
                "runner: strategy version %s#%s %s; not trading",
                strategy_id[:8],
                version,
                reason,
            )
            return None

        resolution = resolve_rules(definition)
        if not resolution.ok:
            logger.warning(
                "runner: strategy version %s#%s cannot be traded — %s (keys: %s)",
                strategy_id[:8],
                version,
                resolution.reason,
                sorted(definition),
            )
            return None
        return resolution.entry, resolution.exit_rules

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
        from atr.services.portfolio import portfolio_gate_for

        live_prices = loop.live_prices()
        portfolio = self.ledger.portfolio(
            loop.user_id,
            deployment_id=loop.deployment_id,
            initial_cash=float(loop.row.get("capital") or 0.0),
            prices=live_prices,
        )
        port_gate = portfolio_gate_for(
            loop.user_id,
            loop.deployment_id,
            ledger=self.ledger,
            prices=live_prices,
            db=self.db,
        )
        orders = get_order_service(portfolio=portfolio, portfolio_gate=port_gate)
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

    def _journal(self, *, now: datetime | None = None) -> None:
        """Bring the trade journal into agreement with each attached book.

        Failure is logged and never raised: journaling is reporting, and a
        reporting fault must not stop the execution loop that produced the trades
        it is trying to describe.
        """
        from atr.services.journal import TradeJournalService

        if not self._loops:
            return
        service = TradeJournalService(db=self.db, ledger=self.ledger)
        for loop in list(self._loops.values()):
            try:
                service.reconcile(loop.user_id, loop.deployment_id)
            except Exception:  # noqa: BLE001 - one bad book must not stop the rest
                logger.exception(
                    "runner: journal reconcile failed for %s", loop.deployment_id[:8]
                )

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

        # After the loop set is settled, so the statement covers exactly the
        # deployments that are running now.
        self._ensure_feed_subscriptions()

    def _ensure_feed_subscriptions(self) -> None:
        """Put every running deployment's universe on the live tick feed.

        Requirement 2, and the hole in it. The paper engine reads its prices from
        the broadcaster — but a symbol only reaches the broadcaster's tick store
        when somebody *subscribes* to it, and the only subscriber was a browser.
        With no dashboard open, ``latest_price`` returned ``None`` for every
        symbol, and ``default_price_source`` fell through to the daily cache: the
        deployment was priced off yesterday's close, or not priced at all, while
        reporting itself as running. Either way it produces no forward
        observation, or a wrong one.

        Declared from the runner because the runner is the only thing that knows
        the current universe — a deployment's symbol list can change while it
        runs, and a stopped one should stop consuming feed bandwidth.

        Never fatal. A feed that cannot be subscribed leaves the venue on the
        cache, which is the pre-existing behaviour rather than an outage.
        """
        from atr.services.paper import ensure_live_symbols

        universe: dict[str, list[str]] = {}
        for loop in self._loops.values():
            universe.setdefault(loop.config.exchange, []).extend(loop.config.symbols)
        if not universe:
            return

        report = ensure_live_symbols(universe)
        if not report:
            # No subscriber installed: a CLI run, a test, or a stream that failed
            # to start. The venue prices off the cache and nothing here is wrong.
            return
        if report.get("added") or report.get("removed"):
            logger.info(
                "runner: feed universe updated (+%d/-%d symbols)",
                len(report.get("added") or []),
                len(report.get("removed") or []),
            )
        unresolved = report.get("unresolved") or []
        if unresolved:
            # Named, because a symbol with no contract can never be priced from
            # live ticks: its deployment would trade on the daily cache forever
            # and look perfectly healthy doing it.
            logger.warning(
                "runner: %d symbol(s) could not be put on the feed and will not "
                "be priced from live ticks: %s",
                len(unresolved),
                ", ".join(sorted(unresolved)[:10]),
            )

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
            rules_for=self.resolve_rules,
        )

    # ------------------------------------------------------------------
    # one pass
    # ------------------------------------------------------------------
    def pass_once(self, *, now: datetime | None = None) -> RunnerTick:
        """One evaluation pass over every running deployment.

        Public and synchronous so a test can drive the loop deterministically
        instead of sleeping on a background task.
        """
        now = now or _now_ist()
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
        market_is_open = in_market_hours(now)
        if market_is_open:
            for loop in list(self._loops.values()):
                try:
                    loop.bind()
                    acted = loop.evaluate(now=now)
                except Exception:  # noqa: BLE001 - one deployment must not stop the others
                    logger.exception("runner: evaluation failed for %s",
                                     loop.deployment_id[:8])
                    continue
                # Per-deployment, not the running total. ``loop.last_pass`` is what
                # the status surface shows for *this* deployment, and a cumulative
                # figure would report the sum across every deployment as this
                # one's own — a number that grows with somebody else's activity.
                loop_orders = sum(
                    1 for a in acted if a.get("status") not in ("REJECTED", None)
                )
                signals += len(acted)
                orders += loop_orders
                loop.last_pass = {
                    "at": now.isoformat(),
                    "signals": len(acted),
                    "orders": loop_orders,
                    "state": loop.state,
                }
        else:
            for loop in list(self._loops.values()):
                loop.state = RunnerDeploymentState.WAITING_FOR_MARKET
                loop.skipped_reason = "market closed or outside market hours"
                loop.last_pass = {
                    "at": now.isoformat(),
                    "signals": 0,
                    "orders": 0,
                    "state": loop.state,
                    "skipped_reason": loop.skipped_reason,
                }

        with self._lock:
            self.passes += 1
            self.signals_total += signals
            self.orders_total += orders

        # Journal after the trades, not during them. The journal is a projection
        # of the position fold, so it must see the fills this pass produced —
        # reconciling first would lag by one pass and reconcile before the very
        # fills it is meant to record.
        self._journal(now=now)

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
                        "state": loop.state,
                        "blocked_reason": loop.blocked_reason,
                        "skipped_reason": loop.skipped_reason,
                        "last_tick_time": loop.last_tick_time.isoformat() if loop.last_tick_time else None,
                        "last_tick_age_seconds": loop.last_tick_age_seconds,
                        "last_strategy_evaluation": loop.last_strategy_evaluation.isoformat() if loop.last_strategy_evaluation else None,
                        "last_signal": loop.last_signal,
                        "last_risk_decision": loop.last_risk_decision,
                        "last_order": loop.last_order,
                        "last_fill": loop.last_fill,
                        "skipped_evaluations_count": loop.skipped_evaluations_count,
                        "skipped_fills_count": loop.skipped_fills_count,
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
