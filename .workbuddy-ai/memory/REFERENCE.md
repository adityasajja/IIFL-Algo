# atr — operational reference

Companion to `MEMORY.md`. UI, broker, build, sandbox and engine-level traps.
Read this before touching the dashboard, the broker client or the backtest engine.

## Engine & funding traps

- **`_can_fund` funds each order against total equity in isolation** — N concurrent
  positions are each affordable, so the book reaches **N × equity**. EP hit **3.49×
  gross** with no error and no log line; the only symptom was a "daily loss" of
  750k on a 1M account. The strategy must cap `max_gross_weight`/`max_positions`
  itself; `RiskLimits.max_gross_exposure` cannot (exceeding it *kills* the run).
- **`reset_daily()` must reseed `day_start_equity`** from the **rollover**, not from
  inside `check` — the engine marks *then* checks, so a baseline seeded there
  compares today's equity to itself and the limit can never fire on daily bars.
  Pinned to the first bar, `max_daily_loss` becomes "cumulative DD since inception":
  trips once, permanently, and reports a loss that never happened.
- **`Position.mark()` must reject a missing price.** The feeds write NaN for a
  symbol with no bar on a step; assigning it made `market_value` → `equity` → the
  whole curve NaN, surfacing far away as `int(nan)` in an unrelated symbol's sizing
  (killed 3 of 6 control runs). Keep the last known price — the same convention
  `buy_and_hold_equity` uses, or the comparison is between two conventions.
- **One resolver per polymorphic payload.** `/dashboard/summary` read
  `quantity`/`last_price` but `IiflClient.positions()` returns camelCase
  (`netQuantity`/`averagePrice`/`ltp`) — three real positions summed to `count: 3,
  value: 0.0`. Use `_pick()` (copies in `main.py` and `brokers/iifl/broker.py`).
- **`logging` uses `%`-style.** `logger.warning("a {} b {}", x, y)` raises
  `TypeError` at *emit* time — the call site looks fine and only breaks when it
  fires. Use `%s`. A missing `logger` binding is the same trap one step earlier.
- **`Row` does not support `in`.** `"close" in row` is always `False` (`Row`
  subclasses tuple, overrides only `get`/`__getitem__`, so `in` tests *values*).
  Silently skipped every paper signal and looked like "no edge". Use `row.get()`.
- **`NaN or fallback` returns NaN.** NaN is truthy, so `pos.last_price or
  pos.avg_price` propagates it, and `NaN <= 0` is False so a plain `<= 0` guard
  lets it through. Guard with `np.isfinite` — see `_finite()` in `episodic_pivot.py`.
- **Stacked frames have duplicate index labels.** `stack.loc[idxmax()]` returns
  *every* symbol's row at that position and `.iloc[0]` reports whichever sits
  first — a wrong answer that looks like a measurement. Use positional
  `np.nanargmax` on `.to_numpy()`.
- **A "7-day" series must be bucketed by day, not sliced by row.** `realised[:7]`
  over a newest-first trade list draws a line through seven fills in one afternoon
  and calls it a week.
- **Reporting an unknown as a zero.** IIFL rows carry no prior close, so `day_pnl`
  was ~0 for a book that may be moving hard. `_prior_closes()` fills it from the
  daily cache; `day_pnl_complete`/`day_pnl_from_cache` record the outcome. An
  *empty* book is `complete: True` — hence `counted == 0 or no_prev == 0`.
- **Bound a derived baseline.** `_prior_closes` refuses a cache session >4 days
  old; the loop rejects a prior close >35% from the live price. An invented `ltp`
  produced "+52.99% in one day". Degrade the field, keep `value` correct.
- **NaN-padded frames.** `_build_frames` NaN-pads any symbol without a bar at a
  step; one symbol per snapshot leaves each ~99% NaN, so `ctx.history(sym)` returns
  nothing and strategies never fire. Put **every symbol on every bar**.
  `ctx.history(sym)` is per-symbol but advances per-snapshot — a sparse grid
  handicaps it twice.

## Broker facts (verified live — external systems don't drift)

- **The IP whitelist is a SEBI mandate, not IIFL policy.** Algo framework fully
  mandatory since 2026-04-01. Set as "Primary Static IP" at
  developers.iiflcapital.com (My Apps → ⋮ → View All Details). One primary + one
  backup. Not waivable via support.
- **Egress MUST be IPv4.** `api.iiflcapital.com` publishes A and AAAA; this host is
  dual-stack so httpx silently picked IPv6 — every gated endpoint returned `EC500
  IP not authorized` even with IPv4 whitelisted. `IiflClient` pins via
  `local_address="0.0.0.0"` (env `IIFL_FORCE_IPV4`). Diagnose with multi-service
  checks (`api64.ipify.org`); `api.ipify.org` is IPv4-only and hid this.
- Redirect: `/login/callback?authcode=...&clientid=...`; handler accepts all three
  spellings. 422 = the auth code was never consumed — may still be live.
- IIFL nests failures in a success envelope (outer `status` always "Ok"). Walk into
  `result` — `_broker_error` does. `EC926 No Trade's are found` = empty.
- Session: `atr login --client-id <ID> --auth-code <CODE>`; JWT dies at midnight
  IST; cached `.cache/iifl_session.json`.
- SEBI: API market orders need non-zero `marketProtectionPercent` (default 0.5%).
- NSEEQ symbols are `SYMBOL-EQ` in the master; cache filenames and every other
  layer use the bare ticker. Try both spellings.
- developers.iiflcapital.com is Akamai-blocked, JS-SPA — WebFetch cannot read it.
- Constituents: `https://niftyindices.com/IndexConstituent/ind_<name>.csv` fetches
  cleanly with a browser UA (nifty50 / niftynext50 / niftymidcap150 /
  niftysmallcap250 → `data/universe/`).

## Repo, sandbox & build quirks

- Remote github.com/adityasajja/IIFL-Algo (private). Commits authored as
  `WorkBuddy <workbuddy@local>` per-command; no global git identity.
- **Anchor every .gitignore pattern.** `data/` (no slash) silently excluded the
  whole `src/atr/data/` layer. Verify with `git check-ignore -v <path>`.
- Sandbox: `text=True` corrupts git plumbing (pass bytes to
  `mktree`/`hash-object`); writes to `.git/refs/remotes/` silently drop and
  `update-ref` reports success (write ref files directly); probe local servers with
  Python `urllib`, not curl; dead ports return HTTPError 502 — use raw
  `socket.connect`.
- **`atr serve` builds the SPA once at startup.** After frontend changes run
  `bun run build` in `web/`; the backend picks it up immediately.
- **Lucide icon props:** use `React.ElementType` or `typeof Foo`, not
  `ComponentType<{size?: number}>` — lucide's `size` accepts `string | number |
  null` and `bun run build` fails TS2322 otherwise.
- **`Edit` can report success without changing the file.** Fall back to a Python
  one-liner for in-place substitutions.
- **`pkill -f uvicorn` does not reliably kill the server here.** A stale process
  kept serving *old code* while a fix was "verified". Kill by PID from
  `Get-NetTCPConnection -LocalPort 8000 -State Listen`.
- **Headless verification without agent-browser** (it does not support Windows).
  Edge: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`, flags
  `--headless=new --disable-gpu --virtual-time-budget=9000 --screenshot=<abs> <url>`.
  Console errors: CDP over `--remote-debugging-port` with `websockets`. Gotchas:
  `document.querySelector('main')` returns the sidebar inset wrapper — use the
  *last* `<main>`; `--user-data-dir` is required or the profile locks; **exactly
  one reader loop per socket** (a `cmd()` looping until its own `id` swallows every
  `Fetch.requestPaused` — route by `id` with a dispatcher); wait for the backend
  before opening the URL or Edge renders its own error page which survives
  `Page.reload`. Helpers: `scripts/probe_*_cdp.py`.
- **The dev server is reaped at turn end**, so "launch + probe" must share one turn.
  Launch `python -m uvicorn atr.api.main:app --host 127.0.0.1 --port 8000` — there
  is no `__main__` block, so `python -m atr.api.main` exits immediately.
- **Verifying a state the account cannot produce.** The book is empty, so the
  populated KPI path never runs against live data. Drive it with a CDP
  `Fetch.fulfillRequest` fixture (`scripts/probe_populated_summary.py`); only the
  *inputs* are synthetic, every formatting line is production.

## Dashboard (web/)

`atr serve` = SPA + API on one port; `atr dev` = FastAPI + Vite. React 19 + Vite 5
+ Tailwind 4, motion components from beui.dev.

- **Navigation**: 8 sections in 5 sidebar groups — Observe (Dashboard, Markets),
  Decide (Strategies, Signals), Act (Trading), Learn (Evidence), Maintain (Risk,
  System). Hash `#tab/subtab`. **Sub-tab state lives in `App()`**, never in a panel.
  `LEGACY_TABS` **and** `LEGACY_SUB` both need the old ids — mapping only the tab
  lands on the right section showing the wrong panel. Add a route → add it to
  `VALID_TABS` and both maps. `openChart(symbol)` is the single symbol → chart entry.
- **shadcn/beui**: `web/components.json`, `@/*` aliased in both `tsconfig.json` and
  `vite.config.ts`. Add with `bunx --bun shadcn add @beui/<name> --yes --overwrite`.
- **`GET /dashboard/summary`** aggregates positions + performance + breadth.
  Sections independently try/excepted so one dead endpoint cannot blank the page.
  `_SUMMARY_TTL = 20s`. **Cache check goes BEFORE `load_cached`** — reading 2,672
  parquets costs ~6s (15.3s/request before the fix, 0.33s warm). Breadth:
  `_BREADTH_TTL = 900s`, `_BREADTH_SAMPLE = 600`, precomputed on a daemon thread.
- **`_frame_last_date(df)` reads the `ts` column, not the index.** `load_cached`
  returns a RangeIndex, so `.index[-1].date()` raises — and a broad `except` turned
  that into `""`, i.e. failed as a plausible result.
- **The environment banner is the single source of truth for live-vs-paper**
  (`components/ui/environment-banner.tsx`, from `/health`). Live needs both
  `env === "live"` and `execution_mode === "live"`; the kill switch outranks
  everything; an unreachable backend renders "ENVIRONMENT UNKNOWN". Never
  re-introduce a buried environment row elsewhere.
- **Timestamps render through `<RelativeTime>`** — "4 days ago · 09-09 09:08",
  tinted fresh/aging/stale. Show both; never make the reader subtract.
- **`NumberTicker` renders each digit as a rolling column**, so a headless
  screenshot catches every digit stacked (`40123456789`). Plain `String(n)` for
  discrete counts.
- **`StatRow` has a `trailing` slot**; keep `min-w-0`/`shrink-0` on the value and
  trailing wrappers or a long value collides with the sparkline.
- `Sparkline` is not a chart — no axes, labels or tooltip; "no data" under 2 points.
- `PortfolioPanel.tsx` is the canonical redesigned panel (hero KPI strip, semantic
  grouping, card-list holdings, status-badged tables).

## Execution mode: paper vs live

Server-side gate, **not** a disabled button. `_STATE["execution_mode"]` defaults
`"paper"`. `_require_live_execution()` gates `POST /orders` and
`POST /trade-signals/{id}/execute`; read-only endpoints deliberately skip it.
`POST /risk/execution-mode` **requires a reason to go live** (400 without) and
appends to `data/audit/audit.jsonl`; `GET /audit` reads it newest-first. 13 tests in
`tests/test_execution_mode.py`.

Rule: an audit trail must survive a restart, and a gate must be enforced where the
money is — not in the component that draws the toggle.

## Orders, risk and the appdb transaction trap

- **pysqlite commits a released `SAVEPOINT`.** `AppDatabase` now sets
  `dbapi_connection.isolation_level = None` on connect and emits an explicit
  `BEGIN` from an `engine.begin` listener. Without it, releasing the *outermost*
  savepoint commits immediately: a write inside it survives the caller's later
  `rollback()` while later statements are undone. Symptom was an order at `NEW`
  with a committed `VALIDATING` event. Regression test:
  `tests/test_appdb_engine.py`. **If you add a savepoint anywhere, that test is
  the reason it works.**
- **`orders.status` is a projection.** Only `OrderEventRepository.append` writes
  it, in the same transaction as the event. There is no `OrderRepository.update()`
  — do not add one.
- **The order machine lives in `atr/execution/oms.py` (pure, no `appdb`); the
  transaction-owning service is `atr/services/orders.py`.** `execution` is a
  compute layer and `tests/test_architecture.py` fails the build if it imports
  `appdb`. Do not merge the two.
- **Never build a broker order at a call site.** Orders are assembled only in
  `atr/services/execution.py`; the venue is a parameter. Three hand-rolled
  constructions existed and two were wrong — one used `OrderType.SL_MARKET`, which
  does not exist (the route placed its *entry* leg and then raised, leaving a live
  position with no stop), and the same leg put the stop trigger in `limit_price`
  and a `broker_params["triggerPrice"]` that `IiflBroker` never reads. The trigger
  is `order.stop_price`. `tests/test_legacy_order_routes.py` greps the AST to stop
  a new call site reappearing.
- **Order types go through `atr.core.enums.OrderType.parse`.** It accepts `SL-M`,
  `SL`, `stop_market` and refuses anything else *before* transmission. `SL-M` is a
  stop-**market** (`STOP`); `SL` is a stop-**limit** (`STOP_LIMIT`). Conflating
  them attaches or drops a limit price silently.
- **A duplicate intent is never resubmitted**, and a venue call that raises leaves
  the order `SUBMITTED` (the outcome is unknown — do not write a fill or a
  rejection there). A fill with no price is refused rather than recorded at zero.
- **`RiskEngine` prices an order at zero when it cannot be priced.** `check_order`
  uses `limit_price or last_price`; with neither, notional is `0.0` and every
  notional limit silently passes. `LimitsRiskGate` refuses instead. Also
  `RiskLimits.max_position_notional` defaults to `float("inf")`, which is truthy.
- **Never derive an idempotency key for a manual order.** With no `signal_id` and
  no `strategy_id` the hash is still stable, so two clicks collapse into one order.
- **The paper engine is a `Venue`, not a parallel system.** `atr/services/paper.py`:
  `PaperVenue` matches (limit/stop **rest** until reached; an unpriceable order is
  rejected, not filled at zero; costs default to `IndianDeliveryCosts`) and
  `PaperLedger` folds `order_events` into `atr.backtest.portfolio.Portfolio`. There
  is deliberately **no paper state table** — the log is the state. Do not add one.
- **`Portfolio` leaves an unmarked position at `last_price = 0`**, so
  `unrealized_pnl` becomes `(0 - avg) * qty` — a fabricated loss of the whole cost
  basis. `PaperLedger.snapshot` handles it: the position is shown with `priced:
  false` and `null` valuation, and `complete`/`unpriced_symbols` say so. Any new
  valuation path needs the same treatment.
- **A stopped deployment is terminal.** `DeploymentRepository.start` accepts only
  `PENDING`/`PAUSED`; restarting is `DeploymentService.reset`, which returns a *new*
  deployment because `order_events` is append-only.
- **Risk state is durable** in `system_state`, read through
  `atr.services.risk.RiskStateService`. It used to be an in-process dict, so a
  restart re-armed trading. `main.py`'s `_risk_state()` is a *view* over it — there
  is one source of truth, do not add a second.
- **`POST /orders` and `/trade-signals/{id}/execute` require a platform account**
  (401 `authentication_required_for_orders`). Deliberate: an order needs an owner.
  Read-only legacy routes stay open.
- **Counting tests:** the shell harness eats pytest's summary line. Use
  `--junit-xml` and read `tests/failures/errors/skipped` from the XML.

## Known gaps

- `load_cached(exchange)` without `symbols` reads all 2,654 parquets (~56s).
- `/health` Postgres probe runs on a daemon thread.
- `LiveRunner` tested (52 pass) but no CLI entrypoint constructs it.
- `scanner.py`: unvalidated `score = ret_1m + vs_high` heuristic and a hardcoded
  `to_date` that will silently go stale.
- Only ~178 NSEEQ symbols have long daily history; fetch more with
  `scripts/fetch_long_history.py` (~2,300 days, ~0.5s/symbol).
