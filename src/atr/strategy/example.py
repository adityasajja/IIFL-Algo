"""A worked strategy definition, so a fresh install can demonstrate the workflow.

Nothing here is a finding. It is an *example*: a definition shaped exactly like
one an operator would author, chosen so that the complete chain —

    create strategy -> create version 1 -> validate -> deploy paper -> live tick
    -> signal -> fill -> closed trade -> PAPER_FORWARD -> learning dataset

— can actually be run on a new database, without anybody having to reverse the
JSON shape out of the runner first. Without it the platform's headline workflow
is real but unreachable, because the first thing it needs is a stored definition
and there is no obvious place to get one.

Why these numbers
-----------------

They are the rule layer's own defaults, tightened in the two places that decide
whether the example *closes* a trade at all:

* the entry is the breakout rule (a new high on heavy volume), which is a real
  condition ``eval_entry`` evaluates rather than a value chosen because it
  happens to be true;
* the exit has a stop loss **and** a take profit. A definition with no live exit
  rule can open a position and never close it, which produces no closed trade
  and therefore no forward observation — the whole point of the exercise. The
  validator reports that case as ``no_exit_rule``; this example does not trip it.

Both the paper path and the backtest path are spelled out, and they agree.
``rules`` is what the live loop evaluates; ``engine_key`` + ``params`` is what a
backtest runs, and ``params`` repeats the same values because the two paths must
be describing one strategy. ``validate_definition`` compares them and warns when
they diverge — a backtest of rules the deployment does not run would score the
wrong subject, and score it convincingly.
"""

from __future__ import annotations

from typing import Any

#: The name a seeded strategy is created under. Also the key used to find an
#: existing seed, so seeding twice is idempotent rather than a duplicate.
EXAMPLE_NAME = "Example breakout"

EXAMPLE_DESCRIPTION = (
    "Worked example: buy a breakout to a 20-bar high on 1.5x average volume, "
    "exit on a 5% stop loss or a 10% take profit. Ships so a fresh install can "
    "run the create -> validate -> deploy -> forward-observation chain. It has "
    "not been validated out of sample and makes no claim to an edge."
)

#: The stored definition. Written as data, not as a class, because that is what
#: a version *is* — the subject a deployment pins and a backtest scores.
EXAMPLE_DEFINITION: dict[str, Any] = {
    # --- what the live paper loop evaluates --------------------------------
    "rules": {
        "entry": {
            # Breakout: within 2% of the 20-bar high, on 1.5x average volume.
            "breakout_lookback": 20,
            "breakout_proximity_pct": 2.0,
            "volume_multiple": 1.5,
            "volume_lookback": 20,
            # No entry until there is enough history for those lookbacks.
            "min_history_bars": 60,
        },
        "exit": {
            # Both live exits are set, so a position can actually be closed.
            "stop_loss_pct": 5.0,
            "take_profit_pct": 10.0,
            # The rest of the exit rules are switched off explicitly rather than
            # left at their defaults: an example that says what it does is worth
            # more than one that inherits four more conditions silently.
            "trailing_stop_pct": None,
            "trend_sma": 0,
            "trend_confirm_bars": 3,
            "rsi_overbought": None,
            "min_history_bars": 60,
        },
    },
    # --- what a backtest runs ----------------------------------------------
    "engine_key": "signals_entry",
    "params": {
        # The same rule values, because the two paths must describe one strategy.
        "breakout_lookback": 20,
        "breakout_proximity_pct": 2.0,
        "volume_multiple": 1.5,
        "volume_lookback": 20,
        "min_history_bars": 60,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "trailing_stop_pct": None,
        "trend_sma": 0,
        "trend_confirm_bars": 3,
        "rsi_overbought": None,
        # Strategy-level parameters. Not rule fields, so there is nothing to
        # compare them against — they exist because the example is also a
        # runnable backtest, and an unsized backtest is not runnable.
        "lookback": 120,
        "allocation": 0.10,
        "max_positions": 5,
    },
}

__all__ = ["EXAMPLE_DEFINITION", "EXAMPLE_DESCRIPTION", "EXAMPLE_NAME"]
