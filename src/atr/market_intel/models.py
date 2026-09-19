"""Data models and configurable specifications for Indian Equity Market Intelligence.

Enforces:
1. Configurable, versioned regime models (never hardcoded truth).
2. Explicit benchmark provenance (never mislabeling ETF proxy as actual index).
3. Separation of raw measurements from categorical classifications.
4. Historical universe date-awareness and survivorship-bias flags.
5. Honest missing data representation (never estimating or fabricating India VIX).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Benchmark Provenance
# ---------------------------------------------------------------------------
BENCHMARK_KIND_ACTUAL_INDEX = "BENCHMARK_ACTUAL_INDEX"
BENCHMARK_KIND_ETF_PROXY = "BENCHMARK_ETF_PROXY"


@dataclass(frozen=True)
class BenchmarkProvenance:
    symbol: str
    display_name: str
    kind: str  # BENCHMARK_ACTUAL_INDEX or BENCHMARK_ETF_PROXY
    tracking_target: str  # e.g. "NIFTY 50" or "NIFTY 500"
    is_proxy: bool
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PROVENANCE_NIFTYBEES = BenchmarkProvenance(
    symbol="NIFTYBEES-EQ",
    display_name="Nippon India ETF Nifty BeES",
    kind=BENCHMARK_KIND_ETF_PROXY,
    tracking_target="NIFTY 50",
    is_proxy=True,
    notes="Tradable cash-market ETF proxy tracking NIFTY 50; not the official index level.",
)

PROVENANCE_MONIFTY500 = BenchmarkProvenance(
    symbol="MONIFTY500-EQ",
    display_name="Motilal Oswal Nifty 500 ETF",
    kind=BENCHMARK_KIND_ETF_PROXY,
    tracking_target="NIFTY 500",
    is_proxy=True,
    notes="Tradable cash-market ETF proxy tracking NIFTY 500; not the official index level.",
)

PROVENANCE_NIFTY50_INDEX = BenchmarkProvenance(
    symbol="NIFTY 50",
    display_name="NSE NIFTY 50 Index",
    kind=BENCHMARK_KIND_ACTUAL_INDEX,
    tracking_target="NIFTY 50",
    is_proxy=False,
    notes="Official cash-market index computed by NSE Indices.",
)


# ---------------------------------------------------------------------------
# Configurable, Versioned Regime Model
# ---------------------------------------------------------------------------
@dataclass
class RegimeModelConfig:
    """Configurable regime thresholds and lookback windows."""

    version: str = "v1.0.0"
    description: str = "Initial quantitative regime classification for Indian equities"

    # Trend thresholds (% distance from SMA)
    bull_min_trend_pct: float = 1.0
    bear_max_trend_pct: float = -1.0

    # Breadth thresholds (% of universe above EMA50)
    bull_min_breadth_pct: float = 55.0
    bear_max_breadth_pct: float = 45.0

    # Volatility thresholds
    high_vol_ratio: float = 1.35  # 10d ATR / 50d ATR
    high_vol_atr_pct: float = 2.0  # ATR14 / Close * 100
    low_vol_ratio: float = 0.80
    low_vol_atr_pct: float = 0.90

    # Lookback windows (trading sessions)
    trend_sma_window: int = 50
    breadth_ema_window: int = 50
    atr_window: int = 14
    vol_short_window: int = 10
    vol_long_window: int = 50

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_REGIME_CONFIG_V1 = RegimeModelConfig(
    version="v1.0.0",
    description="V1 quantitative regime model (trend SMA50, breadth EMA50, ATR ratio 10d/50d)",
)


# ---------------------------------------------------------------------------
# Raw Measurements & Classifications
# ---------------------------------------------------------------------------
@dataclass
class RawMarketMeasurements:
    """Explicit, unrounded numeric measurements before any label is assigned."""

    nifty_trend_pct: float | None
    breadth_above_ema50_pct: float | None
    breadth_above_ema20_pct: float | None
    breadth_above_sma200_pct: float | None
    advancing_stocks: int
    declining_stocks: int
    advance_decline_ratio: float | None
    atr_pct: float | None
    volatility_ratio: float | None
    sector_participation_pct: float | None
    benchmark_provenance: BenchmarkProvenance
    regime_model_version: str
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["benchmark_provenance"] = self.benchmark_provenance.as_dict()
        return d


@dataclass
class MarketRegimeClassification:
    """Regime classification derived deterministically from RawMarketMeasurements."""

    regime: str  # BULLISH_TREND, BEARISH_TREND, SIDEWAYS, HIGH_VOLATILITY, LOW_VOLATILITY
    label: str
    confidence: float
    raw_measurements: RawMarketMeasurements
    regime_model_version: str
    explanation: str
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "label": self.label,
            "confidence": self.confidence,
            "raw_measurements": self.raw_measurements.as_dict(),
            "regime_model_version": self.regime_model_version,
            "explanation": self.explanation,
            "as_of": self.as_of,
        }


# ---------------------------------------------------------------------------
# Sector & Stock Structures
# ---------------------------------------------------------------------------
@dataclass
class SectorMetrics:
    sector: str
    stock_count: int
    return_1d_pct: float
    return_1w_pct: float
    return_1m_pct: float
    relative_strength_1d: float  # Sector return - NIFTY return
    relative_strength_1m: float  # Sector 1m return - NIFTY 1m return
    advancing_count: int
    declining_count: int
    advance_decline_ratio: float
    volume_multiple: float  # Aggregated volume vs 20d avg
    breakout_count: int  # Stocks near 20d/52w high with RVOL >= 1.5x
    above_ema20_count: int
    above_ema20_pct: float
    above_ema50_count: int
    above_ema50_pct: float
    trend: str  # BULLISH, BEARISH, SIDEWAYS
    top_stocks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StockContext:
    symbol: str
    close: float
    change_1d_pct: float
    trend_pct: float  # Close vs SMA50 %
    relative_strength_nifty_20d: float  # Stock 20d ret - NIFTY 20d ret
    relative_volume: float  # Volume / 20d avg volume
    atr_pct: float  # ATR14 / close * 100
    from_52w_high_pct: float  # Distance below 52w high (e.g. -2.5%)
    from_52w_low_pct: float  # Distance above 52w low (e.g. +25.0%)
    gap_pct: float  # Open vs prev close %
    above_ema20: bool
    above_ema50: bool
    is_breakout: bool
    sector: str | None
    sector_relative_strength_1m: float | None
    market_breadth_pct: float | None
    benchmark_provenance: BenchmarkProvenance
    regime_model_version: str
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["benchmark_provenance"] = self.benchmark_provenance.as_dict()
        return d


@dataclass
class MarketSummary:
    as_of: str
    nifty_close: float
    nifty_change_1d_pct: float
    nifty_trend_pct: float
    nifty_above_ema20: bool
    nifty_above_ema50: bool
    nifty_1m_return_pct: float
    benchmark_provenance: BenchmarkProvenance
    regime: MarketRegimeClassification
    raw_measurements: RawMarketMeasurements
    total_stocks_analyzed: int
    advancing_stocks: int
    declining_stocks: int
    unchanged_stocks: int
    advance_decline_ratio: float
    breadth_above_ema20_pct: float
    breadth_above_ema50_pct: float
    breadth_above_sma200_pct: float
    highs_52w_count: int
    lows_52w_count: int
    market_volatility_atr_pct: float
    volatility_ratio: float
    market_trend_strength: float
    sector_participation_pct: float
    strongest_sectors: list[str]
    weakest_sectors: list[str]
    top_breakouts: list[dict[str, Any]]
    top_relative_strength_stocks: list[dict[str, Any]]
    universe_provenance: str = "NSE_EQUITY_UNIVERSE_STATIC_SNAPSHOT_2026"
    survivorship_safeguard: str = "Historical breadth checks prior to 2026 use snapshot constituents; do not treat as survivor-free."
    # ~6 months of the benchmark, with its 50/200-day averages, for the chart.
    benchmark_series: list[dict[str, Any]] = field(default_factory=list)
    # Share of stocks above their 50-day average, per day, for the last ~30 sessions.
    breadth_history: list[dict[str, Any]] = field(default_factory=list)
    # The latest trading day the stock data covers; can lag the benchmark's.
    stocks_as_of: str | None = None
    # The benchmark's 52-week range, for a where-are-we-in-the-range bar.
    nifty_52w_high: float | None = None
    nifty_52w_low: float | None = None
    # Sectors whose last week turned against their last month (top 3 each).
    sectors_turning_up: list[str] = field(default_factory=list)
    sectors_fading: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["benchmark_provenance"] = self.benchmark_provenance.as_dict()
        d["regime"] = self.regime.as_dict()
        d["raw_measurements"] = self.raw_measurements.as_dict()
        return d
