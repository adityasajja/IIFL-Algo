"""Position Sizing & Risk Budgeting engine for cash-equity strategies.

The enforced signal-to-execution pipeline is:

    SIGNAL
    → POSITION SIZE
    → STRATEGY RISK
    → PORTFOLIO RISK
    → OMS

This module owns deterministic position sizing and risk budgeting calculations
across Backtest, Paper, and Live execution paths. It does NOT make autonomous
optimization decisions, dynamic capital reallocations, or AI sizing choices.
Downstream RiskEngine and PortfolioRiskGate remain strictly authoritative.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class SizingMethod(str, Enum):
    """Supported position sizing methods."""

    FIXED_QUANTITY = "FIXED_QUANTITY"
    FIXED_RUPEE_VALUE = "FIXED_RUPEE_VALUE"
    PERCENT_OF_CAPITAL = "PERCENT_OF_CAPITAL"
    PERCENT_OF_AVAILABLE_CAPITAL = "PERCENT_OF_AVAILABLE_CAPITAL"
    RISK_PER_TRADE = "RISK_PER_TRADE"
    ATR_VOLATILITY_SIZING = "ATR_VOLATILITY_SIZING"


class RoundingRule(str, Enum):
    """Quantity rounding behaviors."""

    FLOOR = "FLOOR"  # Default: round down to avoid breaching risk/capital
    ROUND = "ROUND"  # Nearest integer
    CEIL = "CEIL"    # Round up


@dataclass(frozen=True)
class SizingConfig:
    """Strategy or deployment position sizing configuration."""

    method: SizingMethod = SizingMethod.PERCENT_OF_CAPITAL
    capital_allocation: float | None = None
    risk_per_trade_pct: float | None = 1.0  # 1.0 = 1%
    risk_per_trade_rupees: float | None = None
    fixed_quantity: float | None = 100.0
    fixed_rupee_value: float | None = None
    capital_fraction: float | None = 0.10  # 0.10 = 10%
    max_quantity: float | None = None
    max_position_value: float | None = None
    max_portfolio_exposure_pct: float | None = None  # 0.20 = 20%
    min_quantity: float | None = 1.0
    rounding_rule: RoundingRule = RoundingRule.FLOOR
    atr_multiplier: float = 2.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SizingConfig:
        if not data or not isinstance(data, dict):
            return cls()

        method_str = str(data.get("method") or SizingMethod.PERCENT_OF_CAPITAL.value).upper()
        try:
            method = SizingMethod(method_str)
        except ValueError:
            # Fallback for common alias forms
            if method_str in ("FIXED_FRACTION", "PERCENT_OF_EQUITY"):
                method = SizingMethod.PERCENT_OF_CAPITAL
            elif method_str == "EQUAL_WEIGHT":
                method = SizingMethod.PERCENT_OF_AVAILABLE_CAPITAL
            else:
                method = SizingMethod.PERCENT_OF_CAPITAL

        rounding_str = str(data.get("rounding_rule") or RoundingRule.FLOOR.value).upper()
        try:
            rounding = RoundingRule(rounding_str)
        except ValueError:
            rounding = RoundingRule.FLOOR

        def _flt(k: str) -> float | None:
            v = data.get(k)
            if v is None:
                return None
            try:
                val = float(v)
                return val if math.isfinite(val) else None
            except (TypeError, ValueError):
                return None

        return cls(
            method=method,
            capital_allocation=_flt("capital_allocation"),
            risk_per_trade_pct=_flt("risk_per_trade_pct") if data.get("risk_per_trade_pct") is not None else 1.0,
            risk_per_trade_rupees=_flt("risk_per_trade_rupees"),
            fixed_quantity=_flt("fixed_quantity") if data.get("fixed_quantity") is not None else 100.0,
            fixed_rupee_value=_flt("fixed_rupee_value"),
            capital_fraction=_flt("capital_fraction") if data.get("capital_fraction") is not None else 0.10,
            max_quantity=_flt("max_quantity"),
            max_position_value=_flt("max_position_value"),
            max_portfolio_exposure_pct=_flt("max_portfolio_exposure_pct"),
            min_quantity=_flt("min_quantity") if data.get("min_quantity") is not None else 1.0,
            rounding_rule=rounding,
            atr_multiplier=_flt("atr_multiplier") or 2.0,
        )

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["method"] = self.method.value
        d["rounding_rule"] = self.rounding_rule.value
        return d


@dataclass(frozen=True)
class SizingResult:
    """The outcome of position sizing calculation and budgeting preview."""

    method: str
    raw_quantity: float
    final_quantity: int
    entry_price: float
    stop_price: float | None
    risk_per_share: float | None
    risk_amount: float | None
    stop_distance: float | None
    atr: float | None
    atr_multiplier: float | None
    capital_allocated: float
    capital_available: float
    position_value: float
    portfolio_impact_pct: float
    sector_impact_pct: float | None
    remaining_capital: float
    capped_by: str | None
    cap_reasons: list[str]
    rejection_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PositionSizingEngine:
    """Deterministic, unified Position Sizing Engine.

    Shared across Backtest, Paper, and Live order pathways.
    """

    @classmethod
    def calculate(
        cls,
        config: SizingConfig,
        *,
        entry_price: float,
        capital: float,
        available_capital: float | None = None,
        stop_price: float | None = None,
        stop_loss_pct: float | None = None,
        atr: float | None = None,
        current_stock_exposure: float = 0.0,
        max_stock_exposure: float | None = None,
        current_portfolio_exposure: float = 0.0,
        max_total_portfolio_exposure: float | None = None,
        current_sector_exposure: float = 0.0,
        max_sector_exposure: float | None = None,
    ) -> SizingResult:
        """Calculate position size, enforce caps, and construct preview metrics."""
        cap_allocated = max(float(capital or 0.0), 0.0)
        cap_avail = (
            max(float(available_capital), 0.0)
            if available_capital is not None
            else cap_allocated
        )

        if entry_price <= 0 or not math.isfinite(entry_price):
            return cls._empty_result(
                config,
                entry_price,
                cap_allocated,
                cap_avail,
                rejection_reason="Invalid or non-positive entry price",
            )

        # 1. Resolve risk per share and stop loss
        risk_per_share: float | None = None
        stop_distance: float | None = None
        resolved_stop_price: float | None = stop_price

        if resolved_stop_price is not None and resolved_stop_price > 0:
            stop_distance = abs(entry_price - resolved_stop_price)
            if stop_distance > 0:
                risk_per_share = stop_distance
        elif stop_loss_pct is not None and stop_loss_pct > 0:
            # stop_loss_pct given as percentage (e.g. 2.0 = 2%)
            fraction = stop_loss_pct / 100.0 if stop_loss_pct > 1.0 else stop_loss_pct
            risk_per_share = entry_price * fraction
            stop_distance = risk_per_share
            resolved_stop_price = max(entry_price - risk_per_share, 0.0)

        # 2. Calculate raw quantity based on configured method
        raw_qty: float = 0.0
        calculated_risk_amount: float | None = None
        rejection_reason: str | None = None

        method = config.method

        if method == SizingMethod.FIXED_QUANTITY:
            raw_qty = float(config.fixed_quantity or 0.0)

        elif method == SizingMethod.FIXED_RUPEE_VALUE:
            val = float(config.fixed_rupee_value or 0.0)
            raw_qty = (val / entry_price) if val > 0 else 0.0

        elif method == SizingMethod.PERCENT_OF_CAPITAL:
            frac = config.capital_fraction or 0.10
            notional = cap_allocated * frac
            raw_qty = (notional / entry_price) if notional > 0 else 0.0

        elif method == SizingMethod.PERCENT_OF_AVAILABLE_CAPITAL:
            frac = config.capital_fraction or 0.10
            notional = cap_avail * frac
            raw_qty = (notional / entry_price) if notional > 0 else 0.0

        elif method == SizingMethod.RISK_PER_TRADE:
            # Risk amount
            if config.risk_per_trade_rupees is not None and config.risk_per_trade_rupees > 0:
                calculated_risk_amount = config.risk_per_trade_rupees
            else:
                pct = config.risk_per_trade_pct or 1.0
                frac = (pct / 100.0) if pct > 0 else 0.01
                calculated_risk_amount = cap_allocated * frac

            if risk_per_share is None or risk_per_share <= 0:
                rejection_reason = "Zero or invalid stop distance for RISK_PER_TRADE sizing"
                raw_qty = 0.0
            else:
                raw_qty = calculated_risk_amount / risk_per_share

        elif method == SizingMethod.ATR_VOLATILITY_SIZING:
            # Risk amount
            if config.risk_per_trade_rupees is not None and config.risk_per_trade_rupees > 0:
                calculated_risk_amount = config.risk_per_trade_rupees
            else:
                pct = config.risk_per_trade_pct or 1.0
                frac = (pct / 100.0) if pct > 0 else 0.01
                calculated_risk_amount = cap_allocated * frac

            atr_val = atr if (atr is not None and atr > 0) else None
            mult = config.atr_multiplier if (config.atr_multiplier and config.atr_multiplier > 0) else 2.0
            if atr_val is None:
                rejection_reason = "ATR value missing or non-positive for ATR_VOLATILITY_SIZING"
                raw_qty = 0.0
            else:
                risk_per_share = atr_val * mult
                stop_distance = risk_per_share
                if resolved_stop_price is None:
                    resolved_stop_price = max(entry_price - risk_per_share, 0.0)
                raw_qty = calculated_risk_amount / risk_per_share

        if raw_qty <= 0:
            return cls._empty_result(
                config,
                entry_price,
                cap_allocated,
                cap_avail,
                raw_quantity=raw_qty,
                stop_price=resolved_stop_price,
                risk_per_share=risk_per_share,
                risk_amount=calculated_risk_amount,
                stop_distance=stop_distance,
                atr=atr,
                rejection_reason=rejection_reason or "Calculated raw quantity is zero",
            )

        # 3. Apply capping hierarchy
        final_qty_float = raw_qty
        capped_by: str | None = None
        cap_reasons: list[str] = []

        # (max_quantity_value, label) — each cap is applied in order
        _caps: list[tuple[float | None, str]] = [
            ((cap_avail * 0.98) / entry_price if cap_avail > 0 else 0.0, "Available Capital"),
            (config.max_position_value / entry_price if config.max_position_value else None, "Strategy Max Position Value"),
            (config.max_quantity if config.max_quantity else None, "Strategy Max Quantity"),
            (
                (cap_allocated * (config.max_portfolio_exposure_pct if config.max_portfolio_exposure_pct <= 1.0 else config.max_portfolio_exposure_pct / 100.0)) / entry_price
                if config.max_portfolio_exposure_pct and config.max_portfolio_exposure_pct > 0
                else None,
                "Strategy Max Portfolio Exposure",
            ),
            (
                max(max_stock_exposure - current_stock_exposure, 0.0) / entry_price
                if max_stock_exposure and max_stock_exposure > 0
                else None,
                "Stock Exposure Limit",
            ),
            (
                max(max_total_portfolio_exposure - current_portfolio_exposure, 0.0) / entry_price
                if max_total_portfolio_exposure and max_total_portfolio_exposure > 0
                else None,
                "Portfolio Exposure Limit",
            ),
            (
                max(max_sector_exposure - current_sector_exposure, 0.0) / entry_price
                if max_sector_exposure and max_sector_exposure > 0
                else None,
                "Sector Exposure Limit",
            ),
        ]

        for cap_value, label in _caps:
            if cap_value is not None and cap_value < final_qty_float:
                final_qty_float = cap_value
                capped_by = label
                cap_reasons.append(label)

        # 4. Rounding
        final_int: int = 0
        if config.rounding_rule == RoundingRule.ROUND:
            final_int = round(final_qty_float)
        elif config.rounding_rule == RoundingRule.CEIL:
            final_int = math.ceil(final_qty_float)
        else:
            final_int = math.floor(final_qty_float)

        final_int = max(final_int, 0)

        # 5. Enforce minimum quantity
        min_q = int(config.min_quantity or 1)
        if final_int < min_q:
            if final_int > 0:
                cap_reasons.append("Below Minimum Quantity")
                rejection_reason = f"Final quantity ({final_int}) below minimum ({min_q})"
            final_int = 0

        # Derived metrics
        pos_value = final_int * entry_price
        port_impact_pct = (pos_value / cap_allocated * 100.0) if cap_allocated > 0 else 0.0
        sector_impact_pct = (
            ((current_sector_exposure + pos_value) / cap_allocated * 100.0)
            if cap_allocated > 0 and current_sector_exposure > 0
            else None
        )
        remaining_cap = max(cap_avail - pos_value, 0.0)

        return SizingResult(
            method=method.value,
            raw_quantity=round(raw_qty, 2),
            final_quantity=final_int,
            entry_price=round(entry_price, 2),
            stop_price=round(resolved_stop_price, 2) if resolved_stop_price is not None else None,
            risk_per_share=round(risk_per_share, 2) if risk_per_share is not None else None,
            risk_amount=round(calculated_risk_amount, 2) if calculated_risk_amount is not None else None,
            stop_distance=round(stop_distance, 2) if stop_distance is not None else None,
            atr=round(atr, 2) if atr is not None else None,
            atr_multiplier=config.atr_multiplier,
            capital_allocated=round(cap_allocated, 2),
            capital_available=round(cap_avail, 2),
            position_value=round(pos_value, 2),
            portfolio_impact_pct=round(port_impact_pct, 2),
            sector_impact_pct=round(sector_impact_pct, 2) if sector_impact_pct is not None else None,
            remaining_capital=round(remaining_cap, 2),
            capped_by=capped_by,
            cap_reasons=cap_reasons,
            rejection_reason=rejection_reason,
        )

    @classmethod
    def _empty_result(
        cls,
        config: SizingConfig,
        entry_price: float,
        capital_allocated: float,
        capital_available: float,
        raw_quantity: float = 0.0,
        stop_price: float | None = None,
        risk_per_share: float | None = None,
        risk_amount: float | None = None,
        stop_distance: float | None = None,
        atr: float | None = None,
        rejection_reason: str | None = None,
    ) -> SizingResult:
        return SizingResult(
            method=config.method.value,
            raw_quantity=round(raw_quantity, 2),
            final_quantity=0,
            entry_price=round(entry_price, 2) if entry_price > 0 else 0.0,
            stop_price=round(stop_price, 2) if stop_price is not None else None,
            risk_per_share=round(risk_per_share, 2) if risk_per_share is not None else None,
            risk_amount=round(risk_amount, 2) if risk_amount is not None else None,
            stop_distance=round(stop_distance, 2) if stop_distance is not None else None,
            atr=round(atr, 2) if atr is not None else None,
            atr_multiplier=config.atr_multiplier,
            capital_allocated=round(capital_allocated, 2),
            capital_available=round(capital_available, 2),
            position_value=0.0,
            portfolio_impact_pct=0.0,
            sector_impact_pct=None,
            remaining_capital=round(capital_available, 2),
            capped_by=None,
            cap_reasons=[],
            rejection_reason=rejection_reason,
        )
