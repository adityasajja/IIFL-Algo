"""The search harness decides which strategies look real, so its accounting is load-bearing.

The bug these pin down: treating the weight frame as a target that holds forward
rebalances the book to target every session for free. A fixed-weight basket then
collects the rebalancing bonus while reporting no turnover, which flatters exactly
the static baskets a timing rule must beat.
"""

import numpy as np
import pandas as pd
import pytest

from atr.research.hunt import ETF_COSTS, STOCK_COSTS, Costs, run_weights


def _prices(**columns: list[float]) -> pd.DataFrame:
    n = len(next(iter(columns.values())))
    return pd.DataFrame(columns, index=pd.bdate_range("2020-01-01", periods=n))


def test_a_held_basket_drifts_instead_of_being_rebalanced_for_free():
    """One asset doubles while the other is flat: the book must end up lopsided."""
    prices = _prices(up=[100, 200, 400, 800], flat=[100, 100, 100, 100])
    weights = pd.DataFrame({"up": [0.5], "flat": [0.5]}, index=prices.index[:1])

    result = run_weights(prices, weights, ETF_COSTS)

    # Held from day 2 (the target is filled the session after it is set), the
    # basket earns the doubling on a share that grows: 50% -> 66.7% -> 80%.
    assert result.gross_returns.iloc[1] == pytest.approx(0.5)  # half of a +100% move
    assert result.gross_returns.iloc[2] == pytest.approx(2 / 3, rel=1e-6)
    # Rebalanced daily for free it would have earned exactly 0.5 every day.
    assert result.gross_returns.iloc[2] > 0.5


def test_holding_a_basket_steady_costs_money():
    """Rebalancing back to target pays for the distance the book has drifted."""
    prices = _prices(up=[100, 200, 200], flat=[100, 100, 100])
    every_day = pd.DataFrame({"up": [0.5] * 3, "flat": [0.5] * 3}, index=prices.index)

    result = run_weights(prices, every_day, ETF_COSTS)

    # Day 3 resets a book that drifted to 2/3-1/3, so it trades ~0.33 of itself.
    assert result.turnover.iloc[2] == pytest.approx(1 / 3, rel=1e-3)
    assert result.turnover.sum() > 1.0  # the initial purchase alone is 1.0


def test_a_single_asset_position_never_drifts_away_from_itself():
    """The benchmark must be untouched by drift accounting, or comparisons move."""
    prices = _prices(only=[100, 110, 121, 133.1])
    weights = pd.DataFrame({"only": [1.0]}, index=prices.index[:1])

    result = run_weights(prices, weights, ETF_COSTS)

    held = result.gross_returns.iloc[1:]
    assert held.to_numpy() == pytest.approx(np.repeat(0.1, 3))
    assert result.turnover.sum() == pytest.approx(1.0)  # bought once, never traded again


def test_a_signal_is_traded_the_session_after_it_is_known():
    """A target set on a close cannot capture that same close's move."""
    prices = _prices(x=[100, 150, 150])
    weights = pd.DataFrame({"x": [1.0]}, index=prices.index[:1])

    result = run_weights(prices, weights, ETF_COSTS)

    assert result.gross_returns.iloc[0] == pytest.approx(0.0)  # not yet holding
    assert result.gross_returns.iloc[1] == pytest.approx(0.5)
    # The net figure is lower by exactly the cost of buying in that session.
    assert result.returns.iloc[1] < result.gross_returns.iloc[1]


def test_cash_earns_nothing_and_is_not_charged():
    """An empty book neither gains nor pays: the safe leg is the caller's job."""
    prices = _prices(x=[100, 200, 400])
    weights = pd.DataFrame({"x": [0.0]}, index=prices.index[:1])

    result = run_weights(prices, weights, ETF_COSTS)

    assert result.returns.abs().sum() == pytest.approx(0.0)
    assert result.turnover.sum() == pytest.approx(0.0)


def test_shares_cost_far_more_to_trade_than_exchange_traded_funds():
    """The asymmetry the whole search rests on: 0.1% STT a side versus 0.001%."""
    statutory = {
        name: (c.one_way(1_000_000, sell=False) + c.one_way(1_000_000, sell=True) - 2 * c.slippage * 1_000_000)
        for name, c in (("share", STOCK_COSTS), ("etf", ETF_COSTS))
    }
    assert statutory["share"] / statutory["etf"] > 5
    assert STOCK_COSTS.round_trip_pct() > 3 * ETF_COSTS.round_trip_pct() / 2


def test_turnover_is_charged_on_what_actually_changed_hands():
    """Rotating fully out of one holding into another is one round trip, not two."""
    costs = Costs(name="t", stt_buy=0.0, stt_sell=0.0, stamp_buy=0.0, exchange=0.0, sebi=0.0, gst=0.0,
                  brokerage_pct=0.0, brokerage_cap=0.0, slippage=0.001)
    # Sell 100% of A and buy 100% of B: turnover 2.0, but only 1.0 of notional
    # is sold and 1.0 bought, so the charge is one round trip of slippage.
    assert costs.turnover_cost(2.0, 1_000_000) == pytest.approx(2 * 0.001 * 1_000_000)


def test_sharpe_subtracts_the_rate_you_could_have_earned_risk_free():
    """Otherwise a rule that sits in cash scores well for taking no risk at all."""
    from atr.research.hunt import RISK_FREE

    prices = _prices(x=[100.0 * (1.0004**i) for i in range(600)])
    weights = pd.DataFrame({"x": [1.0]}, index=prices.index[:1])

    m = run_weights(prices, weights, ETF_COSTS).metrics()

    assert m["sharpe"] < m["return_vol_ratio"]  # the raw ratio is the flattering one
    expected = (m["cagr_pct"] / 100 - RISK_FREE) / (m["ann_vol_pct"] / 100)
    assert m["sharpe"] == pytest.approx(expected, rel=1e-2)
