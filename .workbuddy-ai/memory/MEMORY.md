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

## Repository & publishing

- Remote: `origin` = https://github.com/adityasajja/IIFL-Algo.git (private).
  Branch `main`. Commits are authored as `WorkBuddy <workbuddy@local>` via
  per-command `-c user.name/-c user.email`; no global git identity is set.
- **`.gitignore` patterns must be anchored.** `data/` (no leading slash)
  matches at every depth, so it silently excluded `src/atr/data/` — the whole
  data layer — from every commit for the project's entire life. It is `/data/`
  now. Before adding an ignore rule, check what else it matches:
  `git check-ignore -v <path>`.
- The `.git` directory was destroyed by the environment once (2026-09-12) and
  recovered from the Recycle Bin. See the daily log for the technique. Two
  traps: `$I` metadata is v1 (path at offset 24) or v2 (offset 28), and
  restoring a recycled *directory* requires walking it — recreating the folder
  alone silently loses its contents.
- Windows git plumbing: never pass `text=True` to `git mktree` /
  `hash-object --stdin`. Python rewrites `\n` to `\r\n` and every object
  hashes wrong.
- This sandbox silently drops writes to `.git/refs/remotes/`. `git update-ref`
  reports success and writes nothing — read the ref back to confirm. Workaround:
  write the ref file directly (`9c21972...` + newline into
  `.git/refs/remotes/origin/main`); that path does persist.
- Probe local servers with Python's `urllib`, not curl: curl here returns exit
  23 with an empty body against a healthy endpoint. And a dead port answers
  `HTTPError 502` rather than a connection error — the sandbox proxy replies
  on behalf of the closed port, so use a raw `socket.connect` to test liveness.

## Dashboard — `web/` and the API behind it (2026-09-12)

`atr serve` builds the SPA and serves API + UI on one port; `atr dev` runs
FastAPI with reload alongside Vite. React 18 + Vite 5 + Tailwind 4, motion
components vendored from **beui.dev** via `shadcn add @beui/<name>`.

Panels: Overview, Scanner, Charts, Signals, Alerts, Briefing, **Research**,
Backtest, Portfolio, Risk, Caches & session. Ctrl+K palette reaches all of them.

- **Research is the panel that matters.** `/backtest` is in-sample by
  construction — one config, one dataset, parameters the user already saw the
  answer for. `/research` picks parameters on the training window, scores once
  on the next unseen window, and reports the deflated Sharpe against the hurdle
  implied by the trial count. Always prefer it; the Backtest tab says so in the
  UI.
- New endpoints: `/research`, `/signals/config`, `/signals/scan`, `/portfolio`
  (all five sections), `/quote`, `/history/status`, `/instruments/status`,
  `/strategies`. `/backtest` takes `source=history`.
- **Walk-forward on the daily cache is structurally limited.** The cache holds
  **249 trading days**, and `pivot_to_snapshots` groups by timestamp, so
  `total` is timeline bars, *not* bars × symbols — adding symbols does not
  lengthen the window. Auto-sized windows give train=63/test=62. `signals_entry`
  needs ~110 bars to warm up, so it cannot trade meaningfully inside a fold;
  `/research` returns a `warnings` array saying exactly that, and the UI renders
  it above the numbers. Use `source=fetch` (~2200 days/symbol) for a real test.
- Verified by walking every panel in a real browser: 0 console errors.
  `agent-browser` is not installed and global npm installs are off-limits, so
  drive the installed Edge headlessly over CDP instead — no downloads.

## Broker access facts (verified live)

- IIFL session is created by `atr login --client-id <ID> --auth-code <CODE>`;
  the auth code is single-use and the JWT dies at **midnight IST**, so this is
  a daily step. Cached at `.cache/iifl_session.json` (gitignored).
- **IIFL redirects with lowercase, unseparated parameter names:**
  `/login/callback?authcode=...&clientid=...` — *not* the camelCase
  `?authCode=&clientId=` that the README originally claimed. The handler
  accepts lowercase, camelCase and snake_case. A 422 on the callback means the
  auth code was **never consumed**, so it may still be usable via the manual
  entry form — worth checking before sending the user back through IIFL.
- The docs and README are not a specification. Where they disagree with
  observed behaviour, the observation wins — and then the doc gets fixed.
- `ENV=dev` in `.env` is fine for read-only calls; order placement needs
  `paper` or `live`.
- Market data (`/marketdata/marketquotes`, historical) and `/profile` work.
- MQTT bridge auth (verified against IIFL's official BridgePy, 2026-09-11):
  MQTT **3.1.1** + `clean_session=True`, keepalive **20**,
  username = JWT `preferred_username`, password = `"OPENID~~" + <raw token> + "~"`.
  Connecting anonymously over MQTTv5 does not work.
- The bridge hands raw bytes to callbacks — the official SDK does NOT decode
  payloads, so `codec.py` is ours to maintain. It is now verified correct
  against live ticks (tick + 10-level depth, no decode errors).
- REST coverage is complete: all 18 endpoints in IIFL's official Postman
  collection are implemented in `IiflClient`.
- Instrument master: all 9 segments cached in `.cache/contracts/` (207,942
  contracts). Live quotes work for NSEEQ, BSEEQ, NSEFO, BSEFO, NSECURR.
  NSECOMM responds but is untraded; **BSECURR and MCXCOMM are not served by
  `marketquotes`** — they return `{exchange: "Z", instrumentId: 0}`.
- Gotcha: `value not in (None, "")` is NOT a null check — `nan not in (None, "")`
  is True. Use `contracts._missing()` for anything coming out of a DataFrame.
- **API egress MUST be IPv4.** `api.iiflcapital.com` has A *and* AAAA records
  and this host is dual-stack, so httpx silently connected over IPv6 — an
  address never whitelisted — and every gated endpoint returned
  `EC500 IP address not authorized for trading` even though the registered IPv4
  was correct. `IiflClient` now pins egress with
  `httpx.HTTPTransport(local_address="0.0.0.0")` (`force_ipv4` / `IIFL_FORCE_IPV4`).
  Diagnosing this needs a **multi-service** IP check: `api.ipify.org` is
  IPv4-only and hid the problem.
- The IP whitelist itself is a **SEBI mandate** (algo framework, circular Feb
  2025, fully mandatory 2026-04-01), not an IIFL policy — a support ticket will
  not waive it. Registered as "Primary Static IP" at
  developers.iiflcapital.com (My Apps → ⋮ → View All Details). IIFL confirmed on
  their own repo (issue #191) it applies to all APIs, read-only included.
- SEBI also requires a **non-zero market-protection value** on API market
  orders (zero/absent is rejected). `IiflBroker` now defaults it to 0.5% for
  MARKET orders; configure via `IIFL_MARKET_PROTECTION_PERCENT`.
- developers.iiflcapital.com blocks automated access (Akamai) and its docs are
  a JS SPA — WebFetch cannot read them.

## Signals (buy/sell) — `src/atr/signals/`

- Sell rules = risk management (stop loss, trailing stop, confirmed trend
  break, take profit). No edge needed; they are facts about the book.
- Buy rules = predictions, so they are validated by `atr signals validate`
  via `SignalEntryStrategy`. **They currently FAIL** (-5.05% OOS vs buy-and-hold
  +57.39%, deflated Sharpe 0.926 < 0.95). Buy signals are labelled unvalidated
  and must never be presented as actionable.
- The live scanner and the backtest strategy must call the same rule
  functions. If they diverge, a validation pass means nothing.
- `min_history_bars` must be well below the walk-forward test window, or the
  strategy trades zero times and "no trades" is mistaken for "no edge".
  `WalkForwardConfig.warmup_bars` now prepends an unscored, untraded prefix so
  a slow strategy is not handicapped by the fold boundary — always set it.
- Sweeps: `atr signals validate --search`. Coarse grids only. A bigger grid
  raises the deflated-Sharpe hurdle, so searching harder makes the test
  stricter, not the result better (observed: OOS +5.91% vs deflated 0.270).
- Rule indicators must be precomputed via `precompute_indicators()` in the
  strategy's `prepare()`. Recomputing per bar is ~15x slower — a pandas rolling
  op costs ~0.35ms regardless of data length.
- **Buy-and-hold on the 19 large-cap universe returned +57% at Sharpe 0.85**
  (2020-2026). That is the bar. Any long/flat timing overlay starts structurally
  behind it. Tuning these entry rules is unlikely to be the answer.
- Before trusting any new rule, check how many positions it flags in one day.
  A single close below SMA50 flagged 10 of 17 holdings — that is fatigue, not
  information.

## Validation doctrine (hard-won)

- **A statistically significant Sharpe is not a useful signal.** Cross-sectional
  momentum cleared the deflated-Sharpe bar (0.976) and was still worthless —
  random selection from the same universe scored 1.41 mean vs momentum's 1.24.
  Always run a **control**: same universe, cadence and sizing, but random
  selection. It is the only thing that isolates the signal's contribution.
- The benchmark must be the thing you would actually do instead — for a
  long-only rotation strategy that is buy-and-hold of the same universe.
- Absolute numbers are **survivorship-inflated**: the universe is today's
  listed names tested over the past. Only relative comparisons are meaningful.
- Prefer testing a *different hypothesis* over tuning parameters of a dead one.
  A bigger grid raises the deflated-Sharpe hurdle, so searching harder makes
  the test stricter, not the result better.

## Known gaps (verified, not speculation)

- **`load_cached(exchange)` with no `symbols` reads every parquet file in the
  cache** (2,654 of them). Always pass the symbols you actually need — reading
  all of them cost 56s per validation run. `/scan-all` is the one caller that
  legitimately wants the whole thing.
- **`/health` must never probe Postgres inline.** `localhost` resolves to both
  `::1` and `127.0.0.1`, so with no DB running the connect timeout is paid
  twice (~4s), and the dashboard polls this every 15s. It now probes on a
  daemon thread and returns the last known answer.
- `load_daily()`'s cache fallback must keep the `ts` column — dropping it makes
  `pivot_to_snapshots` raise and loses the whole feed, precisely when the
  fallback is supposed to be saving you.
- `LiveRunner` is **fixed and tested** (2026-09-11) but still has no CLI
  entrypoint — nothing constructs it yet. Its strategy loop was previously
  incapable of trading at all (prepare() ran once on static frames, so every
  live bar had NaN indicators). See tests/test_live_runner.py.
- `prepare()` must be re-run as live bars arrive. It is a one-shot vectorised
  pass in backtests; in live it is the caller's job to refresh it.
- `IiflHistoricalFeed` returns candles as **positional arrays**
  `[ts, o, h, l, c, v]`, wrapped as `{"result": [{"candles": [...]}]}`.
- `scanner.py` `score_frame()` uses an unvalidated `score = ret_1m + vs_high`
  heuristic, and `scan_symbol()` has a hardcoded `to_date` fallback
  (`"08-Sep-2026"`) that will silently go stale.
- `/signals/scan` does one quote plus one daily fetch per symbol; on the default
  19-name universe that is slow enough to be worth batching.
- `data/` is gitignored wholesale, so `data/alerts/rules.json` config and
  `data/scans/` output are not versioned.
- Pre-existing ruff errors in `alerts/store.py`, `api/main.py`, `briefing.py`,
  `brokers/iifl/bridge.py`, `scanner.py` — untouched so far.
