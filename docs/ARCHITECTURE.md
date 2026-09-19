# atr — Technical Architecture

Status: **Phase 1 in progress**. This document is the design of record. It is
written against the repository as it actually exists, not against an ideal.

Companion documents:

| Document | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | this file — survey, target architecture, events, phases |
| `docs/DATA_MODEL.md` | full schema: control plane, market data, trading, strategy |
| `docs/API_CONTRACT.md` | every endpoint, grouped, with auth requirements |
| `docs/PHASE1.md` | what Phase 1 shipped, how to run it, what is deliberately deferred |
| `.workbuddy-ai/memory/REFERENCE.md` | operational traps (broker, build, sandbox, engine) |
| `.workbuddy-ai/memory/FINDINGS.md` | the statistical record — what has and has not been proven |

---

## Part 0 — Answers to the ten survey questions

### 1. Current frontend stack

React 19 + TypeScript 5.5 + Vite 5 + Tailwind 4, single-page app in `web/src`
(77 source files, 21 panels). Charting is **`lightweight-charts` v4** — TradingView's
own library, which is the right choice and should not be replaced. Motion comes
from `motion` (beui.dev components) and icons from `lucide-react`. There is no
router library: navigation is a hand-rolled hash router (`#tab/subtab`) with tab
and sub-tab state held in `App()`. `bun run build` runs `tsc --noEmit && vite build`;
the FastAPI process serves `web/dist` via `StaticFiles` and mounts `/assets`.

### 2. Current backend stack

FastAPI + uvicorn, Python 3.12, **single 2,773-line module** `atr/api/main.py`
holding every route. No `APIRouter`, no dependency injection, no auth middleware.
SQLAlchemy 2.0 **Core** (not declarative ORM) with an optional Postgres +
TimescaleDB engine. `pydantic` v2 for request/response models, `pydantic-settings`
for config, `loguru` declared but `logging` used in practice, `httpx` + `tenacity`
for outbound calls, `paho-mqtt` for the IIFL market bridge. No task queue.

One defect worth naming: **`polars` is imported by `api/stream.py`,
`data/history.py` and `signals/engine.py` but is absent from `pyproject.toml` and
`uv.lock`.** Those code paths work only because polars happens to be present in the
environment. This is a latent break for a clean install.

### 3. Database

Three tiers, only one of which is a database:

- **Postgres + TimescaleDB** — optional, lazily connected, `atr/data/store.py`.
  Tables `instruments`, `bars`, `ticks`, `orders`, `fills`; hypertables and
  compression set up in `init_schema()`. No migrations (no Alembic) — schema is
  `metadata.create_all`.
- **Parquet cache** — the tier actually in use. `data/iifl_daily/<EXCHANGE>/<SYM>.parquet`,
  one file per symbol, written by `atr/data/history.py` from IIFL
  `/marketdata/historicaldata`. ~2,654 files. A full read takes ~56s.
- **JSON / JSONL side files** — `data/alerts/*.json`, `data/audit/audit.jsonl`,
  `data/trade_signals/{queue,settings}.json`, `data/paper_momentum/*.jsonl`.

### 4. Authentication

**None at the application level.** `settings.api_token` exists and is never read.
There is no user, session, role or login route. The only auth in the process is
the *broker* session: `atr/brokers/iifl/auth.py` completes the IIFL OAuth handshake,
derives a JWT and caches it to `.cache/iifl_session.json` until midnight IST. That
protects nothing in `atr` itself — anyone who can reach the port can call
`POST /orders` (subject only to the paper/live gate) and `POST /risk/kill-switch`.

This is the single largest Phase 1 gap, and the reason it is first.

### 5. Market-data infrastructure

Real and reasonably good. `atr/data/base.py` defines a `DataFeed` ABC;
`csv_feed.py` provides `CsvFeed`/`ParquetFeed`; `history.py` bulk-downloads and
caches; `aggregator.py` does rolling-window/timeframe aggregation; `hygiene.py`
detects and removes corrupt bars (reuse it — the cache is known-bad on
2021-09-15/16). Live ticks arrive over MQTT through `brokers/iifl/bridge.py` +
`codec.py` and are fanned out by `api/stream.py`'s `TickBroadcaster` to
`/ws/ticks`. There is no option chain service, no corporate-action adjustment and
no market calendar.

### 6. Broker integration

Yes, and it is the best-designed part of the repo. `atr/brokers/base.py` defines a
`Broker` ABC (`place_order`, `modify_order`, `cancel_order`, `positions`,
`open_orders`, `last_price`, `cancel_all`) and a `FillListener` ABC.
`IiflBroker` implements it. The backtester uses `SimulatedBroker` through the same
shape. **The abstraction already exists and is honoured** — the strategy layer
never names IIFL. What is missing is not the interface but the *fan-out*: no
capability declaration (a broker that cannot do basket orders, GTT, or option
selling should say so), no reconciliation, no order-state machine.

### 7. Strategy and backtest engine

`Strategy` ABC (`strategy/base.py:277`) with a vectorised `prepare(frames)` and a
per-bar `on_bar(ctx)`; `StrategyContext` exposes equity/cash/position/row/history/
order/target/close and deliberately **no broker**. Strategies are registered in a
`STRATEGIES` dict (17 entries). The engine is **event-driven over pandas frames**
(`backtest/engine.py:125`), ordering each bar as fills → mark → risk → strategy,
with `SimulatedBroker(fill_on_next_open=True)` and a per-bar conservation assert.
Indicators live in `strategy/indicators.py` (sma, ema, rsi, atr, bollinger, vwap,
session_range, crossover/crossunder). Costs are modelled properly for India in
`backtest/costs.py` — `IndianDeliveryCosts` (STT, stamp, GST, DP charges) alongside
an IBKR-style `CommissionModel`. Look-ahead is prevented by ordering plus the
next-open fill, and pinned by `tests/test_engine.py`.

There **is** already a shared rule layer — `signals/rules.py`'s `eval_entry` /
`eval_exit`, used by both the live scanner (`signals/engine.py`) and the
backtest path (`signals/strategy.py`). That is exactly the "same strategy
definition in research and live" property the product needs, and it should be
extended rather than replaced.

### 8. Risk, OMS and execution

**Risk exists and is real**: `execution/risk.py` — `RiskEngine` / `RiskLimits` /
`RiskVerdict` with gross exposure, position notional, per-symbol units, daily loss,
daily trade count, open-position count, order notional, short permission, symbol
allow-list, trading window and a kill switch. It runs in both backtest and live
paths. Execution mode is gated **server-side** (`_require_live_execution`), which
is correct — the gate is where the money is, not in the toggle component.

**OMS does not exist.** There is an `OrderStatus` enum and an `Order` model, but no
state machine, no transition validation, no idempotency, no partial-fill
accounting, no order lifecycle record. **Reconciliation does not exist** beyond a
manual `fills_from_trades()` helper. **Paper trading is a mode flag, not an
engine** — `execution_mode="paper"` blocks live orders, but there is no simulated
matching engine that turns a signal into a fill at the live price.

### 9. Tests

pytest + pytest-asyncio, 15 test modules: engine correctness (no look-ahead,
conservation, risk halt), costs, indicators, validate (walk-forward statistics),
evidence, execution-mode gate + audit, alerts, briefing, dashboard summary,
hygiene, IIFL broker payloads, live runner, signals, alpha candidates,
episodic pivot. The quality is unusually good for this kind of project —
`test_engine.py` and `test_validate.py` in particular test the things that are
easy to get wrong. There is **no** API-level test, no auth test, no OMS test, no
reconciliation test.

### 10. Config and secrets

`atr/config/settings.py` — pydantic-settings, reads `.env`, `lru_cache`d.
Names in `.env.example`: APP_NAME, ENV, LOG_LEVEL, IIFL_APP_KEY, IIFL_APP_SECRET,
IIFL_REDIRECT_URL, IIFL_BASE_URL, IIFL_CLIENT_ID, IIFL_TIMEOUT, IIFL_SESSION_CACHE,
IIFL_DEFAULT_PRODUCT, IIFL_API_ORDER_SOURCE, IIFL_MARKET_PROTECTION_PERCENT,
IIFL_FORCE_IPV4, BRIDGE_HOST, BRIDGE_PORT, DB_*, MAX_*, ALLOW_SHORT,
KILL_SWITCH_ENABLED, SQUARE_OFF_TIME, BT_*, API_HOST/PORT/TOKEN, TELEGRAM_*,
FAST2SMS_API_KEY, ALERTS_POLL_SEC.

Broker credentials live in plaintext in `.env` and the JWT in
`.cache/iifl_session.json`. For a single-operator local tool that is defensible;
for anything multi-user it is not. Phase 1 introduces encrypted-at-rest secret
storage for per-user broker credentials and leaves the `.env` path working for
the single-operator case.

---

## A. Current architecture

```
                     ┌───────────────────────────────────────────┐
                     │  web/  React 19 + Vite + Tailwind 4       │
                     │  21 panels · hash router (#tab/subtab)     │
                     │  lightweight-charts v4 · /ws/ticks         │
                     └────────────────────┬──────────────────────┘
                                          │  fetch (relative URL)
                     ┌────────────────────▼──────────────────────┐
                     │  atr/api/main.py   — 2,773 lines, 60+ routes│
                     │  no APIRouter · no DI · no auth · CORS only│
                     └───┬───────┬───────┬───────┬───────┬───────┘
                         │       │       │       │       │
        ┌────────────────▼─┐ ┌───▼────┐ ┌▼─────┐ ┌▼─────┐ ┌▼──────────┐
        │ data/            │ │signals/│ │back- │ │exec- │ │brokers/   │
        │ history · store  │ │rules   │ │test/ │ │ution/│ │ Broker ABC│
        │ hygiene · feed   │ │engine  │ │engine│ │risk  │ │ IiflBroker│
        │ parquet 2,654    │ │cross_  │ │costs │ │      │ │ MQTT bridge│
        └──────────────────┘ │sectional│ │metrics│ └──────┘ └───────────┘
                             └────────┘ └──────┘
        ┌──────────────────┐ ┌────────┐ ┌────────┐ ┌──────────┐
        │ research/        │ │alerts/ │ │strategy/│ │scanner   │
        │ validate (WF)    │ │3 chan  │ │ABC + 17 │ │scanner_  │
        │ evidence·papers  │ │JSONL   │ │registry │ │custom DSL│
        └──────────────────┘ └────────┘ └────────┘ └──────────┘

  Persistence: Postgres+Timescale (optional) · parquet cache (real) · JSON/JSONL
  Auth: none at app level. Audit: append-only JSONL. No event bus.
```

**What is genuinely good and must not be disturbed:** the broker ABC, the
`Strategy` ABC + registry, the `eval_entry`/`eval_exit` shared rule layer, the
`RiskEngine`, `IndianDeliveryCosts`, `research/validate.py` (walk-forward +
deflated Sharpe), the `evidence` honesty layer, the parquet history cache, and
the execution-mode gate with its audit trail. These are the assets.

**What is structurally wrong:** one giant route module with no auth in front of
it; no user/session/ownership concept anywhere, so nothing can be multi-user and
nothing can be attributed; no OMS, so no order can be reasoned about after
submission; no event bus, so the live path is a chain of direct calls with no
idempotency; and configuration/credentials mixed into a single flat namespace.

## B. Target architecture

### The decision: modular monolith, with enforced seams

The brief says not to over-engineer into microservices. Agreed, and the repo is
already a monolith — the work is not to split it but to **make the seams real**.
A service earns extraction only when it has a different scaling profile, a
different failure domain, or a different deploy cadence. Today exactly one
candidate qualifies (the market-data ingestion loop, which is long-running and
latency-sensitive), and it does not need to move yet.

So: one deployable, one process, layered packages, with a **layering test that
fails the build on a forbidden import**. That is a boundary you can trust,
unlike a convention.

```
                          ┌──────────────────────────┐
   transport  ───────────►│  atr/api/                │  routers, deps, middleware
                          │  no business logic       │  request-id, rate limit,
                          └────────────┬─────────────┘  CSRF, security headers
                                       │
                          ┌────────────▼─────────────┐
   application ──────────►│  atr/services/           │  use cases, transactions,
                          │  orchestrates, no SQL    │  authorization decisions
                          └──┬────────┬────────┬─────┘
                             │        │        │
        ┌────────────────────▼─┐ ┌────▼─────┐ ┌▼──────────────┐
        │ atr/appdb/           │ │atr/data/ │ │atr/brokers/   │
        │ control-plane store  │ │market    │ │Broker ABC +   │
        │ users·sessions·      │ │data feeds│ │adapters       │
        │ watchlists·audit     │ │+history  │ │IIFL now,      │
        │ (SQLAlchemy Core)    │ │+hygiene  │ │Zerodha/Upstox │
        └──────────────────────┘ └──────────┘ │later          │
                                              └───────────────┘

   domain (imported by everyone, imports nobody):  atr/core/  models·enums·events
   compute (pure, no I/O):  atr/backtest/ · atr/research/ · atr/strategy/
   decision:  atr/signals/ (shared eval_entry/eval_exit)  atr/execution/ (risk)
```

**Layering rule**, enforced by `tests/test_architecture.py`:

| Layer | May import | May NOT import |
|---|---|---|
| `atr.core` | stdlib, pydantic | everything else in `atr` |
| `atr.backtest`, `atr.research`, `atr.strategy` | `core`, `data`, `signals` | `api`, `appdb`, `brokers` (except via ABC) |
| `atr.execution` | `core`, `brokers.base` | `api`, `appdb` |
| `atr.appdb`, `atr.data`, `atr.brokers` | `core` | `api`, `services` |
| `atr.services` | all of the above | `api` |
| `atr.api` | `services`, `core` | direct `appdb` / `data` / `brokers` access |

The last row is the one that pays for itself: a route may not touch SQL or a
broker directly, so every action has exactly one place where authorization and
audit happen.

### Service inventory — what exists, what is new, what moves

| Logical service | Today | Target |
|---|---|---|
| Auth Service | **absent** | `atr/auth/` + `atr/services/auth.py` — **built in Phase 1** |
| Market Data Service | `atr/data/` | keep; add `instruments/service.py` (**Phase 1**) and an option-chain provider |
| Screener Service | `scanner*.py` + `signals/` | keep; wrap in `services/screener.py`, persist saved scans in `appdb` |
| Strategy Service | `strategy/` + `STRATEGIES` | keep; add `appdb` strategy + version storage (**Phase 4**) |
| Backtest Service | `backtest/` + `research/` | keep; move job execution off the request thread |
| Paper Trading Engine | mode flag only | `services/paper.py` — `PaperVenue` + `PaperLedger` — **built 2026-09-14** |
| Deployment Service | **absent** | `services/paper.py` — deployments + lifecycle, and `services/monitoring.py` — the read-only projection — **built 2026-09-15** |
| Paper Runner | **absent** | `services/runner.py` — the clock. Consumes live ticks, calls `eval_entry`/`eval_exit`, and sequences risk → OMS → venue → fill. Contains no matching, no costing and no P&L arithmetic of its own — **built 2026-09-15** |
| Trade Journal | **absent** | `services/journal.py` — one row per round trip, reconciled from the position fold — **built 2026-09-15** |
| Risk Engine | `execution/risk.py` | keep; extend limits, add per-strategy scoping |
| Order Management Service | **absent** | `execution/oms.py` state machine + `services/orders.py` + `order_intents` — **built 2026-09-14** |
| Execution Service | **absent** | `services/execution.py` — the one path to a venue — **built 2026-09-14** |
| Risk State Service | module-level dict in `api/main.py` | `services/risk.py` + `system_state` — **built 2026-09-14** |
| Broker Adapter Service | `brokers/base.py` | `services/broker_access.py` owns construction + the live gate; `holdings()`/`funds()` on the ABC — **built 2026-09-14** |
| Reconciliation Service | **absent** | `services/reconcile.py` + `reconciliation_runs` — **built 2026-09-14** |
| Portfolio Service | `api/main.py` inline | `backtest/portfolio.py` (accounting) + `services/paper.py` (the fold); extract the rest |
| Notification Service | `alerts/` | keep; add channels + routing |
| Audit Service | JSONL appends | `appdb.audit_events` + query API (**Phase 1**) |
| Analytics Service | `backtest/metrics.py` | keep; add regime + live-vs-backtest comparison |
| Signal Context Engine | **absent** | `signal_context/` (pure `engine.py` + `analytics.py`), `services`-equivalent `signal_context/service.py`, `signal_contexts` table, `/api/v1/signal-context` — **built 2026-09-17** |
| Attribution Service | **absent** | `analytics/` (pure `excursions.py` + `attribution.py` + `aggregation.py`), `services/attribution.py`, `trade_attributions` table, `/api/v1/analytics` — **built 2026-09-17** |

### The order path, and why it is one path

```
route ──► services/execution.py ──► services/orders.py ──► execution/oms.py
                    │                        │
                    │                        └──► appdb.repositories (order_events, order_intents)
                    └──► Venue ──┬──► BrokerVenue    ──► IiflBroker (live)
                                 └──► PaperVenue     ──► matching + Indian costs (paper)
                                            │
                                            └──► PaperLedger folds order_events back
                                                 into backtest.portfolio.Portfolio
```

Downstream of the closed trade, one more leg reads that fold without writing to it:

```
trade_journal (closed episode) ──► services/attribution.py ──► analytics/ (pure)
                                            │                        │
                                            │                        └── 9-branch tree, excursions,
                                            │                            execution quality, buckets
                                            └──► trade_attributions ──► /api/v1/analytics
                                                                  └──► learning dataset columns
```

**The arrow is one-way.** The journal does not know attribution exists. Journaling
is about *what happened*; attribution is about *why*, and keeping the direction
one-way is what stops a reporting concern from being able to fail a trade
recording. `services/attribution.py` calls the journal's idempotent `reconcile`
first — not because it needs to write, but because attributing a trade requires
the trade to exist.

**`analytics/` imports no storage and `services/attribution.py` writes exactly one
table.** Those two properties are what make the layer safe to run on a schedule
and what four of the integrity tests assert structurally rather than trusting to
review.

Three rules hold this shape together, and each exists because breaking it caused a
real bug:

1. **A route never builds an order.** Orders are assembled only in
   `services/execution.py`. When three call sites each built one, two of them were
   wrong — one named an enum member that did not exist, and the same leg put the
   stop trigger in a field the broker does not read. Both would have reached the
   exchange.
2. **`RISK_APPROVED` is unreachable without a decision.** The transition is taken
   inside `OrderService.validate`, between the two events, in one transaction.
3. **The venue is a parameter.** That is what makes `BACKTEST → PAPER → LIVE` one
   strategy definition rather than three implementations: the lifecycle, the risk
   gate and the rule layer are shared; only the venue differs.

`PaperLedger` closes the loop, and it is why there is no paper state table: the
account is a **fold over `order_events`**, so a paper position cannot drift from the
orders that produced it. `Portfolio` is reused rather than reimplemented — it
already asserts `equity == cash + market value` after every fill, a check the
backtester has been running for as long as the backtester has existed.

`execution/oms.py` is a compute layer and may not import `appdb`; `services/orders.py`
owns the transaction. `tests/test_architecture.py` enforces the split.

### Reconciliation is the control that checks the rest

Every other component reasons from the platform's own records. That is fine until a
record is wrong, and then every conclusion drawn from it is wrong in the same
direction with no symptom. Reconciliation is the only thing that compares the
platform's belief against an outside source:

```
services/reconcile.py ──┬── platform side: the order-event fold (shared with the
                        │                     paper ledger and the risk gate)
                        └── broker side:   broker.open_orders() / positions()
                                           / holdings() / funds()
                                    │
                                    └──► reconciliation_runs  (persisted)
                                         + an audit row + a notification
```

Two rules make it trustworthy rather than decorative:

* **A scope that cannot be compared is `warning` with a reason, never `ok`.** The
  platform keeps no cash ledger for a live account, so `funds` is not comparable
  today — and the run says so instead of comparing nothing and reporting success. A
  control that reports clean while skipping half its scopes is worse than one that
  reports a problem, because it is believed.
* **A run that cannot reach the broker is recorded, not raised.** Reconciliation
  that throws is reconciliation that stops running.

The platform's side is deliberately the *same* fold the paper ledger and the risk
gate use, so there is one answer to "what does the platform hold" rather than three.

### Non-functional targets

- **Type-safe at the boundary.** Pydantic v2 request/response models on every
  route; `mypy`-clean on `atr/services` and `atr/auth` if the team wants it
  (the repo is not currently type-checked, so this is aspirational, not claimed).
- **Observable.** A request ID on every response and every log line; a
  `correlation_id` on every event and every order.
- **Fault-tolerant.** One dead endpoint must not blank a page — the existing
  `dashboard/summary` pattern of independently try/excepting each section is the
  house style and should be copied, not invented around.
- **Secure by default.** Auth on by default; the paper/live gate stays where it
  is; secrets never in a response body; a dangerous action always requires an
  explicit confirmation *and* a server-side reason string.

## C. Database schema

Full DDL and rationale in **`docs/DATA_MODEL.md`**. Summary of the split:

| Tier | Store | Why |
|---|---|---|
| **Control plane** | `atr/appdb` — SQLite by default, Postgres by URL | users, sessions, API keys, watchlists, preferences, audit. Low volume, relational, must work with zero setup. |
| **Market data** | `atr/data/store.py` — Postgres + TimescaleDB | high-volume time series, hypertables, compression, continuous aggregates. |
| **Market data (cache)** | parquet under `data/` | what actually runs today; keep it as the read path for history. |
| **Strategy + trading** | Postgres when available, `appdb` for definitions | definitions are relational and small; fills are time series. |

Phase 1 creates the control-plane tables only. It deliberately does **not**
create the trading or options tables — an unused table is a liability, and
`docs/DATA_MODEL.md` specifies them so Phase 3/4 can build them without
redesign.

## D. API contract

Full contract in **`docs/API_CONTRACT.md`**. Phase 1 introduces the versioned
prefix `/api/v1` for all new routes and leaves the existing unversioned routes
working, so nothing breaks while the surface is reorganised.

The three rules the contract encodes:

1. **Every new route declares its permission.** `require_permission("watchlist:write")`
   as a FastAPI dependency, so the check cannot be forgotten in the body.
2. **Dangerous actions require a reason.** Same pattern as
   `POST /risk/execution-mode` — a 400 without a reason string, and the reason
   lands in the audit log.
3. **Ownership is checked, not implied.** A watchlist belongs to a user; a route
   takes an ID and must prove the caller owns it.

## E. Event architecture

Today there is no bus: the live path is `TickBroadcaster` → WebSocket, and
signals → orders by direct call. That is fine at one strategy and wrong at ten,
because nothing deduplicates and nothing is replayable.

**Design: a synchronous in-process `EventBus` behind an interface that a real
broker (Redis Streams, Kafka, NATS) can implement later.** Synchronous because
in-process asynchrony buys nothing here and costs determinism; the interface is
what matters.

```
Market feed (MQTT / parquet replay)
        │
        ▼
   Normalizer            ← atr/data/ (hygiene, timeframe aggregation, symbol canonicalisation)
        │
        ▼
   EventBus  ── typed, ordered, idempotent ──────────────────────────┐
        │                                                            │
   ┌────┴─────┬──────────────┬───────────────┬──────────────┐        │
   ▼          ▼              ▼               ▼              ▼        │
Screener  Strategy       Charts          Journal       Analytics     │
Engine    Engine         (WS fan-out)    Recorder      Recorder      │
   │          │                                                      │
   │          ▼                                                      │
   │     SignalEmitted ──► Risk Engine ──► OrderIntent ──► OMS ──► BrokerAdapter ──► Exchange
   │                          │                │            │
   │                          ▼                │            ▼
   │                     RiskViolation         │      OrderAcked / OrderFilled
   │                          │                │            │
   └──────────────────────────┴────────────────┴────────────┴──► PositionUpdated
                                                                     │
                                                          Reconciliation (periodic)
                                                                     │
                                                              MismatchDetected
                                                                     │
                                                              Notification Service
```

**Event envelope** (every event, no exceptions):

```
event_id        uuid4          identity, for dedupe and replay
event_type      str            "SignalEmitted", "OrderFilled", …
occurred_at     datetime UTC   when it happened in the world
recorded_at     datetime UTC   when we learned about it  ← these differ under latency
correlation_id  uuid4          one user intent / one strategy cycle
causation_id    uuid4 | None   the event that caused this one — gives a full trace
strategy_id     str | None
strategy_version int | None
schema_version  int            bump on shape change; consumers refuse unknown
payload         dict           type-specific
```

**Idempotency.** The order path is where duplicate execution costs money, so it
is keyed explicitly:

```
OrderIntent.idempotency_key = sha256(strategy_id | version | bar_ts | symbol | side | leg_index)
```

The OMS keeps a unique index on `idempotency_key`; a second intent with the same
key returns the first order instead of placing a second one. This is what makes a
retry, a reconnect, or a duplicated feed message safe.

**Why not Kafka.** One process, one user, thousands of bars a day. An external
broker would add an operational dependency to buy nothing. The interface is the
deliverable; the transport is a config value.

## The learning pipeline: from a live tick to a finding

The self-learning engine reads what the system actually did and reports what it
means. It is **advisory only** — no route, no method and no payload on this path
can change a strategy, place an order or bypass the risk engine, and
`tests/test_learning_api.py` asserts the router's mutating method set is empty.

The chain, and the module that owns each link:

```
LIVE MARKET      atr/services/runner.py      the deployment clock; ticks and bars
      ↓
PAPER STRATEGY   atr/signals/rules.py        eval_entry / eval_exit — the SAME
                                             functions the backtester calls
      ↓
PAPER ORDER      atr/services/orders.py      OrderRepository.create writes the order
                                             and its NEW event, stamped with provenance
      ↓
PAPER FILL       atr/services/paper.py       PaperVenue matches against the live price,
                                             with IndianDeliveryCosts and a slippage model
      ↓
CLOSED TRADE     atr/services/journal.py     reconcile() folds order_events into positions
                                             and opens/closes a trade_journal episode
      ↓
LEARNING DATASET atr/services/learning.py    LearningDatasetBuilder normalises
                                             backtest_trades + trade_journal + the paper
                                             ledger into one table, with entry-time
                                             features enriched point-in-time; journal
                                             rows keep their signal_id, opening
                                             order and recorded context
                                             (score/version/class) as recorded
      ↓
DAILY ANALYSIS   atr/services/learning.py    DailyLearningReport + PerformanceAnalysis
                                             + the drift comparison
      ↓
READINESS        atr/services/learning.py    readiness(): per-strategy evidence
                       + research/              against the 10/30/50 gates, research
                         learning_readiness.py  states, weekly accumulation and
                                             data-quality flags — read-only
```

**Nothing in the chain is a manual export.** A paper trade that closes becomes a
learning observation on the next dataset build, because the journal is a
projection of the same position fold the account's P&L comes from — the journal
cannot disagree with the book, and it cannot be skipped.

### Being on the feed is not the same as reading it

The first link has two halves and only one of them is a read:

```
READING    atr/services/paper.py      live_tick_source → broadcaster.latest_price()
BEING ON   atr/api/stream.py          ensure_symbols() → bridge.subscribe_feed()
```

A symbol reaches the broadcaster's tick store only when somebody subscribes to
it, and subscription used to be reachable from one place only — the browser
path. So with no dashboard open the bridge was never connected, every
`latest_price` returned `None`, and `default_price_source` fell through to the
daily cache: the deployment was priced at yesterday's close, or not priced at
all, while reporting itself as running. Either outcome is worse than an honest
refusal, and both are silent.

The runner therefore *states* its universe — `PaperRunner.sync_loops` →
`ensure_live_symbols` → `TickBroadcaster.ensure_symbols` — on every pass, and the
broadcaster keeps server-side subscriptions separate from browser ref counts so
the two cannot unsubscribe each other. The statement is declarative (the delta
against the previous statement is what reaches the bridge), retried when the
bridge was down, and reports symbols it could not resolve to a contract rather
than swallowing them.

### Two levels of evidence, because they answer two questions

`evidence_class` says **where a record came from**: `BACKTEST`, `IN_SAMPLE`,
`PAPER_FORWARD`, `LIVE_FORWARD`. The drift comparison needs all four — comparing
paper against real money is the point of having a paper engine.

`evidence_grade` says **whether a finding may depend on it**: `forward` or
`in_sample`. Every consumer that has to decide whether it may publish a number
reads this one. Both are defined once, in `atr/research/learning_evidence.py`,
and the grade is derived from the class by `grade_of`, so a row cannot carry a
grade that contradicts its class.

**Anything unrecognised, absent or ambiguous grades in-sample.** Over-claiming
independence is silent — an in-sample number wearing a forward label reads
exactly like a finding — while under-claiming it is merely conservative. The two
failure modes are not symmetric, so the default is not neutral.

### How "forward" is established, and why a timestamp cannot do it

A record is forward when it was written **before its outcome was known**. The
tempting test is the gap between an order's `created_at` and the market time it
claims, and it does not work: a backfill harness writes both, so it can write
them consistently, and no reader can tell a genuine record from a well-forged one.

So the marker is something only the live path can write. `OrderRepository.create`
— the one method that raises an order — stamps the `NEW` event's `raw` payload
with `{"provenance": "forward", "recorded_at": <wall clock>}`, and
`TradeJournalService` copies that verdict onto the episode it opens. Anything
inserted into the tables by another route has no stamp, and **absence grades
in-sample**.

### The report states facts and withholds claims

The daily report has two jobs with different evidence requirements. *What
happened* is a fact: a count, a figure and its arithmetic against the preceding
window, produced for the whole book — a book of in-sample trades still had a day.
*Whether it was good* is a claim, gated on `DailyReport.claimable`, which counts
**forward** observations only. A thousand in-sample rows do not make a claim
supportable, because they were measured on the history the rules were chosen from.

Gating the facts would go silent on a real trading day; ungating the claim would
publish a restatement of the selection history as a result. The split is the
design.

### Deliberately absent

No automatic parameter change, no automatic strategy modification, no automatic
deployment, no AI-generated trading decision. `LearningService` has no method
whose name contains `apply`, `deploy`, `optimize` or `place_order`, and a test
enumerates the class to keep it that way. The findings carry `evidence` and never
a `proposed_value`.

## The signal context engine: annotating signals, not gating them

The Context-Aware Signal Engine answers *"what was the market, sector and stock
context when this signal fired?"* for every signal the platform raises, and scores
that context deterministically. It is an annotation layer: it never blocks a
signal, changes a strategy parameter, or alters an order.

```
LIVE / PAPER   atr/services/runner.py ──► signal_context/service.py ──► engine.py
                     (after the order is placed, best-effort)              │
                                                                          ▼
BACKTEST       atr/services/backtests.py ─► signal_context/service.py   signal_contexts
                     (after the run completes, point-in-time)
                                                                          │
SIGNAL EXPLORER  /api/v1/signal-context ──────────────────────────────────┘
```

The seams that make it safe:

* **`engine.py` is pure.** It imports only market-intel models, the point-in-time
  enrichment primitives and the indicators. The environment-dependent universe
  scan (breadth and sector metrics at a past date) lives in `service.py`, so the
  engine's tests need neither a database nor a data root. Same inputs → same
  score, which is what makes a backtest comparison meaningful.
* **Point-in-time by construction.** Every stock, sector and market measurement is
  computed from frames truncated at the signal timestamp, reusing
  `research/learning_enrich._as_of`. A price move after the signal cannot change
  its score.
* **Best-effort, and never in the path of an order.** The runner records the
  context *after* `execution.place` returns; the backtest worker records contexts
  *after* the run is COMPLETED. Every entry point catches and logs — a lost
  annotation is never a failed or delayed signal.
* **The two evidence kinds are kept apart.** A backtest context resolves to an
  `in_sample` outcome; a live/paper context resolves forward only through the real
  book (the order carrying the same `signal_id`, then the journal episode inside
  its session). Statistics are suppressed until the forward sample supports them,
  exactly as the learning pipeline withholds claims.

## F. Phase 1 implementation plan

Phase 1 is **Foundation**, and the survey changes what it means: the instrument
master, market-data abstraction, database and charts largely exist. The real
Phase 1 is *identity and ownership*, because without it nothing else can be
multi-user, attributable or safe.

| # | Workstream | Deliverable | Status |
|---|---|---|---|
| 1.1 | App persistence | `atr/appdb/` — engine, schema, repositories; SQLite default, Postgres-compatible | **shipped** |
| 1.2 | Authentication | `atr/auth/` — scrypt passwords, opaque sessions, TOTP MFA, API keys | **shipped** |
| 1.3 | Authorization | RBAC roles + permission matrix, `require_permission` dependency | **shipped** |
| 1.4 | Hardening | request ID, security headers, CSRF for cookie auth, rate limiting | **shipped** |
| 1.5 | Audit | append to `appdb.audit_events`, keep writing the existing JSONL | **shipped** |
| 1.6 | Instrument master | `atr/instruments/service.py` — search, resolve, metadata | **shipped** |
| 1.7 | Watchlists | persisted watchlists: CRUD, reorder, custom columns | **shipped** |
| 1.8 | API surface | `/api/v1/{auth,watchlists,instruments}` routers | **shipped** |
| 1.9 | Frontend | auth gate + account panel + watchlist panel, nav wiring | **shipped** |
| 1.10 | Tests | auth, RBAC, rate limit, watchlists, instruments, layering | **shipped** |

Explicitly **not** in Phase 1: strategy persistence, screener persistence,
option chain, OMS, paper engine, reconciliation. Those are Phases 2–4 and are
specified in `docs/DATA_MODEL.md` / `docs/API_CONTRACT.md` so they can be built
without re-litigating the design.

### Migration safety

The auth layer ships **enabled by default but fail-open for loopback in `dev`**:
if no user exists yet and the request comes from `127.0.0.1` in `env=dev`, the
existing single-operator workflow keeps working. Any other configuration
requires a session. This is stated plainly because a security control that
silently breaks the operator's own tooling gets disabled within a day, and then
protects nothing.

## G. Immediate implementation tasks

Ordered, with the reason each is where it is:

1. **`atr/appdb`** — nothing else can store ownership without it.
2. **`atr/auth`** — the largest gap; unblocks multi-user and attribution.
3. **`atr/api/deps.py`** — one place to enforce every check.
4. **`atr/api/middleware.py`** — rate limit and headers before any new route.
5. **`atr/instruments/service.py`** — every screen, chart and watchlist needs it.
6. **`atr/api/routers/*`** — the new surface, versioned.
7. **Wire into `main.py`** — include routers; do not rewrite existing routes.
8. **Tests** — including a layering test, which is what keeps this honest.
9. **Frontend gate + watchlist panel** — make it usable, not just correct.

Then Phase 2 begins on top of a foundation that has an owner for every row.

---

## Appendix — risks and honest caveats

- **Regulatory.** This is retail-facing Indian market software. The audit trail,
  the separation of research from execution permissions, and the "no guaranteed
  returns" posture are design requirements, not decoration. Nothing here should
  be read as a compliance certification; the applicable SEBI/exchange/broker
  requirements must be confirmed against current circulars before any live use.
- **Broker-specific behaviour is not abstracted away yet.** The `Broker` ABC is
  real, but order payloads, product codes and market-protection bands are
  Indian-broker-specific. Adding a second broker will surface assumptions; that
  is the point of the adapter, and it has not been tested against a second
  adapter yet. Do not claim broker-agnosticism as proven.
- **No claim is made that any strategy here is profitable.** See
  `FINDINGS.md` — 0 of 27 pre-registered tests pass, and the one signal that
  clears its statistical bars is modest and decaying.
