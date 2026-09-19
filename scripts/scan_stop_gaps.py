"""Scan every stored backtest trade for a stop that closed past its level.

The frontend asserts that a stop is a *trigger*, not a guaranteed price — the
engine fills on the next open, so a gap can close a trade well past the level.
That claim is only worth making if it happened in this project's own data, so
this script measures it rather than assuming it.

Read-only. Prints the worst overshoots and the count of trades affected.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "src")
os.environ.setdefault(
    "APP_DB_URL", f"sqlite:///{(Path(__file__).resolve().parents[1] / 'data' / 'app.db').as_posix()}"
)

from sqlalchemy import select  # noqa: E402

from atr.appdb.engine import AppDatabase  # noqa: E402
from atr.appdb.schema import backtest_runs, backtest_trades  # noqa: E402


def main() -> int:
    db = AppDatabase()
    with db.session_factory() as session:
        # The stop percentage is a property of the *run's* config, not of the
        # trade row — a trade records what happened, the run records the rules
        # it happened under. So this joins rather than reads it off the fill.
        rows = session.execute(
            select(
                backtest_trades.c.symbol,
                backtest_trades.c.exit_reason,
                backtest_trades.c.return_pct,
                backtest_runs.c.config,
            ).join(backtest_runs, backtest_trades.c.run_id == backtest_runs.c.run_id)
        ).all()

    print(f"trades in db: {len(rows)}")

    stopped: list[tuple[float, float, str]] = []
    for symbol, reason, ret, config in rows:
        if reason != "stop_loss" or ret is None:
            continue
        stop = (config or {}).get("stops", {}).get("stop_loss_pct")
        if stop is None:
            continue
        stopped.append((float(ret), float(stop), str(symbol)))

    print(f"stop_loss exits: {len(stopped)}")
    if not stopped:
        print("nothing to measure yet")
        return 0

    stopped.sort()
    print("\nworst overshoots (realised return past a negative stop):")
    for ret, stop, symbol in stopped[:10]:
        overshoot = -stop - ret
        verdict = "GAPPED" if overshoot > 0.5 else "held"
        print(
            f"  {symbol:<10} stop={stop:>5.2f}%  realised={ret:>8.3f}%  "
            f"overshoot={overshoot:>7.3f}pp  {verdict}"
        )

    gapped = [t for t in stopped if -t[1] - t[0] > 0.5]
    print(f"\ngapped through stop (>0.5pp past): {len(gapped)} of {len(stopped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
