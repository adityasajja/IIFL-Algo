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

## Broker access facts (verified live)

- IIFL session is created by `atr login --client-id <ID> --auth-code <CODE>`;
  the auth code is single-use and the JWT dies at **midnight IST**, so this is
  a daily step. Cached at `.cache/iifl_session.json` (gitignored).
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
- `/limits`, `/positions`, `/holdings`, `/orders` return
  `EC500 IP address not authorized for trading` — the API key is IP-whitelisted.
  Whitelisting the current public IP (was `122.177.247.238` on 2026-09-10, but
  home broadband is usually dynamic) is required before portfolio data works.

## Known gaps (verified, not speculation)

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
- `data/` is gitignored wholesale, so `data/alerts/rules.json` config and
  `data/scans/` output are not versioned.
- Pre-existing ruff errors in `alerts/store.py`, `api/main.py`, `briefing.py`,
  `brokers/iifl/bridge.py`, `scanner.py` — untouched so far.
