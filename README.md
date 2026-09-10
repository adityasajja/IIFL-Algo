# atr — algorithmic trading backend

A Python backend for systematic trading on **IIFL Capital** (NSE/BSE equity, F&O,
currency, commodity). Backtest-first: the same strategy class, portfolio, and
risk engine run against historical data and live markets.

Managed with [uv](https://docs.astral.sh/uv/).

---

## What's in here

```
src/atr/
  core/          domain models, enums, events (broker-agnostic)
  data/          feeds (CSV/Parquet/synthetic), TimescaleDB store, bar aggregation
  backtest/      portfolio, simulated broker, cost models, metrics, engine
  strategy/      Strategy/StrategyContext base + indicators + built-ins
  execution/     pre-trade & intraday risk
  brokers/
    base.py      Broker ABC (what live trading must implement)
    iifl/        IIFL Capital: auth, REST client, contracts, orders, MQTT bridge
  api/           FastAPI control plane
  cli.py         `atr` command
```

Design rules that matter:

1. **No look-ahead.** Market orders signalled on bar *i* fill at the open of bar
   *i+1*. Limit orders fill at the worse of limit price and open, so a gap
   through your limit doesn't fill at your price.
2. **Conservation is asserted.** `Portfolio.check_invariant()` runs every bar:
   equity must equal `initial_cash + realised + unrealised − commission`. If the
   fills or margin logic is wrong, the backtest crashes instead of lying.
3. **Costs are explicit.** Commission (per-share for equity, per-contract for
   F&O, with min per order) and slippage (bps and/or ticks) are separate,
   pluggable models.
4. **Risk runs in both modes.** The same `RiskEngine` gates backtest orders and
   live orders.

---

## Setup

```bash
uv sync                 # create .venv and install everything
cp .env.example .env    # then fill in your app key/secret
```

Requires Python 3.12+ (uv will fetch one if needed).

---

## Quick start

```bash
# Backtest the sample strategy on synthetic data — no broker needed
uv run atr backtest --strategy sma_crossover --symbols AAPL,MSFT

# Model the same symbols as futures to exercise margin + lot sizing
uv run atr backtest --strategy sma_crossover --futures --cash 1000000

# Validate out of sample — parameters chosen only on data the test never saw
uv run atr research --strategy sma_crossover --train 5000 --test 1250 \
    --fast 5,10,20 --slow 30,50,100

# Start the HTTP control plane
uv run atr serve        # http://127.0.0.1:8000/docs

# Tests
uv run pytest
```

**Authenticate with IIFL:**

```bash
uv run atr login --print-url
# open the URL, log in with your trading credentials + OTP
# you land on your redirect URL with ?authCode=...&clientId=...
uv run atr login --client-id <CLIENTID> --auth-code <AUTHCODE>
```

The session JWT is cached in `.cache/iifl_session.json`. It dies at **midnight
IST** and the auth code is single-use, so you re-login once per trading day.

**Fetch the instrument master** (needed before live trading — you must know the
`instrumentId` for anything you trade):

```bash
uv run atr instruments sync --exchanges NSEEQ,NSEFO
uv run atr instruments search --query NIFTY --limit 20
```

**Stream live ticks:**

```bash
uv run atr live --topics nseeq/2885,nsefo/35005 --seconds 30
```

---

## IIFL Capital API reference (as implemented)

Base URL: `https://api.iiflcapital.com/v1` · Auth: `Authorization: Bearer <userSession>`

| Area | Method | Endpoint |
|---|---|---|
| Session | POST | `/getusersession` — body `{checkSum}` = `SHA256(clientId + authCode + appSecret)` |
| User | GET | `/profile`, `/limits` · POST `/profile/logout` |
| Orders | POST | `/orders` (array), PUT `/orders/{id}`, DELETE `/orders/{id}` |
| Orders | GET | `/orders`, `/orders/{id}`, `/trades` |
| Portfolio | GET | `/positions`, `/holdings` |
| Margin | POST | `/spanexposure` (array), `/preordermargin` |
| Market data | POST | `/marketdata/historicaldata`, `/marketdata/marketquotes`, `/marketdata/marketdepth`, `/marketdata/openinterest` |
| Instruments | GET | `/contractfiles/{EXCHANGE}.json` |

Enums observed in the docs:

* **exchange** — `NSEEQ BSEEQ NSEFO BSEFO NSECURR BSECURR NSECOMM BSECOMM MCXCOMM NCDEXCOMM INDICES`
* **product** — `NORMAL INTRADAY DELIVERY BNPL`
* **orderComplexity** — `REGULAR AMO BO CO`
* **orderType** — `LIMIT MARKET SL SLM`
* **validity** — `DAY IOC`
* **interval** — `1 minute, 5 minutes, 10 minutes, 15 minutes, 30 minutes, 60 minutes, 1 day, weekly, monthly`
* **dates** — `dd-MMM-yyyy`, e.g. `19-Sep-2024`

### Realtime market data (MQTT bridge)

Host `bridge.iiflcapital.com`, port `8883`, TLS. The client id and the
order/trade update topic are the JWT's `preferred_username` claim.

| Stream | Topic prefix |
|---|---|
| Market feed | `prod/marketfeed/mw/v1/` + `nseeq/2885` |
| Index feed | `prod/marketfeed/index/v1/` |
| Open interest | `prod/marketfeed/oi/v1/` |
| Market status | `prod/marketfeed/marketstatus/v1/` + `nseeq` |
| LPP band | `prod/marketfeed/lpp/v1/` |
| Circuits / 52-week | `prod/marketfeed/{uppercircuit,lowercircuit,high52week,low52week}/v1/` |
| Order updates | `prod/updates/order/v1/` + `<clientId>` |
| Trade updates | `prod/updates/trade/v1/` + `<clientId>` |

Market feed packets are **186 bytes** of packed binary
(`src/atr/brokers/iifl/codec.py`). Every price is an `Int32` that must be
divided by the `priceDivisor` field in the packet. Depth is 10 levels of
`(quantity u32, price i32, orders i16, transactionType i16)`. Limits: 6000
subscriptions per client, 1024 topics per request.

> We implement the bridge natively with `paho-mqtt` rather than depending on
> IIFL's `BridgePy` package, so the decoder is inspectable and testable.

### Caveat: contract file schema

`contracts.py` tolerates several field spellings (`instrumentId` /
`ExchangeInstrumentId`, `tradingSymbol` / `Symbol`, …) because the per-segment
files aren't perfectly uniform. After your first
`atr instruments sync`, spot-check a few rows — better to find a mapping bug
before you trade than after.

---

## Validating a strategy

A single backtest is not evidence. Fit enough parameter combinations to one
price series and the best of them will look excellent by chance — that is the
failure mode this section exists to block.

```bash
uv run atr research --strategy sma_crossover --train 5000 --test 1250 \
    --fast 5,10,20 --slow 30,50,100
```

`atr research` walks forward fold by fold. Parameters are chosen **only** on
the training window, then scored once on the window that follows it. The equity
curve it reports is stitched from unseen windows only — nothing in it was used
to pick a parameter.

It then withholds a pass unless every one of these holds:

* enough folds, and enough trades, for the result to mean anything
* **deflated Sharpe** above your confidence threshold — the Sharpe corrected
  for how many combinations you tried (Bailey & López de Prado). Picking the
  best of 261 combos demands far stronger evidence than testing one idea, and
  this is the number that says so.
* a positive out-of-sample Sharpe that **beats buy-and-hold**
* drawdown inside your limit

Run against the synthetic feed, `sma_crossover` **fails** — and that is the
correct answer. There is no signal in a random walk; a harness that flattered
you here would be worthless. On that run the reason is visible in the numbers:
the strategy turns over ~245 times in six months and pays roughly 12% of
notional in slippage, so costs, not direction, are what sink it.

Passing this is necessary, not sufficient. It means "not yet disproven".

---

## Writing a strategy

```python
from atr.strategy.base import Strategy
from atr.strategy.indicators import crossover, sma
import pandas as pd

class MyStrategy(Strategy):
    name = "my_strategy"
    fast, slow = 20, 50

    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for f in frames.values():          # vectorised, runs once
            f["sma_fast"] = sma(f["close"], self.fast)
            f["sma_slow"] = sma(f["close"], self.slow)

    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:     # per bar
            row = ctx.row(symbol)
            if pd.isna(row.sma_slow):
                continue
            if crossover(*self._pair(ctx, symbol)):
                ctx.target(symbol, self._size(ctx, symbol, row.close))
```

`ctx` gives you `equity`, `cash`, `buying_power`, `position(symbol)`,
`history(symbol, n)`, `row(symbol)`, `order(...)`, `target(...)`, `close(...)`.
It deliberately exposes no broker — that's how the same code goes live.

### Going live

`atr.live.runner.LiveRunner` drives a strategy from the bridge feed and routes
orders through `IiflBroker`. Before you enable it:

* run `atr instruments sync` and confirm your symbols resolve
* set `ENV=paper` and verify order placement against the paper endpoint
* confirm `RiskLimits` match what you're actually willing to lose
* keep `square_off_time` on for intraday F&O

---

## Storage (optional)

Postgres + TimescaleDB for bars/ticks/orders/fills:

```bash
docker run -d --name timescale -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=algodb \
  timescale/timescaledb:latest-pg16
uv run python -c "from atr.data.store import Database; Database().init_schema()"
```

`BarRepository` upserts on `(ts, symbol, timeframe)`; `bars` and `ticks` become
hypertables with compression enabled when the extension is present.

---

## Roadmap / not done yet

* Bracket/cover order support (`BO`/`CO`) beyond payload passthrough
* Option greeks and an F&O strategy template
* Reconciliation job (broker positions vs internal portfolio)
