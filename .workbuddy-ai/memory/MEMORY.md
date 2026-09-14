# atr — IIFL algo trading backend (NSE/BSE)

Index only. Keep this file short — it is injected at the start of every session and
is truncated if it grows.

| file | holds |
|---|---|
| `FINDINGS.md` | the full statistical record and the rules for producing one |
| `REFERENCE.md` | operational reference — UI, broker, build, sandbox, code traps |
| `YYYY-MM-DD.md` | append-only daily work logs |

## The principle

> "I want to develop an intelligent but fact based system which acts on pure
> statistical and strategic data, not on emotions."

"Emotion" = **overfitting / cherry-picking**. Every result must be out-of-sample,
corrected for trial count (deflated Sharpe), compared to buy-and-hold *and* to a
matched control, and never a citation presented as a measurement. Details and the
measured tables: `FINDINGS.md`.

## Where the evidence stands

- **0 of 27 pre-registered tests pass.** Paper 0/7, production entry rules 0/4,
  Episodic Pivot 0/9, alpha hunt 0/7 short + 1/6 long-and-fragile. All lose to
  simply being invested.
- **One exception:** cross-sectional momentum, 6-month lookback + 1-week skip,
  mid/small caps. Clears deflated Sharpe, 13/15 walk-forward folds positive, beats
  its control and B&H on Sharpe. Modest (~0.2–1.5%/wk) and flat in its most recent
  two years.
- **The 5%/week target is definitionally the oracle** — it needs a 100% hit rate.
  Leverage is not the bridge (32.8x / 7.4x / 10.7x; ruin certain, not probable).
- **A passing backtest is only as good as its cost assumption.** Use
  `IndianDeliveryCosts` (`--costs india`), not the IBKR-style `CommissionModel`.

## Standing rules

- `atr backtest` is in-sample by construction. `atr research` is the honest one.
- Never an equal-weight basket for absolute numbers — quote NIFTYBEES/JUNIORBEES.
- A mean without its best-week and its per-year breakdown is not evidence.
- Check the premise exists before scoring the rule.
- Live scanner and backtest call the **same `eval_entry`/`eval_exit`**.
- Diff equity curve *and* every fill, not just `num_trades >= 1`.
- Python 3.12 via `./.venv/Scripts/python.exe`; **no pip** — `uv pip install
  --python .venv/Scripts/python.exe`. No scipy.

## Current work

Building the modular monolith's Phase 2 (trading safety & correctness) on top of
the existing subsystems. `docs/ARCHITECTURE.md`, `docs/API_CONTRACT.md` and
`docs/DATA_MODEL.md` are the source of truth; `docs/NEXT_STAGE_GAP_REPORT.md` is
the code-verified audit and the prioritised gap list. Do not redesign what already
works.
