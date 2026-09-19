"""End-to-end probe: Strategy -> Version -> Backtest -> Results.

Run with the real market-data cache but a throwaway database:

    ./.venv/Scripts/python.exe scripts/verify_backtest_workflow.py

It exercises the service the same way the HTTP routes do (submit -> poll ->
metrics -> curves -> monthly -> trades -> one trade), then re-submits the same
config to confirm the result is reproducible. Nothing here writes to the
operator's real database: APP_DB_URL points at a temp directory, which is the
same variable the test suite uses.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="atr-backtest-"))
os.environ["APP_DB_URL"] = f"sqlite:///{(TMP / 'app.db').as_posix()}"
os.environ["ATR_SECRET_KEY"] = "probe-key"
os.environ["ENV"] = "dev"

# Settings read the environment on first use, so the cache must be dropped after
# the variables are set. Forgetting this is what makes a "throwaway" script write
# into the real database.
from atr.config.settings import get_settings  # noqa: E402

get_settings.cache_clear()

from atr.appdb.engine import get_app_db, reset_app_db_cache  # noqa: E402
from atr.appdb.repositories import UserRepository  # noqa: E402

reset_app_db_cache()


def main() -> int:
    db = get_app_db()
    db.prepare()
    # Guard: this script must never touch the operator's real store. Compare
    # resolved paths, not raw strings — SQLite URLs normalise separators to
    # forward slashes on Windows, so a naive substring check gives a false alarm.
    resolved = Path(db.url.replace("sqlite:///", "")).resolve()
    if resolved.parent != TMP.resolve():
        print(f"REFUSING: probe database is not the temp one ({db.url})")
        return 1
    print(f"db: {db.url}")

    with db.session() as session:
        user = UserRepository.create(
            session,
            email="probe@local",
            username="probe",
            password_hash="x",
            role="owner",
        )
    user_id = user["user_id"]

    from atr.services.backtests import get_backtest_service, reset_backtest_service

    service = get_backtest_service()

    print("\n=== options (what the config form is built from) ===")
    options = service.options()
    print(f"  strategies: {len(options['strategies'])}")
    print(f"  universes:  {[u.get('name') for u in options['universes']][:6]}")
    print(f"  timeframe:  {[t['value'] for t in options['timeframes'] if t['available']]}")
    print(f"  costs:      {[c['value'] for c in options['cost_models']]}")
    print(f"  sizing:     {[m['value'] for m in options['sizing_modes']]}")

    payload = {
        "strategy": "sma_crossover",
        "symbols": ["RELIANCE", "TCS", "INFY"],
        "exchange": "NSEEQ",
        "params": {"fast": 10, "slow": 30},
        "start": "2025-11-01",
        "end": "2026-09-11",
        "initial_cash": 1_000_000.0,
        "sizing": {"mode": "fixed_fraction", "percent": 15.0},
        "stops": {
            "stop_loss_pct": 5.0,
            "take_profit_pct": 20.0,
            "trailing_stop_pct": 8.0,
        },
        "costs": {"model": "india_delivery", "slippage_bps": 5.0},
    }

    print("\n=== submit ===")
    submitted = service.submit(payload, user_id=user_id)
    run_id = submitted["run_id"]
    print(f"  run_id:   {run_id}")
    print(f"  status:   {submitted['status']}  (returned immediately)")
    print(f"  config fp:{submitted['fingerprint'][:16]}")

    print("\n=== poll ===")
    deadline = time.time() + 120
    status = service.run(run_id, user_id)
    seen = {status["status"]}
    while status["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
        if time.time() > deadline:
            print("  TIMED OUT")
            return 1
        time.sleep(0.2)
        status = service.run(run_id, user_id)
        seen.add(status["status"])
    print(f"  states seen: {sorted(seen)}")
    print(f"  final:       {status['status']}  progress={status['progress']}")
    if status["status"] != "COMPLETED":
        print(f"  error: {status['error']}")
        return 1

    print("\n=== metrics ===")
    metrics = service.metrics(run_id, user_id)["metrics"]
    for key in (
        "start_equity",
        "end_equity",
        "total_return_pct",
        "cagr_pct",
        "sharpe",
        "sortino",
        "max_drawdown_pct",
        "max_drawdown_days",
        "win_rate_pct",
        "profit_factor",
        "num_trades",
        "total_commission",
        "total_slippage",
        "exposure_pct",
    ):
        value = metrics.get(key)
        shown = f"{value:,.4f}" if isinstance(value, (int, float)) else value
        print(f"  {key:22} {shown}")
    for warning in metrics.get("warnings") or []:
        print(f"  ! {warning}")

    print("\n=== curves ===")
    curves = service.equity(run_id, user_id)
    print(
        f"  equity {len(curves['equity'])} pts | "
        f"drawdown {len(curves['drawdown'])} pts | "
        f"exposure {len(curves['exposure'])} pts"
    )
    if curves["equity"]:
        first, last = curves["equity"][0], curves["equity"][-1]
        print(f"  {first['ts'][:10]} {first['value']:,.2f} -> {last['ts'][:10]} {last['value']:,.2f}")
    if curves["drawdown"]:
        worst = min(curves["drawdown"], key=lambda p: p["value"])
        print(f"  deepest drawdown: {worst['value'] * 100:.2f}% on {worst['ts'][:10]}")

    print("\n=== monthly returns ===")
    monthly = service.monthly(run_id, user_id)
    for row in monthly["matrix"]:
        cells = " ".join(
            f"{'-':>7}" if v is None else f"{v:>6.2f}%" for v in row["months"]
        )
        total = row["year_total"]
        print(f"  {row['year']}  {cells}   year {total:+.2f}%" if total is not None else f"  {row['year']}  {cells}")

    print("\n=== trades ===")
    trades = service.trades(run_id, user_id, limit=8)
    print(f"  total: {trades['total']}")
    for trade in trades["trades"]:
        print(
            f"  #{trade['seq']:<3} {trade['symbol']:<9} {trade['direction']:<5} "
            f"qty={trade['quantity']:>7.0f} "
            f"{str(trade['entry_ts'])[:10]} -> {str(trade['exit_ts'])[:10]} "
            f"pnl={trade['net_pnl']:>10,.2f}  {trade['exit_reason']}"
        )

    if trades["trades"]:
        print("\n=== one trade, opened ===")
        seq = trades["trades"][0]["seq"]
        detail = service.trade(run_id, user_id, seq)
        for key in (
            "symbol",
            "direction",
            "quantity",
            "entry_ts",
            "entry_price",
            "exit_ts",
            "exit_price",
            "gross_pnl",
            "commission",
            "net_pnl",
            "return_pct",
            "duration_days",
            "exit_reason",
        ):
            print(f"  {key:15} {detail.get(key)}")
        print(f"  strategy        {detail['strategy']}")
        print(f"  exits           {detail['exits']}")
        print(f"  signal_reason   {detail['signal_reason']}")

    print("\n=== reproducibility ===")
    again = service.submit(payload, user_id=user_id)
    for _ in range(600):
        state = service.run(again["run_id"], user_id)
        if state["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.2)
    repeat = service.metrics(again["run_id"], user_id)["metrics"]
    same_fp = service.run(again["run_id"], user_id)["data_fingerprint"] == status["data_fingerprint"]
    print(f"  same config fingerprint: {again['fingerprint'] == submitted['fingerprint']}")
    print(f"  same data fingerprint:   {same_fp}")
    print(f"  same total return:       {repeat.get('total_return_pct') == metrics.get('total_return_pct')}")
    print(f"  same trade count:        {repeat.get('num_trades') == metrics.get('num_trades')}")

    print("\n=== history ===")
    history = service.history(user_id)
    print(f"  runs: {history['total']}")
    for row in history["runs"]:
        print(f"  {row['run_id'][:8]} {row['status']:<10} {row['strategy']:<16} trades={row['num_trades']}")

    print("\n=== error handling ===")
    for label, bad in (
        ("unknown strategy", {**payload, "strategy": "does_not_exist"}),
        ("bad stop", {**payload, "stops": {"stop_loss_pct": -3}}),
        ("empty range", {**payload, "start": "2030-01-01", "end": "2030-06-01"}),
        ("capital too low", {**payload, "initial_cash": 100}),
        ("bad timeframe", {**payload, "timeframe": "5m"}),
    ):
        try:
            result = service.submit(bad, user_id=user_id)
            for _ in range(600):
                state = service.run(result["run_id"], user_id)
                if state["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                time.sleep(0.2)
            print(f"  {label:18} -> {state['status']}: {state['error']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:18} -> rejected: {exc}")

    print("\n=== reading a missing run (404 path) ===")
    try:
        service.run("0" * 32, user_id)
        print("  UNEXPECTED: no error raised")
    except Exception as exc:  # noqa: BLE001
        print(f"  correctly refused: {exc}")

    reset_backtest_service()
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
