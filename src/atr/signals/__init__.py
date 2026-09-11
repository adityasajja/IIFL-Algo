"""Buy/sell signal generation.

Two jobs, deliberately kept separate:

* **Sell side** — check what you actually hold against risk rules. A stop-loss
  is risk management, not a prediction, so it needs no edge to be worth acting
  on. If you are down 37% the rule fires; that is a fact about your book.
* **Buy side** — scan a universe for entry setups. These *are* predictions, and
  a prediction is only worth acting on if it survives out-of-sample testing.
  Every entry rule here is also exposed as a :class:`~atr.signals.strategy.Strategy`
  so ``atr research`` can walk-forward validate it. Signals carry a
  ``validated`` flag, and the system says so when it is false.

Nothing in this package decides position size or overrides your risk limits.
"""
