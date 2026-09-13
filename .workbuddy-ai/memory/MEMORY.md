# atr — IIFL algo trading backend (NSE/BSE)

## User's principle (verbatim)

> "I want to develop an intelligent but fact based system which acts on pure
> statistical and strategic data, not on emotions."

In code, "emotion" = overfitting / cherry-picking. Therefore:

- **Out-of-sample only.** Use `atr research` (walk-forward). `atr backtest`
  is in-sample by construction — a tempting number and a meaningless one.
- **Correct for trial count** (deflated Sharpe). Searching harder makes the
  test stricter, not the result better — observed OOS +5.91% with deflated
  Sharpe 0.270 over an 80-trial sweep.
- **Compare to buy-and-hold** of the same universe. On 19 large caps
  (2020-2026), B&H was +57.39% at Sharpe 0.85. A long/flat timing overlay
  starts structurally behind this bar.
- **Run a control** that isolates the signal's own contribution (random
  selection, same universe/cadence/sizing). Cross-sectional momentum cleared
  deflated-Sharpe (0.976) but sat at the 25th percentile of random (z=-0.71).
  Significance is not usefulness.
- **A citation is not a measurement.** `papers.py` carried hardcoded win rates
  (0.58–0.65) and confidence literals (0.85–0.94) that reached the UI as
  `historical_win_rate`. All 7 were over-optimistic by 3–12 points once
  measured; see "Paper alpha" below. Never let a literature figure appear as a
  result — property name, tooltip and all.

## Paper alpha (measured 2026-09-13 — the honest baseline)

18 NSE large caps, 29,358 symbol-bars, 2020-02-17 → 2026-09-11, 400 train /
200 test / 260 warmup, 5 bps slippage. Control (random selection, 8 seeds):
**mean Sharpe −0.35**. Buy & hold: **Sharpe +0.69, +49.31%**.

**None of the 7 pass.** Closest: `avellaneda_lee` OOS Sharpe +0.46, +3.15σ over
control — the only one that plausibly holds signal, and it still loses to B&H.
`multi_factor_composite` +0.26 (+2.37σ). The rest are negative or ~0.
Reproduce: `scripts/validate_paper_strategies.py`; served at `GET /validation`;
rendered in Research → "Measured results".

Fetch long history with `scripts/fetch_long_history.py` first — the daily cache
holds only 249 bars, far too short for folds plus warmup.

## Navigation model (8 sections + sub-tabs) — since 2026-09-13

Sidebar groups follow the trader's loop. **Sub-tab state lives in `App()`**,
never in a panel — a panel owning its own tab state makes deep links and
"open this chart" impossible to route.

| Group | Sections (sub-tabs) |
|---|---|
| Observe | Dashboard · Markets (scanner, custom, charts) |
| Decide | Strategies · Signals (brief, alerts, queue) |
| Act | Trading (portfolio, mode) |
| Learn | Evidence (research, measured, backtest) |
| Maintain | Risk · System |

Hash format is `#tab/subtab`. `LEGACY_TABS` + `LEGACY_SUB` in `App.tsx` map the
old ids; **both layers are needed** — mapping only the tab lands you on the
right section showing the wrong panel, which is still a broken link. `#charts`
must open Markets *with the Charts sub-tab active*. Add a route → add it to
`VALID_TABS` and both maps.

`openChart(symbol)` is the single entry point for symbol → chart (writes
`atr.chartSymbol` to sessionStorage, sets sub-tab, navigates).

## Execution mode: paper vs live

Server-side gate, **not** a disabled button. `_STATE["execution_mode"]`
defaults `"paper"`. `_require_live_execution()` gates `POST /orders` and
`POST /trade-signals/{id}/execute`; read-only endpoints deliberately skip it.
`POST /risk/execution-mode` **requires a reason to go live** (400 without) and
appends to `data/audit/audit.jsonl` via `_append_audit()`. `GET /audit` reads it
newest-first. 13 tests in `tests/test_execution_mode.py`.

Rule: an audit trail must survive a restart, and a gate must be enforced where
the money is — not in the component that draws the toggle.

## Traps that fail as plausible results

- **`Row` does not support `in`.** `"close" in row` is always `False` — `Row`
  subclasses tuple and only overrides `get`/`__getitem__`, so `in` tests
  *values*, not field names. This silently skipped every paper signal and looked
  exactly like "no edge". Use `row.get("close")`. Same class of bug as an
  unanchored `.gitignore` pattern: no error, wrong answer.
- **`logging` uses `%`-style, not `str.format`.** `logger.warning("a {} b {}", x, y)`
  raises `TypeError: not all arguments converted` at *emit* time — the call site
  looks fine and only breaks when the line actually fires. Use `%s`. A missing
  `logger` binding is the same trap one step earlier: `NameError` on a code path
  that rarely runs.
- **NaN-padded frames.** `BacktestEngine._build_frames` NaN-pads any symbol
  without a bar at a step. A feed with one symbol per snapshot leaves each
  symbol ~99% NaN, so `ctx.history(sym)` returns no prices and strategies never
  fire. Put **every symbol on every bar**.
- **`ctx.history(sym)` is per-symbol but advances per-snapshot.** It returns
  that symbol's frame sliced to the global index, so a sparse grid handicaps it
  twice.


## Broker facts (verified live — external systems don't drift)

- **The IP whitelist is a SEBI mandate, not IIFL policy.** Algo framework
  fully mandatory since 2026-04-01. Set as "Primary Static IP" at
  developers.iiflcapital.com (My Apps → ⋮ → View All Details). One primary
  + one backup allowed. Not waivable via support.
- **Egress MUST be IPv4.** `api.iiflcapital.com` publishes A and AAAA; this
  host is dual-stack so httpx silently picked IPv6 — every gated endpoint
  returned `EC500 IP not authorized` even with the IPv4 whitelisted.
  `IiflClient` pins via `local_address="0.0.0.0"` (env `IIFL_FORCE_IPV4`,
  param `force_ipv4`). Diagnose with multi-service checks
  (`api64.ipify.org`); `api.ipify.org` is IPv4-only and hid this.
- IIFL redirect: `/login/callback?authcode=...&clientid=...` (lowercase,
  no separators). Handler accepts all three spellings. 422 means the auth
  code was never consumed — may still be live.
- IIFL nests failures in a success envelope (outer `status` is always
  "Ok"). Walk into `result` to detect failures — `_broker_error` does.
  `EC926 No Trade's are found` = empty, not failure.
- Session: `atr login --client-id <ID> --auth-code <CODE>`. JWT dies at
  midnight IST — daily step. Cached `.cache/iifl_session.json`.
- SEBI: API market orders need non-zero `marketProtectionPercent`. Default
  0.5% in `IiflBroker` (`IIFL_MARKET_PROTECTION_PERCENT` override).
- developers.iiflcapital.com: Akamai-blocked, JS-SPA — WebFetch cannot read.

## Conventions

- Diff outputs (equity curve + every fill), not just `num_trades >= 1`.
- Backtests must stay fast enough for sweeps (~1.9s for 17.5k bars).
- Python 3.12 via `./.venv/Scripts/python.exe`. No scipy — use
  `statistics.NormalDist`.
- Live scanner and backtest strategy must call the **same `eval_entry` /
  `eval_exit` functions**; otherwise a validation pass is meaningless.
- Pre-compute indicators in `prepare()` via `precompute_indicators()`.
  Per-bar recompute is ~15x slower.
- `min_history_bars` < test window, else "no trades" is indistinguishable
  from "no edge".
- Before trusting any new rule, count how many positions it flags in one
  day (fatigue check).
- Entry rules are currently unvalidated (-5.05% OOS vs +57.39% B&H,
  deflated Sharpe 0.926 < 0.95). Do not present as actionable.

## Repo & sandbox quirks

- Remote: github.com/adityasajja/IIFL-Algo (private), `origin/main`. Commits
  authored as `WorkBuddy <workbuddy@local>` per-command. No global git
  identity set.
- **Anchor every .gitignore pattern.** `data/` (no slash) silently excluded
  the entire `src/atr/data/` layer. Verify with `git check-ignore -v <path>`.
  Screenshots go to `/.workbuddy-ai/shots-*/` and are ignored — they are
  regenerated in seconds, and the Chromium profiles beside them are megabytes
  of cache that will otherwise land in a commit.
- Sandbox: (a) `text=True` corrupts git plumbing — pass bytes to
  `mktree`/`hash-object`; (b) writes to `.git/refs/remotes/` silently drop,
  `update-ref` reports success — write ref files directly; (c) probe local
  servers with Python `urllib`, not curl (returns exit 23/empty here);
  (d) dead ports return HTTPError 502, not connection refused — use raw
  `socket.connect`.

## Web / React build gotchas (added 2026-09-12)

- **`atr serve` builds the SPA once at startup** (bun/npm run build) and serves
  the resulting bundle. To see frontend changes, rebuild (`bun run build` in
  `web/`) — the running backend will pick them up immediately.
- **Lucide icon type is broader than `ComponentType<{ size?: number }>`.** Use
  `React.ElementType` for props typed as `icon: SomeComponent`, or `typeof Foo`
  when assigning an icon import to a slot — otherwise `bun run build` fails
  with TS2322 because lucide's `size` accepts `string | number | null`.
- **`Edit` can report "updated successfully" without changing the file.** When
  that happens (sandbox file-cache weirdness), fall back to `Bash` with a
  Python one-liner for in-place substitutions — much more reliable for
  multi-line edits and import-block rewrites.
- **Headless verification without agent-browser** (it does not support
  Windows). Edge is at
  `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`. Screenshot:
  `msedge --headless=new --disable-gpu --hide-scrollbars
  --virtual-time-budget=9000 --window-size=1600,1100 --screenshot=<abs path> <url>`.
  For console errors or DOM assertions, drive CDP over
  `--remote-debugging-port` with `websockets` (stdlib `http.client` for
  `/json/version` to get `webSocketDebuggerUrl`); listen for
  `Runtime.exceptionThrown` + `Log.entryAdded` with `level == "error"`.
  Two gotchas: **`document.querySelector('main')` returns the sidebar inset
  wrapper, not the page** — use the *last* `<main>`; and passing
  `--user-data-dir` is required or the profile locks.
- Portfolio (`web/src/PortfolioPanel.tsx`) is the canonical example of a
  redesigned panel: hero KPI strip with `NumberTicker`, semantic grouping
  (Limits → 4 named cards), card-list Holdings/Positions, status-badged
  Orders/Trades tables, BUY/SELL segmented control with live preview. Same
  pattern works for the other panels.

## Dashboard (web/)

`atr serve` = SPA + API on one port; `atr dev` = FastAPI + Vite. React 19 +
Vite 5 + Tailwind 4, motion components from beui.dev. Sections: Dashboard,
Markets, Strategies, Signals, Trading, Evidence, Risk, System (see "Navigation
model" above). Evidence → Measured results is the section that matters; the
in-sample Backtest sub-tab says so in its own UI.

- **React 19** (upgraded 2026-09-12 from 18.3.1). Done so the beui Animated
  Sidebar (which assumes React 19 types — `inert` attr, mutable `RefObject`)
  works without patching. `motion@13` and `lucide-react@1.45` both declare
  peer `react: ^18 || ^19`. No code changes needed beyond `bun add react@^19
  react-dom@^19` + matching types.
- **shadcn / beui setup**: `web/components.json` present, `@/*` aliased in
  both `tsconfig.json` (`baseUrl: "."`, `paths: {"@/*": ["./src/*"]}`) and
  `vite.config.ts` (resolve.alias via `fileURLToPath`). Install a new
  component with `bunx --bun shadcn add @beui/<name> --yes --overwrite`.
- **Navigation**: `AnimatedSidebarProvider` wraps the whole app in `App.tsx`;
  the sidebar composes `AnimatedSidebar{Header,Content,Footer,Rail}` and the
  main area sits inside `AnimatedSidebarInset`. Default state is expanded
  (`defaultOpen: true`); `Ctrl+B` toggles to the icon rail. The rail
  component is a drag handle for resize, not a hover trigger.

Daily-cache walk-forward is structurally limited: 249 trading days, auto-
sized train/test = 63/62. `signals_entry` needs ~110 bars to warm up, so it
cannot trade meaningfully inside a fold. Use `source=fetch` (~2200 days /
symbol) for a real test.

## Known gaps

- `load_cached(exchange)` without `symbols` reads all 2,654 parquets (~56s).
- `/health` Postgres probe runs on daemon thread (dual-stack `localhost`
  was paying the connect timeout twice inline).
- `load_daily()` cache fallback must keep `ts` column.
- `LiveRunner` tested (52 pass) but no CLI entrypoint constructs it.
- `scanner.py`: unvalidated `score = ret_1m + vs_high` heuristic and a
  hardcoded `to_date` that will silently go stale.
