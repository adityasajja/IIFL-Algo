"""Command line entrypoint.

    uv run atr backtest --strategy sma_crossover --source synthetic
    uv run atr research --strategy sma_crossover --train 5000 --test 1250
    uv run atr login
    uv run atr instruments sync --exchanges NSEEQ,NSEFO
    uv run atr live --symbols NSEFO:NIFTY-I
    uv run atr serve
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atr", description="Algo trading backend")
    sub = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------
    bt = sub.add_parser("backtest", help="run a backtest")
    bt.add_argument("--strategy", default="sma_crossover")
    bt.add_argument("--source", default="synthetic", choices=["synthetic", "csv", "parquet"])
    bt.add_argument("--path", help="directory or file for csv/parquet sources")
    bt.add_argument("--symbols", default="AAPL,MSFT")
    bt.add_argument("--cash", type=float, default=1_000_000.0)
    bt.add_argument("--fast", type=int, default=20)
    bt.add_argument("--slow", type=int, default=50)
    bt.add_argument("--futures", action="store_true", help="model symbols as futures (margin)")
    bt.add_argument("--square-off-eod", action="store_true")
    bt.add_argument("--slippage-bps", type=float, default=5.0)
    bt.add_argument(
        "--max-daily-loss",
        type=float,
        default=None,
        help="halt trading after this much loss in a day (default: 10% of starting cash)",
    )
    bt.add_argument("--save", help="write equity curve to this CSV path")

    # ------------------------------------------------------------------
    rs = sub.add_parser(
        "research", help="walk-forward validation — score a strategy out of sample"
    )
    rs.add_argument("--strategy", default="sma_crossover")
    rs.add_argument("--source", default="synthetic", choices=["synthetic", "csv", "parquet"])
    rs.add_argument("--path", help="directory or file for csv/parquet sources")
    rs.add_argument("--symbols", default="AAPL")
    rs.add_argument("--cash", type=float, default=1_000_000.0)
    rs.add_argument("--train", type=int, default=5_000, help="bars used to pick parameters")
    rs.add_argument("--test", type=int, default=1_250, help="unseen bars each fold is scored on")
    rs.add_argument("--step", type=int, default=None, help="slide size (default: --test)")
    rs.add_argument("--fast", default="10,20,30", help="comma-separated parameter grid")
    rs.add_argument("--slow", default="50,100", help="comma-separated parameter grid")
    rs.add_argument("--min-trades", type=int, default=100)
    rs.add_argument("--confidence", type=float, default=0.95)
    rs.add_argument("--save", help="write per-fold results to this CSV path")

    # ------------------------------------------------------------------
    pf = sub.add_parser("portfolio", help="pull live portfolio from IIFL")
    pf.add_argument(
        "section", nargs="?", default="all",
        choices=["all", "limits", "positions", "holdings", "orders", "trades"],
    )
    pf.add_argument("--limit", type=int, default=25, help="max rows printed per section")
    pf.add_argument("--json", dest="json_path", help="write raw payload to this file")
    pf.add_argument("--save", dest="save_dir", help="write one CSV per section into this directory")

    # ------------------------------------------------------------------
    qt = sub.add_parser("quote", help="live market quotes for symbols")
    qt.add_argument("--symbols", required=True, help="comma-separated, e.g. RELIANCE-EQ,INFY-EQ")
    qt.add_argument("--exchange", default="NSEEQ")
    qt.add_argument("--json", dest="json_path", help="write raw payload to this file")

    # ------------------------------------------------------------------
    sg = sub.add_parser("signals", help="buy/sell signals for your book and a watchlist")
    sg.add_argument(
        "action", nargs="?", default="scan",
        choices=["scan", "buy", "sell", "validate", "init-config"],
    )
    sg.add_argument("--symbols", help="watchlist for the buy scan (comma-separated)")
    sg.add_argument("--telegram", action="store_true", help="push the report to Telegram")
    sg.add_argument("--json", dest="json_path", help="write signals to this JSON file")
    sg.add_argument("--train", type=int, default=300, help="walk-forward train bars (daily)")
    sg.add_argument("--test", type=int, default=200, help="walk-forward test bars (daily)")
    sg.add_argument(
        "--search", action="store_true",
        help="sweep a parameter grid instead of testing one config (validate only)",
    )

    # ------------------------------------------------------------------
    login = sub.add_parser("login", help="complete the IIFL OAuth login")
    login.add_argument("--client-id", help="clientId returned to your redirect URL")
    login.add_argument("--auth-code", help="authCode returned to your redirect URL")
    login.add_argument("--print-url", action="store_true", help="only print the login URL")

    # ------------------------------------------------------------------
    inst = sub.add_parser("instruments", help="instrument master")
    inst.add_argument("action", choices=["sync", "search"])
    inst.add_argument("--exchanges", default="NSEEQ,NSEFO")
    inst.add_argument("--query", default="NIFTY")
    inst.add_argument("--limit", type=int, default=20)

    # ------------------------------------------------------------------
    live = sub.add_parser("live", help="stream ticks from the IIFL bridge")
    live.add_argument("--topics", default="nseeq/2885", help="comma separated exchange/id")
    live.add_argument("--seconds", type=float, default=10.0)

    sub.add_parser("serve", help="start the FastAPI control plane")

    # ------------------------------------------------------------------
    hist = sub.add_parser("history", help="bulk history cache + full-market scan")
    hist.add_argument("action", choices=["sync", "scan-all"])
    hist.add_argument("--exchange", default="NSEEQ")
    hist.add_argument("--interval", default="1d")
    hist.add_argument("--from", dest="from_date", default=None)
    hist.add_argument("--to", dest="to_date", default=None)
    hist.add_argument("--workers", type=int, default=6)
    hist.add_argument("--start", type=int, default=0)
    hist.add_argument("--end", type=int, default=None)

    # ------------------------------------------------------------------
    al = sub.add_parser("alerts", help="evaluate price/indicator alert rules")
    al.add_argument("action", choices=["check", "test", "list"])

    # ------------------------------------------------------------------
    br = sub.add_parser("brief", help="morning briefing (preview or Telegram it)")
    br.add_argument("action", choices=["preview", "send"], nargs="?", default="preview")
    return parser


def _run_backtest(args) -> int:
    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig, BacktestEngine
    from atr.data.csv_feed import CsvFeed, ParquetFeed
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.execution.risk import RiskLimits
    from atr.strategy.strategies import STRATEGIES

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    if args.source == "synthetic":
        feed = SyntheticFeed(
            SyntheticConfig(symbols=symbols, start=datetime(2024, 1, 1, 9, 30),
                            end=datetime(2024, 6, 28, 15, 59)),
            futures=args.futures,
        )
    elif args.source == "csv":
        feed = CsvFeed(directory=args.path) if not args.path.endswith(".csv") else CsvFeed(path=args.path)
    else:
        feed = ParquetFeed(directory=args.path)

    strategy_cls = STRATEGIES.get(args.strategy)
    if strategy_cls is None:
        logger.error("unknown strategy {!r}; available: {}", args.strategy, list(STRATEGIES))
        return 2
    strategy = strategy_cls(fast=args.fast, slow=args.slow) if args.strategy == "sma_crossover" else strategy_cls()

    config = BacktestConfig(
        initial_cash=args.cash,
        slippage=SlippageModel(bps=args.slippage_bps),
        square_off_eod=args.square_off_eod,
        risk=RiskLimits(max_daily_loss=args.max_daily_loss or args.cash * 0.10),
    )
    result = BacktestEngine(feed, strategy, config).run()
    print(result.summary())
    if args.save:
        result.equity.to_csv(args.save, header=True)
        logger.info("wrote equity curve to {}", args.save)
    return 0


def _run_research(args) -> int:
    """Walk a strategy forward and report only the out-of-sample evidence."""
    from atr.backtest.costs import SlippageModel
    from atr.backtest.engine import BacktestConfig
    from atr.data.csv_feed import CsvFeed, ParquetFeed
    from atr.data.synthetic import SyntheticConfig, SyntheticFeed
    from atr.execution.risk import RiskLimits
    from atr.research.validate import ValidationConfig, WalkForwardConfig, walk_forward
    from atr.strategy.strategies import STRATEGIES

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())

    if args.source == "synthetic":
        feed = SyntheticFeed(
            SyntheticConfig(symbols=symbols, start=datetime(2024, 1, 1, 9, 30),
                            end=datetime(2024, 6, 28, 15, 59))
        )
    elif args.source == "csv":
        feed = CsvFeed(directory=args.path) if not args.path.endswith(".csv") else CsvFeed(path=args.path)
    else:
        feed = ParquetFeed(directory=args.path)

    strategy_cls = STRATEGIES.get(args.strategy)
    if strategy_cls is None:
        logger.error("unknown strategy {!r}; available: {}", args.strategy, list(STRATEGIES))
        return 2

    def grid(text: str) -> list[int]:
        return [int(x) for x in text.split(",") if x.strip()]

    # Parameter grids are strategy-specific; only SMA crossover takes fast/slow.
    param_grid = (
        {"fast": grid(args.fast), "slow": grid(args.slow)}
        if args.strategy == "sma_crossover"
        else {}
    )

    result = walk_forward(
        feed,
        strategy_cls,
        param_grid,
        config=WalkForwardConfig(train_bars=args.train, test_bars=args.test, step_bars=args.step),
        backtest=BacktestConfig(
            initial_cash=args.cash,
            slippage=SlippageModel(bps=5.0),
            risk=RiskLimits(max_daily_loss=args.cash * 0.10),
        ),
        validation=ValidationConfig(min_trades=args.min_trades, min_confidence=args.confidence),
    )
    print(result.summary())
    if args.save:
        result.to_frame().to_csv(args.save, index=False)
        result.oos_equity.to_csv(args.save.replace(".csv", "_equity.csv"), header=True)
        logger.info("wrote per-fold results and OOS equity next to {}", args.save)
    return 0


def _rows(payload) -> list[dict]:
    """IIFL wraps list responses in ``{"result": [...]}``; tolerate either."""
    if isinstance(payload, dict):
        for key in ("result", "data", "positions", "holdings", "orders", "trades", "limits"):
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return [v for v in value if isinstance(v, dict)]
                if isinstance(value, dict):
                    return [value]
        return [payload]
    return [v for v in (payload or []) if isinstance(v, dict)]


def _print_table(rows: list[dict], limit: int) -> None:
    """Print rows, skipping nested columns that would render unreadably."""
    import pandas as pd

    frame = pd.DataFrame(rows)
    if frame.empty:
        print("  (empty)")
        return
    flat = [
        c for c in frame.columns
        if not frame[c].map(lambda v: isinstance(v, (dict, list))).any()
    ]
    print(frame[flat].head(limit).to_string(index=False))
    if len(frame) > limit:
        print(f"  ... {len(frame) - limit} more (raise with --limit)")


def _authenticated_client():
    """Return a client with a live session, or None if we must log in first."""
    from atr.brokers.iifl.client import IiflClient
    from atr.config.settings import get_settings

    settings = get_settings()
    client = IiflClient(app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret)
    if not client.restore_session():
        logger.error("no active session — run `atr login --print-url` and complete the login")
        return None
    return client


def _run_portfolio(args) -> int:
    import json as _json

    client = _authenticated_client()
    if client is None:
        return 1

    sections = (
        ["limits", "positions", "holdings", "orders", "trades"]
        if args.section == "all"
        else [args.section]
    )
    fetchers = {
        "limits": client.limits,
        "positions": client.positions,
        "holdings": client.holdings,
        "orders": client.order_book,
        "trades": client.trades,
    }

    raw: dict = {}
    with client:
        for name in sections:
            try:
                raw[name] = fetchers[name]()
            except Exception as exc:  # noqa: BLE001 - one dead endpoint shouldn't abort
                logger.error("{} failed: {}", name, exc)
                raw[name] = None

    for name in sections:
        rows = _rows(raw[name])
        print(f"\n=== {name} ===")
        if not rows:
            print("  (empty)")
            continue
        if name == "limits":
            for key, value in rows[0].items():
                print(f"  {str(key):32} {value}")
        else:
            _print_table(rows, args.limit)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            _json.dump(raw, fh, indent=2, default=str)
        logger.info("wrote raw payload to {}", args.json_path)

    if args.save_dir:
        import os

        import pandas as pd

        os.makedirs(args.save_dir, exist_ok=True)
        written = 0
        for name in sections:
            rows = _rows(raw[name])
            if not rows:
                continue
            path = os.path.join(args.save_dir, f"{name}.csv")
            pd.DataFrame(rows).to_csv(path, index=False)
            written += 1
            logger.info("wrote {}", path)
        if not written:
            logger.warning("nothing to save — every section came back empty or errored")
    return 0


def _run_quote(args) -> int:
    import json as _json

    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    client = _authenticated_client()
    if client is None:
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    with client:
        master = InstrumentMaster(client)
        try:
            master.load_cached([args.exchange])
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "no cached instrument master for {} — run `atr instruments sync --exchanges {}`",
                args.exchange, args.exchange,
            )
            logger.debug("load_cached failed: {}", exc)
            return 1

        legs, resolved, failed = [], [], []
        for symbol in symbols:
            try:
                legs.append((args.exchange, resolve_conid(master, symbol, args.exchange)))
                resolved.append(symbol)
            except Exception as exc:  # noqa: BLE001
                failed.append((symbol, str(exc)[:120]))

        for symbol, reason in failed:
            logger.error("could not resolve {}: {}", symbol, reason)
        if not legs:
            return 1

        try:
            payload = client.market_quotes(legs)
        except Exception as exc:  # noqa: BLE001
            logger.error("quote request failed: {}", exc)
            return 1

    quotes = _rows(payload)
    for symbol, quote in zip(resolved, quotes, strict=False):
        quote["symbol"] = symbol
    _print_table(quotes, limit=len(quotes) or 1)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, indent=2, default=str)
        logger.info("wrote raw payload to {}", args.json_path)
    return 0


def _run_signals(args) -> int:
    import contextlib
    import dataclasses
    import json as _json

    import pandas as pd

    from atr.signals.engine import format_report, scan_holdings, scan_universe
    from atr.signals.models import DEFAULT_CONFIG_PATH, ScanResult, SignalConfig

    cfg = SignalConfig.load()

    if args.action == "init-config":
        cfg.save(DEFAULT_CONFIG_PATH)
        print(f"wrote default thresholds to {DEFAULT_CONFIG_PATH}")
        print("These are starting points, not findings. Validate before trusting them.")
        return 0

    if args.action == "validate":
        from atr.backtest.engine import BacktestConfig
        from atr.core.enums import Timeframe
        from atr.core.models import Instrument
        from atr.data.base import ListFeed, pivot_to_snapshots
        from atr.research.validate import ValidationConfig, WalkForwardConfig, walk_forward
        from atr.scanner import UNIVERSE
        from atr.signals.strategy import SignalEntryStrategy

        symbols = cfg.universe or UNIVERSE
        # Fetch real history rather than reading the cache: the local cache
        # holds about a year, which is too short to carve into folds that also
        # leave room for indicator warmup.
        client = _authenticated_client()
        if client is None:
            return 1
        from atr.signals.engine import load_daily

        master = None
        frames = []
        with client:
            from atr.brokers.iifl.contracts import InstrumentMaster

            master = InstrumentMaster(client)
            master.load_cached([cfg.exchange])
            for symbol in symbols:
                conid = None
                with contextlib.suppress(KeyError):
                    conid = master.find(symbol, cfg.exchange).conid
                frame = load_daily(
                    symbol, cfg.exchange, client, conid, lookback_days=2200
                )
                if frame.empty:
                    continue
                frame = frame.copy()
                frame["symbol"] = symbol
                frames.append(frame)
                logger.info("{}: {} daily bars", symbol, len(frame))
        if not frames:
            logger.error("no daily history available")
            return 1

        combined = pd.concat(frames, ignore_index=True)
        snapshots = pivot_to_snapshots(combined, Timeframe.DAY_1)
        instruments = {s: Instrument(symbol=s, exchange=cfg.exchange) for s in symbols}
        feed = ListFeed(snapshots, instruments)

        params = {
            **dataclasses.asdict(cfg.entries),
            **dataclasses.asdict(cfg.exits),
            "allocation": 0.10,
        }
        # One combination = a single test. A grid = a search, and the deflated
        # Sharpe then has to clear a hurdle scaled to how many were tried.
        grid = {k: [v] for k, v in params.items()}
        if args.search:
            from atr.signals.models import SEARCH_GRID

            grid.update(SEARCH_GRID)
            combos = 1
            for values in grid.values():
                combos *= len(values)
            logger.info("sweeping {} parameter combinations", combos)

        result = walk_forward(
            feed,
            SignalEntryStrategy,
            grid,
            config=WalkForwardConfig(
                train_bars=args.train,
                test_bars=args.test,
                # Enough history for the longest indicator to be valid before
                # the first scored bar, or the strategy never trades at all.
                warmup_bars=cfg.entries.min_history_bars + 60,
            ),
            backtest=BacktestConfig(initial_cash=1_000_000.0),
            validation=ValidationConfig(min_folds=2, min_trades=5),
        )
        print(result.summary())
        out = Path("data/signals/validation.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            _json.dumps(
                {
                    "passed": result.verdict.passed,
                    "oos_sharpe": result.oos_metrics.sharpe,
                    "deflated_sharpe": result.deflated_sharpe,
                    "folds": len(result.folds),
                    "trades": result.oos_metrics.num_trades,
                    "checks": [
                        {"name": n, "ok": ok, "detail": d}
                        for n, ok, d in result.verdict.checks
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("wrote {}", out)
        return 0 if result.verdict.passed else 1

    # Whether the entry rules have actually passed out-of-sample validation.
    # Read from disk rather than assumed, so the report can never quietly
    # present an unproven rule as though it were established.
    validated = False
    validation_path = Path("data/signals/validation.json")
    if validation_path.exists():
        try:
            validated = bool(_json.loads(validation_path.read_text(encoding="utf-8")).get("passed"))
        except Exception:  # noqa: BLE001 - a corrupt file just means "not validated"
            validated = False

    client = _authenticated_client()
    if client is None:
        return 1

    result = ScanResult()
    with client:
        if args.action in ("scan", "sell"):
            sells, errors = scan_holdings(client, cfg)
            result.sells = sells
            result.errors.extend(errors)
        if args.action in ("scan", "buy"):
            symbols = args.symbols or ",".join(cfg.universe)
            from atr.scanner import UNIVERSE as SCAN_UNIVERSE

            watchlist = [
                s.strip().upper()
                for s in (symbols.split(",") if symbols else SCAN_UNIVERSE)
                if s.strip()
            ]
            buys, errors = scan_universe(client, watchlist, cfg)
            result.buys = buys
            result.errors.extend(errors)

    title, body = format_report(result, validated=validated)
    print(title)
    print(body)
    for error in result.errors[:10]:
        logger.warning(error)

    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(
            _json.dumps(
                {
                    "buys": [dataclasses.asdict(s) for s in result.buys],
                    "sells": [dataclasses.asdict(s) for s in result.sells],
                    "errors": result.errors,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.info("wrote signals to {}", args.json_path)

    if args.telegram:
        from atr.alerts.channels import TelegramChannel
        from atr.config.settings import get_settings

        settings = get_settings()
        channel = TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
        if channel.send(title, body):
            logger.info("pushed to telegram")
        else:
            logger.error("telegram send failed — check TELEGRAM_BOT_TOKEN / CHAT_ID")
            return 1
    return 0


def _run_login(args) -> int:
    from atr.brokers.iifl.auth import login_url
    from atr.config.settings import get_settings

    settings = get_settings()
    if args.print_url or not (args.client_id and args.auth_code):
        print(login_url(settings.iifl_app_key, settings.iifl_redirect_url))
        print("\nOpen the URL, log in, then re-run with --client-id and --auth-code.")
        return 0

    from atr.brokers.iifl.client import IiflClient

    client = IiflClient(app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret)
    session = client.create_session(args.client_id, args.auth_code)
    print(json.dumps({"client_id": session.client_id, "expires_at": session.expires_at.isoformat()}))
    return 0


def _run_instruments(args) -> int:
    from atr.brokers.iifl.client import IiflClient
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.config.settings import get_settings

    settings = get_settings()
    client = IiflClient(app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret)
    if not client.restore_session():
        logger.error("no active session — run `atr login` first")
        return 1
    master = InstrumentMaster(client)
    exchanges = [e.strip() for e in args.exchanges.split(",")]
    master.sync(exchanges)
    if args.action == "search":
        print(master.search(args.query, limit=args.limit)[["symbol", "exchange", "conid", "expiry"]].to_string())
    else:
        print(f"synced {len(master.frame)} contracts")
    return 0


def _run_live(args) -> int:
    import time

    from atr.brokers.iifl.auth import SessionStore
    from atr.brokers.iifl.bridge import BridgeClient
    from atr.config.settings import get_settings

    settings = get_settings()
    session = SessionStore(settings.iifl_session_cache).load()
    if session is None:
        logger.error("no active session — run `atr login` first")
        return 1

    bridge = BridgeClient(session)
    bridge.on_feed = lambda topic, feed: print(
        f"{topic} ltp={feed.ltp:.2f} bid={feed.best_bid_price:.2f} ask={feed.best_ask_price:.2f} vol={feed.traded_volume}"
    )
    bridge.on_error = lambda code, msg: logger.error("{}: {}", code, msg)
    bridge.connect()
    topics = [t.strip() for t in args.topics.split(",")]
    bridge.subscribe_feed(topics)
    logger.info("streaming {} for {}s ...", topics, args.seconds)
    time.sleep(args.seconds)
    bridge.disconnect()
    return 0


def _run_history(args) -> int:
    from datetime import date

    import pandas as pd

    from atr.data.history import load_cached, sync_all
    from atr.scanner import score_frame

    if args.action == "sync":
        sync_all(args.exchange, args.interval, args.from_date, args.to_date,
                 workers=args.workers, start=args.start, end=args.end)
        return 0

    frames = load_cached(args.exchange)
    rows = []
    for symbol, df in frames.items():
        try:
            rows.append(score_frame(symbol, df))
        except Exception:  # noqa: BLE001 — thin/odd histories just don't rank
            continue
    scan = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    out = f"data/scans/scan_all_{date.today():%Y%m%d}.csv"
    scan.to_csv(out, index=False)
    up = int((scan["trend"] == "UP").sum())
    print(f"scored {len(scan)}/{len(frames)} names -> {out}")
    print(f"breadth: {up} UP / {len(scan) - up} not-UP")
    print("\n--- top 10 ---\n" + scan.head(10).to_string(index=False))
    print("\n--- bottom 10 ---\n" + scan.tail(10).to_string(index=False))
    breakout = scan[scan["breakout"]]
    if not breakout.empty:
        print("\n--- breakouts ---\n" + breakout[["symbol", "last", "ret_1m", "vol_x"]].to_string(index=False))
    return 0


def _run_alerts(args) -> int:
    from atr.alerts.channels import channels_from_settings
    from atr.alerts.engine import check, market_open_now
    from atr.alerts.store import AlertStore
    from atr.brokers.iifl.client import IiflClient
    from atr.config.settings import get_settings

    settings = get_settings()
    store = AlertStore()
    if args.action == "list":
        for r in store.rules():
            print(f"[{'ARMED' if r.armed else 'off'}] {r.id} {r.display} (cooldown {r.cooldown_min}m)")
        return 0
    if args.action == "test":
        for ch in channels_from_settings(settings):
            if ch.send("ATR test", "alerts are wired — you will get firing rules here."):
                print(f"test message sent via {ch.name}")
                return 0
        print("nothing configured — message went nowhere; set TELEGRAM_BOT_TOKEN/CHAT_ID")
        return 1
    client = IiflClient(app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret)
    if client.restore_session() is None:
        print("no active session — run `atr login` first")
        return 1
    fired = check(store, client, channels_from_settings(settings))
    print(f"market_open={market_open_now()} fired={len(fired)}")
    for e in fired:
        print(f"[{e.channel}] {e.rule} — {e.message}")
    return 0


def _run_brief(args) -> int:
    from atr.alerts.channels import channels_from_settings
    from atr.briefing import build_brief, load_config, record_sent
    from atr.config.settings import get_settings

    cfg = load_config()
    message, stats = build_brief(cfg)
    print(message)
    if args.action == "send":
        if not cfg.send_enabled:
            print("(send disabled in config — preview only)")
            return 0
        for ch in channels_from_settings(get_settings()):
            if ch.send("ATR morning brief", message):
                record_sent(message, ch.name)
                print(f"sent via {ch.name}")
                return 0
        print("nothing configured to send with")
        return 1
    return 0


def _run_serve(args) -> int:
    import uvicorn

    from atr.config.settings import get_settings

    settings = get_settings()
    uvicorn.run("atr.api.main:app", host=settings.api_host, port=settings.api_port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    handlers = {
        "backtest": _run_backtest,
        "research": _run_research,
        "portfolio": _run_portfolio,
        "quote": _run_quote,
        "signals": _run_signals,
        "login": _run_login,
        "instruments": _run_instruments,
        "history": _run_history,
        "alerts": _run_alerts,
        "brief": _run_brief,
        "live": _run_live,
        "serve": _run_serve,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
