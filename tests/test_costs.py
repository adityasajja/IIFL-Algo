"""Transaction cost model tests.

The IBKR-style default charges Rs 0.005/share with no statutory levy. On NSE
cash equity delivery the real bill is ~0.28% of turnover and almost all of it is
Securities Transaction Tax, which is ad valorem and charged on **both** legs.
Getting this wrong is the classic way a backtest looks profitable and live
trading does not, so the numbers are pinned here rather than trusted.
"""

from __future__ import annotations

import pytest

from atr.backtest.costs import CommissionModel, IndianDeliveryCosts, SlippageModel
from atr.core.enums import AssetClass, Side
from atr.core.models import Instrument

EQ = Instrument(symbol="TEST", asset_class=AssetClass.EQUITY, currency="INR")


def test_delivery_round_trip_costs_about_28_basis_points():
    """Rs 1,00,000 in and out. STT alone is 0.2% of turnover."""
    model = IndianDeliveryCosts()
    buy = model.compute(1_000, 100.0, EQ, side=Side.BUY)
    sell = model.compute(1_000, 100.0, EQ, side=Side.SELL)
    round_trip_pct = (buy + sell) / 100_000 * 100
    assert round_trip_pct == pytest.approx(0.283, abs=0.005)


def test_stt_dominates_every_other_charge():
    model = IndianDeliveryCosts()
    buy = model.compute(1_000, 100.0, EQ, side=Side.BUY)
    stt = model.stt_pct * 100_000
    # STT is 100 of the 142 total; nothing else is close.
    assert stt / buy > 0.65


def test_stamp_duty_is_buy_only_and_dp_is_sell_only():
    model = IndianDeliveryCosts()
    buy = model.compute(1_000, 100.0, EQ, side=Side.BUY)
    sell = model.compute(1_000, 100.0, EQ, side=Side.SELL)
    assert buy == pytest.approx(142.22, abs=0.02)
    assert sell == pytest.approx(140.72, abs=0.02)
    # The asymmetry is real but small next to STT, which is symmetric.
    assert buy != sell


def test_costs_scale_with_notional_not_share_count():
    """The old model was per-share, so the same rupee exposure cost wildly
    different amounts depending on the share price."""
    model = IndianDeliveryCosts()
    cheap = model.compute(1_000, 100.0, EQ, side=Side.BUY)
    expensive = model.compute(100, 1_000.0, EQ, side=Side.BUY)
    assert cheap == pytest.approx(expensive, rel=1e-9)


def test_ibkr_default_understates_indian_delivery_by_an_order_of_magnitude():
    """The number that motivated this model: ~28x too cheap."""
    notional = 100_000.0
    old = CommissionModel()
    new = IndianDeliveryCosts()
    old_round_trip = (
        old.compute(1_000, 100.0, EQ, side=Side.BUY)
        + old.compute(1_000, 100.0, EQ, side=Side.SELL)
    )
    new_round_trip = (
        new.compute(1_000, 100.0, EQ, side=Side.BUY)
        + new.compute(1_000, 100.0, EQ, side=Side.SELL)
    )
    assert new_round_trip / old_round_trip > 25


def test_turnover_is_what_makes_costs_dangerous():
    """Cost is proportional to turnover, so the trade count sets the bar.

    Per leg it is ~0.14% of the traded notional. A rule that trades 1,900 times
    and one that trades 20 times face the same rate but a 95x different bill —
    which is larger than most of the edges anyone was chasing. The account-level
    figure depends on position size, so it is measured on the real runs rather
    than assumed here.
    """
    model = IndianDeliveryCosts()
    per_leg = model.compute(1_000, 100.0, EQ, side=Side.BUY) / 100_000
    assert per_leg == pytest.approx(0.00142, abs=0.00002)

    heavy_legs = 1_900 * 2
    light_legs = 20 * 2
    assert heavy_legs / light_legs == pytest.approx(95.0)

    # Same rate, 95x the bill.
    assert heavy_legs * per_leg / (light_legs * per_leg) == pytest.approx(95.0)


def test_commission_model_still_ignores_side_by_default():
    """Backwards compatibility: existing callers pass no side."""
    model = CommissionModel()
    assert model.compute(1_000, 100.0, EQ) == model.compute(1_000, 100.0, EQ, side=Side.BUY)


def test_zero_notional_is_free_not_an_error():
    assert IndianDeliveryCosts().compute(0, 100.0, EQ, side=Side.BUY) == 0.0
    assert IndianDeliveryCosts().compute(1_000, 0.0, EQ, side=Side.BUY) == 0.0


def test_slippage_still_hurts_in_the_right_direction():
    model = SlippageModel(bps=10.0)
    assert model.apply(100.0, Side.BUY, EQ) > 100.0
    assert model.apply(100.0, Side.SELL, EQ) < 100.0
