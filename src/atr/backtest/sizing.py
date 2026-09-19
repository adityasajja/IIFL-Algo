"""Position sizing and protective exits, as a transparent strategy wrapper.

The engine already has everything needed to *run* a strategy, but nothing that
turns "risk 10% of equity per position, stop 2%, target 6%, trail 3%" into
orders. Rather than put those concerns inside the engine — where they would be
entangled with the fill loop and impossible to test in isolation — they live
here as a decorator around any :class:`~atr.strategy.base.Strategy`.

The wrapper does three things and nothing else:

1. **Sizing.** When the inner strategy buys or sells, the wrapper rescales the
   order to the configured fraction of equity. A strategy that says "go long
   RELIANCE" gets `quantity = max(0, equity * pct / price)` instead of whatever
   the strategy guessed.
2. **Protective exits.** On every bar it checks the open position against the
   stop / target / trail levels and closes it when one is breached.
3. **Trade annotation.** It records *why* each entry happened (the signal
   conditions) so the trade list can explain itself months later.

What it deliberately does **not** do: it never fabricates an entry. If the inner
strategy emits nothing, no position is opened, and a run with no trades stays a
run with no trades. A wrapper that invents signals to make a chart look busy is
the exact failure mode this project exists to avoid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from atr.strategy.base import Strategy, StrategyContext


from atr.strategy.sizing import (
    PositionSizingEngine,
    RoundingRule,
    SizingConfig,
    SizingMethod,
    SizingResult,
)


@dataclass
class SizingPlan:
    """Resolved sizing rules, backed by the unified PositionSizingEngine."""

    mode: str = "fixed_fraction"
    fraction: float = 0.10
    quantity: float = 100.0
    max_position_fraction: float = 1.0
    # Additional risk budgeting settings
    risk_per_trade_pct: float | None = None
    risk_per_trade_rupees: float | None = None
    max_quantity: float | None = None
    max_position_value: float | None = None
    atr_multiplier: float = 2.0
    rounding_rule: str = "FLOOR"

    def to_sizing_config(self) -> SizingConfig:
        method_map = {
            "fixed_quantity": SizingMethod.FIXED_QUANTITY,
            "fixed_fraction": SizingMethod.PERCENT_OF_CAPITAL,
            "equal_weight": SizingMethod.PERCENT_OF_AVAILABLE_CAPITAL,
            "fixed_rupee_value": SizingMethod.FIXED_RUPEE_VALUE,
            "risk_per_trade": SizingMethod.RISK_PER_TRADE,
            "atr_volatility_sizing": SizingMethod.ATR_VOLATILITY_SIZING,
        }
        method = method_map.get(self.mode.lower(), SizingMethod.PERCENT_OF_CAPITAL)
        rounding = (
            RoundingRule.ROUND
            if self.rounding_rule.upper() == "ROUND"
            else RoundingRule.CEIL
            if self.rounding_rule.upper() == "CEIL"
            else RoundingRule.FLOOR
        )
        return SizingConfig(
            method=method,
            capital_fraction=self.fraction,
            fixed_quantity=self.quantity,
            max_portfolio_exposure_pct=self.max_position_fraction,
            risk_per_trade_pct=self.risk_per_trade_pct,
            risk_per_trade_rupees=self.risk_per_trade_rupees,
            max_quantity=self.max_quantity,
            max_position_value=self.max_position_value,
            atr_multiplier=self.atr_multiplier,
            rounding_rule=rounding,
        )

    def target_quantity(
        self,
        *,
        equity: float,
        price: float,
        stop_price: float | None = None,
        stop_loss_pct: float | None = None,
        atr: float | None = None,
    ) -> float:
        """Shares to hold, calculated by PositionSizingEngine."""
        if price <= 0 or not math.isfinite(price):
            return 0.0

        cfg = self.to_sizing_config()
        result = PositionSizingEngine.calculate(
            cfg,
            entry_price=price,
            capital=equity,
            available_capital=equity,
            stop_price=stop_price,
            stop_loss_pct=stop_loss_pct,
            atr=atr,
        )
        return float(result.final_quantity)


@dataclass
class ExitPlan:
    """Resolved protective levels, in fractions."""

    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop: float | None = None

    @property
    def any(self) -> bool:
        return any(v is not None for v in (self.stop_loss, self.take_profit, self.trailing_stop))


@dataclass
class _Track:
    """Per-symbol state the wrapper keeps across bars."""

    entry_price: float = 0.0
    direction: int = 0
    high_water: float = 0.0
    low_water: float = 0.0
    entry_ts: datetime | None = None
    reason: str | None = None


class _OrderRecordingContext:
    """A deferring context that captures the order deltas a strategy asks for.

    ``order`` and ``target`` are **not** forwarded. They record the intent and
    return ``None``; the sizing wrapper then places a single correctly-sized
    order for that intent. Forwarding *and* sizing would submit two orders per
    signal — the strategy's guess and the wrapper's — which doubles every fill
    and halves the meaning of the trade list.

    Everything else is delegated to the real context unchanged, so a strategy
    that reads prices, positions or history behaves exactly as it would
    unwrapped. Written as an explicit delegating object rather than
    ``__getattr__`` magic so the two intercepted methods are visible at a
    glance, and so a typo in a delegated name fails loudly.
    """

    __slots__ = ("_ctx", "_wants", "_tags")

    def __init__(self, ctx: StrategyContext, wants: dict[str, list[float]]) -> None:
        self._ctx = ctx
        self._wants = wants
        self._tags: dict[str, str] = {}

    def _record(self, symbol: str, delta: float, tag: str | None = None) -> None:
        if abs(delta) < 1e-9:
            return
        self._wants.setdefault(symbol, []).append(float(delta))
        if tag:
            self._tags[symbol] = tag

    def order(self, symbol: str, quantity: float, *args, tag: str | None = None, **kwargs):
        self._record(symbol, float(quantity), tag)

    def target(self, symbol: str, target_quantity: float, *, tag: str | None = None, **kwargs):
        current = self._ctx.position(symbol).quantity
        self._record(symbol, float(target_quantity) - current, tag)

    # -- delegated ------------------------------------------------------
    def close(self, symbol: str, tag: str = "exit"):
        position = self._ctx.position(symbol)
        if position.is_flat:
            return None
        self._record(symbol, -float(position.quantity), tag)

    def close_all(self):
        for symbol in self._ctx.instruments:
            self.close(symbol)

    def cancel(self, order, reason: str = "cancelled"):
        return self._ctx.cancel(order, reason)

    def position(self, symbol: str):
        return self._ctx.position(symbol)

    def has_position(self, symbol: str) -> bool:
        return self._ctx.has_position(symbol)

    def price(self, symbol: str) -> float:
        return self._ctx.price(symbol)

    def row(self, symbol: str):
        return self._ctx.row(symbol)

    def history(self, symbol: str, *args, **kwargs):
        return self._ctx.history(symbol, *args, **kwargs)

    def window(self, symbol: str):
        return self._ctx.window(symbol)

    @property
    def instruments(self):
        return self._ctx.instruments

    @property
    def equity(self) -> float:
        return self._ctx.equity

    @property
    def cash(self) -> float:
        return self._ctx.cash

    @property
    def now(self):
        return self._ctx.now

    @property
    def index(self) -> int:
        return self._ctx.index

    @property
    def bars(self):
        return self._ctx.bars

    @property
    def portfolio(self):
        return self._ctx.portfolio


def _ts_key(value: object) -> str:
    """Normalise a timestamp to a string for matching entry-to-annotation.

    The engine's trade frame and the wrapper's annotations reach the same bar
    through different paths, so the same instant can arrive as a ``Timestamp``
    with nanoseconds, a ``datetime``, or a ``date``. Comparing the raw objects
    would fail on representation alone and every trade would lose its reason.

    Kept byte-for-byte equivalent to ``runner._ts_key`` — the two are compared
    against each other, so any divergence here silently drops every annotation.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = pd.Timestamp(value).to_pydatetime()
        except (ValueError, TypeError):
            return ""
    return stamp.isoformat()


class SizedStrategy(Strategy):
    """Decorates a strategy with sizing, protective exits and trade reasons.

    Implements the :class:`Strategy` interface, so the engine cannot tell it
    apart from a plain strategy — which is what keeps the engine unmodified.
    """

    def __init__(
        self,
        inner: Strategy,
        *,
        sizing: SizingPlan | None = None,
        exits: ExitPlan | None = None,
        name: str | None = None,
        allow_short: bool = False,
    ) -> None:
        super().__init__()
        self.inner = inner
        self.sizing = sizing or SizingPlan()
        self.exits = exits or ExitPlan()
        self.name = name or inner.name
        #: Whether a request to go short is a real entry. When a run disallows
        #: shorting, a sell request against a flat book is an exit with nothing
        #: to exit — annotating it as an entry would invent a trade.
        self.allow_short = allow_short
        self._track: dict[str, _Track] = {}
        #: symbol -> the last reasons the inner strategy gave for wanting in.
        #: Captured every bar so that when a fill finally happens the entry is
        #: attributed to the signal that actually caused it, not to whatever
        #: the strategy happens to be saying on the bar the fill lands.
        self._pending_reason: dict[str, str] = {}
        #: symbol -> signed deltas the inner strategy asked for this bar.
        #: Captured by the recording proxy, because an order does not move the
        #: position until the engine fills it on the next bar.
        self._wants: dict[str, list[float]] = {}
        #: Bars the inner strategy asked for, as computed by the last on_bar.
        self._pending_orders: dict[str, float] = {}
        #: Completed annotations, keyed by (symbol, entry_ts). The trade log
        #: reconstructs round trips from fills, so matching by entry time is
        #: what survives into the persisted trade list.
        self.annotations: list[dict] = []

    # ------------------------------------------------------------------
    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        self.inner.prepare(frames)

    def on_start(self, ctx: StrategyContext) -> None:
        self.inner.on_start(ctx)

    def on_session_end(self, ctx: StrategyContext) -> None:
        self.inner.on_session_end(ctx)

    def on_stop(self, ctx: StrategyContext) -> None:
        self.inner.on_stop(ctx)

    # ------------------------------------------------------------------
    def on_bar(self, ctx: StrategyContext) -> None:
        """Exits first, then let the inner strategy act.

        Order matters. If the inner strategy gets to run first it can add to a
        position on the same bar the stop should have fired, and the stop then
        measures a different cost basis than the one the user's rule assumed.
        """
        self._check_exits(ctx)
        self._wants = {}
        self._run_inner(ctx)
        # Reasons are captured *after* the inner strategy has run, because a
        # signal like "the fast SMA crossed above the slow" only becomes true on
        # this bar — asking before it ran would describe the previous bar's
        # state, and the annotation would be off by one bar for every trade.
        self._capture_reasons(ctx)
        self._resize_new_positions(ctx)
        self._wants = {}

    # ------------------------------------------------------------------
    def _run_inner(self, ctx: StrategyContext) -> None:
        """Run the inner strategy with its order calls intercepted.

        The first attempt at this compared the portfolio before and after the
        inner strategy ran, on the assumption that an order moves the position.
        It does not: ``ctx.target()`` and ``ctx.order()`` *submit* an order, and
        the engine fills it on the next bar (that is what prevents look-ahead).
        So the diff was always zero, ``_pending_orders`` was always empty, and
        no annotation was ever produced — the reason for a trade was known and
        thrown away.

        The fix is to watch what the strategy actually asks for. A thin proxy
        over the context records the requested deltas without changing any
        behaviour: the real ``order`` is still called, with the same arguments.
        """
        proxy = _OrderRecordingContext(ctx, self._wants)
        self.inner.on_bar(proxy)
        # Sum the requested deltas per symbol, then apply the *last* requested
        # direction. A strategy that submits several orders on one bar is
        # expressing one intent; summing would let it accidentally open a
        # position twice over.
        self._pending_orders = {
            symbol: sum(wants) for symbol, wants in self._wants.items() if wants
        }

    def _resize_new_positions(self, ctx: StrategyContext) -> None:
        """Size the strategy's requested orders, and annotate real entries.

        Two decisions, deliberately kept apart:

        1. **Size the order.** Every non-zero request the inner strategy made
           this bar is replaced by an order that moves the position to the
           configured size. This happens regardless of whether the wrapper is
           already tracking the symbol, because an exit request must still be
           honoured — a sizing rule that swallowed sells would trap every
           position forever.
        2. **Annotate an entry.** An annotation is written only when the request
           moves the book from flat to a new position. That is the definition of
           an entry, and it is what makes the annotation count match the trade
           count. Annotating every request produced one row per *signal bar*
           rather than per trade — 72 annotations against 22 trades in the
           reference data.

        The delta is what the strategy *requested*, not a realised position
        change: fills land on the next bar.
        """
        for symbol, delta in getattr(self, "_pending_orders", {}).items():
            if abs(delta) < 1e-9:
                continue

            held = ctx.position(symbol).quantity
            track = self._track.get(symbol)
            was_flat = track is None or track.direction == 0

            price = ctx.price(symbol)
            if price <= 0:
                continue

            want = self.sizing.target_quantity(equity=ctx.equity, price=price)
            want = math.floor(want) if want >= 1 else want
            if want <= 0:
                continue

            signed_want = want if delta > 0 else -want
            # A short request when shorting is disabled is not an entry, and
            # sizing it into one would open positions the run asked not to have.
            if not self.allow_short and signed_want < 0:
                # Still honour it as an exit if there is something to exit.
                if abs(held) > 1e-9:
                    ctx.order(symbol, -held, tag="sizing-exit")
                    self._track.pop(symbol, None)
                continue

            adjust = signed_want - held
            if abs(adjust) < 1e-9:
                continue
            ctx.order(symbol, adjust, tag="sizing")

            if not was_flat:
                # Adding to an existing position. Refresh the water marks so a
                # trail does not measure a stale extreme, but do not re-annotate:
                # this is one trade's second fill, not a second trade.
                existing = self._track.get(symbol)
                if existing is not None:
                    self._refresh_water(ctx, symbol, existing)
                continue

            # A genuine entry: flat before, positioned after. Record the track
            # and the reason now, at the bar the signal fired, because the
            # engine will not report the fill until the next bar.
            entry = self._track.setdefault(symbol, _Track())
            entry.direction = 1 if signed_want > 0 else -1
            entry.entry_price = price
            entry.entry_ts = ctx.now
            entry.high_water = entry.low_water = price
            entry.reason = self._pending_reason.get(symbol)
            self._annotate_entry(symbol, entry, ctx)

    def _annotate_entry(self, symbol: str, track: _Track, ctx: StrategyContext) -> None:
        """Record why this position was opened.

        Emitted at entry rather than at exit because the two are independent:
        a position can close on the inner strategy's own exit signal, on a
        protective level, or at the end of the data. Waiting for a protective
        exit to annotate means every other closure path loses the reason, and
        the trade list then shows "not recorded" for trades whose reason was
        perfectly well known at the time it was opened.
        """
        cfg = self.sizing.to_sizing_config()
        stop_price = self._stop_price(track)
        stop_loss_pct = (
            (self.exits.stop_loss * 100.0) if self.exits.stop_loss is not None else None
        )
        res = PositionSizingEngine.calculate(
            cfg,
            entry_price=track.entry_price,
            capital=ctx.equity,
            available_capital=ctx.equity,
            stop_price=stop_price,
            stop_loss_pct=stop_loss_pct,
        )

        self.annotations.append(
            {
                "symbol": symbol,
                "entry_ts": track.entry_ts,
                "exit_reason": None,
                "signal_reason": track.reason or self._pending_reason.get(symbol),
                "stop_price": stop_price,
                "direction": "LONG" if track.direction > 0 else "SHORT",
                "sized_to": self.sizing.mode,
                "sizing_method": res.method,
                "raw_quantity": res.raw_quantity,
                "final_quantity": res.final_quantity,
                "risk_amount": res.risk_amount,
                "stop_distance": res.stop_distance,
                "atr": res.atr,
                "capital_available": res.capital_available,
                "position_value": res.position_value,
                "portfolio_impact_pct": res.portfolio_impact_pct,
                "capped_by": res.capped_by,
            }
        )

    # ------------------------------------------------------------------
    def _capture_reasons(self, ctx: StrategyContext) -> None:
        """Ask the inner strategy what it currently sees, if it can say.

        Strategies opt in by exposing `describe_signal(symbol, ctx)`. Those that
        do not simply contribute no reason, and the trade list records the
        absence honestly rather than inventing a sentence.
        """
        describe = getattr(self.inner, "describe_signal", None)
        if describe is None:
            return
        for symbol in ctx.instruments:
            try:
                reason = describe(symbol, ctx)
            except Exception:  # noqa: BLE001 — annotation must never break a run
                reason = None
            if reason:
                self._pending_reason[symbol] = reason

    # ------------------------------------------------------------------
    def _positions(self, ctx: StrategyContext):
        for symbol in ctx.instruments:
            pos = ctx.position(symbol)
            if not pos.is_flat:
                yield symbol, pos

    def _refresh_water(self, ctx: StrategyContext, symbol: str, track: _Track) -> None:
        price = ctx.price(symbol)
        if price <= 0:
            return
        if track.direction > 0:
            track.high_water = max(track.high_water or price, price)
        else:
            track.low_water = min(track.low_water or price, price)

    def _check_exits(self, ctx: StrategyContext) -> None:
        """Close any position that has breached a protective level."""
        for symbol, pos in list(self._positions(ctx)):
            track = self._track.setdefault(symbol, _Track())

            if track.direction == 0:
                # A position exists that we did not open (an inner strategy
                # that trades on its own): adopt it so exits still apply.
                track.direction = pos.direction
                track.entry_price = pos.avg_price
                track.entry_ts = pos.opened_at
                track.high_water = track.low_water = ctx.price(symbol)

            self._refresh_water(ctx, symbol, track)
            price = ctx.price(symbol)
            if price <= 0 or track.entry_price <= 0:
                continue

            exit_reason = self._breach(price, track)
            if exit_reason is None:
                continue

            ctx.close(symbol, tag=f"exit:{exit_reason}")
            self._annotate_exit(symbol, track, exit_reason)
            self._track[symbol] = _Track()

        # Positions opened or closed by the inner strategy also need their
        # tracking seeded, so the *next* bar's stop check has a cost basis.
        for symbol, pos in self._positions(ctx):
            track = self._track.setdefault(symbol, _Track())
            if track.direction == 0:
                track.direction = pos.direction
                track.entry_price = pos.avg_price
                track.entry_ts = pos.opened_at
                track.high_water = track.low_water = ctx.price(symbol)
                track.reason = self._pending_reason.get(symbol)

    def _annotate_exit(self, symbol: str, track: _Track, exit_reason: str) -> None:
        """Attach the exit reason to the annotation made when the position opened.

        The entry annotation is the record of the trade; the exit reason is a
        field on it. Appending a second annotation for the exit would put two
        rows under one ``(symbol, entry_ts)`` key, and whichever won the dict
        build would decide whether the UI showed the entry reason or the exit
        reason — never both, which is the only useful answer.
        """
        key = (symbol, _ts_key(track.entry_ts))
        for note in self.annotations:
            if (note.get("symbol"), _ts_key(note.get("entry_ts"))) == key:
                note["exit_reason"] = exit_reason
                note["stop_price"] = self._stop_price(track)
                return
        # No entry annotation (a position the inner strategy opened without the
        # wrapper seeing the fill). Record what is known rather than nothing.
        self.annotations.append(
            {
                "symbol": symbol,
                "entry_ts": track.entry_ts,
                "exit_reason": exit_reason,
                "signal_reason": track.reason or self._pending_reason.get(symbol),
                "stop_price": self._stop_price(track),
            }
        )

    def _stop_price(self, track: _Track) -> float | None:
        if self.exits.stop_loss is None:
            return None
        if track.direction > 0:
            return track.entry_price * (1 - self.exits.stop_loss)
        return track.entry_price * (1 + self.exits.stop_loss)

    def _breach(self, price: float, track: _Track) -> str | None:
        """Which protective level, if any, this price crosses."""
        long = track.direction > 0

        if self.exits.stop_loss is not None:
            limit = track.entry_price * (1 - self.exits.stop_loss if long else 1 + self.exits.stop_loss)
            if (long and price <= limit) or (not long and price >= limit):
                return "stop_loss"

        if self.exits.take_profit is not None:
            limit = track.entry_price * (1 + self.exits.take_profit if long else 1 - self.exits.take_profit)
            if (long and price >= limit) or (not long and price <= limit):
                return "take_profit"

        if self.exits.trailing_stop is not None:
            # The trail follows the best price seen since entry, so it can only
            # ever ratchet toward the position — never away from it.
            if long and track.high_water > 0:
                limit = track.high_water * (1 - self.exits.trailing_stop)
                if price <= limit:
                    return "trailing_stop"
            elif not long and track.low_water > 0:
                limit = track.low_water * (1 + self.exits.trailing_stop)
                if price >= limit:
                    return "trailing_stop"

        return None
