"""Data models for the Context-Aware Signal Engine.

Design invariants
-----------------
* Every dataclass is frozen where immutability matters (config, snapshots).
* Raw numeric measurements and categorical labels are kept separate — the
  label is derived from the measurement, but the measurement is what is stored
  so a future version can reclassify without re-running the scan.
* ``SignalContext.score`` is the **sum of met criterion weights**. It is not
  a probability. The docstring on every score-emitting method must say so.
* ``SignalContextModelConfig.version`` is stored alongside every context
  object. Changing the config without bumping the version is forbidden by
  convention (no code prevents it, but tests verify it is a non-empty string).

Indian cash equities only — no F&O, no Greeks, no option chains.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class SignalContextClass(str, Enum):
    """Categorical label for the overall context quality of a signal.

    These labels describe *how many measurable contextual conditions were
    present*, not whether the trade is likely to be profitable. Statistical
    evidence linking context class to outcomes must come from the learning
    system using genuine forward observations.
    """

    STRONG_CONTEXT = "STRONG_CONTEXT"
    NEUTRAL_CONTEXT = "NEUTRAL_CONTEXT"
    WEAK_CONTEXT = "WEAK_CONTEXT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Versioned scoring configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringCriterion:
    """One scoreable dimension of the signal context.

    ``key`` is a stable machine-readable identifier. ``weight`` is the points
    awarded when the criterion is met. Weights sum to 100 across the default
    model so the total score is interpretable as a percentage, but this
    invariant is not enforced — a custom model may use a different total.
    """

    key: str
    label: str
    weight: int
    description: str


@dataclass(frozen=True)
class SignalContextModelConfig:
    """Configurable, versioned scoring specification.

    Bump ``version`` whenever weights, thresholds, or the set of criteria
    changes. The version is stored with every ``SignalContext`` produced by
    this config, so historical records remain self-describing.
    """

    version: str
    description: str

    # ── Scoring criteria (ordered; order is stable for UI rendering) ─────────
    criteria: tuple[ScoringCriterion, ...] = field(default_factory=tuple)

    # ── Classification thresholds (score ∈ [0, sum-of-weights]) ─────────────
    strong_min_score: int = 70       # score ≥ this → STRONG_CONTEXT
    neutral_min_score: int = 40      # score ≥ this → NEUTRAL_CONTEXT
    # score < neutral_min_score → WEAK_CONTEXT

    # ── Market thresholds ────────────────────────────────────────────────────
    bullish_trend_min_pct: float = 1.0      # benchmark trend% above this → bullish
    bearish_trend_max_pct: float = -1.0     # benchmark trend% below this → bearish
    breadth_strong_min_pct: float = 55.0    # % above EMA50 for breadth criterion
    volatility_elevated_ratio: float = 1.3  # ATR%/median ratio above this → elevated

    # ── Sector thresholds ────────────────────────────────────────────────────
    sector_strong_rs_pct: float = 0.0       # sector RS > this → strong sector

    # ── Stock thresholds ────────────────────────────────────────────────────
    stock_strong_rs_pct: float = 0.0        # stock RS > this vs NIFTY → outperforming
    rvol_confirmation_min: float = 1.5      # RVOL above this → volume confirmed

    def classify(self, score: int, has_insufficient_data: bool) -> SignalContextClass:
        if has_insufficient_data:
            return SignalContextClass.INSUFFICIENT_DATA
        if score >= self.strong_min_score:
            return SignalContextClass.STRONG_CONTEXT
        if score >= self.neutral_min_score:
            return SignalContextClass.NEUTRAL_CONTEXT
        return SignalContextClass.WEAK_CONTEXT

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["criteria"] = list(d["criteria"])  # tuple → list for JSON
        return d


# ── Default model v1.0.0 ─────────────────────────────────────────────────────
# Weights chosen to match the user specification:
#   Market trend     +20
#   Market breadth   +15
#   Strong sector    +20
#   Stock RS         +20
#   RVOL             +15
#   Trend confirm    +10
#   ─────────────────────
#   Total            100

DEFAULT_CONTEXT_MODEL_V1 = SignalContextModelConfig(
    version="v1.0.0",
    description=(
        "Initial signal context scoring model for Indian cash equities. "
        "Score represents measurable contextual conditions present at signal time. "
        "A score of 80/100 means 80% of predefined conditions were met — "
        "it is NOT a claim that the trade will be profitable."
    ),
    criteria=(
        ScoringCriterion(
            key="market_bullish_trend",
            label="NIFTY bullish trend",
            weight=20,
            description="Benchmark trend % ≥ bull threshold vs SMA50",
        ),
        ScoringCriterion(
            key="market_breadth",
            label="Broad market breadth",
            weight=15,
            description="% of NIFTY 500 stocks above EMA50 ≥ breadth threshold",
        ),
        ScoringCriterion(
            key="strong_sector",
            label="Strong sector",
            weight=20,
            description="Sector relative strength vs NIFTY 1M > 0%",
        ),
        ScoringCriterion(
            key="stock_rs",
            label="Stock outperforming NIFTY",
            weight=20,
            description="Stock RS vs NIFTY 20D > 0%",
        ),
        ScoringCriterion(
            key="rvol_confirmation",
            label="Volume confirmation (RVOL)",
            weight=15,
            description="Relative volume ≥ 1.5×",
        ),
        ScoringCriterion(
            key="trend_confirmation",
            label="Trend confirmation (price > SMA50)",
            weight=10,
            description="Stock close above SMA50 at signal time",
        ),
    ),
    strong_min_score=70,
    neutral_min_score=40,
    bullish_trend_min_pct=1.0,
    bearish_trend_max_pct=-1.0,
    breadth_strong_min_pct=55.0,
    volatility_elevated_ratio=1.3,
    sector_strong_rs_pct=0.0,
    stock_strong_rs_pct=0.0,
    rvol_confirmation_min=1.5,
)


# ---------------------------------------------------------------------------
# Context snapshots — raw measurements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketContextSnapshot:
    """Market-level measurements at signal time.

    All fields that could not be computed are ``None``; they are never
    estimated or fabricated. The benchmark provenance is always explicit.
    """

    regime: str | None                          # BULLISH_TREND / BEARISH_TREND / SIDEWAYS / etc.
    nifty_trend_pct: float | None               # benchmark % distance from SMA50
    breadth_above_ema50_pct: float | None       # % universe above EMA50
    breadth_above_ema20_pct: float | None
    volatility_atr_pct: float | None
    volatility_ratio: float | None              # current ATR / median ATR
    advance_decline_ratio: float | None
    benchmark_symbol: str                       # e.g. NIFTYBEES-EQ
    benchmark_is_proxy: bool
    regime_model_version: str
    as_of: str                                  # ISO timestamp


@dataclass(frozen=True)
class SectorContextSnapshot:
    """Sector-level measurements at signal time."""

    sector: str | None
    return_1d_pct: float | None
    return_1m_pct: float | None
    relative_strength_1m: float | None          # sector RS vs benchmark 1M
    breadth_above_ema50_pct: float | None
    volume_multiple: float | None
    trend: str | None                           # BULLISH / BEARISH / SIDEWAYS
    as_of: str


@dataclass(frozen=True)
class StockContextSnapshot:
    """Stock-level measurements at the signal entry bar.

    All indicators are computed point-in-time via ``_as_of()`` truncation —
    never forward-contaminated.
    """

    relative_strength_nifty_20d: float | None   # stock return - benchmark return, 20D
    relative_volume: float | None               # RVOL vs 20-session average
    atr_pct: float | None                       # ATR as % of close
    trend_pct: float | None                     # % distance from SMA50
    above_sma50: bool | None
    from_52w_high_pct: float | None             # negative = below 52W high
    from_52w_low_pct: float | None              # positive = above 52W low
    gap_pct: float | None                       # gap from prev close to open
    close: float | None
    as_of: str


# ---------------------------------------------------------------------------
# Score criterion evaluation result
# ---------------------------------------------------------------------------

@dataclass
class ScoreCriterion:
    """The evaluated result of a single scoring criterion.

    Returned in the ``score_breakdown`` list on every ``SignalContext``.
    ``met`` is False when the condition was not satisfied OR the required data
    was absent. ``value`` is the raw measurement used to evaluate the criterion,
    as a string for display (may be None).
    """

    key: str
    label: str
    weight: int
    met: bool
    points_awarded: int
    value: str | None       # human-readable measurement, e.g. "+3.2%" or "2.1×"
    reason: str | None      # why met or unmet, e.g. "NIFTY trend +1.8% ≥ threshold"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The assembled Signal Context
# ---------------------------------------------------------------------------

@dataclass
class SignalContext:
    """Complete context object for one strategy signal.

    The ``score`` is the sum of ``points_awarded`` across all criteria. It
    represents how many predefined contextual conditions were present. It is
    **not** a probability of profit. Statistical evidence linking score ranges
    to trade outcomes must be established by the learning system using genuine
    forward observations.

    The ``context_model_version`` stored here is the primary key for
    interpreting ``score`` and ``context_class``. If the model is updated and
    version is bumped, older records remain correctly labelled under their
    original model.
    """

    signal_id: str
    strategy_id: str | None
    strategy_version: int | None
    symbol: str
    action: str                         # BUY / SELL
    signal_source: str                  # LIVE / PAPER / BACKTEST
    signal_ts: str                      # ISO timestamp of signal firing

    context_model_version: str
    context_class: SignalContextClass
    context_score: int                  # 0 to sum(criterion.weight)
    max_possible_score: int             # sum(criterion.weight) for this model
    has_insufficient_data: bool

    market_context: MarketContextSnapshot
    sector_context: SectorContextSnapshot
    stock_context: StockContextSnapshot

    score_breakdown: list[ScoreCriterion]
    missing_fields: list[str]           # fields that were None / unavailable

    benchmark_provenance: dict[str, Any]  # from MarketContextSnapshot
    created_at: str                       # ISO timestamp of enrichment

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["context_class"] = self.context_class.value
        d["score_breakdown"] = [asdict(c) for c in self.score_breakdown]
        return d

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), default=str)
