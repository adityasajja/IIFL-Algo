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

    serve = sub.add_parser("serve", help="start the app: FastAPI + built frontend on one port")
    serve.add_argument("--host", help="override API_HOST")
    serve.add_argument("--port", type=int, help="override API_PORT")
    serve.add_argument("--no-browser", action="store_true", help="don't open the dashboard on start")
    serve.add_argument("--rebuild", action="store_true", help="force a frontend rebuild")
    serve.add_argument("--no-build", action="store_true", help="skip the build/missing check")

    # ------------------------------------------------------------------
    sub.add_parser("backup", help="write a backup of the database and state now")

    hist = sub.add_parser("history", help="bulk history cache + full-market scan")
    hist.add_argument("action", choices=["sync", "scan-all", "refresh-eod"])
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

    # ------------------------------------------------------------------
    # Read-only by design: every action here inspects what already happened.
    # Learning has no action that writes to a deployment, an order or a risk
    # limit. Learning may propose; execution stays with
    # Strategy -> RiskEngine -> OMS -> Broker/Paper. (Authoring a strategy is
    # `atr strategy`, which writes to `strategies`/`strategy_versions` and
    # nothing else.)
    learn = sub.add_parser("learn", help="what the trade history says (read-only)")
    learn.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "dataset", "performance", "report", "drift", "snapshot"],
    )
    learn.add_argument("--strategy", help="restrict to one strategy (id, or id@version)")
    learn.add_argument(
        "--reference", default=None,
        help="drift: which source is the baseline (default: BACKTEST)",
    )
    learn.add_argument(
        "--roles", default=None,
        help="comma-separated subset of backtest,paper,live (default: all present)",
    )
    learn.add_argument(
        "--by", default=None,
        help="comma-separated axes to slice on (default: the declared axis set)",
    )
    learn.add_argument(
        "--metric",
        default=None,
        help=(
            "outcome column the buckets are ranked on; defaults to the column "
            "the book actually records (net_pnl where any trade has rupees, "
            "otherwise return_pct)"
        ),
    )

    learn.add_argument("--min-sample", type=int, default=None, help="override the sample floor")
    learn.add_argument(
        "--window-days", type=int, default=90,
        help="history window the daily report compares against",
    )
    learn.add_argument("--rows", type=int, default=15, help="max buckets printed per axis")
    learn.add_argument("--no-write", action="store_true", help="do not persist the snapshot parquet")
    learn.add_argument("--json", dest="json_path", help="write the structured result to this file")

    # ------------------------------------------------------------------
    # Authoring. Unlike ``learn``, this one writes — to ``strategies`` and
    # ``strategy_versions`` only. It cannot place an order, start a deployment or
    # change a risk limit; those stay behind Strategy -> Risk -> OMS -> venue.
    st = sub.add_parser(
        "strategy",
        help="create strategies and their immutable versions",
    )
    st.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "show", "create", "version", "validate", "seed"],
    )
    # `atr strategy show <id>` is what people actually type. Without this the
    # natural form dies in argparse with "unrecognized arguments", which reads
    # as a broken command rather than a missing flag.
    st.add_argument(
        "target", nargs="?",
        help="the strategy id, for show/version/validate (same as --strategy-id)",
    )
    st.add_argument("--user", default=None, help="account id (default: the only/owner account)")
    st.add_argument("--name", default=None, help="strategy name, for create/seed")
    st.add_argument(
        "--kind", default="rules", choices=["nocode", "rules", "code", "options"],
        help="how the strategy is defined (default: rules)",
    )
    st.add_argument("--description", default=None)
    st.add_argument("--engine-key", default=None, help="registry key this version wraps")
    st.add_argument("--strategy-id", default=None, help="the strategy to act on")
    st.add_argument(
        "--definition", default=None,
        help="path to a JSON file holding the definition (version/validate)",
    )
    st.add_argument("--version", type=int, default=None, help="version number, for show/validate")
    st.add_argument("--change-note", default=None)
    st.add_argument(
        "--force", action="store_true",
        help="store a definition that fails structural validation",
    )
    st.add_argument("--json", dest="json_path", help="write the structured result to this file")

    # ------------------------------------------------------------------
    dev = sub.add_parser("dev", help="development: FastAPI (reload) + Vite dev server")
    dev.add_argument("--no-browser", action="store_true", help="don't open the dashboard on start")
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


def _run_backup(args) -> int:
    from atr.config.settings import get_settings
    from atr.infra.backup import backup_now
    from atr.market_intel.service import DATA_ROOT

    settings = get_settings()
    print(backup_now(DATA_ROOT, settings.backup_dir, keep=settings.backup_keep))
    return 0


def _run_history(args) -> int:
    from datetime import date

    import pandas as pd

    from atr.data.history import load_cached, sync_all
    from atr.scanner import score_frame

    if args.action == "refresh-eod":
        # No broker session needed: tops up the tracked names from public daily bars.
        from atr.data.eod_refresh import refresh
        from atr.market_intel.service import DATA_ROOT, get_market_intel_service

        print(refresh(DATA_ROOT, get_market_intel_service().get_universe_symbols()))
        return 0

    if args.action == "sync":
        sync_all(args.exchange, args.interval, args.from_date, args.to_date,
                 workers=args.workers, start=args.start, end=args.end)
        # The Nifty 50 itself is not a tradable stock, so it has its own series.
        from atr.data.indices import sync_index
        from atr.market_intel.service import DATA_ROOT
        from atr.services.broker_access import authed_client

        print("nifty 50 index:", sync_index(authed_client(), DATA_ROOT))
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


def _run_learn(args) -> int:
    """Read-only inspection of what the trade history actually supports.

    Every branch here is a read. The command cannot change a strategy, a
    deployment, an order or a risk limit, and it has no flag that would let it
    try — which is the whole point of keeping the learning engine on the far
    side of the execution pipeline.
    """
    import json as _json

    from atr.research import learning_axes as axes_mod
    from atr.research import learning_stats as stats
    from atr.services.learning import get_learning_service

    svc = get_learning_service()

    def _payload(obj) -> dict:
        return obj if isinstance(obj, dict) else obj.as_dict()

    if args.action == "status":
        payload = svc.status()
        if args.json_path:
            Path(args.json_path).write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
        if not payload.get("available"):
            print(f"learning unavailable — {payload.get('reason')}")
            return 1
        counts = payload["counts"]
        print(f"backtest_runs      {counts['backtest_runs']} ({counts['completed_runs']} completed)")
        print(f"backtest_trades    {counts['backtest_trades']}")
        journal_forward = counts.get("trade_journal_forward", 0)
        journal_note = f" ({journal_forward} forward)" if counts["trade_journal"] else ""
        print(f"trade_journal      {counts['trade_journal']}{journal_note}")
        if counts.get("paper_ledger_trades"):
            forward = counts.get("paper_ledger_forward_trades", 0)
            print(
                f"paper ledger       {counts['paper_ledger_trades']} settled "
                f"({forward} forward, {counts['paper_ledger_trades'] - forward} in-sample)"
            )
        # Two figures, because they answer two questions. "Is there enough to
        # analyse?" and "may anything be claimed?" are not the same number, and
        # a book of four hundred backfilled rows clears the first and fails the
        # second.
        forward_available = payload.get("forward_available", 0)
        print(f"usable trades      {payload['trades_available']}")
        print(f"forward evidence   {forward_available}")
        print(f"sample floor       {stats.MIN_SAMPLE} (MIN_SAMPLE)")
        print(
            "claimable          "
            + ("yes" if payload.get("sufficient_for_a_claim") else "no — below the floor")
        )
        # The note is always shown when it says something the counts do not: a
        # book that clears the sample floor while holding no forward trade is
        # exactly the state that reads as "we have plenty of data".
        note = payload.get("note") or ""
        if note and (not payload["sufficient_for_analysis"] or "forward" in note):
            print(f"note               {note}")
        if payload["missing_features"]:
            print("\nrequested but unavailable in this build:")
            for name, why in payload["missing_features"].items():
                print(f"  - {name}: {why}")
        return 0

    roles = tuple(r.strip() for r in args.roles.split(",") if r.strip()) if args.roles else None
    wanted = tuple(a.strip() for a in args.by.split(",") if a.strip()) if args.by else None
    if wanted:
        unknown = [a for a in wanted if a not in axes_mod.AXES_BY_NAME]
        if unknown:
            logger.error(
                "unknown axis/axes {!r}; available: {}",
                unknown, ", ".join(sorted(axes_mod.AXES_BY_NAME)),
            )
            return 2

    dataset = svc.dataset(refresh=True)

    if args.action == "dataset":
        summary = dataset.summary()
        if args.json_path:
            Path(args.json_path).write_text(_json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"rows               {summary['trades']} ({summary['closed_trades']} closed, "
              f"{summary['open_trades']} open)")
        print(f"strategies         {', '.join(summary['strategies']) or '(none)'}")
        print(f"symbols            {summary['symbols']}")
        for source, count in summary["sources"].items():
            print(f"  {source:<16} {count}")
        grades = summary.get("evidence_grades") or {}
        if grades:
            print(
                f"evidence           {grades.get('forward', 0)} forward, "
                f"{grades.get('in_sample', 0)} in-sample"
            )
        # The date, not just the count. "We hold two forward trades" and "the
        # most recent one closed eleven months ago" are different findings about
        # whether anything is still being measured, and the count alone cannot
        # tell them apart.
        latest = summary.get("latest_forward_ts")
        if latest:
            print(f"latest forward     {str(latest)[:10]}")
        elif summary["trades"]:
            print("latest forward     none — no observation is graded forward")
        coverage = summary.get("metric_coverage") or {}
        if coverage:
            print(
                "outcome columns    "
                + ", ".join(f"{name} {count}" for name, count in coverage.items())
            )
        ledger = summary.get("paper_ledger") or {}
        if ledger.get("present"):
            print(f"paper ledger       {ledger.get('note')}")
        if summary["date_range"]["first"]:
            print(f"window             {summary['date_range']['first']} → {summary['date_range']['last']}")
        if summary["net_pnl_total"] is not None:
            print(f"net pnl total      {summary['net_pnl_total']:,.2f}")
        if summary["missing_features"]:
            print("\nmissing features (recorded, not filled in):")
            for name, why in summary["missing_features"].items():
                print(f"  - {name}: {why}")
        for warning in summary["warnings"]:
            print(f"warning            {warning}")
        return 0

    if args.action == "performance":
        analysis = svc.performance(
            strategy=args.strategy, roles=roles, axes=list(wanted) if wanted else None,
            metric=args.metric,
        )
        payload = analysis.as_dict()
        if args.json_path:
            Path(args.json_path).write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(analysis.text() if hasattr(analysis, "text") else _format_performance(analysis, args.rows))
        return 0

    if args.action == "drift":
        analysis = svc.drift(
            strategy=args.strategy, reference=args.reference,
            roles=roles, min_sample=args.min_sample,
        )
        if args.json_path:
            Path(args.json_path).write_text(
                _json.dumps(analysis.as_dict(), indent=2, default=str), encoding="utf-8"
            )
        print(analysis.headline)
        print()
        print(_format_drift(analysis, args.rows))
        return 0

    if args.action == "report":
        report = svc.daily_report(window_days=args.window_days)
        if args.json_path:
            Path(args.json_path).write_text(
                _json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8"
            )
        print(report.headline)
        print()
        print(_format_report(report, args.rows))
        return 0

    payload = svc.snapshot(write=not args.no_write)
    if args.json_path:
        Path(args.json_path).write_text(_json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(payload["report"]["headline"])
    written = payload.get("written") or {}
    if written:
        for kind, path in written.items():
            print(f"wrote {kind:<8} {path}")
    else:
        print("(not written — pass without --no-write to persist)")
    return 0


def _format_performance(analysis, limit: int) -> str:
    lines: list[str] = []
    lines.append(f"strategy   {analysis.strategy}")
    lines.append(f"metric     {analysis.metric}")
    lines.append(f"trades     {analysis.n}")

    overall = analysis.overall or {}
    if overall.get("n"):
        mean = overall.get("mean")
        low, high = overall.get("ci_low"), overall.get("ci_high")
        interval = f" [95% CI {(low or 0):,.1f} … {(high or 0):,.1f}]" if low is not None else ""
        lines.append(f"expectancy {'—' if mean is None else f'{mean:,.1f}'} per trade{interval}")
        if overall.get("win_rate") is not None:
            lines.append(f"win rate   {overall['win_rate']:.1%} of {overall['n']}")
        if overall.get("profit_factor") is not None:
            lines.append(f"PF         {overall['profit_factor']:.2f}")
        else:
            lines.append("PF         undefined — no losing trades in the sample")
    for caveat in analysis.caveats:
        lines.append(f"caveat     {caveat}")

    for breakdown in analysis.breakdowns:
        coverage = (
            f"{breakdown.rows_with_value}/{breakdown.rows_scanned}"
            if breakdown.rows_scanned
            else "0/0"
        )
        lines.append("")
        lines.append(f"--- by {breakdown.axis} ({breakdown.label}) — coverage {coverage} ---")
        if not breakdown.buckets:
            lines.append("    no row in the sample carries a value for this axis")
            continue
        lines.append(f"{'bucket':<26}{'n':>6}{'mean':>12}{'lift':>12}  {'significance':<16}")
        # ``lift`` is the difference between this bucket's mean and the baseline
        # mean — in the metric's own units, not a ratio. Rendering it as a
        # percentage would report a 2.8-rupee gap as "280%".
        percentage = analysis.metric == "return_pct"
        for bucket in breakdown.buckets[:limit]:
            stats = bucket.get("stats") or {}
            mean = stats.get("mean")
            lift = bucket.get("lift")
            lift_text = "—"
            if lift is not None:
                lift_text = f"{lift:+.2f}%" if percentage else f"{lift:+,.2f}"
            lines.append(
                f"{str(bucket.get('label')):<26}{bucket.get('n', 0):>6}"
                f"{'—' if mean is None else f'{mean:,.2f}':>12}"
                f"{lift_text:>12}  "
                f"{bucket.get('significance', ''):<16}"
            )
            if bucket.get("suppressed"):
                lines.append(f"{'':<26}suppressed — {bucket.get('note')}")
        if len(breakdown.buckets) > limit:
            lines.append(f"    … {len(breakdown.buckets) - limit} more bucket(s); raise --rows")

    if analysis.notable:
        lines.append("")
        lines.append("--- notable (survived both the sample floor and the correction) ---")
        for bucket in analysis.notable[:limit]:
            stats = bucket.get("stats") or {}
            lines.append(
                f"  [{bucket.get('significance')}] {bucket.get('axis')}="
                f"{bucket.get('label')} n={bucket.get('n')} "
                f"mean={stats.get('mean')} lift={bucket.get('lift')} "
                f"p={bucket.get('p_value')} (adjusted {bucket.get('p_adjusted')})"
            )
    else:
        lines.append("")
        lines.append("--- notable ---")
        lines.append("  none: no bucket separated from its baseline after correction")
    return "\n".join(lines)


def _format_drift(analysis, limit: int) -> str:
    lines: list[str] = []
    present = ", ".join(f"{k}={v}" for k, v in analysis.available_sources.items())
    lines.append(f"strategy   {analysis.strategy}")
    lines.append(f"sources    {present}")

    if not analysis.pairs:
        lines.append("")
        lines.append("no two sources hold trades, so no comparison could be formed")
    for pair in analysis.pairs:
        lines.append("")
        lines.append(
            f"--- {pair.reference} ({pair.reference_n}) vs "
            f"{pair.comparison} ({pair.comparison_n}) — {pair.status} ---"
        )
        if pair.status != "ok":
            for item in pair.limitations:
                lines.append(f"  {item}")
        lines.append(
            f"{'metric':<22}{'baseline':>11}{'live':>11}{'delta':>11}"
            f"{'relative':>10}  {'verdict':<26}"
        )
        for item in pair.metrics[:limit]:
            if item.status != "ok":
                lines.append(
                    f"{item.label:<22}{'—':>11}{'—':>11}{'—':>11}{'—':>10}  "
                    f"{item.status}"
                )
                if item.reason:
                    lines.append(f"{'':<22}{item.reason}")
                continue
            lines.append(
                f"{item.label:<22}"
                f"{item.reference_value:>11,.2f}{item.comparison_value:>11,.2f}"
                f"{item.delta:>+11,.2f}"
                f"{'—' if item.relative is None else f'{item.relative * 100:+.1f}%':>10}  "
                f"{item.direction + ' / ' + item.magnitude:<26}"
            )
        if len(pair.metrics) > limit:
            lines.append(f"    … {len(pair.metrics) - limit} more metric(s); raise --rows")

        if pair.regime.get("status") == "ok":
            lines.append("")
            lines.append(f"  regime mix: {pair.regime['summary']}")

    if analysis.findings:
        lines.append("")
        lines.append("--- findings (advisory — nothing is applied) ---")
        for item in analysis.findings:
            lines.append(f"  [{item.get('severity')}] {item.get('statement')}")
            if item.get("evidence"):
                lines.append(f"      evidence  {item['evidence']}")

    if analysis.limitations:
        lines.append("")
        lines.append("--- limitations ---")
        for item in analysis.limitations:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def _format_report(report, limit: int) -> str:
    lines: list[str] = []
    lines.append(f"as of        {report.as_of} (window {report.window_days}d)")
    lines.append(f"metric       {report.metric} ({report.aggregate})")
    if report.metric_note:
        lines.append(f"             {report.metric_note}")
    lines.append(f"today        {report.trades_today} trade(s)")
    if report.metric_total is not None:
        # The unit travels with the number: ``net_pnl`` is money, ``return_pct``
        # is a percentage, and a bare figure in front of a reader who cannot tell
        # which is the misread this line exists to prevent.
        unit = "%" if report.metric == "return_pct" else ""
        lines.append(f"{report.metric:<12} {report.metric_total:,.2f}{unit}")
    if report.net_pnl_today is not None:
        lines.append(f"net pnl      {report.net_pnl_today:,.2f}")

    expectation = report.expectation or {}
    if expectation.get("mean_per_day") is not None:
        lines.append(
            f"expected     {expectation['mean_per_day']:,.2f}/day "
            f"over {expectation.get('trading_days', '?')} trading day(s) with a trade"
        )
    deviation = report.deviation or {}
    if deviation.get("relative") is not None:
        lines.append(f"deviation    {deviation['relative'] * 100:+.1f}% vs that baseline")

    regime = report.regime or {}
    if regime.get("tied"):
        lines.append(f"regime       tied — {', '.join(regime['tied'])}")
    elif regime.get("label"):
        counts = ", ".join(f"{k}={v}" for k, v in (regime.get("counts") or {}).items())
        lines.append(f"regime       {regime['label']} ({counts})")

    for block, title in (
        (report.by_strategy, "by strategy"),
        (report.strongest, "strongest setups"),
        (report.weakest, "weakest setups"),
    ):
        if not block:
            continue
        lines.append("")
        lines.append(f"--- {title} ---")
        for row in block[:limit]:
            label = (
                row.get("strategy") or row.get("setup") or row.get("label")
                or row.get("regime") or row.get("symbol") or "?"
            )
            sample = row.get("n", row.get("trades", 0))
            value = row.get("net_pnl", row.get("mean", row.get("value")))
            shown = "—" if value is None else f"{value:,.2f}"
            lines.append(f"  {str(label):<28}{sample:>5}  {shown}")

    if report.unusual:
        lines.append("")
        lines.append("--- unusual ---")
        for item in report.unusual:
            lines.append(f"  {item}")

    if report.execution:
        lines.append("")
        lines.append("--- execution ---")
        for key, value in report.execution.items():
            lines.append(f"  {key:<24}{value}")

    if report.observations:
        lines.append("")
        lines.append("--- observations (advisory — nothing is applied) ---")
        for obs in report.observations:
            lines.append(f"  [{obs.get('severity')}] {obs.get('statement')}")
            lines.append(f"      evidence    {obs.get('evidence')}")
            lines.append(
                f"      confidence  {obs.get('confidence')} (n={obs.get('sample_size')})"
            )

    if report.drift:
        lines.append("")
        lines.append("--- drift ---")
        for key, value in report.drift.items():
            lines.append(f"  {key:<24}{value}")

    if report.limitations:
        lines.append("")
        lines.append("--- limitations ---")
        for item in report.limitations:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _web_dir() -> Path:
    return _project_root() / "web"


def _web_dist() -> Path:
    return _web_dir() / "dist"


def _frontend_built() -> bool:
    dist = _web_dist()
    return (dist / "index.html").exists() and (dist / "assets").is_dir()


def _build_frontend() -> None:
    """Run the Vite production build so the backend can serve the SPA."""
    import shutil
    import subprocess

    tool = shutil.which("bun") or shutil.which("npm") or shutil.which("npx")
    if not tool:
        logger.warning(
            "no bun/npm found on PATH — cannot build the frontend. "
            "Run `bun run build` in web/ yourself, or use `atr dev`."
        )
        return
    cmd = (
        [tool, "run", "build"]
        if tool.endswith(("bun", "bun.exe", "npm", "npm.exe"))
        else [tool, "vite", "build"]
    )
    logger.info("building frontend with {} …", tool)
    res = subprocess.run(cmd, cwd=str(_web_dir()))
    if res.returncode != 0:
        logger.error("frontend build failed — the dashboard will not be served")


def _open_browser(url: str) -> None:
    import webbrowser

    try:
        if not webbrowser.open(url):
            logger.info("dashboard at {}", url)
    except Exception:  # noqa: BLE001 — headless machines just get the URL
        logger.info("dashboard at {}", url)


def _session_ready() -> None:
    from atr.brokers.iifl.auth import SessionStore
    from atr.config.settings import get_settings

    settings = get_settings()
    session = SessionStore(settings.iifl_session_cache).load()
    if session:
        logger.info("IIFL session for {} valid until {}", session.client_id,
                    session.expires_at.strftime("%d-%b %H:%M IST"))
    else:
        logger.warning("no active IIFL session — log in from the dashboard when it opens")


def _run_serve(args) -> int:
    import uvicorn

    from atr.config.settings import get_settings

    settings = get_settings()
    host = args.host or settings.api_host
    port = args.port or settings.api_port

    if not args.no_build:
        if args.rebuild or not _frontend_built():
            _build_frontend()

    url = f"http://{host}:{port}"
    logger.info("starting ATR on {}", url)
    _session_ready()
    if not args.no_browser:
        _open_browser(url)
    uvicorn.run("atr.api.main:app", host=host, port=port, reload=False, log_level="info")
    return 0


def _run_dev(args) -> int:
    """Run the FastAPI backend (reload) and the Vite dev server together."""
    import os
    import signal
    import subprocess
    import time

    from atr.config.settings import get_settings

    settings = get_settings()
    host = settings.api_host
    port = settings.api_port
    if not _web_dir().exists():
        logger.error("web/ directory missing at {}", _web_dir())
        return 2

    children = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "atr.api.main:app",
             "--host", host, "--port", str(port), "--reload"],
            cwd=str(_project_root()),
        ),
        subprocess.Popen(
            ["bun", "run", "dev"],
            cwd=str(_web_dir()),
            env={**os.environ, "VITE_API_URL": f"http://127.0.0.1:{port}"},
        ),
    ]

    url = "http://localhost:5173"
    logger.info("dashboard (dev) at {}", url)
    if not args.no_browser:
        _open_browser(url)

    def _terminate(*_: object) -> None:
        for p in children:
            if p.poll() is None:
                p.terminate()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)
    try:
        while all(p.poll() is None for p in children):
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for p in children:
            if p.poll() is None:
                p.kill()
    return 0


def _resolve_cli_user(explicit: str | None) -> str:
    """Which account a CLI write acts as.

    Refuses to guess when there is more than one. Picking "the first row of
    ``users``" is how one account ends up authoring another's strategies — the
    same bug the backtest options endpoint had, where a listing resolved to the
    wrong user and showed strategies that were not the caller's.
    """
    from sqlalchemy import select

    from atr.appdb.engine import get_app_db
    from atr.appdb.schema import users

    with get_app_db().session() as session:
        if explicit:
            row = session.execute(
                select(users.c.user_id, users.c.username).where(users.c.user_id == explicit)
            ).first()
            if row is None:
                raise SystemExit(f"no account with id {explicit!r}")
            return explicit
        rows = session.execute(select(users.c.user_id, users.c.username)).all()
    if not rows:
        raise SystemExit("no accounts exist yet — start the server and bootstrap one")
    if len(rows) > 1:
        names = ", ".join(f"{r.username}={r.user_id}" for r in rows)
        raise SystemExit(f"more than one account ({names}); pass --user")
    return rows[0].user_id


def _run_strategy(args) -> int:
    """Create strategies and their immutable versions from the command line.

    The same service the HTTP routes use, so a strategy authored here is
    byte-identical to one authored from the UI — including the validation that
    refuses a definition the trading loop would later refuse.
    """
    import json as _json

    from atr.services.strategies import StrategyError, StrategyService

    service = StrategyService()
    user_id = _resolve_cli_user(args.user)
    # Both spellings, one value. `show <id>` and `show --strategy-id <id>` are
    # the same request; accepting only one of them is a trap for the caller.
    strategy_id = args.strategy_id or getattr(args, "target", None)

    def _emit(payload: dict) -> None:
        if args.json_path:
            Path(args.json_path).write_text(
                _json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )

    def _report(report: dict) -> None:
        status = "OK" if report["ok"] else "INVALID"
        print(f"validation: {status}")
        for err in report["errors"]:
            print(f"  error   {err['field']}: {err['message']}")
        for warn in report["warnings"]:
            print(f"  warning {warn['field']}: {warn['message']}")
        paper = report.get("paper") or {}
        print(f"  paper path:    {'deployable' if paper.get('resolvable') else 'NOT deployable'}"
              + (f" — {paper['reason']}" if paper.get("reason") else ""))
        backtest = report.get("backtest") or {}
        print(f"  backtest path: {'runnable' if backtest.get('resolvable') else 'NOT runnable'}"
              + (f" — {backtest['reason']}" if backtest.get("reason") else ""))
        # Said out loud on every validation, because a green tick next to a
        # strategy name is otherwise read as a finding.
        print(f"  {report['statistical_validation']['reason']}")

    def _load_definition() -> object:
        if not args.definition:
            raise SystemExit("--definition <file.json> is required")
        return _json.loads(Path(args.definition).read_text(encoding="utf-8"))

    try:
        if args.action == "seed":
            from atr.strategy.example import EXAMPLE_NAME

            result = service.seed_example(user_id, name=args.name or EXAMPLE_NAME)
            strategy, version = result["strategy"], result["version"]
            print(
                f"{'created' if result['created'] else 'already present'}: "
                f"{strategy['name']} ({strategy['strategy_id']}) "
                f"version {version['version']}"
            )
            if result["note"]:
                print(f"  {result['note']}")
            print(f"  deployable: {version['deployable']}")
            if version.get("validation"):
                _report(version["validation"])
            print(
                "\nnext: POST /api/v1/paper/deployments with "
                f'{{"strategy_id": "{strategy["strategy_id"]}", '
                f'"strategy_version": {version["version"]}, "capital": 500000}}'
            )
            _emit(result)
            return 0

        if args.action == "list":
            rows = service.list(user_id)
            if not rows:
                print("no strategies — `atr strategy seed` creates the worked example")
                return 0
            for row in rows:
                latest = row["latest_version"]
                state = "no versions" if latest is None else f"v{latest}"
                if latest is not None and not row["deployable"]:
                    state += " (not deployable)"
                print(f"{row['strategy_id']}  {state:<22} {row['name']}")
            _emit({"strategies": rows})
            return 0

        if args.action == "show":
            if not strategy_id:
                raise SystemExit("--strategy-id (or a positional id) is required")
            strategy = service.get(user_id, strategy_id)
            print(f"{strategy['name']}  ({strategy['strategy_id']}, {strategy['kind']})")
            if strategy.get("description"):
                print(f"  {strategy['description']}")
            for row in service.versions(user_id, strategy_id):
                note = f"  — {row['change_note']}" if row.get("change_note") else ""
                flag = "" if row["deployable"] else "  NOT deployable"
                print(f"  v{row['version']}  {row['created_at']}{flag}{note}")
                if not row["deployable"]:
                    print(f"      {row['not_deployable_reason']}")
            _emit(strategy)
            return 0

        if args.action == "create":
            if not args.name:
                raise SystemExit("--name is required")
            row = service.create(
                user_id,
                name=args.name,
                kind=args.kind,
                description=args.description,
                engine_key=args.engine_key,
            )
            print(f"created {row['name']} ({row['strategy_id']})")
            print("it has no version yet, so it cannot be deployed — add one with "
                  "`atr strategy version`")
            _emit(row)
            return 0

        if args.action == "version":
            if not strategy_id:
                raise SystemExit("--strategy-id (or a positional id) is required")
            row = service.create_version(
                user_id,
                strategy_id,
                definition=_load_definition(),
                change_note=args.change_note,
                force=args.force,
            )
            print(f"created version {row['version']} of {strategy_id}")
            print(f"  deployable: {row['deployable']}")
            if row.get("not_deployable_reason"):
                print(f"  {row['not_deployable_reason']}")
            _report(row["validation"])
            _emit(row)
            return 0

        # validate
        if not strategy_id:
            raise SystemExit("--strategy-id (or a positional id) is required")
        report = service.validate(
            user_id,
            strategy_id,
            version=args.version,
            definition=_load_definition() if args.definition else None,
        )
        where = "draft" if not report.get("stored") else f"stored v{report['version']}"
        print(f"validating {where} definition of {strategy_id}")
        _report(report)
        _emit(report)
        return 0 if report["ok"] else 1

    except StrategyError as exc:
        print(f"refused ({exc.code}): {exc}")
        return 1


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
        "backup": _run_backup,
        "alerts": _run_alerts,
        "brief": _run_brief,
        "learn": _run_learn,
        "strategy": _run_strategy,
        "live": _run_live,
        "serve": _run_serve,
        "dev": _run_dev,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
