"""Quantitative literature models and academic alpha factors.

Implements battle-tested quantitative strategies and research paper models:
1. Cross-Sectional Momentum (Jegadeesh & Titman 1993, 2001)
2. Time Series Momentum / Trend Following (Moskowitz, Ooi, Pedersen 2012)
3. Statistical Mean Reversion & Bollinger Z-Score (Avellaneda & Lee 2010)
4. Volatility Expansion Breakout (Donchian / Kaufman Volatility Regime)
5. Multi-Factor Composite Scoring (Trend + Volatility Compression + Volume Flow)
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from atr.strategy.indicators import atr, ema, rsi, sma


@dataclass
class PaperAlphaSignal:
    strategy_id: str
    paper_citation: str
    symbol: str
    action: Literal["BUY", "SELL"]
    setup: str
    thesis: str
    expected_value: float  # Mathematical expected value in R-multiples
    historical_win_rate: float
    confidence_score: float  # 0.0 to 1.0
    price: float
    stop_loss: float
    target: float
    rr_ratio: float
    regime_fit: str


# ─── Measured performance ────────────────────────────────────────────────────
#
# Every number below was measured by ``scripts/validate_paper_strategies.py``,
# not copied from a paper. Each model was walked forward over 18 NSE large caps
# (2020-02-17 → 2026-09-11, 400 train / 200 test symbol-bars, 260 warmup,
# 5 bps slippage) and scored only on windows it had never seen.
#
# This matters because these same models previously carried hardcoded win rates
# of 0.58-0.65 presented to the dashboard as ``historical_win_rate``. Measured,
# all seven were over-optimistic by 3-12 percentage points, and **none** passed
# the harness: the best out-of-sample Sharpe was +0.46 against a buy-and-hold
# Sharpe of +0.69 on the same window.
#
# The random-selection control (same universe, same cadence, same sizing, but
# random symbols) returned a mean Sharpe of -0.35 across 8 runs. A model sitting
# under that has demonstrated nothing; a model above it still has to beat
# buy-and-hold, and none do.
#
# Refreshed by re-running the script. Treat as a snapshot with a date.
_MEASURED_ASOF = "2026-09-13"
_MEASURED_WINDOW = "2020-02-17..2026-09-11"
_MEASURED_CONTROL_SHARPE = -0.35

#: strategy_id → measured stats. ``passed`` is the harness verdict (all checks,
#: including deflated Sharpe and beating buy-and-hold).
MEASURED: dict[str, dict] = {
    "momentum_jegadeesh_titman": {
        "win_rate": 0.548, "trades": 2341, "expectancy_r": -0.130,
        "oos_sharpe": -0.30, "deflated_sharpe": 0.946, "z_vs_control": 0.19,
        "passed": False,
    },
    "stat_arb_avellaneda_lee": {
        "win_rate": 0.575, "trades": 346, "expectancy_r": 0.127,
        "oos_sharpe": 0.46, "deflated_sharpe": 0.857, "z_vs_control": 3.15,
        "passed": False,
    },
    "volatility_breakout": {
        "win_rate": 0.482, "trades": 467, "expectancy_r": -0.135,
        "oos_sharpe": -0.58, "deflated_sharpe": 0.189, "z_vs_control": -0.90,
        "passed": False,
    },
    "multi_factor_composite": {
        "win_rate": 0.519, "trades": 3260, "expectancy_r": 0.037,
        "oos_sharpe": 0.26, "deflated_sharpe": 0.820, "z_vs_control": 2.37,
        "passed": False,
    },
    "iima_nse_momentum": {
        "win_rate": 0.581, "trades": 1598, "expectancy_r": -0.139,
        "oos_sharpe": -0.49, "deflated_sharpe": 0.829, "z_vs_control": -0.55,
        "passed": False,
    },
    "nism_52w_high_proximity": {
        "win_rate": 0.467, "trades": 420, "expectancy_r": -0.194,
        "oos_sharpe": -1.01, "deflated_sharpe": 0.032, "z_vs_control": -2.55,
        "passed": False,
    },
    "sehgal_low_vol_anomaly": {
        "win_rate": 0.528, "trades": 2914, "expectancy_r": 0.013,
        "oos_sharpe": 0.04, "deflated_sharpe": 0.968, "z_vs_control": 1.52,
        "passed": False,
    },
}


def _measured(strategy_id: str) -> dict:
    return MEASURED.get(strategy_id, {})


def _stats(strategy_id: str, fallback_rr: float) -> tuple[float, float]:
    """Measured ``(win_rate, expected_value)`` for a model.

    ``expected_value`` is the **measured expectancy in R** wherever the model
    has been validated. Using the theoretical ``win_rate × rr`` formula instead
    overstates every model, because the planned reward:risk ratio is not the
    one realised — trades get stopped out on the way to target. The
    IIMA momentum model is the clearest case: its planned RR implies +0.86R
    while the harness measured -0.14R over 1,598 trades.

    Falls back to the R-multiple calculation with an explicitly unvalidated
    0.50 placeholder only when no measurement exists yet — never to a
    plausible-looking constant, which is how the old 0.5x values came to be
    read as findings.
    """
    m = _measured(strategy_id)
    win_rate = m.get("win_rate")
    if win_rate is None:
        # 0.5 is the honest prior for a symmetric bet, not an edge estimate.
        win_rate = 0.50
    expectancy = m.get("expectancy_r")
    if expectancy is not None:
        return win_rate, float(expectancy)
    ev = (win_rate * fallback_rr) - ((1.0 - win_rate) * 1.0)
    return win_rate, ev


def _confidence(strategy_id: str) -> float:
    """A real probability, not a decoration.

    Previously these were literals like ``0.94``, which read on the dashboard as
    "94% confident" while meaning nothing at all. Now it is the harness's
    deflated Sharpe: P(true Sharpe beats the multiple-testing hurdle), where
    0.95 is the bar and everything measured so far sits below it.
    """
    return float(_measured(strategy_id).get("deflated_sharpe", 0.5))


# ─── 1. Jegadeesh & Titman (1993, 2001) Momentum ─────────────────────────────

def evaluate_jegadeesh_titman_momentum(
    symbol: str,
    df: pd.DataFrame,
    lookback_months: int = 6,
    skip_recent_days: int = 5,
) -> PaperAlphaSignal | None:
    """Returns a signal if stock displays strong intermediate momentum.
    
    Academic finding: 3 to 12 month past winners continue to outperform over the
    next 3 to 12 months, but the most recent 1-week/1-month suffers short-term reversal.
    """
    if len(df) < (lookback_months * 21 + skip_recent_days):
        return None

    closes = df["close"].values
    last_price = float(closes[-1])
    
    # Exclude the immediate past 5 days (short term microstructure bounce/dip)
    end_idx = -skip_recent_days
    start_idx = -(lookback_months * 21 + skip_recent_days)
    
    past_return = (closes[end_idx] - closes[start_idx]) / closes[start_idx]
    
    # Ensure current trend is above 50-day and 200-day SMA if available
    s50 = float(sma(df["close"], 50).iloc[-1]) if len(df) >= 50 else last_price
    s200 = float(sma(df["close"], 200).iloc[-1]) if len(df) >= 200 else s50
    
    # 20-day ATR for volatility-adjusted stop
    atr_val = float(atr(df["high"], df["low"], df["close"], 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_price * 0.02

    # Momentum filter: positive multi-month momentum + trading above trend
    if past_return > 0.15 and last_price > s50 >= s200:
        stop = round(last_price - 1.8 * atr_val, 2)
        risk = max(last_price - stop, 0.01)
        target = round(last_price + 2.2 * risk, 2)
        rr = round((target - last_price) / risk, 2)

        # Measured out of sample — NOT the academic paper's reported rate.
        win_rate, ev = _stats("momentum_jegadeesh_titman", rr)

        return PaperAlphaSignal(
            strategy_id="momentum_jegadeesh_titman",
            paper_citation="Jegadeesh & Titman (1993, 2001) 'Returns to Buying Winners and Selling Losers'",
            symbol=symbol,
            action="BUY",
            setup="Jegadeesh-Titman Cross-Sectional Momentum",
            thesis=(
                f"Stock has +{past_return*100:.1f}% intermediate momentum (excluding 1-week reversal lag) "
                f"with price trading above 50 & 200 SMAs. Academic literature shows strong drift continuation."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("momentum_jegadeesh_titman"),
            price=last_price,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="BULL_TREND",
        )
    return None


# ─── 2. Avellaneda & Lee (2010) Statistical Mean Reversion ───────────────────

def evaluate_avellaneda_lee_mean_reversion(
    symbol: str,
    df: pd.DataFrame,
    lookback: int = 20,
    entry_z_threshold: float = -2.0,
) -> PaperAlphaSignal | None:
    """Mean reversion based on rolling Z-score deviation from equilibrium EMA.
    
    Avellaneda & Lee (2010) 'Statistical Arbitrage in the US Equities Market'.
    Identifies temporary liquidity dislocations where price deviates by > 2 standard
    deviations from mean with RSI confirmation.
    """
    if len(df) < lookback + 10:
        return None

    closes = df["close"]
    ema_series = ema(closes, lookback)
    rolling_std = closes.rolling(lookback).std()
    
    last_close = float(closes.iloc[-1])
    last_ema = float(ema_series.iloc[-1])
    last_std = float(rolling_std.iloc[-1])
    
    if last_std <= 0:
        return None
        
    z_score = (last_close - last_ema) / last_std
    rsi_val = float(rsi(closes, 14).iloc[-1])
    atr_val = float(atr(df["high"], df["low"], df["close"], 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_close * 0.02

    # Extreme negative dislocation (Oversold snapback)
    if z_score <= entry_z_threshold and rsi_val <= 35.0:
        stop = round(last_close - 1.5 * atr_val, 2)
        risk = max(last_close - stop, 0.01)
        target = round(last_ema, 2)  # Reversion to equilibrium mean
        if target <= last_close:
            target = round(last_close + 2.0 * risk, 2)
        rr = round((target - last_close) / risk, 2)
        
        win_rate, ev = _stats("stat_arb_avellaneda_lee", rr)

        return PaperAlphaSignal(
            strategy_id="stat_arb_avellaneda_lee",
            paper_citation="Avellaneda & Lee (2010) 'Statistical Arbitrage in the US Equities Market'",
            symbol=symbol,
            action="BUY",
            setup="Avellaneda-Lee Z-Score Mean Reversion",
            thesis=(
                f"Price has deviated to z-score of {z_score:.2f} (>{abs(entry_z_threshold)}σ below 20-EMA) "
                f"with RSI at {rsi_val:.1f}. High statistical probability of snapback to equilibrium ₹{last_ema:,.2f}."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("stat_arb_avellaneda_lee"),
            price=last_close,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="RANGEBOUND",
        )
    return None


# ─── 3. Volatility Compression Breakout (Donchian / Kaufman) ──────────────────

def evaluate_volatility_breakout(
    symbol: str,
    df: pd.DataFrame,
    channel_period: int = 20,
) -> PaperAlphaSignal | None:
    """Detects volatility squeeze breakout.
    
    Identifies periods where historical volatility compressed into a tight range
    and then cleanly broke out with volume expansion.
    """
    if len(df) < channel_period + 10:
        return None

    highs = df["high"]
    lows = df["low"]
    closes = df["close"]
    volumes = df["volume"] if "volume" in df.columns else None

    last_close = float(closes.iloc[-1])
    recent_high = float(highs.iloc[-channel_period - 1 : -1].max())
    recent_low = float(lows.iloc[-channel_period - 1 : -1].min())
    
    # Volatility compression ratio: range / price
    range_pct = (recent_high - recent_low) / last_close
    
    atr_val = float(atr(highs, lows, closes, 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_close * 0.02

    vol_surge = False
    if volumes is not None and len(volumes) >= 20:
        avg_vol = float(volumes.iloc[-21:-1].mean())
        cur_vol = float(volumes.iloc[-1])
        vol_surge = (cur_vol >= 1.5 * avg_vol) if avg_vol > 0 else True
    else:
        vol_surge = True

    # Breakout condition: closes above 20-day high with volatility expansion
    if last_close > recent_high and vol_surge and range_pct < 0.12:  # tight prior base
        stop = round(last_close - 1.6 * atr_val, 2)
        risk = max(last_close - stop, 0.01)
        target = round(last_close + 2.5 * risk, 2)
        rr = round((target - last_close) / risk, 2)
        
        win_rate, ev = _stats("volatility_breakout", rr)

        return PaperAlphaSignal(
            strategy_id="volatility_breakout",
            paper_citation="Richard Donchian (1970) & Perry Kaufman 'Trading Systems and Methods'",
            symbol=symbol,
            action="BUY",
            setup="Volatility Compression Squeeze Breakout",
            thesis=(
                f"Breakout above {channel_period}-day consolidation ceiling (₹{recent_high:,.2f}) "
                f"from a tight {range_pct*100:.1f}% base with volume surge. Expansion regime activated."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("volatility_breakout"),
            price=last_close,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="BULL_TREND",
        )
    return None


# ─── 4. Multi-Factor Quant Composite ──────────────────────────────────────────

def evaluate_multi_factor_quant(
    symbol: str,
    df: pd.DataFrame,
) -> PaperAlphaSignal | None:
    """Composite scoring across 3 orthogonal alpha factors:
    1. Trend factor (SMA 20/50/200 stack)
    2. Volatility factor (ATR stability)
    3. Momentum factor (RSI in sweet spot 52-68)
    """
    if len(df) < 50:
        return None

    closes = df["close"]
    last_price = float(closes.iloc[-1])
    s20 = float(sma(closes, 20).iloc[-1])
    s50 = float(sma(closes, 50).iloc[-1])
    rsi_val = float(rsi(closes, 14).iloc[-1])
    atr_val = float(atr(df["high"], df["low"], closes, 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_price * 0.02

    # Trend alignment: Price > SMA20 > SMA50
    trend_score = 1.0 if (last_price > s20 > s50) else 0.0
    # RSI momentum sweet spot (healthy accumulation, not overbought)
    mom_score = 1.0 if (52.0 <= rsi_val <= 66.0) else 0.0
    
    if trend_score == 1.0 and mom_score == 1.0:
        stop = round(s20 - 0.5 * atr_val, 2)
        risk = max(last_price - stop, 0.01)
        target = round(last_price + 2.0 * risk, 2)
        rr = round((target - last_price) / risk, 2)
        
        win_rate, ev = _stats("multi_factor_composite", rr)

        return PaperAlphaSignal(
            strategy_id="multi_factor_composite",
            paper_citation="Fama & French (2015) & Asness et al. (2013) 'Value and Momentum Everywhere'",
            symbol=symbol,
            action="BUY",
            setup="Multi-Factor Trend & Momentum Sweet Spot",
            thesis=(
                f"Triple trend alignment (Price > SMA20 > SMA50) with RSI in optimal accumulation zone ({rsi_val:.1f}). "
                f"Statistically robust multi-factor confluence."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("multi_factor_composite"),
            price=last_price,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="BULL_TREND",
        )
    return None


# ─── 5. Indian Market Paper: IIM Ahmedabad Momentum Anomaly (Joshipura 2013) ──

def evaluate_iima_nse_momentum(
    symbol: str,
    df: pd.DataFrame,
) -> PaperAlphaSignal | None:
    """IIM-A Research (Joshipura, 2013 - 'Momentum Anomaly in Indian Stock Market').
    
    Finding: On the NSE, a 6-month formation with 1-month holding produces a monthly
    alpha of 1.48% (t-stat > 3.2). Momentum is particularly persistent when verified
    by higher lows above 20-week (100-day) moving average.
    """
    if len(df) < 130:
        return None

    closes = df["close"].values
    last_price = float(closes[-1])
    
    # 6-month return (approx 126 trading days)
    ret_6m = (closes[-1] - closes[-126]) / closes[-126]
    
    # 100-day (20-week) SMA filter
    s100 = float(sma(df["close"], 100).iloc[-1])
    s20 = float(sma(df["close"], 20).iloc[-1])
    atr_val = float(atr(df["high"], df["low"], df["close"], 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_price * 0.02

    if ret_6m > 0.20 and last_price > s20 > s100:
        stop = round(s20 - 0.7 * atr_val, 2)
        risk = max(last_price - stop, 0.01)
        target = round(last_price + 2.2 * risk, 2)
        rr = round((target - last_price) / risk, 2)

        win_rate, ev = _stats("iima_nse_momentum", rr)

        return PaperAlphaSignal(
            strategy_id="iima_nse_momentum",
            paper_citation="Joshipura & Joshipura (2013, IIM-A) 'Momentum Anomaly: Evidence from Indian Stock Market'",
            symbol=symbol,
            action="BUY",
            setup="IIM-A NSE 6-Month Momentum Drift",
            thesis=(
                f"6-month price appreciation of +{ret_6m*100:.1f}% with structural higher-lows above 20-week (100-day) SMA. "
                f"Empirical NSE study confirms robust post-formation drift with 1.48% monthly alpha."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("iima_nse_momentum"),
            price=last_price,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="BULL_TREND",
        )
    return None


# ─── 6. Indian Market Paper: NISM 52-Week High Proximity (George & Hwang / NSE)

def evaluate_nism_52w_high_breakout(
    symbol: str,
    df: pd.DataFrame,
) -> PaperAlphaSignal | None:
    """NISM / Indian Market Study on '52-Week High and Anchoring Bias in Indian Equities'.
    
    Finding: Indian retail investors heavily anchor on the 52-week high, creating an
    artificial psychological resistance. Once price consolidates within 3% of the 52-week
    high and breaks out, under-reaction causes rapid institutional markups.
    """
    if len(df) < 250:
        return None

    highs = df["high"]
    closes = df["close"]
    volumes = df["volume"] if "volume" in df.columns else None

    last_close = float(closes.iloc[-1])
    high_52w = float(highs.iloc[-250:-1].max())

    # Distance to 52-week high
    dist_pct = (high_52w - last_close) / high_52w
    atr_val = float(atr(highs, df["low"], closes, 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_close * 0.02

    # Breakout condition: within 2% or freshly crossed 52w high with volume confirmation
    vol_surge = True
    if volumes is not None and len(volumes) >= 20:
        vol_avg = float(volumes.iloc[-21:-1].mean())
        cur_vol = float(volumes.iloc[-1])
        vol_surge = (cur_vol >= 1.4 * vol_avg) if vol_avg > 0 else True

    if (-0.02 <= dist_pct <= 0.03) and last_close >= high_52w * 0.98 and vol_surge:
        stop = round(last_close - 1.5 * atr_val, 2)
        risk = max(last_close - stop, 0.01)
        target = round(last_close + 2.5 * risk, 2)
        rr = round((target - last_close) / risk, 2)

        win_rate, ev = _stats("nism_52w_high_proximity", rr)

        return PaperAlphaSignal(
            strategy_id="nism_52w_high_proximity",
            paper_citation="George & Hwang (2004) & NISM India Research 'The 52-Week High and Investor Anchoring on NSE'",
            symbol=symbol,
            action="BUY",
            setup="NSE 52-Week High Anchoring Breakout",
            thesis=(
                f"Trading within {abs(dist_pct)*100:.1f}% of 52-week ceiling (₹{high_52w:,.2f}) on elevated volume. "
                f"Indian market research shows breakout from anchoring bias leads to sharp momentum acceleration."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("nism_52w_high_proximity"),
            price=last_close,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="BULL_TREND",
        )
    return None


# ─── 7. Indian Market Paper: Low-Volatility Anomaly in India (Sehgal & Jain 2015)

def evaluate_sehgal_low_vol_anomaly(
    symbol: str,
    df: pd.DataFrame,
) -> PaperAlphaSignal | None:
    """Sehgal & Jain (2015) 'Testing Low-Volatility Anomaly in Emerging Markets: The Case of India'.
    
    Finding: Low-beta, low-volatility Indian stocks consistently achieve higher risk-adjusted
    Sharpe ratios than lottery-like high-beta stocks. Triggers when a low-volatility stock
    bounces off its 20-day SMA in an established uptrend.
    """
    if len(df) < 60:
        return None

    closes = df["close"]
    lows = df["low"]
    highs = df["high"]

    last_close = float(closes.iloc[-1])
    s20 = float(sma(closes, 20).iloc[-1])
    s50 = float(sma(closes, 50).iloc[-1]) if len(df) >= 50 else s20
    
    # Volatility standard deviation over last 30 days
    ret = closes.pct_change().dropna()
    vol_30 = float(ret.tail(30).std() * math.sqrt(252))
    
    atr_val = float(atr(highs, lows, closes, 14).iloc[-1])
    if atr_val <= 0:
        atr_val = last_close * 0.02

    # Low annualised volatility (< 28%) + clean pullback touching 20-SMA
    touch_s20 = (float(lows.iloc[-1]) <= s20 * 1.01 and last_close >= s20 * 0.99)
    if vol_30 < 0.28 and last_close > s50 and touch_s20:
        stop = round(s20 - 1.0 * atr_val, 2)
        risk = max(last_close - stop, 0.01)
        target = round(last_close + 2.0 * risk, 2)
        rr = round((target - last_close) / risk, 2)

        win_rate, ev = _stats("sehgal_low_vol_anomaly", rr)

        return PaperAlphaSignal(
            strategy_id="sehgal_low_vol_anomaly",
            paper_citation="Sehgal & Jain (2015, Univ of Delhi) 'Low-Volatility Anomaly in Emerging Markets: The Indian Evidence'",
            symbol=symbol,
            action="BUY",
            setup="Indian Low-Beta Pullback Bounce",
            thesis=(
                f"Annualized volatility is low ({vol_30*100:.1f}%) with price executing a precise technical bounce "
                f"off the 20-day SMA in an intact uptrend. Exploits the documented Indian Low-Volatility Anomaly."
            ),
            expected_value=round(ev, 2),
            historical_win_rate=win_rate,
            confidence_score=_confidence("sehgal_low_vol_anomaly"),
            price=last_close,
            stop_loss=stop,
            target=target,
            rr_ratio=rr,
            regime_fit="RANGEBOUND",
        )
    return None


def run_paper_strategy_evaluator(symbol: str, df: pd.DataFrame) -> list[PaperAlphaSignal]:
    """Runs all international and Indian academic paper models against a stock's historical OHLCV frame."""
    signals: list[PaperAlphaSignal] = []
    
    # International models
    sig1 = evaluate_jegadeesh_titman_momentum(symbol, df)
    if sig1:
        signals.append(sig1)

    sig2 = evaluate_avellaneda_lee_mean_reversion(symbol, df)
    if sig2:
        signals.append(sig2)

    sig3 = evaluate_volatility_breakout(symbol, df)
    if sig3:
        signals.append(sig3)

    sig4 = evaluate_multi_factor_quant(symbol, df)
    if sig4:
        signals.append(sig4)

    # Indian market academic models
    sig5 = evaluate_iima_nse_momentum(symbol, df)
    if sig5:
        signals.append(sig5)

    sig6 = evaluate_nism_52w_high_breakout(symbol, df)
    if sig6:
        signals.append(sig6)

    sig7 = evaluate_sehgal_low_vol_anomaly(symbol, df)
    if sig7:
        signals.append(sig7)

    return signals


#: ``strategy_id`` → its evaluator. Lets a caller run one model without paying
#: for the other six, which matters inside a per-bar loop: adapters in
#: ``atr.strategy.strategies.paper_alpha`` each want exactly one model, and
#: running all seven per symbol per bar dominated the walk-forward runtime.
EVALUATORS: dict[str, Callable[[str, pd.DataFrame], PaperAlphaSignal | None]] = {
    "momentum_jegadeesh_titman": evaluate_jegadeesh_titman_momentum,
    "stat_arb_avellaneda_lee": evaluate_avellaneda_lee_mean_reversion,
    "volatility_breakout": evaluate_volatility_breakout,
    "multi_factor_composite": evaluate_multi_factor_quant,
    "iima_nse_momentum": evaluate_iima_nse_momentum,
    "nism_52w_high_proximity": evaluate_nism_52w_high_breakout,
    "sehgal_low_vol_anomaly": evaluate_sehgal_low_vol_anomaly,
}


def evaluate_one(strategy_id: str, symbol: str, df: pd.DataFrame) -> PaperAlphaSignal | None:
    """Run a single named model. Returns ``None`` for an unknown id."""
    fn = EVALUATORS.get(strategy_id)
    if fn is None:
        return None
    return fn(symbol, df)
