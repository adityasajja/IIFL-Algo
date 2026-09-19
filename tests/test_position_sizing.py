"""Tests for Position Sizing & Risk Budgeting Engine.

Verifies:
1. All 6 sizing methods (fixed quantity, fixed rupee value, % capital, % available capital, risk per trade, ATR sizing).
2. Stop-loss aware sizing and zero/invalid stop distance.
3. ATR volatility sizing and multiplier scaling.
4. Quantity rounding rules (FLOOR, ROUND, CEIL).
5. Downstream limits and multi-tier capping hierarchy (strategy limits, stock exposure, portfolio exposure, cash).
6. Deterministic output and exact equivalence across backtest and paper paths.
7. Downstream pipeline verification:
   SIGNAL → POSITION SIZE → STRATEGY RISK → PORTFOLIO RISK → OMS
   and proving sizing can NEVER bypass downstream gates.
"""

import pytest

from atr.execution.risk import RiskEngine, RiskLimits, Side
from atr.services.portfolio import (
    BookPosition,
    DeploymentSlice,
    PortfolioBook,
    PortfolioPolicy,
    evaluate_order,
)
from atr.strategy.sizing import (
    PositionSizingEngine,
    RoundingRule,
    SizingConfig,
    SizingMethod,
    SizingResult,
)


def test_fixed_quantity_sizing():
    cfg = SizingConfig(
        method=SizingMethod.FIXED_QUANTITY,
        fixed_quantity=75,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
    )
    assert res.raw_quantity == 75.0
    assert res.final_quantity == 75
    assert res.position_value == 75_000.0
    assert res.portfolio_impact_pct == 15.0
    assert res.capped_by is None


def test_fixed_rupee_value_sizing():
    cfg = SizingConfig(
        method=SizingMethod.FIXED_RUPEE_VALUE,
        fixed_rupee_value=150_000.0,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
    )
    assert res.raw_quantity == 150.0
    assert res.final_quantity == 150
    assert res.position_value == 150_000.0
    assert res.portfolio_impact_pct == 30.0


def test_percent_of_capital_sizing():
    cfg = SizingConfig(
        method=SizingMethod.PERCENT_OF_CAPITAL,
        capital_fraction=0.20,  # 20%
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=500.0,
        capital=500_000.0,
        available_capital=500_000.0,
    )
    # 20% of 500k = 100k; 100k / 500 = 200 shares
    assert res.raw_quantity == 200.0
    assert res.final_quantity == 200
    assert res.position_value == 100_000.0
    assert res.portfolio_impact_pct == 20.0


def test_percent_of_available_capital_sizing():
    cfg = SizingConfig(
        method=SizingMethod.PERCENT_OF_AVAILABLE_CAPITAL,
        capital_fraction=0.50,  # 50% of available
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=200_000.0,  # 200k available
    )
    # 50% of 200k = 100k; 100k / 1000 = 100 shares
    assert res.raw_quantity == 100.0
    assert res.final_quantity == 100
    assert res.position_value == 100_000.0


def test_risk_per_trade_with_stop_loss():
    # Capital: 5,00,000
    # Risk: 1% -> 5,000
    # Entry: 1,000
    # Stop: 950 -> Risk/share: 50
    # Qty: 5,000 / 50 = 100 shares
    cfg = SizingConfig(
        method=SizingMethod.RISK_PER_TRADE,
        risk_per_trade_pct=1.0,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
        stop_price=950.0,
    )
    assert res.risk_amount == 5000.0
    assert res.risk_per_share == 50.0
    assert res.stop_distance == 50.0
    assert res.raw_quantity == 100.0
    assert res.final_quantity == 100
    assert res.position_value == 100_000.0
    assert res.portfolio_impact_pct == 20.0


def test_atr_volatility_sizing():
    # Capital = 5,00,000
    # Risk = 1% -> 5,000
    # ATR = 20
    # ATR multiplier = 2 -> Risk/share = 40
    # Qty = 5,000 / 40 = 125 shares
    cfg = SizingConfig(
        method=SizingMethod.ATR_VOLATILITY_SIZING,
        risk_per_trade_pct=1.0,
        atr_multiplier=2.0,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
        atr=20.0,
    )
    assert res.risk_amount == 5000.0
    assert res.risk_per_share == 40.0
    assert res.raw_quantity == 125.0
    assert res.final_quantity == 125
    assert res.position_value == 125_000.0


def test_zero_or_invalid_stop_distance():
    cfg = SizingConfig(
        method=SizingMethod.RISK_PER_TRADE,
        risk_per_trade_pct=1.0,
    )
    # Stop price equals entry price -> zero risk per share
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
        stop_price=1000.0,
    )
    assert res.final_quantity == 0
    assert res.rejection_reason is not None
    assert "stop distance" in res.rejection_reason.lower()


def test_rounding_rules():
    # 5,000 / 33 = 151.5151...
    cfg_floor = SizingConfig(
        method=SizingMethod.FIXED_RUPEE_VALUE,
        fixed_rupee_value=5000.0,
        rounding_rule=RoundingRule.FLOOR,
    )
    res_floor = PositionSizingEngine.calculate(
        cfg_floor,
        entry_price=33.0,
        capital=100_000.0,
    )
    assert res_floor.final_quantity == 151

    cfg_round = SizingConfig(
        method=SizingMethod.FIXED_RUPEE_VALUE,
        fixed_rupee_value=5000.0,
        rounding_rule=RoundingRule.ROUND,
    )
    res_round = PositionSizingEngine.calculate(
        cfg_round,
        entry_price=33.0,
        capital=100_000.0,
    )
    assert res_round.final_quantity == 152

    cfg_ceil = SizingConfig(
        method=SizingMethod.FIXED_RUPEE_VALUE,
        fixed_rupee_value=5000.0,
        rounding_rule=RoundingRule.CEIL,
    )
    res_ceil = PositionSizingEngine.calculate(
        cfg_ceil,
        entry_price=33.0,
        capital=100_000.0,
    )
    assert res_ceil.final_quantity == 152


def test_capping_hierarchy_stock_exposure():
    # Raw quantity: 180 (from 180k value)
    # Strategy max position value: 150k -> 150 shares
    # Stock exposure limit: 120k -> 120 shares
    # Final quantity should be 120, capped by Stock Exposure Limit
    cfg = SizingConfig(
        method=SizingMethod.FIXED_RUPEE_VALUE,
        fixed_rupee_value=180_000.0,
        max_position_value=150_000.0,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
        max_stock_exposure=120_000.0,
    )
    assert res.raw_quantity == 180.0
    assert res.final_quantity == 120
    assert res.capped_by == "Stock Exposure Limit"
    assert "Strategy Max Position Value" in res.cap_reasons
    assert "Stock Exposure Limit" in res.cap_reasons


def test_capping_by_available_capital():
    # Capital allocated 500k, but available cash only 50k
    cfg = SizingConfig(
        method=SizingMethod.PERCENT_OF_CAPITAL,
        capital_fraction=0.30,  # wants 150k (150 shares)
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=50_000.0,  # only 50k cash (with 2% fee buffer = 49k -> 49 shares)
    )
    assert res.raw_quantity == 150.0
    assert res.final_quantity == 49
    assert res.capped_by == "Available Capital"


def test_equivalence_across_backtest_and_paper():
    from atr.backtest.sizing import SizingPlan

    # Both must return the exact same 100 shares for identical inputs
    plan = SizingPlan(
        mode="fixed_fraction",
        fraction=0.20,
    )
    backtest_qty = plan.target_quantity(equity=500_000.0, price=1000.0)

    cfg = SizingConfig(
        method=SizingMethod.PERCENT_OF_CAPITAL,
        capital_fraction=0.20,
    )
    res = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
    )
    assert backtest_qty == float(res.final_quantity) == 100.0


def test_pipeline_cannot_bypass_risk_engine_or_portfolio_gate():
    """Prove SIGNAL → POSITION SIZE → STRATEGY RISK → PORTFOLIO RISK → OMS

    Even if sizing calculates 100 shares, downstream gates remain authoritative
    and can reject or cap the order.
    """
    cfg = SizingConfig(
        method=SizingMethod.FIXED_QUANTITY,
        fixed_quantity=100,
    )
    sized = PositionSizingEngine.calculate(
        cfg,
        entry_price=1000.0,
        capital=500_000.0,
        available_capital=500_000.0,
    )
    assert sized.final_quantity == 100

    # 1. Downstream Strategy Risk Engine check
    # Let's say RiskEngine has max_order_notional = 50,000 (100 shares * 1000 = 100,000 exceeds it)
    from atr.core.models import Instrument, Order
    inst = Instrument(symbol="INFY", multiplier=1.0)
    order = Order(
        instrument=inst,
        side=Side.BUY,
        quantity=float(sized.final_quantity),
        limit_price=1000.0,
    )

    class MockPortfolio:
        equity = 500_000.0
        gross_exposure = 0.0
        positions = {}
        def position(self, s):
            class P:
                quantity = 0.0
                last_price = 1000.0
            return P()

    limits = RiskLimits(max_order_notional=50_000.0)
    risk_engine = RiskEngine(limits=limits)
    verdict = risk_engine.check_order(order, MockPortfolio())
    assert not verdict.allowed
    assert "notional" in verdict.reason

    # 2. Downstream Portfolio Risk Gate check
    # Let's say portfolio policy has max_stock_exposure = 60,000
    policy = PortfolioPolicy(max_stock_exposure=60_000.0)
    book = PortfolioBook(
        deployments=[
            DeploymentSlice(
                deployment_id="dep-1",
                strategy_id="strat-1",
                strategy_version=1,
                mode="PAPER",
                status="RUNNING",
                capital=500_000.0,
            )
        ]
    )

    class Draft:
        symbol = "INFY"
        side = "BUY"
        quantity = float(sized.final_quantity)  # 100 shares * 1000 = 100k > 60k
        deployment_id = "dep-1"
        strategy_id = "strat-1"

    decision = evaluate_order(
        Draft(),
        book=book,
        policy=policy,
        price=1000.0,
    )
    assert not decision.allowed
    assert "INFY exposure 100,000 would exceed 60,000" in decision.reason
