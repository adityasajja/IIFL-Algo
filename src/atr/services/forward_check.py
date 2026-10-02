"""Is the strategy doing in paper trading what its backtest said it would?

Compared per trade, not per period, so a six-week paper run can be set beside a six-year backtest:
how often it wins, what an average trade returns, and how much it makes per rupee it loses. Too few
paper trades means "too early", never a verdict.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

#: Fewer closed paper trades than this and any comparison is noise.
MIN_FORWARD_TRADES = 20
#: Paper must keep at least this share of the backtest's average trade, and win within this many
#: points of its win rate, to count as "holding up".
KEEP_SHARE = 0.5
WIN_TOLERANCE_PTS = 15.0


def _stats(returns: list[float], pnls: list[float]) -> dict[str, Any]:
    n = len(returns)
    if n == 0:
        return {"trades": 0, "win_rate_pct": None, "avg_trade_pct": None, "profit_factor": None}
    wins = sum(1 for p in pnls if p > 0)
    gain = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    return {
        "trades": n,
        "win_rate_pct": round(wins / n * 100, 1),
        "avg_trade_pct": round(sum(returns) / n, 2),
        "profit_factor": round(gain / loss, 2) if loss > 1e-9 else None,
    }


def verdict(backtest: dict[str, Any], forward: dict[str, Any]) -> tuple[str, str]:
    """(code, plain sentence)."""
    if backtest["trades"] == 0:
        return "no_backtest", "No backtest to compare with."
    if forward["trades"] < MIN_FORWARD_TRADES:
        return "too_early", f"Too early: {forward['trades']} of {MIN_FORWARD_TRADES} trades."
    bt_avg, fw_avg = backtest["avg_trade_pct"], forward["avg_trade_pct"]
    win_gap = (backtest["win_rate_pct"] or 0) - (forward["win_rate_pct"] or 0)
    keeps_edge = fw_avg > 0 and (bt_avg <= 0 or fw_avg >= KEEP_SHARE * bt_avg)
    if keeps_edge and win_gap <= WIN_TOLERANCE_PTS:
        return "holding_up", "Matching the backtest."
    return "weaker", "Weaker than the backtest."


def compare(db: Any, user_id: str, strategy_id: str, version: int | None = None) -> dict[str, Any]:
    from atr.appdb.schema import backtest_runs, backtest_trades
    from atr.services.gating import _forward_rows

    with db.session() as session:
        stmt = select(backtest_runs).where(
            backtest_runs.c.user_id == user_id,
            backtest_runs.c.strategy_id == strategy_id,
            backtest_runs.c.status == "COMPLETED",
        )
        if version is not None:
            stmt = stmt.where(backtest_runs.c.strategy_version == version)
        run = session.execute(stmt.order_by(backtest_runs.c.created_at.desc()).limit(1)).mappings().first()
        bt_returns: list[float] = []
        bt_pnls: list[float] = []
        run_id = None
        if run is not None:
            run_id = run["run_id"]
            rows = session.execute(
                select(backtest_trades.c.return_pct, backtest_trades.c.net_pnl).where(
                    backtest_trades.c.run_id == run_id, backtest_trades.c.exit_ts.is_not(None)
                )
            ).all()
            for ret, pnl in rows:
                if ret is not None:
                    bt_returns.append(float(ret))
                    bt_pnls.append(float(pnl or 0.0))

    fw_returns: list[float] = []
    fw_pnls: list[float] = []
    for row in _forward_rows(strategy_id, version, user_id=user_id, db=db):
        if row.get("exit_ts") is None or row.get("net_pnl") is None:
            continue
        cost = float(row.get("entry_price") or 0.0) * float(row.get("quantity") or 0.0)
        if cost <= 0:
            continue
        fw_returns.append(float(row["net_pnl"]) / cost * 100.0)
        fw_pnls.append(float(row["net_pnl"]))

    backtest, forward = _stats(bt_returns, bt_pnls), _stats(fw_returns, fw_pnls)
    code, message = verdict(backtest, forward)
    return {
        "strategy_id": strategy_id,
        "version": version,
        "backtest": {**backtest, "run_id": run_id},
        "paper": forward,
        "verdict": code,
        "message": message,
        "min_paper_trades": MIN_FORWARD_TRADES,
    }
