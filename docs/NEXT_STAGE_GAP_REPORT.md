# ATR — Next Stage Gap Report

**Audit date:** 2026-09-14
**Scope:** the repository at `D:\ALGO` (21,294 lines of Python across `src/atr`, 21 + 2 React panels, 341 test functions).
**Method:** every claim below was verified against the code, not the documentation. Where a
design-of-record document specifies something, that is stated separately from whether it
exists. Line references are to the state of the tree on the audit date.

---

## 0. How this audit was performed

Four passes, in this order:

1. **Inventory.** `find src/atr -name '*.py' | wc -l` and per-module line counts, to see where
   the mass actually is. `api/main.py` alone is 2,854 lines — 13% of the backend.
2. **Capability grep.** For each of ~30 capability keywords (`order_events`, `order_intents`,
   `idempotency`, `reconciliation`, `OrderState`, `option_chain`, `backtest_runs`,
   `strategy_versions`, `corporate_action`, `market_calendar`, …) a case-insensitive search
   across `src/atr`. A keyword that appears only in a docstring, a permission name, or a
   `CREATE TABLE` comment is *not* an implementation, and is reported as such.
3. **Path tracing.** For the trading path specifically, each route that can reach the broker
   was read end to end to determine what actually guards it.
4. **Frontend inspection.** Each panel's imports and API calls, to distinguish "a page exists"
   from "the workflow exists".

**Two defects were found by reading the code rather than the docs, and both are security or
safety relevant** (§4). One was fixed during this audit.

---

## 1. Classification table

| Capability | Current State | Evidence | Priority | Planned Phase |
|---|---|---|---|---|
| **AUTHENTICATION** | | | | |
| Users, sessions, MFA, API keys | IMPLEMENTED | `atr/auth/service.py` (907 L), `passwords.py` scrypt, `totp.py` RFC 6238, `tokens.py` | — | shipped |
| Roles & permissions (23 × 5) | IMPLEMENTED | `atr/auth/rbac.py`; matrix in `DATA_MODEL.md` §1.7 | — | shipped |
| Ownership scoping (404 not 403) | IMPLEMENTED | `WatchlistRepository.get(session, id, user_id)`; `tests/test_watchlists.py` | — | shipped |
| Auth middleware, security headers | IMPLEMENTED | `atr/api/middleware.py` — request id, CSP, HSTS, Permissions-Policy | — | shipped |
| Rate limiting | IMPLEMENTED | `atr/auth/rate_limit.py` token bucket, separate login bucket | — | shipped |
| CSRF | **BROKEN → FIXED** | `middleware.py` allowed only 4 hardcoded ports; see §4.1 | P0 | fixed 2026-09-14 |
| **MARKET DATA** | | | | |
| Instrument master | IMPLEMENTED | `atr/instruments/service.py`; 2,662 symbols, fingerprint-cached JSON index | — | shipped |
| Historical OHLCV (parquet) | IMPLEMENTED | `data/iifl_daily/NSEEQ/*.parquet`, 3,089 files; `atr/data/history.py` | — | shipped |
| Timeframe aggregation | IMPLEMENTED | `atr/data/aggregator.py`; `iifl_15m/` granularity dir | — | shipped |
| Data hygiene | IMPLEMENTED | `atr/data/hygiene.py`, `tests/test_hygiene.py` | — | shipped |
| Live ticks (MQTT → bus) | IMPLEMENTED | `atr/brokers/iifl/feeds.py`, `bridge.py` | — | shipped |
| WebSocket tick stream | PARTIALLY | `atr/api/stream.py:209` `connect()` accepts **without authenticating** | P0 | Phase 2 |
| Corporate actions | MISSING | no code; `DATA_MODEL.md` §2 specifies `corporate_actions` | P2 | Phase 3 |
| Market calendar | MISSING | no code; §2 specifies `market_calendar` | P2 | Phase 3 |
| Expiry calendar | MISSING | no code; §2 specifies `expiry_calendar` | P3 | Phase 4 |
| Option chain / Greeks / OI | MISSING | no code; `client.open_interest()` exists, unused by any route | P3 | Phase 4 |
| **STRATEGY** | | | | |
| Strategy ABC + registry | IMPLEMENTED | `atr/strategy/base.py`, 17 registered strategies | — | shipped |
| Shared rule layer | IMPLEMENTED | `atr/signals/rules.py` `eval_entry`/`eval_exit` used by scanner *and* backtest | — | shipped |
| Out-of-sample validation | IMPLEMENTED | `atr/research/validate.py` — walk-forward, deflated Sharpe | — | shipped |
| Strategy persistence | MISSING | `DATA_MODEL.md` §4 says "Shipped today: nothing persisted" — still true | P1 | Phase 2 |
| Immutable versions | MISSING | no `strategy_versions` table or repository | P1 | Phase 2 |
| No-code / nested rule trees | PARTIALLY | `CustomScannerPanel` has a **flat** list with one AND/OR flag | P2 | Phase 3 |
| Natural-language generation | MISSING | not attempted | P4 | later |
| **BACKTESTING** | | | | |
| Event-driven engine | IMPLEMENTED | `atr/backtest/engine.py`; fills→mark→risk→strategy ordering, per-bar conservation assert | — | shipped |
| Metrics, equity, drawdown, trades | IMPLEMENTED | `atr/backtest/metrics.py`; `max_drawdown`, `exposure_pct` | — | shipped |
| Walk-forward | IMPLEMENTED | `research/validate.py:walk_forward`; 9 folds on the honest sample | — | shipped |
| Monthly returns | MISSING | not computed in `metrics.py` | P2 | Phase 3 |
| Monte Carlo | MISSING | no bootstrap/path simulation in `src/atr` | P2 | Phase 3 |
| Asynchronous jobs | MISSING | `POST /backtest` is synchronous; no job abstraction | P1 | Phase 2 |
| **TRADING** | | | | |
| Broker abstraction (live) | IMPLEMENTED | `atr/brokers/base.py` `Broker` ABC honoured by `IiflBroker` | — | shipped |
| Broker abstraction (backtest) | PARTIALLY | `SimulatedBroker` does **not** implement `Broker` (`submit`/`cancel`/`on_bar` vs `place_order`/`cancel_order`) — a second interface | P1 | Phase 2 |
| **OMS / order state machine** | **MISSING** | `Order.status` is one mutable field (`core/models.py:167`); no `order_events` | **P0** | Phase 2 |
| **Durable idempotency** | **MISSING** | no `order_intents`; a retried signal places a second order | **P0** | Phase 2 |
| Partial fills | PARTIALLY | `Order.filled_quantity`/`avg_fill_price` exist; no lifecycle to record them | P0 | Phase 2 |
| Paper trading engine | PARTIALLY | `execution_mode` is a **gate**, not an engine. No simulated matching, no paper positions | **P0** | Phase 2 |
| Live execution | IMPLEMENTED | `atr/live/runner.py` (LiveRunner), `POST /orders`, signal execute | — | shipped |
| Reconciliation | MISSING | `store.py:351` mentions it in a docstring only; no table, no code | P1 | Phase 2 |
| Execution latency capture | MISSING | no ack/fill timestamps anywhere | P2 | Phase 2 |
| **RISK** | | | | |
| RiskEngine + limits | IMPLEMENTED | `atr/execution/risk.py` — 11 limit types incl. gross exposure, daily loss, trading window | — | shipped |
| Risk enforced on the order path | **BROKEN** | `RiskEngine` is never referenced in `api/main.py`; see §4.2 | **P0** | Phase 2 |
| Kill switch enforced everywhere | PARTIALLY | checked on signal-execute (`main.py:630`) and inside the engine; **not** on manual `POST /orders` | **P0** | Phase 2 |
| Per-strategy limits | MISSING | no deployment-scoped limits | P2 | Phase 3 |
| Square-off | PARTIALLY | `LiveRunner.flatten()` exists; no scheduled EOD square-off | P2 | Phase 3 |
| **ANALYTICS** | | | | |
| Portfolio P&L | IMPLEMENTED | `GET /portfolio`, `GET /dashboard/summary` | — | shipped |
| Strategy attribution | MISSING | no per-strategy P&L | P2 | Phase 3 |
| Trade journal | MISSING | `trade_journal` specified, not built; no MFE/MAE anywhere | P2 | Phase 3 |
| Live-vs-backtest comparison | MISSING | not implemented | P2 | Phase 3 |
| Market regime | PARTIALLY | `research/self_learning.py` classifies a regime from breadth/vol; methodology is in-repo but not surfaced as a product feature | P3 | Phase 3 |
| **FRONTEND** | | | | |
| Dashboard shell, 21 panels | IMPLEMENTED | `web/src/*.tsx`; hash router, sidebar, command palette | — | shipped |
| Auth gate + watchlist panel | IMPLEMENTED | `AuthGate.tsx`, `WatchlistPanel.tsx`; verified in a real browser 2026-09-14 | — | shipped |
| lightweight-charts | IMPLEMENTED | `ChartsPanel.tsx` (1,187 L) | — | shipped (must not be replaced) |
| Strategy workflow UI | MISSING | `StrategiesPanel.tsx` is a **read-only registry viewer** — no create/version/validate/backtest | P1 | Phase 2 |
| Screener UI (nested + explanations) | PARTIALLY | `CustomScannerPanel.tsx` — flat conditions, no match explanation | P2 | Phase 3 |
| Risk control center | PARTIALLY | `RiskPanel.tsx` shows limits + kill switch; **no reason field**, no per-strategy limits | P1 | Phase 2 |
| Async backtest UI | MISSING | depends on the backend job runner | P1 | Phase 2 |
| Options chain UI | MISSING | depends on the data layer | P3 | Phase 4 |

---

## 2. The top 10 highest-value gaps

Ordered by the brief's own criteria — trading safety, then correctness, then infrastructure,
then workflow, then analytical, then differentiation.

### 1. Risk is not enforced on the legacy order path — **fixed 2026-09-14**

**Status: closed on both surfaces.**

`RiskEngine` is comprehensive and well tested, but `api/main.py` never referenced it.
`POST /orders` ran `_require_live_execution()` (a mode check) and then called
`broker.place_order()` directly. With execution mode set to `live`, an order that breached
gross exposure, order notional, the daily loss limit, the symbol allowlist, the trading
window or the kill switch was transmitted to the exchange.

Every order path now goes through `atr.services.execution.ExecutionService`, which drives the
OMS: create → risk-check → submit → transmit. `RISK_APPROVED` is unreachable without a
decision, and the legacy routes were rewired onto the same path rather than left as a
documented bypass. Closing them required deciding who owns an order placed without a session;
the answer taken is that an order requires an account, and the reasoning is in §4.2.

The structural fix is the one that matters: **orders are assembled in one place and the venue
is a parameter.** Three hand-rolled constructions existed, and two of them were wrong — see
§4.5 for what that cost.

### 2. No OMS — order state is a single mutable field — **fixed 2026-09-14**

**Status: implemented.** `atr/execution/oms.py` holds the 11-state machine
(`NEW → VALIDATING → RISK_APPROVED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED`,
plus `CANCEL_PENDING → CANCELLED`, `REJECTED`, `EXPIRED`); `VALID_TRANSITIONS` is the whole
definition and `assert_transition` is its only reader, so an illegal move raises.
`atr/services/orders.py` applies it to persisted orders, and every transition is appended to
`order_events` with `seq`, `from_status`, `to_status`, broker/ack/fill timestamps, requested
and filled price, cumulative filled quantity, slippage, latency, rejection reason, the raw
broker payload, and the source of the change.

The lifecycle is reconstructable from the log alone: `orders.status` is a projection of the
newest event written in the same transaction, asserted by
`tests/test_oms.py::test_the_projection_equals_the_log_after_every_step`. There is no
`OrderRepository.update()` — a generic setter on the status column is the escape hatch that
would let a caller change an order's state without leaving a trace.

*Remaining:* the frontend order-detail timeline (Phase 3, with the strategy UI).

### 3. No durable idempotency — **fixed 2026-09-14**

**Status: implemented.** `order_intents.idempotency_key` is the primary key, so the `INSERT`
*is* the guard — there is no check-then-act window. A duplicate returns the order that already
owns the key with `created: false` rather than raising, because a retry is the case the guard
exists for. The order row and the key claim are written inside one savepoint, so a lost race
rolls back the briefly-inserted order and leaves no stray `NEW` order for reconciliation to
trip over.

Covered under real concurrency by
`tests/test_oms.py::test_concurrent_duplicate_intents_produce_exactly_one_order` (four threads,
one key, one order).

One thing this exposed: a key must **not** be derived for a manual order. With no `signal_id`
and no `strategy_id` the hash is still stable, so two unrelated clicks would collapse into one
order — an invisible bug. The route derives a key only for a signal-driven order.

### 4. Paper mode is a gate, not an engine — **fixed 2026-09-14**

**Status: implemented.** `atr/services/paper.py` provides two pieces, and the split
is the point.

**`PaperVenue`** decides whether and at what price an order fills. It is a
simulation of the exchange, so it models the things that make a fill uncertain and
refuses to model the things it cannot know:

* **A limit order the market has not reached rests.** Filling a buy limit above its
  own limit is the classic paper-trading lie — it manufactures edge the market never
  offered. The same for a stop that has not triggered. `match()` re-evaluates a
  resting order against a later price, so a paper order behaves like one that is
  live at the exchange rather than one that fills instantly and always.
* **An order that cannot be priced is rejected, not filled at zero.** A fill at
  `0.0` would give the position a zero cost basis and an infinite return.
* Costs default to `IndianDeliveryCosts`, **not** the IBKR-style default, which
  understates NSE delivery by roughly 28x. A paper account costed with it reports an
  edge that does not exist.

**`PaperLedger`** is the account, and it holds **no state of its own**. It replays
`order_events` into the existing `atr.backtest.portfolio.Portfolio`, so a paper
position cannot drift from the orders that produced it — the log is the state, and
`Portfolio` already asserts `equity == cash + market value` after every fill. No
paper state table was added; the schema was written for exactly this ("positions and
P&L are folds over those rows").

`order_events` gained a `commission` column so the fold is faithful: the frictions
actually charged are a fact, and recomputing them would restate history whenever a
rate changed.

**`BACKTEST → PAPER → LIVE` is now one strategy definition.** `PaperVenue` and
`BrokerVenue` both satisfy `Venue`, and both run through the same `ExecutionService`,
the same OMS lifecycle and the same risk gate. `POST
/api/v1/paper/deployments/{id}/orders` places a paper order through the venue; the
gate is handed that deployment's own folded portfolio, so the **position-level
limits now bite** — the one limit family that could not fire before.

Honesty at the API boundary: a position with no mark is returned with `null`
valuation and `priced: false`, `complete` is `false`, and `unpriced_symbols` names
it. The totals cover the priced positions and the response says so.

Covered by `tests/test_paper_engine.py` (48) and `tests/test_api_paper.py` (24).

*Remaining:* the paper loop is request-driven, not a scheduler — nothing yet
re-evaluates resting orders on a tick, and no live-quote price source is wired (the
seam exists: `atr.services.paper.default_price_source`). Both belong with the
deployment runner, which is the next piece of gap 6's infrastructure.

### 4. Paper mode is a gate, not an engine — **original assessment (2026-09-13)**

`execution_mode: "paper"` blocks transmission. It does not simulate anything. There is no
paper position book, no simulated fill, no slippage, no cost application. So the
`BACKTEST → PAPER → LIVE` progression the brief asks for does not exist: paper currently
means "signals are generated and nothing is transmitted".

*Needs:* backend (matching engine, paper portfolio) + frontend (paper positions/P&L).

### 5. WebSocket is unauthenticated — **P0, security**

`TickBroadcaster.connect()` calls `ws.accept()` unconditionally (`api/stream.py:209`). Any
client that can reach the port can stream live ticks. Every other surface now requires a
credential, so this is the remaining hole.

*Needs:* backend. The `API_CONTRACT.md` already specifies the first-frame auth handshake
(`{"type":"auth","token":...}`, 5 s timeout, close code 4401).

### 6. No reconciliation — **fixed 2026-09-14**

**Status: implemented.** `atr/services/reconcile.py` compares the platform's records
against the broker's and persists the result to `reconciliation_runs`, which was
specified in `DATA_MODEL.md` §3 and had no writer.

Everything else in this platform reasons from its own records, which is fine until a
record is wrong — and then every conclusion is wrong in the same direction with no
symptom. Reconciliation is the only thing that compares the platform's belief
against an outside source, which is what makes it the control that catches
everything else.

**Severity is asymmetric, deliberately.** An order the broker holds and the platform
does not know about is `critical`: something is live that nothing is managing — no
stop, no square-off, no risk check. A position that disagrees is `critical`, because
every risk limit is measured against it. A *holding* that disagrees is a `warning`:
on a T+1 market a holding and a position legitimately differ for a day after every
trade, and calling that critical trains an operator to ignore the alarm.

**A scope that cannot be compared is `warning` with a reason, never `ok`.** This is
the part worth keeping. A run that reported clean while quietly skipping half its
scopes is worse than one that reports a problem, because it is believed. The
platform keeps no cash ledger for a live account, so `funds` is not comparable
today — and the run says so instead of comparing nothing and reporting success.

**A run that cannot reach the broker is recorded, not raised.** Reconciliation that
throws is reconciliation that stops running, and this is the control that has to keep
running. The run is persisted with a severity that is not `ok` and an `error` naming
the cause, and the dashboard shows "reconciliation is not running" rather than a
blank screen.

`Broker` gained `holdings()` and `funds()`, and `IiflBroker` implements both over the
client's `/holdings` and `/limits`. They are **not** abstract methods: adding an
abstract method to that ABC breaks every existing implementer at import time, so they
raise `NotImplementedError` instead and the reconciler turns that into an explicit
"not comparable". `atr/services/broker_access.py` now owns client construction and
the paper/live gate, so `api/main.py` no longer builds a broker itself.

The platform's side of the comparison is the **same order-event fold the paper ledger
and the risk gate use**, so there is one answer to "what does the platform hold"
rather than three.

Covered by `tests/test_reconcile.py` (30) and `tests/test_api_reconciliation.py` (16).

*Remaining:* no scheduler — a run happens when somebody asks. A periodic run belongs
with the deployment runner. Routing a critical run into the alert-*rule* machinery
(thresholds, dedupe, per-channel policy) is still open; today it goes to the
configured channels directly.

### 6. No reconciliation — **original assessment (2026-09-13)**

Nothing compares internal state to the broker. If an order is rejected silently, or a
position is closed outside the system, the platform will not notice. The broker client already
exposes `positions()`, `holdings()`, `order_book()`, `trades()` and `limits()` — every input
reconciliation needs — so this is wiring plus a table, not new data acquisition.

*Needs:* backend (`reconciliation_runs` + comparison) + a dashboard surface.

### 7. Backtests are synchronous — **P1, infrastructure**

`POST /backtest` holds the connection for the duration. A walk-forward across 2,662 symbols
runs for minutes, which will hit any proxy timeout. There is no job abstraction, so the
`/api/v1/backtests` contract in `API_CONTRACT.md` is unimplemented.

*Needs:* backend (job runner + 6 endpoints) + frontend (progress and results).

### 8. Strategies are not persisted — **P1, workflow**

17 strategies exist as Python classes. Nothing is stored, so there is no versioning, no
immutability, no comparison, and no link from a backtest to the definition that produced it.
`DATA_MODEL.md` §4 specifies `strategies`, `strategy_versions`, `backtest_runs` — none exist.

*Needs:* backend then frontend. The Python registry must keep working unchanged.

### 9. Strategy workflow UI does not exist — **P1, workflow**

`StrategiesPanel` renders the registry. It cannot create, rename, version, validate or
backtest anything. The brief's `Strategy → Version → Definition → Validate → Backtest →
Paper → Live` path has no UI at all.

*Needs:* frontend, on top of gap 8. The rules shown must map to the real engine — a fake
editor would be worse than none.

### 10. Screener lacks nested conditions and match explanations — **P2, differentiation**

`CustomScannerPanel` supports a flat condition list with one global AND/OR. The brief asks
for nested AND/OR trees, and for each result to explain **why** it matched, from actually
evaluated conditions. The evaluation primitives in `signals/rules.py` are already
sufficient — this is a tree walk plus a per-condition trace.

*Needs:* backend (tree model + evaluator returning per-leaf verdicts) + frontend.

---

## 3. What must not be disturbed

Confirmed present and load-bearing. The next stage extends these; it does not replace them.

| Asset | Why it stays |
|---|---|
| `Broker` ABC (`brokers/base.py`) | The seam that lets backtest and live share code. Reconciliation will *add* methods to it, not reshape it. |
| `Strategy` ABC + `STRATEGIES` registry | 17 working strategies and the whole research layer depend on it. The persistent layer is built *beside* it. |
| `eval_entry` / `eval_exit` (`signals/rules.py`) | The "same strategy in research and live" property. The screener and the paper engine both consume it. |
| `RiskEngine` + `RiskLimits` | Correct and well tested. It needs *callers*, not changes. |
| `IndianDeliveryCosts` (`backtest/costs.py`) | The paper engine must reuse it so paper P&L is comparable to backtest P&L. |
| `research/validate.py`, `research/evidence.py` | The honesty layer. Nothing in this stage may loosen it. |
| Parquet history cache | The only real price history. Option work must not fabricate data on top of it. |
| Execution-mode gate + `data/audit/audit.jsonl` | The existing safety and audit trail. New dangerous actions extend the same mechanism. |
| `lightweight-charts` (`ChartsPanel`) | Explicitly not to be replaced. |

---

## 4. Defects found

### 4.1 CSRF rejected same-origin writes on any port but four — **fixed 2026-09-14**

`middleware.py` compared the `Origin` header against a hardcoded set of six dev URLs
(`:5173`, `:3000`, `:8000`). Browsers send `Origin` on **every** unsafe method, including
same-origin ones. So `atr serve --port 8123` served the SPA and the API from
`http://127.0.0.1:8123`, the browser sent that as `Origin`, it was not in the list, and every
write was refused with `csrf_origin_rejected` — a same-origin request rejected as
cross-origin.

*Fix:* the rule is now "is this the origin you connected to?" — the origin's
`scheme://host:port` is compared against the request's `Host` header, with the dev-origin list
kept for the genuinely cross-origin `atr dev` case, and `Origin: null` refused.
*Evidence:* `middleware.py::_same_origin`; `tests/test_api_v1.py::test_a_same_origin_request_is_allowed_on_any_port`;
`scripts/smoke_api.py` runs 68 live checks against a server on port 8899.

### 4.2 Risk limits bypassed on every API order path — **fixed 2026-09-14**

`RiskEngine` was never imported by `api/main.py`. `POST /orders` checked the execution mode and
nothing else. The signal-execute path additionally checked the kill switch (`main.py:630`) but
still did not evaluate limits. `_require_live_execution()` documents itself as the gate that
"every endpoint that can reach the broker with an order" calls — it is a *mode* gate, and mode
is not risk.

**Fixed on both surfaces.** `POST /api/v1/orders/{id}/validate` is the only route to
`RISK_APPROVED` and it takes the decision between the two transitions inside one transaction.
`LimitsRiskGate` adapts the existing `RiskEngine` unchanged, and `RiskStateService.gate()` builds
it from the durable kill switch and limits, so the switch applies to every order path.

The two **legacy** routes were rewired onto the same path rather than left as a documented
bypass. That required deciding who owns an order placed without a session, and the answer taken
is: **an order requires an account.** `orders.user_id` is a non-null foreign key, and an
unattributed order is one nobody can be asked about — the per-account kill switch would have
nothing to switch. So `POST /orders` and `POST /trade-signals/{id}/execute` now return `401`
`authentication_required_for_orders` without one. Read-only legacy routes stay open; the
dashboard still renders without signing in.

This is a deliberate behaviour change. It is recorded here and in `API_CONTRACT.md` rather than
made quietly, because it is the one place where "keep unversioned routes working" and "no
unauthorized access on every resource" pull in opposite directions, and trading safety wins.

Three things the adapter fixed rather than inherited:

* `RiskEngine.check_order` prices an order as `limit_price or last_price`. With neither
  available the notional comes out `0.0`, so a MARKET order passed every notional limit
  without being measured against any of them — the "unknown reported as a zero" failure. The
  gate now refuses an order it cannot price when a finite notional limit is configured.
* `RiskLimits.max_position_notional` defaults to `float("inf")`, which is truthy, so a naive
  `if limit` read "no limit" as "a limit is configured".
* The kill switch and execution mode were an **in-process dict**, so a restart silently re-armed
  trading. They are now durable in `system_state` and read through one service.

Covered by `tests/test_api_orders.py`, `tests/test_risk_state.py`,
`tests/test_legacy_order_routes.py` and `tests/test_execution_mode.py`.

### 4.5 `OrderType.SL_MARKET` did not exist — the signal bracket crashed after placing the entry — **fixed 2026-09-14**

The most dangerous defect found in this audit, and it was live.

`api/main.py:662` built the stop-loss leg with `OrderType.SL_MARKET`. The enum has
`MARKET`, `LIMIT`, `STOP` and `STOP_LIMIT` — there is no `SL_MARKET`, so the attribute access
raised `AttributeError`. The route places its legs in order, so the **entry order had already
been transmitted** when the exception fired:

```
POST /trade-signals/{id}/execute
  1. entry  -> placed at the exchange
  2. stop   -> AttributeError          <- 500 returned, entry still live
  3. target -> never reached
```

The operator saw a 500 and had no way to tell that a position had been opened. The platform
had produced the one state it must never produce: **a live position with no stop-loss.**

Two further faults in the same leg would have survived the name being correct:

* the trigger was passed as `limit_price` and as `broker_params["triggerPrice"]`, and
  `IiflBroker` reads the trigger from `order.stop_price` — so the stop would have reached the
  exchange with no trigger price;
* there was no idempotency guard, so clicking Execute twice placed the bracket twice.

*Fix:* order types are canonicalised in one place, `atr.core.enums.OrderType.parse`, which
accepts the broker's spellings (`SL-M`, `SL`, `stop_market`) and refuses an unknown one **before
anything is transmitted**. Orders are assembled only in `atr.services.execution`, so a route can
no longer build one wrong. Each leg carries an idempotency key derived from
`(signal_id, leg_index)`, so a double click returns the existing order. A failed leg now names
itself and returns the legs already placed, so a half-placed bracket is a described state rather
than a mystery.

*Tests:* `tests/test_execution_service.py` (order-type mapping, trigger placement, per-leg
idempotency), `tests/test_legacy_order_routes.py` (the AST pins that would have caught the
original).

### 4.3 Two broker interfaces — **open, P1**

`SimulatedBroker` (`backtest/broker.py`) does not implement the `Broker` ABC: it exposes
`submit`/`cancel`/`on_bar`, while `Broker` declares `place_order`/`cancel_order`. The
abstraction is honoured for live and duplicated for backtest. This is the structural reason a
paper engine cannot simply be "the backtest broker wired to live ticks" — which is precisely
what gap 4 needs.

*Fix:* Phase 2 — give `SimulatedBroker` a `Broker`-compatible adapter so backtest, paper and
live all satisfy one interface.

### 4.4 `polars` undeclared — **fixed 2026-09-13**

Imported by three modules (`stream.py` among them) but absent from `pyproject.toml` and
`uv.lock`. A clean install would fail at import. Added as `polars>=1.0`.

---

## 5. Verification of the audit's own claims

The classification above is falsifiable. Three checks back it:

| Claim | Command |
|---|---|
| The suite passes on the audited tree | 358 passed, 1 skipped, 0 failed (`pytest -q`) |
| The API contract the frontend consumes is real | `scripts/smoke_api.py` — 68/68 live checks on a real socket |
| The UI actually renders and works | `scripts/probe_phase1_ui.py` — gate, login, watchlist, real prices, in headless Chromium |

---

## 6. Recommended sequence

```
Phase 2 — trading safety & correctness   (gaps 1, 2, 3, 5, 6)
  [done] 1. order_events + order_intents + the OMS state machine
  [done] 2. risk check inside the OMS, so no route can bypass it
  [done] 2b. every order path through one execution service + a venue
  [done] 3. paper execution engine on the shared rule layer
  [next] 4. a deployment runner: drive the loop, poll resting orders, price off
           the live quote rather than the daily cache
  [next] 5. reconciliation + a visible mismatch surface
  [next] 6. WebSocket auth
  7. async backtest jobs + persisted strategies + the strategy UI

Phase 3 — workflow & analytics           (gaps 9, 10)
  7. screener with nested conditions and match explanations
  8. risk control center UI, trade journal (MFE/MAE), attribution
  9. live-vs-backtest, monthly returns, Monte Carlo

Phase 4 — market breadth
 10. corporate actions, market calendar, expiry calendar
 11. option chain, Greeks, IV, OI, multi-leg, payoff
 12. regime engine with a reproducible methodology
```

Phase 2 is ordered so that each step is a prerequisite for the next: the OMS owns the
transitions the paper engine emits, idempotency protects both, and reconciliation compares
what the OMS recorded against what the broker reports.

**The paper engine is built** (`services/paper.py`), and it is what the design predicted: a
`Venue`. `PaperVenue` and `BrokerVenue` both satisfy the protocol and both run through the same
`ExecutionService`, OMS lifecycle and risk gate, so `BACKTEST → PAPER → LIVE` is one strategy
definition rather than three implementations. `PaperLedger` closes the loop by folding
`order_events` into the existing `Portfolio` — no paper state table, so a paper position cannot
drift from the orders that produced it — and `POST /api/v1/paper/deployments/{id}/orders` hands
the gate that deployment's own portfolio, which is what finally makes the position-level limits
bite.

**The immediate next step is the deployment runner.** The engine is *request-driven*: nothing
re-evaluates a resting order on a tick, and the price source is still the daily cache.
`PaperVenue.match()` already implements the re-evaluation and
`atr.services.paper.default_price_source` is the seam for a live quote feed, so the runner is
the piece that joins them into a loop — and it is the same loop a live deployment needs.

**Reconciliation is now cheap and worth doing at the same time**, because both of its sides
exist: the platform's side is a fold over `order_events` (the paper ledger does it today), and
the broker's side is `IiflBroker.positions()` and `open_orders()`. What is missing is
`holdings()`/`funds()` on the `Broker` ABC, the comparison itself, and a visible surface for a
mismatch.

Two smaller items that are now cheap and worth doing alongside it:

* `atr.data.store`'s deprecated Postgres `orders`/`fills` DDL should be deleted, so there is
  no second schema claiming to describe orders.
* `TradeSignal` should carry `strategy_id`/`strategy_version`, so an executed signal is
  attributable to a strategy version in the order row and the journal rather than only to a
  signal id.
