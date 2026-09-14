# Automation memory — ATR signals → Telegram (market-hours gated)

Task: run `atr.cli signals scan --telegram` and report buy/sell counts, but ONLY
on a weekday between 09:15 and 15:30 IST. Outside that window: do nothing, send
no Telegram message. Never re-authenticate a dead IIFL session — notify instead.
Notify-only system; never place orders.

## Runs

| date | local time (IST) | outcome |
|---|---|---|
| 2026-09-14 (Mon) | 16:50 | Guard tripped — after 15:30 close. No scan run, no Telegram message. |

## Notes / open issues

- This run fired at 16:50 IST, i.e. **outside** the 09:15–15:30 window the task
  itself defines. Either the schedule is set to the wrong wall-clock time, or it
  is expressed in a non-IST timezone (16:50 IST = 11:20 UTC). Flagged to Aditya.
- Buy-side entry rules are watchlist-only: deflated Sharpe 0.926 vs 0.95 bar,
  -5.05% out-of-sample vs B&H +57.39%. Never describe as validated.
- Session token expires midnight IST; renewal needs an interactive auth code.
