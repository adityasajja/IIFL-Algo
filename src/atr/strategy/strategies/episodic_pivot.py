"""Episodic Pivot (EP) — Pradeep Bonde's playbook, formalised on daily bars.

Source
------
``Pradeep Bonde's Playbook`` (Tradezella, Apr 2025) describes an *Episodic
Pivot* as a stock that has been **neglected** for months and then receives a
**new catalyst** that forces the market to reprice it **rapidly** — "gains of
50–300% or more in a short period", "often in as little as 10–20 trading days".
The playbook names five setups: Classical Earnings/Sales Growth, Turnaround,
Story/Thematic, Delayed Reaction, and EP 9 Million (volume-only).

What survives translation to daily OHLCV bars
---------------------------------------------
Pillar 2 of the model — *the catalyst* — is a news event. A daily bar cannot
read an earnings release, so the only observable proxy is the market's own
reaction to it: an abnormal **gap** plus abnormal **volume**. The playbook
concedes exactly this in its own EP 9 Million variant — *"You may not even need
to fully understand the story – the volume itself is the clue."* Every rule
below therefore keys off price and volume only. There is no news feed, no
sector tag, and no market-cap filter.

The three pillars, mechanically:

======================  ==========================================================
Playbook                Implemented as
======================  ==========================================================
Neglect                 ``vol_120`` — realised daily volatility over the prior 120
                        sessions — below ``neglect_max_vol``. "Done very little
                        for months" is, numerically, a quiet tape.
New catalyst            ``gap_pct >= gap_min`` (open vs prior close) **and**
                        ``vol_ratio >= vol_mult`` (volume vs its own trailing
                        20-session average).
Rapid repricing         Not a filter — the *bet*. Entered next open, exited by a
                        trailing stop under swing lows.
======================  ==========================================================

Entry timing and the fill model
-------------------------------
The playbook enters "near the open on Day 1". At 09:15 you know the gap but not
the day's volume, so the two conditions become available at different times:

* :class:`EpisodicPivotDay1` triggers on the **gap alone** — information a
  trader genuinely has at the open — and is therefore the faithful day-one
  entry. The backtest engine fills market orders at the *next* open, so this
  variant is handicapped by one session relative to the playbook. That
  handicap is stated in the results rather than hidden.
* :class:`EpisodicPivotDelayed` and :class:`EpisodicPivot9M` require the
  completed bar (gap **and** volume), so entering next open involves no
  modelling compromise at all. This is the playbook's own Delayed Reaction
  variant, and it is the cleanest test of the idea.

Exits
-----
* Initial stop below the **signal bar's low** — the playbook's "stop below the
  opening-range low" / "below the structure of that day".
* Moved to break-even once the trade is ``be_trigger_r`` × initial risk in
  profit.
* Then trailed under the lowest low of the last ``trail_bars`` sessions —
  "trail the position under daily swing lows".
* ``max_hold_bars`` bounds "exit if momentum fades".

The stop is a **resting SELL STOP**, not a close-price rule, so it fills at the
stop price intraday and at the open when the bar gaps through it. The engine
models both. It is armed on the bar *after* entry fills: submitting it earlier
would let it fill before the entry, producing a phantom short.

Sizing is risk-based, not equal-notional
----------------------------------------
EP is a fat-tailed model — the playbook sells it on "a single EP can move an
account 20–50%". Equal-notional sizing flattens exactly the tail the model
claims to capture, so each position risks ``risk_per_trade`` of equity,
measured from the entry reference price to the stop. ``max_weight`` caps the
resulting notional. This is also what makes the stop meaningful: with a tight
stop you size up, which is the playbook's stated edge.

Honest limitation
-----------------
A backtest can only confirm that *gap + volume + quiet tape* predicts anything
on the tested universe. It cannot confirm the playbook's trade examples — SMCI
+189% in 28 days, ROOT +358% in 16, ANF +240% — because those were small,
genuinely neglected companies on the US tape. Whether the *pond* contains such
fish is a separate, measurable question; ``scripts/research_episodic_pivot.py``
measures it before any of these rules are scored.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.core.enums import OrderType, TimeInForce
from atr.strategy.base import Strategy

#: Sentinel written by the feed builders for "no bar for this symbol today".
#: A real Nifty-50 name never trades zero shares, so zero means missing, and
#: averaging it in would understate the volume baseline it is compared against.
_MISSING_VOLUME = 0.0


def _finite(*candidates) -> float:
    """First candidate that is a usable positive number, else 0.0.

    ``a or b`` is wrong here: ``float("nan")`` is *truthy*, so a NaN price passes
    straight through ``NaN or fallback`` and poisons every downstream sum. It
    surfaced as ``cannot convert float NaN to integer`` deep inside the sizer,
    which reads like a sizing bug rather than a missing price.
    """
    for value in candidates:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number) and number > 0:
            return number
    return 0.0


def _prep(frame: pd.DataFrame) -> pd.DataFrame:
    """Add every column the EP rules read. Called once per symbol by ``prepare``."""
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    open_ = pd.to_numeric(frame["open"], errors="coerce")
    volume = pd.to_numeric(frame.get("volume", pd.Series(0.0, index=frame.index)),
                           errors="coerce")
    volume = volume.where(volume > _MISSING_VOLUME)

    prev_close = close.shift(1)

    # --- neglect -------------------------------------------------------
    ret = close.pct_change()
    # 120 sessions ≈ 6 months, matching "ignored for months" / "6–7 months".
    vol_120 = ret.rolling(120, min_periods=60).std()
    # Secondary neglect shapes the playbook describes: "trading near lows or
    # stuck in a dead range". Kept as columns so a sweep can select on them.
    ret_120 = close / close.shift(120) - 1.0
    range_pct = (high - low) / close.replace(0.0, np.nan)

    # --- catalyst ------------------------------------------------------
    gap_pct = (open_ / prev_close - 1.0) * 100.0
    # Shifted by one so "average volume" never includes the day being tested.
    vol_avg20 = volume.rolling(20, min_periods=10).mean().shift(1)
    vol_ratio = volume / vol_avg20

    frame["ep_prev_close"] = prev_close
    frame["ep_vol_120"] = vol_120
    frame["ep_ret_120"] = ret_120
    frame["ep_range_pct"] = range_pct
    frame["ep_gap_pct"] = gap_pct
    frame["ep_vol_ratio"] = vol_ratio
    frame["ep_signal_low"] = low
    frame["ep_signal_high"] = high
    return frame


def _is_red_to_green(row) -> bool:
    """A red-to-green day: opens below the prior close, closes above it."""
    prev = row.get("ep_prev_close")
    open_ = row.get("open")
    close = row.get("close")
    if prev is None or not np.isfinite(prev) or prev <= 0:
        return False
    if open_ is None or close is None or not np.isfinite(open_) or not np.isfinite(close):
        return False
    return open_ < prev and close > prev


class EpisodicPivotBase(Strategy):
    """Shared entry/exit machinery. Subclasses define only the trigger."""

    name = "episodic_pivot"

    #: Fraction of equity risked between entry and the initial stop.
    risk_per_trade: float = 0.01
    #: Cap on a single position's notional, as a fraction of equity.
    max_weight: float = 0.25
    #: Never enter a position smaller than this fraction of equity — below it
    #: the fixed per-trade cost dominates and the trade is noise.
    min_weight: float = 0.01
    #: Maximum number of concurrent open positions.
    max_positions: int = 8
    #: Maximum gross exposure as a multiple of equity.
    #:
    #: This has to be enforced by the strategy, not by the risk engine. The
    #: engine's `_can_fund` tests each order against total equity *on its own*,
    #: so N concurrent positions are each individually affordable and the book
    #: can reach N x equity. Measured on this strategy it reached 3.49x before
    #: the cap existed — and because the leverage is invisible in the trade log,
    #: the only symptom was the risk engine reporting a "daily loss" of 750k on
    #: a 1M account, which is what exposed it.
    #:
    #: 1.0 = fully cash-backed, which is the honest comparison against a
    #: buy-and-hold benchmark that is itself 100% invested.
    max_gross_weight: float = 1.0

    # --- entry parameters (swept) --------------------------------------
    gap_min: float = 4.0
    vol_mult: float = 3.0
    #: Neglect gate on 120-session realised volatility, in percent per day.
    #: 99.0 disables the gate — the sweep decides whether it earns its place.
    neglect_max_vol: float = 99.0

    # --- exit parameters ------------------------------------------------
    trail_bars: int = 10
    #: Risk multiple at which the stop moves to break-even.
    be_trigger_r: float = 1.0
    #: Hard time stop, in sessions. 0 disables.
    max_hold_bars: int = 0

    def __init__(self, **params) -> None:
        super().__init__(**params)
        #: symbol -> resting protective stop order
        self._stops: dict[str, object] = {}
        #: symbol -> (entry_price, initial_stop, entry_index, highest_close)
        self._trades: dict[str, tuple[float, float, int, float]] = {}
        #: Symbols whose protective stop has actually been placed. Until then
        #: the position is unprotected, so the entry bar's low is checked
        #: explicitly instead of being silently ignored.
        self._armed: set[str] = set()
        #: Memoised portfolio budget for the current bar (see `_book_load`).
        self._load_index: int = -1
        self._load_count: int = 0
        self._load_weight: float = 0.0

    # ------------------------------------------------------------------
    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for frame in frames.values():
            _prep(frame)

    # ------------------------------------------------------------------
    # Trigger — overridden by each variant
    # ------------------------------------------------------------------
    def _trigger(self, row, signal_low: float) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            close = row.get("close")
            if close is None or not np.isfinite(close) or close <= 0:
                continue

            position = ctx.position(symbol)
            if position.is_flat:
                self._maybe_enter(ctx, symbol, row, float(close))
            else:
                self._manage(ctx, symbol, row, float(close))

    # ------------------------------------------------------------------
    def _maybe_enter(self, ctx, symbol: str, row, close: float) -> None:
        signal_low = row.get("ep_signal_low")
        if signal_low is None or not np.isfinite(signal_low) or signal_low <= 0:
            return
        if not self._trigger(row, float(signal_low)):
            return
        self._open_position(ctx, symbol, row, close, float(signal_low))

    # ------------------------------------------------------------------
    def _open_position(self, ctx, symbol: str, row, close: float,
                       signal_low: float) -> None:
        """Size and submit one entry. Shared by every variant's trigger logic.

        Kept separate from :meth:`_maybe_enter` so a variant that gates on
        something other than the current bar (the delayed reaction, which gates
        on how long ago the catalyst was) reuses the sizing rather than
        reimplementing it — a duplicated sizer drifts, and the drift shows up as
        a different risk per trade between variants that are meant to be
        comparable.
        """
        # Size off the *reference* price the fill will actually get — the next
        # open, which we cannot see. The current close is the honest proxy.
        risk_per_share = close - signal_low
        # `NaN <= 0` is False, so a plain `<= 0` guard lets NaN through and it
        # detonates later as `int(nan)`.
        if not np.isfinite(risk_per_share) or risk_per_share <= 0:
            return

        equity = ctx.equity
        if not np.isfinite(equity) or equity <= 0:
            return

        # --- portfolio budget ------------------------------------------
        # The broker funds each order against total equity in isolation, so the
        # only place the *portfolio's* leverage can be capped is here.
        open_count, open_weight = self._book_load(ctx)
        if open_count >= self.max_positions:
            return
        budget = self.max_gross_weight - open_weight
        if budget <= self.min_weight:
            return

        qty = int((equity * self.risk_per_trade) / risk_per_share)
        instrument = ctx.instruments[symbol]
        step = max(getattr(instrument, "quantity_step", 1) or 1, 1)
        qty = int(qty / step) * step
        if qty <= 0:
            return

        weight = qty * close / equity
        if weight < self.min_weight:
            return
        cap = min(self.max_weight, budget)
        if weight > cap:
            qty = int((equity * cap / close) / step) * step
        if qty <= 0:
            return

        # Reserve the budget immediately. Orders fill on the *next* bar, so
        # without this every signal on the same day passes the same check and
        # the book overshoots on the one day it matters most — a market-wide gap.
        self._load_weight += weight
        self._load_count += 1

        # Remember the intended risk so `_manage` can arm the stop on the bar
        # the fill actually lands, not on this one.
        self._trades[symbol] = (close, signal_low, ctx.index, close)
        self._armed.discard(symbol)
        ctx.target(symbol, qty, tag=f"{self.name}-entry")

    # ------------------------------------------------------------------
    def _book_load(self, ctx) -> tuple[int, float]:
        """``(open positions, gross weight)`` — the portfolio budget, once a bar.

        Memoised on ``ctx.index`` because the naive version is O(symbols) per
        *candidate* per bar, which on a 129-name universe is ~26M iterations a
        run. The cached totals are then mutated in place as entries are
        submitted, so several signals on one bar share one budget rather than
        each seeing the pre-trade book.
        """
        if self._load_index != ctx.index:
            count, weight = 0, 0.0
            equity = ctx.equity
            if equity and np.isfinite(equity) and equity > 0:
                for other in ctx.instruments:
                    pos = ctx.position(other)
                    if pos.is_flat:
                        continue
                    count += 1
                    # NaN is truthy, so `pos.last_price or pos.avg_price` would
                    # return NaN rather than the fallback. See `_finite`.
                    price = _finite(pos.last_price, pos.avg_price)
                    weight += (
                        abs(pos.quantity) * price * ctx.instruments[other].multiplier
                    ) / equity
            self._load_index, self._load_count, self._load_weight = ctx.index, count, weight
        return self._load_count, self._load_weight

    # ------------------------------------------------------------------
    def _manage(self, ctx, symbol: str, row, close: float) -> None:
        state = self._trades.get(symbol)
        low = row.get("low")
        if low is None or not np.isfinite(low):
            return
        if state is None:
            # Position opened outside our own bookkeeping (should not happen);
            # adopt the current bar so the stop still gets armed.
            self._trades[symbol] = (close, float(low), ctx.index, close)
            state = self._trades[symbol]
        entry_price, initial_stop, entry_index, high_water = state

        if symbol not in self._armed:
            # First bar the position exists. Two things happen here, and the
            # order matters.
            #
            # 1. Re-anchor to the price we actually got. `_trades` was written on
            #    the signal bar from its *close*, but the fill is the next open —
            #    and for a gap-driven model that difference is the largest single
            #    error in the trade. Anchoring break-even to the signal close
            #    would place it wrong by the whole overnight gap.
            # 2. Repair the unprotected window. The resting stop is not in the
            #    book yet, so a breach of the initial stop inside this bar would
            #    otherwise pass unnoticed. Exit at the next open — deliberately
            #    the worse fill, because we are repairing an omission rather than
            #    modelling one we would be happy to keep.
            filled_at = _finite(ctx.position(symbol).avg_price)
            if filled_at > 0:
                entry_price = filled_at
                self._trades[symbol] = (filled_at, initial_stop, ctx.index, filled_at)
            if float(low) <= initial_stop:
                self._trades.pop(symbol, None)
                ctx.close(symbol, tag=f"{self.name}-stop-late")
                return
            self._arm_stop(ctx, symbol, initial_stop, close)
            return

        high_water = max(high_water, close)

        stop = initial_stop
        risk = entry_price - initial_stop
        if risk > 0 and (high_water - entry_price) >= self.be_trigger_r * risk:
            stop = max(stop, entry_price)          # break-even, never lower

        swing = ctx.history(symbol, n=self.trail_bars + 1)["low"]
        if len(swing) >= 2:
            trail = float(pd.to_numeric(swing.iloc[:-1], errors="coerce").min())
            if np.isfinite(trail):
                stop = max(stop, trail)

        self._trades[symbol] = (entry_price, initial_stop, entry_index, high_water)

        if self.max_hold_bars and (ctx.index - entry_index) >= self.max_hold_bars:
            self._cancel_stop(ctx, symbol)
            self._trades.pop(symbol, None)
            ctx.close(symbol, tag=f"{self.name}-time")
            return

        self._arm_stop(ctx, symbol, stop, close)

    # ------------------------------------------------------------------
    def _arm_stop(self, ctx, symbol: str, stop: float, close: float) -> None:
        """Keep a resting GTC sell-stop at ``stop``, re-pricing only upward.

        A stop already at or above the target is left alone: cancelling and
        re-submitting every bar would burn order slots and, in live trading,
        briefly leave the position unprotected while the replacement is in
        flight.
        """
        current = self._stops.get(symbol)
        if current is not None and current.is_active:
            if (current.stop_price or 0.0) >= stop - 1e-9:
                return
            ctx.cancel(current, f"{self.name}-trail-up")
            self._stops.pop(symbol, None)

        qty = ctx.position(symbol).quantity
        if qty <= 0:
            return
        order = ctx.order(
            symbol,
            -qty,
            OrderType.STOP,
            stop_price=stop,
            tif=TimeInForce.GTC,
            tag=f"{self.name}-stop",
        )
        self._stops[symbol] = order
        self._armed.add(symbol)

    def _cancel_stop(self, ctx, symbol: str) -> None:
        order = self._stops.pop(symbol, None)
        if order is not None:
            ctx.cancel(order, f"{self.name}-cancel")
        self._armed.discard(symbol)

    # ------------------------------------------------------------------
    def on_stop(self, ctx) -> None:
        self._stops.clear()
        self._trades.clear()
        self._armed.clear()


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


class EpisodicPivotDay1(EpisodicPivotBase):
    """Classical Growth / Turnaround EP: gap on the day, enter day one.

    The trigger reads only ``open`` and the prior close, so it is decidable at
    09:15 — the playbook's market-on-open entry. The engine fills at the next
    open, which handicaps this variant by one session; that is a limitation of
    daily bars, not of the rule.
    """

    name = "episodic_pivot_day1"

    def _trigger(self, row, signal_low: float) -> bool:
        gap = row.get("ep_gap_pct")
        if gap is None or not np.isfinite(gap) or gap < self.gap_min:
            return False
        vol_120 = row.get("ep_vol_120")
        if vol_120 is None or not np.isfinite(vol_120):
            return False
        return (vol_120 * 100.0) <= self.neglect_max_vol


class EpisodicPivotDelayed(EpisodicPivotBase):
    """Delayed Reaction EP: the catalyst day is messy, the clean entry is later.

    Waits ``delay_min``–``delay_max`` sessions after a gap+volume catalyst for a
    red-to-green day, then enters. This is the playbook's answer to "day-one
    moves have become more aggressive … gaps of 20–40%": no compromise is needed
    to trade it, because every input is a completed bar.
    """

    name = "episodic_pivot_delayed"

    delay_min: int = 1
    delay_max: int = 20

    def __init__(self, **params) -> None:
        super().__init__(**params)
        #: symbol -> bar index of the most recent catalyst day
        self._catalyst_at: dict[str, int] = {}

    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        super().prepare(frames)
        for frame in frames.values():
            gap_ok = pd.to_numeric(frame["ep_gap_pct"], errors="coerce") >= self.gap_min
            vol_ok = pd.to_numeric(frame["ep_vol_ratio"], errors="coerce") >= self.vol_mult
            neglect = pd.to_numeric(frame["ep_vol_120"], errors="coerce") * 100.0
            neglect_ok = neglect <= self.neglect_max_vol
            # A catalyst day that closed weak — the playbook's HIMS case: price
            # moved, volume exploded, but the day failed to hold strength.
            weak = pd.to_numeric(frame["close"], errors="coerce") < pd.to_numeric(
                frame["open"], errors="coerce"
            )
            frame["ep_catalyst"] = (gap_ok & vol_ok & neglect_ok & weak).fillna(False)

    def on_bar(self, ctx) -> None:
        # Record catalyst days before the generic entry/exit pass, so a
        # catalyst and a red-to-green day can never be the same bar.
        for symbol in ctx.instruments:
            if bool(ctx.row(symbol).get("ep_catalyst", False)):
                self._catalyst_at[symbol] = ctx.index
        super().on_bar(ctx)

    def _trigger(self, row, signal_low: float) -> bool:
        """Unused — this variant gates on catalyst age, not the current bar.

        Never consulted, because :meth:`_maybe_enter` is fully overridden. It
        exists only to satisfy the abstract contract.
        """
        return False

    def _maybe_enter(self, ctx, symbol: str, row, close: float) -> None:
        at = self._catalyst_at.get(symbol)
        if at is None:
            return
        age = ctx.index - at
        if age > self.delay_max:
            self._catalyst_at.pop(symbol, None)
            return
        if age < self.delay_min:
            return
        if not _is_red_to_green(row):
            return
        signal_low = row.get("ep_signal_low")
        if signal_low is None or not np.isfinite(signal_low) or signal_low <= 0:
            return
        # Reuse the shared sizer rather than calling `super()._maybe_enter`,
        # which would consult `_trigger` and always decline.
        self._open_position(ctx, symbol, row, close, float(signal_low))


class EpisodicPivot9M(EpisodicPivotBase):
    """EP 9 Million: abnormal volume *is* the catalyst.

    The playbook scans for stocks trading 9M+ shares "when this is far above its
    normal volume". Nine million shares is a US small-cap threshold; on NSE it
    is meaningless, so the rule keeps the *relative* half of the definition
    (``vol_mult`` × trailing average) and adds a rupee-turnover floor, which is
    the scale-free equivalent of "the market's attention has concentrated here".
    """

    name = "episodic_pivot_9m"

    #: Minimum traded value in rupees for the day, in crores.
    min_turnover_cr: float = 50.0
    #: Require a gap as well. False = pure volume EP.
    require_gap: bool = False

    def _trigger(self, row, signal_low: float) -> bool:
        ratio = row.get("ep_vol_ratio")
        if ratio is None or not np.isfinite(ratio) or ratio < self.vol_mult:
            return False
        vol_120 = row.get("ep_vol_120")
        if vol_120 is None or not np.isfinite(vol_120):
            return False
        if (vol_120 * 100.0) > self.neglect_max_vol:
            return False
        if self.require_gap:
            gap = row.get("ep_gap_pct")
            if gap is None or not np.isfinite(gap) or gap < self.gap_min:
                return False
        if self.min_turnover_cr > 0:
            volume = row.get("volume")
            close = row.get("close")
            if volume is None or close is None:
                return False
            if not (np.isfinite(volume) and np.isfinite(close)):
                return False
            # rupees → crores
            if volume * close / 1e7 < self.min_turnover_cr:
                return False
        return True
