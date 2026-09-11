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
from atr.signals.rules import eval_entry, eval_exit
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

    def on_bar(self, ctx) -> None:
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

            if position.is_flat and eval_entry(symbol, window, self.entries):
                notional = ctx.equity * self.allocation
                quantity = int(notional / price)
                if quantity > 0:
                    ctx.order(symbol, quantity, tag="entry")
