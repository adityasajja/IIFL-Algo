"""Market regime classification API endpoint powered by Convai's Laya model."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from atr.strategy.regime_laya import get_regime_classifier

router = APIRouter(prefix="/api/v1/regime", tags=["Market Regime"])


class RegimeEvaluationRequest(BaseModel):
    symbol: str = Field(default="NIFTY50", description="Asset or index identifier")
    atr_pct: float = Field(default=1.5, description="Average True Range as a % of close price")
    adx: float = Field(default=25.0, description="ADX trend strength (0-100)")
    rsi: float = Field(default=55.0, description="RSI momentum (0-100)")
    return_20d: float = Field(default=2.5, description="20-period return %")
    sma_ratio: float = Field(default=1.015, description="Fast SMA to Slow SMA ratio")


class RegimeEvaluationResponse(BaseModel):
    symbol: str
    regime: str
    confidence: float
    model: str
    recommended_strategy_types: list[str]


@router.post("/evaluate", response_model=RegimeEvaluationResponse)
def evaluate_market_regime(req: RegimeEvaluationRequest) -> RegimeEvaluationResponse:
    """Classifies current market indicators into a discrete market regime."""
    classifier = get_regime_classifier()
    verdict = classifier.classify_market_context(
        symbol=req.symbol,
        atr_pct=req.atr_pct,
        adx=req.adx,
        rsi=req.rsi,
        return_20d=req.return_20d,
        sma_ratio=req.sma_ratio,
    )

    strategy_map = {
        "TRENDING_BULLISH": ["MomentumBreakout", "SmaCrossover", "JegadeeshTitmanMomentum"],
        "TRENDING_BEARISH": ["CashPreservation", "ShortHorizonReversal"],
        "MEAN_REVERTING": ["AvellanedaLeeMeanReversion", "SehgalLowVolAnomaly"],
        "HIGH_VOLATILITY": ["VolatilityBreakout", "VolatilityManaged"],
    }

    return RegimeEvaluationResponse(
        symbol=req.symbol,
        regime=verdict.regime,
        confidence=verdict.confidence,
        model=classifier.model_name,
        recommended_strategy_types=strategy_map.get(verdict.regime, []),
    )
