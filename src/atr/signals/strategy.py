"""The entry rules, expressed as a Strategy.

This exists so the *same* code that generates live buy signals can be
walk-forward validated by ``atr research``. If the live scanner and the backtest
had separate implementations, passing validation would tell you nothing about
what the scanner actually does.

Because ``eval_entry``/``eval_exit`` are pure and take a frame, the strategy
just hands them the window of bars ending at the current one — no duplicated
rule logic, no chance of the two drifting apart.
"""

from __future__ import annotations

import dataclasses

from atr.signals.models import EntryRules, ExitRules
from atr.signals.rules import eval_entry, eval_exit, precompute_indicators
from atr.strategy.base import Strategy

#: Bars handed to the rule evaluator. Must exceed EntryRules.min_history_bars.
DEFAULT_LOOKBACK = 260


def _subset(cls, params: dict):
    """Build a rules dataclass from whichever of its fields were supplied."""
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in params.items() if k in known})


class SignalEntryStrategy(Strategy):
    """Buys on the entry rules, exits on the risk rules.

    Being a normal Strategy, it runs unchanged in a backtest and in live
    trading — which is the only way a validation result transfers.
    """

    name = "signals_entry"

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self.entries = _subset(EntryRules, params)
        self.exits = _subset(ExitRules, params)
        self.lookback = int(params.get("lookback", DEFAULT_LOOKBACK))
        self.allocation = float(params.get("allocation", 0.10))
        # Cap concurrent holdings. Without it, every symbol that signals gets a
        # full allocation and gross exposure is unbounded — with 19 names at
        # 10% each the book is 190% invested, and whichever orders happen to be
        # rejected is decided by iteration order rather than by any rule.
        self.max_positions = int(params.get("max_positions", 10))

    def prepare(self, frames) -> None:
        """Compute the rule indicators once, over the whole frame.

        Without this, every bar recomputes them from scratch — a rolling
        indicator costs roughly the same in pandas whether it covers 120 rows
        or 1,500, so the per-bar cost dominates everything. Precomputing here
        is what makes a parameter sweep finish in minutes instead of hours.
        """
        for frame in frames.values():
            precompute_indicators(frame, self.entries, self.exits)

    def on_bar(self, ctx) -> None:
        open_positions = sum(
            1 for s in ctx.instruments if not ctx.position(s).is_flat
        )
        for symbol in ctx.instruments:
            window = ctx.history(symbol, self.lookback)
            if len(window) < self.entries.min_history_bars:
                continue

            price = float(window["close"].iloc[-1])
            if not price:
                continue

            position = ctx.position(symbol)
            if not position.is_flat:
                # Same exit rules the live scanner reports.
                fired = eval_exit(
                    symbol, window, position.avg_price, self.exits,
                    quantity=position.quantity,
                )
                if fired:
                    ctx.close(symbol, tag=f"exit-{fired[0].rule}")
                    continue

            if (
                position.is_flat
                and open_positions < self.max_positions
                and eval_entry(symbol, window, self.entries)
            ):
                notional = ctx.equity * self.allocation
                quantity = int(notional / price)
                if quantity > 0:
                    ctx.order(symbol, quantity, tag="entry")
                    open_positions += 1
