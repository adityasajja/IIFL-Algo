# Automation memory — ATR signals → Telegram (market-hours gated)

Task: run `atr.cli signals scan --telegram` and report buy/sell counts, but ONLY
on a weekday between 09:15 and 15:30 IST. Outside that window: do nothing, send
no Telegram message. Never re-authenticate a dead IIFL session — notify instead.
Notify-only system; never place orders.

## Runs

| date | local time (IST) | outcome |
|---|---|---|
| 2026-09-14 (Mon) | 16:50 | Guard tripped — after 15:30 close. No scan run, no Telegram message. |
| 2026-09-14 (Mon) | 17:51 | Guard tripped again — after 15:30 close. No scan, no Telegram message. |

**Recurring pattern:** the job has now fired twice on 2026-09-14, both ~45–60 min
after the close, never inside 09:15–15:30. Each run therefore does nothing. The
run log alone cannot distinguish "schedule is set to the wrong wall-clock time"
from "the machine's local clock is wrong" — see the clock-check note below.

## Notes / open issues

- **Schedule never lands inside the window.** Both 2026-09-14 runs fired after
  the close. Until this is fixed the job is inert — it can never send a signal.
  Either the schedule is set to the wrong wall-clock time, or it is expressed in
  a non-IST timezone. Flagged to Aditya on both runs.
- **Clock check (2026-09-14):** the host local clock and the injected session
  time agree — both read 17:51 IST. So the schedule, not the clock, is at fault.
- **Trap — `TZ=` is broken under Git Bash.** `TZ=Asia/Kolkata date ...` silently
  returns a wrong or `GMT`-labelled result; every zone renders as `+0000`, and
  `/usr/share/zoneinfo` is absent. Do **not** trust shell `date` for IST.
  Verify IST with Python instead, which is correct:
  `./.venv/Scripts/python.exe -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Asia/Kolkata')))"`
  Note Python's local `datetime.now()` is already IST-correct here (the host
  clock is IST); only `TZ=` overrides in Git Bash are unreliable.

- Buy-side entry rules are watchlist-only: deflated Sharpe 0.926 vs 0.95 bar,
  -5.05% out-of-sample vs B&H +57.39%. Never describe as validated.
- Session token expires midnight IST; renewal needs an interactive auth code.
