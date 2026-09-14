"""Execution and risk management.

This package is a compute layer: it defines the order state machine
(:mod:`atr.execution.oms`) and the pre-trade risk controls
(:mod:`atr.execution.risk`), and it may not import ``atr.appdb``. The
transaction-owning use case that persists transitions is
:class:`atr.services.orders.OrderService`.
"""

from atr.execution.oms import (
    EVENT_SOURCES,
    ORDER_STATES,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    LimitsRiskGate,
    OrderDraft,
    RiskDecision,
    RiskGate,
    assert_transition,
    can_transition,
    elapsed_ms,
    fill_status,
    is_terminal,
    next_states,
    slippage_bps,
)
from atr.execution.risk import RiskEngine, RiskLimits, RiskVerdict

__all__ = [
    "EVENT_SOURCES",
    "ORDER_STATES",
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "InvalidTransition",
    "LimitsRiskGate",
    "OrderDraft",
    "RiskDecision",
    "RiskEngine",
    "RiskGate",
    "RiskLimits",
    "RiskVerdict",
    "assert_transition",
    "can_transition",
    "elapsed_ms",
    "fill_status",
    "is_terminal",
    "next_states",
    "slippage_bps",
]
