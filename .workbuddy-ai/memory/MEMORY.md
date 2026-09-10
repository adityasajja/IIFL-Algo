# Project: atr — algorithmic trading backend (IIFL Capital, NSE/BSE)

## Governing principle (stated by the user)

> "I want to develop an intelligent but fact based system which acts on pure
> statistical and strategic data, not on emotions."

In an automated system "emotion" does not mean fear or greed at a keyboard — it
means **overfitting and cherry-picking**. Practical consequences:

- Never report a backtest result that has not been validated out of sample.
  Use `atr research` (walk-forward), not a single `atr backtest` run.
- Any parameter chosen after seeing the result is contaminated. Parameter
  selection happens on the training window only.
- Correct for how many things you tried (deflated Sharpe), not just the winner.
- Compare against buy-and-hold. A strategy that cannot beat doing nothing is
  just paying commission.
- Prefer "not yet disproven" over "this works".

## Conventions

- Verify optimizations by diffing results (equity curve + every fill) against
  the previous implementation, not merely by tests passing. Weak assertions
  like `num_trades >= 1` will not catch a silent behaviour change.
- Backtests must stay fast enough for parameter sweeps — sweeps are the whole
  point, and they were infeasible at 90s/run. Current: ~1.9s for 17.5k bars.
- Python 3.12, managed with `uv`. Use `./.venv/Scripts/python.exe`.
- No scipy in dependencies — use `statistics.NormalDist` for normal CDF/inverse.

## Known gaps (verified, not speculation)

- `LiveRunner` is never instantiated anywhere; the live strategy loop is
  unwired. `_ensure_frames()` calls `strategy.prepare()` on **empty** frames and
  `LiveConfig.warmup_bars` is declared but never used, so indicators stay NaN
  and a live strategy would never trade. Fix before any real trading.
- `scanner.py` `score_frame()` uses an unvalidated `score = ret_1m + vs_high`
  heuristic, and `scan_symbol()` has a hardcoded `to_date` fallback
  (`"08-Sep-2026"`) that will silently go stale.
- `data/` is gitignored wholesale, so `data/alerts/rules.json` config and
  `data/scans/` output are not versioned.
- Pre-existing ruff errors in `alerts/store.py`, `api/main.py`, `briefing.py`,
  `brokers/iifl/bridge.py`, `scanner.py` — untouched so far.
