# atr — API Contract

Companion to `docs/ARCHITECTURE.md`. Two parts:

- **Part 1 — shipped.** The routes that exist today (legacy, unversioned) and the
  Phase 1 additions under `/api/v1`.
- **Part 2 — specified.** The full target surface for Phases 2–4. Nothing in Part 2
  is implemented; it is written down so the shape is agreed before the code exists.

## Conventions

**Base URL.** New routes live under `/api/v1`. Existing routes stay unversioned
and keep working — the SPA depends on them and breaking them to tidy a URL is not
a trade worth making. Part 1 marks which is which.

**Auth.** Three accepted credentials, checked in this order:

| Credential | Header | Notes |
|---|---|---|
| Session cookie | `atr_session=<token>` | HttpOnly, SameSite=Lax, `Secure` when `env=live`. Subject to CSRF checks on unsafe methods. |
| Bearer token | `Authorization: Bearer <token>` | Same session token, for non-browser clients. No CSRF check (no ambient authority). |
| API key | `X-API-Key: atr_<prefix>_<secret>` | Scoped to the key's own permission set. |

**Errors.** RFC-shaped and consistent:

```json
{ "detail": "human-readable message", "code": "machine_code", "request_id": "a1b2c3d4" }
```

`code` is stable and intended for client branching; `detail` is for humans and may
change wording.

| Status | When |
|---|---|
| 400 | malformed request, or a dangerous action missing its `reason` |
| 401 | no credential, or an expired/revoked one |
| 403 | authenticated but the role or key scope lacks the permission |
| 404 | not found **or not owned by the caller** — deliberately indistinguishable |
| 409 | conflict (duplicate name, duplicate symbol in a watchlist) |
| 422 | schema validation failure (FastAPI default) |
| 429 | rate limited; `Retry-After` is set |

**The 404-not-403 rule is intentional.** Returning 403 for someone else's resource
confirms that the resource exists. Every ownership failure returns 404.

**Every response carries `X-Request-ID`**, echoed from the request if the client
supplied one, otherwise generated. The same ID appears in the log line and in any
`audit_events` row the request wrote.

**Dangerous actions.** A route that can move money, stop a strategy, or change the
execution environment requires a non-empty `reason` in the body. Missing it is a
400, and the reason is written to the audit log. This mirrors the existing
`POST /risk/execution-mode` behaviour and extends the same rule rather than
inventing a second one.

**Timestamps** are ISO-8601 UTC with a `Z` suffix. Money is JSON numbers in rupees.
Symbols are the **bare ticker** (`RELIANCE`) in every request and response; the
`SYMBOL-EQ` spelling is an internal detail of the instrument master.

---

# Part 1 — Shipped

## 1.1 Existing routes (unversioned)

These predate Phase 1. Their **paths and response shapes are unchanged**; three
of them were tightened on 2026-09-14, and the tightenings are listed below rather
than buried, because they are behaviour changes an existing client must know about.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | env, database, broker session, execution mode, kill switch |
| POST | `/backtest` | run one backtest configuration |
| GET | `/positions` | broker positions |
| POST | `/orders` | place an order — **now requires an account**; goes through the OMS |
| POST | `/risk/kill-switch` | trip/reset the kill switch — **now requires a reason** |
| GET/POST | `/risk/execution-mode` | read / change paper↔live (POST needs a reason) |
| GET | `/risk/status` | current limits and state |
| GET | `/self-learning/status`, POST `/self-learning/train` | adaptive layer |
| GET/PUT | `/trade-signals/settings` | semi-auto queue settings |
| GET | `/trade-signals` | pending signals |
| POST | `/trade-signals/scan` | rescan |
| POST | `/trade-signals/{id}/execute` \| `/skip` | act on a signal — **now requires an account**, and is idempotent per leg |
| GET/POST/PATCH/DELETE | `/alerts/rules` | alert rules |
| GET | `/alerts/events` | alert history |
| POST | `/alerts/check`, `/alerts/test` | evaluate / test |
| GET/PUT | `/alerts/intelligent/config` | intelligent monitor config |
| POST | `/alerts/intelligent/evaluate` | evaluate now |
| GET/PUT | `/briefing/config`, POST `/briefing/preview`, `/briefing/send` | morning brief |
| GET | `/audit` | audit trail, newest first |
| GET | `/scan`, `/scan-all` | momentum scans |
| POST | `/scanner/custom` | indicator-condition DSL scan |
| GET/PUT/DELETE | `/scanner/saved` | saved scans |
| GET | `/candles` | OHLCV for charting |
| GET | `/symbols` | symbol list |
| GET | `/strategies` | strategy registry |
| POST | `/research` | walk-forward run |
| GET | `/validation`, `/validation/episodic-pivot`, `/validation/alpha-hunt` | measured baselines |
| GET | `/evidence` | honesty layer |
| GET | `/dashboard/summary` | aggregated dashboard payload |
| GET | `/portfolio` | holdings + positions + P&L |
| GET | `/quote` | live quotes |
| GET | `/history/status`, `/instruments/status` | cache/master status |
| GET | `/login/status`, POST `/login`, GET `/login/callback` | IIFL broker OAuth |
| WS | `/ws/ticks` | live tick stream |
| GET | `/ticks/history`, `/ticks/vwap`, `/ticks/candles` | tick-derived series |

### The three tightenings (2026-09-14)

**`POST /orders` and `POST /trade-signals/{id}/execute` require an account.** They
used to be reachable without one, guarded only by loopback and `ENV != dev`, and
they called the broker directly with no risk check and no event log. An order needs
an owner — `orders.user_id` is a non-null foreign key — and an unattributed order
is one nobody can be asked about. Both now route through
`atr.services.execution.ExecutionService`, so the risk gate and the event log apply.

```json
// 401 when no principal is present
{ "detail": { "detail": "placing an order requires a platform account — sign in first. Read-only routes remain available without one.",
              "code": "authentication_required_for_orders" } }
```

Read-only legacy routes are untouched: the dashboard still renders without signing
in. `POST /trade-signals/{id}/execute` is also now **idempotent per leg** — each leg
carries a key derived from `(signal_id, leg_index)`, so a double click returns the
existing orders instead of placing a second bracket. A leg that fails returns `502`
with `code: "venue_failed"` and a `placed` map of the legs that did succeed, so a
half-placed bracket is a described state rather than a mystery.

**`POST /risk/kill-switch` requires a `reason`.** Both directions. Releasing the
switch re-enables trading, so a release with no recorded reason is indistinguishable
from someone clearing it by accident. `400` with `code: "reason_required"` otherwise.

The state itself is now **durable** (`system_state`): it was an in-process dict, so a
restart silently re-armed trading and dropped the platform back to paper.

## 1.2 Phase 1 additions — `/api/v1`

### Authentication — `/api/v1/auth`

| Method | Path | Permission | Body / notes |
|---|---|---|---|
| GET | `/bootstrap` | public | `{ needs_setup: bool }` — true when zero users exist. Drives the first-run screen. |
| POST | `/bootstrap` | public, **only while zero users exist** | Creates the first user as `owner`. Returns a session. 409 once any user exists. |
| POST | `/register` | public, **only when `atr_allow_signup` is on** | Creates a `viewer`. Off by default — a trading platform should not accept strangers. |
| POST | `/login` | public | `{ identifier, password, totp_code? }` → `{ token, expires_at, user }`. If the user has MFA on and no code is supplied, returns `202` with `{ mfa_required: true }` and no token. |
| POST | `/logout` | authenticated | Revokes the current session. |
| POST | `/logout-all` | authenticated | Revokes every session for the user. |
| GET | `/me` | authenticated | The caller's profile, role and effective permissions. |
| PATCH | `/me` | authenticated | Change display name, email, password (requires current password). |
| POST | `/me/mfa/enroll` | authenticated | Returns `{ secret, otpauth_uri }`. Does **not** enable MFA. |
| POST | `/me/mfa/activate` | authenticated | `{ code }` — verifies and enables. |
| POST | `/me/mfa/disable` | authenticated | `{ password }` — requires the password, not just a session. |
| GET | `/me/sessions` | authenticated | Active sessions with IP and last-seen, so a user can spot one they did not create. |
| DELETE | `/me/sessions/{id}` | authenticated | Revoke one. |
| GET/POST | `/me/api-keys` | `apikey:manage` | List (prefixes only) / create. The secret is returned **once**. |
| DELETE | `/me/api-keys/{id}` | `apikey:manage` | Revoke. |
| GET | `/users` | `user:read` | Admin: list users. |
| PATCH | `/users/{id}` | `user:write` | Admin: change role, activate/deactivate. Refuses to demote the last owner. |

Why `POST /bootstrap` is public: the first run has no user to authenticate as, so
the alternative is a CLI-only setup or a default password — both worse. It is
guarded by "zero users exist", checked inside the same transaction as the insert,
so two concurrent bootstraps cannot both win.

Why `POST /login` returns `202` rather than `401` for an MFA challenge: the
password was correct, so it is not an authentication failure. A distinct status
lets the client show the code field instead of an error.

### Watchlists — `/api/v1/watchlists`

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/` | `watchlist:read` | All watchlists for the caller, with item counts and column config. |
| POST | `/` | `watchlist:write` | `{ name, exchange?, columns? }`. 409 on duplicate name. |
| GET | `/{id}` | `watchlist:read` | One watchlist with items in order. 404 if not owned. |
| PATCH | `/{id}` | `watchlist:write` | Rename, change exchange, set default. |
| DELETE | `/{id}` | `watchlist:write` | Deletes items and columns with it. |
| POST | `/{id}/items` | `watchlist:write` | `{ symbol }` or `{ symbols: [...] }`. Validates against the instrument master. 409 on duplicate. |
| DELETE | `/{id}/items/{symbol}` | `watchlist:write` | |
| PUT | `/{id}/items/order` | `watchlist:write` | `{ symbols: [...] }` — the complete new order. |
| PUT | `/{id}/columns` | `watchlist:write` | The complete column config. Unknown keys rejected with the valid list. |
| GET | `/columns/available` | `watchlist:read` | The column registry: key, label, group, whether it needs live data. |
| GET | `/{id}/quotes` | `market:read` | Live rows for the watchlist — every column resolved. |

`PUT .../items/order` takes the **whole list** rather than a move operation
(`{symbol, new_index}`). A full-list PUT is idempotent, so a retry after a dropped
response cannot reorder twice; a relative move cannot make that promise.

`GET /{id}/quotes` returns a `columns` array alongside `rows` so the table renders
from the response alone and a client that does not know a column can still show it.

### Instruments — `/api/v1/instruments`

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/search` | `instrument:read` | `?q=&exchange=&asset_class=&limit=` — ranked prefix-then-substring match. |
| GET | `/{symbol}` | `instrument:read` | Canonical metadata: exchange, asset class, lot size, tick size, expiry/strike for derivatives. |
| GET | `/exchanges` | `instrument:read` | Available exchanges with symbol counts. |
| GET | `/status` | `instrument:read` | Master provenance: cache dir, symbol count, newest bar date. |
| POST | `/refresh` | `instrument:read` | Force a master rebuild from the parquet cache. |

`GET /search` accepts `RELIANCE`, `reliance`, `RELIANCE-EQ` and `NIFTY 50` and
returns the same canonical record — canonicalisation is the service's job, not
every caller's.

### Audit — `/api/v1/audit`

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/events` | `audit:read` | `?since=&until=&action=&user_id=&result=&limit=&offset=`. Paginated. |
| GET | `/events/{id}` | `audit:read` | One event with its full detail JSON. |
| GET | `/actions` | `audit:read` | The distinct action names, for building a filter UI. |

### Orders — `/api/v1/orders` (shipped 2026-09-14)

Every route goes through `atr.services.orders.OrderService`, so every order in
this surface is risk-checked, logged as an append-only event sequence, and
idempotent when asked. `orders.status` is a **projection** of the newest event;
`GET /{id}/history` is the authoritative record.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/state-machine` | — | The states and legal transitions, served so the client cannot offer a button the server will refuse. |
| POST | `/` | `order:place` | Create at `NEW`. Nothing is transmitted and no risk decision is taken yet. `201`. |
| GET | `/` | `order:read` | `?status=&symbol=&mode=&deployment_id=&correlation_id=&limit=&offset=`. Paginated. |
| GET | `/open` | `order:read` | Orders not in a terminal state — what reconciliation watches. |
| GET | `/{id}` | `order:read` | One order. Another account's id is a `404`, never a `403`. |
| GET | `/{id}/history` | `order:read` | Every transition, oldest first. The lifecycle, reconstructable. |
| GET | `/{id}/fills` | `order:read` | Only the execution events, with their slippage and latency. |
| POST | `/{id}/validate` | `order:place` | Take the risk decision: `NEW` → `RISK_APPROVED` or `REJECTED`. |
| POST | `/{id}/cancel` | `order:cancel` | `{ reason }` (required, non-empty). → `CANCEL_PENDING`. |
| POST | `/{id}/cancel/confirm` | `order:cancel` | The broker confirmed. `CANCEL_PENDING` → `CANCELLED`. |

**Idempotency.** An `Idempotency-Key` header makes a retry return the order that
already owns the key, with `created: false` and a `201`. When no header is sent, a
key is derived **only for a signal-driven order** — one naming a `signal_id` or
`strategy_id`. A manual order gets no key: two clicks are two orders, and silently
collapsing them would be an invisible bug.

**Error contract.** A refused transition is a `409` with the two states named:

```json
{ "detail": { "detail": "illegal order transition RISK_APPROVED -> VALIDATING (order …)",
              "code": "invalid_transition",
              "from_status": "RISK_APPROVED",
              "to_status": "VALIDATING" } }
```

A rejection by risk is **not** an error: `POST /{id}/validate` returns `200` with
`{"status": "REJECTED", "allowed": false, "reject_reason": "…"}`. The request
succeeded; the answer is that the order may not proceed.

Not here yet, deliberately: **submission to a broker.** Creating and validating an
order is safe and testable on its own; transmitting belongs to the execution
service, where the paper/live split lives.

### Reconciliation — `/api/v1/reconciliation` (shipped 2026-09-14)

The control that compares the platform's records against the broker's. Reads need
`risk:read`; running needs `risk:configure`, because a run writes a run row and an
audit row.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/status` | `risk:read` | The banner source. `{clear, run}`. |
| GET | `/scopes` | `risk:read` | Which scopes can be run. |
| GET | `/runs` | `risk:read` | `?severity=&limit=`. History. |
| GET | `/runs/latest` | `risk:read` | `404` when no run exists yet. |
| POST | `/run` | `risk:configure` | `{ scopes?, notify? }`. Compares now. |

**Severity is asymmetric, and that is the design.** An order the broker holds and
the platform does not know about is `critical` — something is live that nothing is
managing. A position that disagrees is `critical`, because every risk limit is
measured against it. A *holding* that disagrees is a `warning`: on a T+1 market a
holding and a position legitimately differ for a day after every trade, and calling
that critical trains an operator to ignore the alarm.

**A scope that could not be compared is reported, never skipped.**

```json
{ "severity": "warning", "mismatches": 4, "differences": 0, "critical": 0,
  "error": "no active IIFL session — run `atr login`",
  "incomparable": [ { "scope": "orders", "reason": "the run failed before this scope was reached" } ] }
```

`mismatches` is everything that stops the run being `ok` — discrepancies **and**
unchecked scopes — and it is the same number the stored row holds, so the response
and the row cannot disagree. `differences` is the strict count of actual
disagreements. A run that reported `ok` while quietly skipping half its scopes is
the failure this control exists to prevent.

**A run that cannot reach the broker is not an error response.** It is recorded with
a severity that is not `ok` and an `error` naming the cause, because reconciliation
that throws is reconciliation that stops running — and this is the control that has
to keep running. `funds` is `incomparable` on the live path because the platform
keeps no cash ledger for a live account; that absence is surfaced rather than
defaulted away.

A `critical` run writes an audit event with `result: "failure"` and pushes to the
configured notification channels (unless `notify: false`).

### Paper trading — `/api/v1/paper` (shipped 2026-09-14)

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/deployments` | `algo:start` | `{ strategy_id, strategy_version, capital, mode, config }`. Allocates capital to a strategy version. |
| GET | `/deployments` | `order:read` | Each row carries its own `pnl`. |
| GET | `/deployments/{id}` | `order:read` | Another account's id is `404`. |
| POST | `/deployments/{id}/start` | `algo:start` | Start or resume. A **stopped** deployment is terminal. |
| POST | `/deployments/{id}/pause` | `algo:stop` | `{ reason }` required. |
| POST | `/deployments/{id}/stop` | `algo:stop` | `{ reason }` required. |
| POST | `/deployments/{id}/reset` | `algo:start` | `{ reason }` required. Returns a **new** deployment. |
| GET | `/deployments/{id}/positions` | `order:read` | Folded from `order_events`. |
| POST | `/deployments/{id}/positions` | `order:read` | Same, with explicit `{ prices }` marks. |
| GET | `/deployments/{id}/pnl` | `order:read` | `?marks=false` skips the cache. |
| GET | `/deployments/{id}/orders` | `order:read` | Scoped to this deployment. |
| POST | `/deployments/{id}/orders` | `order:place` | **Place a paper order.** Fills via the paper venue. |
| GET | `/account` | `order:read` | The whole paper account, across deployments. |

**The paper engine is the same pipeline as live with a different venue** —
`ExecutionService` → `OrderService` → the OMS, with `PaperVenue` instead of the
broker. Same lifecycle events, same risk gate, same rule layer. There is no second
strategy implementation.

**`reset` returns a new deployment, it does not delete anything.** `order_events` is
append-only, so a paper account's history cannot be erased; the response's
`reset_from` names the stopped deployment, whose orders and P&L stay readable.

**Two honesty fields on every position/P&L response.**

```json
{ "complete": false,
  "unpriced_symbols": ["RELIANCE"],
  "positions": [ { "symbol": "RELIANCE", "quantity": 10, "avg_price": 2500.0,
                   "last_price": null, "market_value": null,
                   "unrealized_pnl": null, "priced": false } ] }
```

A position with no mark is **shown**, because the position is real; only its
valuation is `null`, because that is unknown. Omitting the row would say "you hold
nothing" and reporting it at zero would show a fabricated loss of the entire cost
basis. The totals cover the priced positions and `complete` says whether that was
all of them.

The paper venue **rejects an order it cannot price** rather than filling at zero,
and a limit or stop that the market has not reached **rests** rather than filling —
filling a buy limit above its own limit is the classic paper-trading lie.

### Risk — `/api/v1/risk` (shipped 2026-09-14)

Reads need `risk:read`; every write needs `risk:configure` (admin and above).

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/state` | `risk:read` | Kill switch, execution mode, limits. |
| GET | `/limits/schema` | `risk:read` | Which limits are configurable. |
| POST | `/kill-switch` | `risk:configure` | `{ engaged, reason }`. **Reason required in both directions.** |
| POST | `/execution-mode` | `risk:configure` | `{ mode: PAPER\|LIVE, reason }`. Reason required for `LIVE` only. |
| PUT | `/limits` | `risk:configure` | Wholesale replacement. Unknown fields are `422`, not ignored. |

State is **durable** (`system_state`), so a restart cannot re-arm trading. The kill
switch is enforced through the OMS risk gate, which reads this state, so it applies
to every order path rather than to the routes that remembered to ask.

`limits` reports `null` for "no limit" rather than `Infinity`, which is not valid
JSON and which a JavaScript client cannot parse.

---

# Part 2 — Specified (Phases 2–4)

Shapes are fixed here so the frontend can be built against them; none of it is
implemented.

## Strategies — `/api/v1/strategies` (Phase 2/4)

```
GET    /                       list strategies (own + shared)
POST   /                       create  { name, description, kind }
GET    /{id}                   metadata + latest version
PATCH  /{id}                   rename / describe / archive
GET    /{id}/versions          version list with metrics summary
POST   /{id}/versions          create a NEW version (never mutates an old one)
GET    /{id}/versions/{v}      the definition
GET    /{id}/compare?from=&to= side-by-side metric comparison
POST   /{id}/validate          structural validation without saving
POST   /{id}/from-nl           natural language → rules (AI, returns a draft)
```

## Screener — `/api/v1/screener` (Phase 2)

**Shipped 2026-09-14.** Implemented surface:

```
GET    /universes              available universes and their sizes
GET    /indicators              the indicator catalog (incl. unavailable ones)
GET    /columns                 display-column registry + permitted sort keys
POST   /run                    { universe, conditions, sort, limit } → ranked rows
POST   /validate               validate a condition tree without running it
GET    /saved                  saved scans
POST   /saved                  save a scan
GET    /saved/{id}              one saved scan
PUT    /saved/{id}              edit a saved scan
DELETE /saved/{id}
GET    /saved/{id}/results     run a saved scan (live, see note)
```

**Not implemented, deliberately:**

```
POST   /saved/{id}/schedule    needs a real scheduler (missed windows, overlap,
                               backfill after downtime) — not a stub endpoint
WS     /stream                 live results as events fire
```

`GET /saved/{id}/results` runs the scan live rather than serving a cached last
result. There is no result store behind it, and returning a stale result labelled
"last results" would be worse than returning a fresh one.

Condition trees are owned by `atr.screener.conditions`. A leaf is
`{indicator, op, value|rhs_indicator, period?, rhs_period?, upper?}`; a group is
`{match: "all"|"any", conditions: [...]}` and nests to 8 levels. The operator set
is exactly `atr.screener.OPERATORS`.

Every returned row carries `why` (evidence for the leaves that passed) and
`evidence` (all leaves, including failures), each with the measured value, the
target, and a sentence. An indicator that could not be computed sets
`unmeasurable: true` — distinct from `passed: false`.

## Backtest — `/api/v1/backtests` (Phase 2)

```
POST   /                       submit a run → { run_id, status }
GET    /{run_id}               status + progress
GET    /{run_id}/metrics       the full metric set
GET    /{run_id}/equity        equity + drawdown curves
GET    /{run_id}/trades        paginated trade list
GET    /{run_id}/monthly       monthly/yearly return matrix
GET    /{run_id}/exposure      exposure over time
POST   /{run_id}/monte-carlo   { paths, method } → distributions
POST   /{run_id}/walk-forward  { train, test, folds } → folds + aggregate
GET    /                       run history
```

Runs are **asynchronous** and return a `run_id` immediately. A walk-forward over a
decade of daily bars is a minutes-long job, and holding an HTTP connection open
for it is how you get a dead dashboard.

## Paper trading — `/api/v1/paper`

**Shipped 2026-09-14 — see §1.2 above for the implemented surface.**

## Live algos — `/api/v1/algos` (Phase 3)

```
GET    /                       live deployments with status + P&L
POST   /{id}/start             requires reason
POST   /{id}/pause             requires reason
POST   /{id}/stop              requires reason
POST   /{id}/square-off        requires reason + explicit confirm flag
GET    /{id}/signals           signal history
GET    /{id}/execution-log     the latency-trace view
```

## Orders / positions / portfolio (Phase 3)

```
GET    /orders                 ?status=&strategy=&since=
GET    /orders/{id}            full lifecycle with every timestamp
DELETE /orders/{id}            cancel
GET    /positions
DELETE /positions/{symbol}     square off one
POST   /positions/square-off-all   requires reason + confirm flag
GET    /portfolio              holdings, positions, margin, exposure
GET    /portfolio/attribution  by strategy | symbol | sector | asset class
```

## Options (Phase 4)

```
GET    /options/chain          ?underlying=&expiry=&strikes=
GET    /options/expiries       ?underlying=
POST   /options/strategies     build a multi-leg strategy → payoff + greeks + margin
GET    /options/templates      the named templates
POST   /options/backtest       options-specific backtest config
```

## Risk, execution control, reconciliation

**Shipped 2026-09-14 — see `/api/v1/risk` and `/api/v1/reconciliation` in Part 1 for the implemented surfaces.**

## Alerts, journal, calendar, analytics (Phase 3/4)

```
GET/POST/DELETE /alerts/rules
GET    /alerts/events
GET    /journal                ?strategy=&from=&to=
PATCH  /journal/{trade_id}     attach a note
GET    /calendar/holidays      ?exchange=&year=
GET    /calendar/expiries      ?underlying=
GET    /analytics/pnl          ?group_by=strategy|symbol|sector|asset_class
GET    /analytics/regimes      regime classification over time
```

## Developer platform (Phase 5)

```
POST   /apikeys                create (returns secret once)
GET    /apikeys                list prefixes
DELETE /apikeys/{id}
WS     /ws/market              authenticated market stream
WS     /ws/orders              authenticated order stream
POST   /webhooks               register a webhook endpoint
GET    /openapi.json           generated — kept accurate by construction
```

**WebSocket authentication.** A browser `WebSocket` cannot set an
`Authorization` header, and putting a long-lived token in a query string leaks it
into access logs. So: the client sends a first frame
`{"type":"auth","token":"…"}` and the server closes the socket with code 4401 if
that frame does not arrive within 5 seconds or fails validation. Every subsequent
frame is authorised against the permission set established at handshake.
