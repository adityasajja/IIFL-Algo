from atr.services.forward_check import MIN_FORWARD_TRADES, _stats, verdict


def _s(trades, win, avg, pf=1.5):
    return {"trades": trades, "win_rate_pct": win, "avg_trade_pct": avg, "profit_factor": pf}


def test_no_backtest_and_too_early_are_not_verdicts():
    assert verdict(_s(0, None, None), _s(50, 60, 1))[0] == "no_backtest"
    code, msg = verdict(_s(100, 55, 1.0), _s(5, 80, 3.0))
    assert code == "too_early" and str(MIN_FORWARD_TRADES - 5) in msg


def test_holding_up_vs_weaker():
    bt = _s(100, 55, 1.0)
    assert verdict(bt, _s(30, 52, 0.8))[0] == "holding_up"
    assert verdict(bt, _s(30, 52, 0.2))[0] == "weaker"  # lost most of the edge
    assert verdict(bt, _s(30, 30, 0.9))[0] == "weaker"  # wins far less often
    assert verdict(bt, _s(30, 55, -0.3))[0] == "weaker"


def test_stats_math():
    st = _stats([2.0, -1.0, 3.0, -1.0], [20, -10, 30, -10])
    assert st == {"trades": 4, "win_rate_pct": 50.0, "avg_trade_pct": 0.75, "profit_factor": 2.5}
    assert _stats([], [])["trades"] == 0
