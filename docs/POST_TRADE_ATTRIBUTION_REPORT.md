# Post-Trade Analytics & Trade Attribution — Delivery Report

**Feature:** deterministic post-trade attribution for Indian cash-equity trading
**Date:** 2026-09-17 · **Scope:** NSE/BSE equity only — no F&O, no options
**Verdict:** shipped, 1469 passed / 0 failed, frozen subsystems unchanged

---

## 1. Files and modules changed

### New — pure engine (`src/atr/analytics/`, no I/O, no storage imports)

| File | Lines | Contents |
|---|---|---|
| `reason_codes.py` | 7.9k | Closed vocabulary: **20 reason codes** in three families (`DECISION_CODES`, `EXECUTION_CODES`, `EXIT_CAUSE_CODES`), `family_of()`, `ADVICE_WORDS` |
| `models.py` | 11.0k | Frozen dataclasses: `TradeDetails`, `AttributionLeg` (with a `fill_ratio` property), `AttributionInput` |
| `excursions.py` | 21.6k | `compute_excursions()`, `execution_quality()`, `leg_slippage_bps()`, `leg_slippage_amount()` |
| `attribution.py` | 31.4k | `attribute_trade()`, `build_input()`, `TradeAttribution` — nine branch dataclasses, `.as_dict()` (the tree), `.flat()` (28 dataset fields) |
| `aggregation.py` | 27.7k | `overview()`, `by_branch()`, `evidence_counts()`, bucket helpers, `mae_mfe()`, `execution()`, `diagnostics()` |

### New — service, API, schema, tests

| File | Lines | Contents |
|---|---|---|
| `src/atr/services/attribution.py` | 50.5k | `AttributionService` — the only storage access; `attribute_user`, `attribute_episode`, `build_input`, `get`, `rows`, `all_rows`, `fingerprint`, `coverage` |
| `src/atr/api/routers/analytics.py` | 24.8k | The nine read-only GETs |
| `tests/test_analytics_integrity.py` | 56.7k | **66 tests** — the invariants |
| `tests/test_attribution_chain.py` | **new** | **20 tests** — the acceptance chain, on a real DB |

### Modified

| File | Change |
|---|---|
| `appdb/schema.py` | `trade_attributions` table (47 columns, 5 indexes) |
| `appdb/repositories.py` | `TradeAttributionRepository` — `upsert`/`get`/`fingerprint_for`/`list_for_user`/`all_for_user`/`for_trades`/`delete_for_user` |
| `api/routers/__init__.py` | registered `analytics.router` (19 routers total) |
| `services/learning.py` | 28 `attribution_*` columns in `DATASET_COLUMNS` (96 total), `_attach_attribution()`, `_mark_missing()` |
| `research/learning_axes.py` | 10 attribution axes in a separate `ATTRIBUTION_AXES` tuple |
| `tests/conftest.py` | `reset_attribution_service()` in `_reset_all()` |
| `tests/test_appdb_schema.py` | `trade_attributions` added to `EXPECTED_TABLES` |
| `docs/API_CONTRACT.md`, `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md` | the new tier |

### Frontend

`web/src/AnalyticsPanel.tsx` (new, ~960 lines), `web/src/lib/analytics-view.ts` (new),
`web/src/lib/analytics-view.test.ts` (new, 49 tests), `web/src/api.ts` (+336 lines),
`web/src/App.tsx`, `web/src/icons.tsx`.

**Not touched:** `execution/risk.py`, `execution/oms.py`, `services/paper.py`,
`services/runner.py`, `research/learning_evidence.py`.

---

## 2. APIs added

Nine `GET` routes under `/api/v1/analytics`, all principal-scoped, all read-only.

```
GET /trades/{trade_id}/attribution          the full nine-branch tree
GET /trades                                 ?strategy_id=&strategy_version=&symbol=&start=&end=
                                            &source=&evidence_grade=&market_regime=&sector=
                                            &limit=&offset=
GET /summary                                headline + coverage + grade counts
GET /performance/by-strategy                the six comparison surfaces, by branch
GET /performance/by-regime
GET /performance/by-context
GET /performance/by-sizing
GET /performance/by-execution
GET /mae-mfe                                distributions + P&L relationships
```

Auth, ownership scoping, response envelope and error handling are the existing
conventions — no new auth path was introduced. `coverage` returns `complete: None`
(not `False`) when the count cannot be taken.

**Deliberately not built:** a "best strategy" endpoint. It would be the most-read
figure on the page and would carry no sample size, no grade split and no per-regime
breakdown. The comparison surfaces report every slice *with* its `n` and its
`evidence_grade` counts instead, so the reader supplies the judgement.

---

## 3. Database / schema changes

One additive table. No Alembic; uses the existing `metadata.create_all` +
`ADDITIVE_COLUMNS` reconciliation, so an existing database gains it without a
migration step.

**`trade_attributions`** — 47 columns, `trade_id` primary key, FK to `users`, five
indexes (`user_id`, `symbol`, `evidence_grade`, `market_regime`, `computed_at`).

- `trade_id` is the PK, so **idempotency is structural**: a trade cannot be
  attributed twice. `input_fingerprint` distinguishes a re-measurement from a
  duplicate; `upsert` returns `"inserted"` or `"replaced"`.
- `evidence_grade` / `evidence_class` are **copied** from the journal, never
  derived here.
- `attribution` holds the full nine-branch tree as a blob; `missing_fields`
  explains every absent value.
- `symbol` and `side` are lifted into columns because the API filters on them.
  `strategy_id`/`strategy_version` are **not** — they are already indexed on
  `trade_journal`, and a second index of one field is a second thing that can go
  stale.

---

## 4. Attribution fields

`TradeDetails` carries ~40 requested fields. The engine emits:

- **Outcome** — gross/net P&L, gross/net return %, prices, quantity, position value
- **Risk & sizing** — stop price, planned risk, realized risk %, sizing method, cap
  reason, cap value, MFE/risk, realized/risk
- **Signal & context** — signal id, strategy id, strategy version, context model
  version, context score, context class, market regime, sector, sector strength,
  stock relative strength, RVOL, ATR, ATR %
- **Execution** — expected vs actual entry/exit price, slippage (bps and rupees),
  execution delay, transaction costs, cost %, partial-fill flag, fill ratio
- **Timing** — entry/exit timestamps, holding duration, signal→order, order→fill
- **Provenance** — source, evidence class, evidence grade, simulated flag

**Every field preserves provenance.** `missing_fields` names each absent value and
why, so a `None` is explained rather than a hole — an absent field is never `0.0`
and never a default.

---

## 5. MAE/MFE methodology

**Truncation-first, and the truncation is a `TypeError`.** `compute_excursions()`
takes `bars_until` as a **keyword-only argument with no default**. The tempting
signature lets the scan use every bar it has — correct for a closed trade whose
series happens to end at the exit, and silently wrong for every other call, always
in the direction that *overstates* MFE. Requiring the cutoff turns "I forgot to
truncate" from a wrong number into a `TypeError`.

**Extremes come from each bar's high and low, never its close.** A close-to-close
excursion understates both: the value of MAE is the *worst* the position was
marked, and a position whose low was under every close it printed was still stopped
at that low.

**Sign convention: MFE favourable-positive, MAE adverse-negative, both in the
position's direction.** They get summed downstream, so a sign that flipped with
direction would make every sum a statement about direction rather than size.

**Direction is branched explicitly.** This is where the code was wrong. For a long,
`best = max(high)` and `worst = min(low)`. For a short they **invert** — a short
profits when price falls, so the *low* is favourable and the favourable extreme is
the *minimum* of the lows. Tracking max-high/min-low regardless of direction and
then subtracting labels a short's best mark as its worst.

**Missing or non-positive high/low bars are skipped, not coerced to zero** — a zero
low on a long manufactures a −100% MAE, i.e. a fabricated disaster.

---

## 6. Frontend changes

An **Attribution** tab under the Learn group, with seven sub-tabs:

| Sub-tab | Shows |
|---|---|
| Overview | net P&L, win rate, profit factor, expectancy, avg win/loss, avg holding time, total costs, total slippage |
| Attribution | the nine branches as reason-code groups |
| MAE/MFE | distributions plus a hand-rolled SVG scatter against P&L |
| Execution | delays and slippage by symbol and time of day |
| Context & sizing | by context score, regime, sector, sizing method, risk per trade |
| Strategies | strategy/version compared across regime, context, sector, sizing and exit |
| Trades | the book, drilling into the per-trade tree |

Every money and rate figure renders `—` on absent data rather than `0`. Grade and
class counts are shown side by side, with `class_unrecorded` surfaced. **No "best
strategy" ranking appears anywhere.**

A pure view-model layer (`lib/analytics-view.ts`) does the formatting so the
component holds no arithmetic — 49 vitest tests cover it, including the empty-book
and all-`None` cases.

---

## 7. Tests passed

| Suite | Result |
|---|---|
| `test_attribution_chain.py` | **20 passed** |
| `test_analytics_integrity.py` | **66 passed** |
| Full backend regression | **1469 passed, 1 skipped, 0 failed** (baseline 1382 + 1) |
| Frontend `vitest` | **224 passed** across 5 files (49 new) |

The chain suite walks Signal → Context → Sizing → Risk → Gate → OMS → Execution →
Closed Trade → Attribution → Learning Dataset on a **real temporary database**,
asserting the stored row rather than a return value. Coverage includes: an open
trade is never attributed; attributing twice leaves one row; `force` replaces
rather than duplicates; a post-exit bar cannot inflate MFE; the nine tree branches;
metadata survival; grades copied not derived; **an unstamped trade grades in-sample
and the sweep never upgrades one**; ownership scoping; and the learning dataset
declaring its attribution columns.

Data-integrity coverage also proves: `bars_until` is mandatory (`TypeError`
asserted); MAE/MFE arithmetic on both sides of the book; partial fills; multiple
order events not becoming multiple trades; the reason vocabulary closed, unique and
**free of advice words**; and the RiskEngine module importing no analytics.

---

## 8. Frontend build result

`tsc --noEmit` clean · `vite build` **succeeded** — 2388 modules, 7.90s.

```
dist/index.html                     1.34 kB │ gzip:   0.66 kB
dist/assets/index-CCJrwoVL.css     99.76 kB │ gzip:  15.50 kB
dist/assets/vendor-react.js         4.12 kB │ gzip:   1.54 kB
dist/assets/vendor-icons.js        54.81 kB │ gzip:  12.71 kB
dist/assets/vendor-motion.js      137.81 kB │ gzip:  45.81 kB
dist/assets/vendor-charts.js      162.41 kB │ gzip:  51.80 kB
dist/assets/index.js              994.18 kB │ gzip: 262.53 kB
```

One pre-existing warning: the main chunk exceeds 600 kB. No new warning was
introduced.

---

## 9. Known limitations

**Three defects were found and fixed during this work.** Two were in code written
earlier in this same feature, and all three passed every other test while being
wrong — they surfaced only because the new tests assert *behaviour* rather than
restating the implementation. They are listed here because they changed behaviour,
not because they remain.

1. **Short-side excursions were inverted** (`analytics/excursions.py`). Max-high /
   min-low tracking regardless of direction labelled a short's best mark as its
   worst, reporting a losing short as having moved in your favour. Fixed by
   branching on direction.
2. **`evidence_counts` contradicted itself** (`analytics/aggregation.py`). An
   absent `evidence_class` was defaulted to `IN_SAMPLE`, producing
   `by_grade: {forward: 1}` beside `by_class: {IN_SAMPLE: 1}` on one row. Fixed by
   counting absent classes under `class_unrecorded` and leaving them out of the
   split.
3. **The injected-frame lookup raised** (`services/attribution.py`).
   `frames.get(sym) or frames.get(f"{sym}-EQ")` truth-tests a DataFrame, which
   pandas makes a `ValueError`. It only appeared to work when the first key was
   absent — the case that never reaches the boolean. Replaced with explicit
   `is None` lookups.

### Limitations that remain

- **The attribution table is empty in practice.** 0 rows exist, because it
  attributes closed journal episodes and no forward deployment has traded. The
  layer is wired and tested end to end; it has not yet measured a real trade.
- **Coverage is honest but low.** Where a trade predates the signal-context engine
  or was not linked to an opening order, fields stay `None` with a reason. The
  dashboard will show `—` on most real rows until the upstream tiers have been
  running long enough to populate them.
- **Slippage is `None` when unmeasurable, never `0.0`** — which is correct but
  means slippage aggregates exclude a large share of early rows.
- **Attribution axes are opt-in.** They are defined in `ATTRIBUTION_AXES` but kept
  out of `DEFAULT_AXES`, because near-zero coverage on a default scan would inflate
  the comparisons count and deflate every p-value for no information gained. They
  must be requested explicitly.
- **No re-attribution trigger on upstream change.** A trade attributed before its
  signal context was recorded will not re-measure itself; it needs `force=True`
  or a later sweep with a changed input.
- **The main JS chunk exceeds 600 kB.** Pre-existing, unrelated, and untouched.
- **`atr.api.main` route enumeration is a trap.** Reading `app.routes` and `.path`
  misses every included router — FastAPI wraps them in an `_IncludedRouter` with no
  `.path` — so a naive scan reports zero `/api/v1` routes and looks exactly like a
  registration failure. Walk `original_router` instead. This cost real time; the
  routes were correct throughout.

### Unchanged, as required

RiskEngine, PortfolioRiskGate, OMS, paper runner, live runner, the learning
safeguards, champion/challenger and the experiment lab are behaviourally
untouched. Asserted structurally, not assumed: the RiskEngine module imports no
analytics; the attribution service writes exactly one table; and the layer exposes
no method named `apply`, `deploy`, `optimize` or `place_order`.
