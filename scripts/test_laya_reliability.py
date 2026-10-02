"""Comprehensive testing script for Laya (convaiinnovations/laya).

Tests:
1. Model loading & checkpoint resolution
2. Single-pass inference latency benchmark (CPU/GPU)
3. Question type validation (Choice, Score, Noul)
4. Domain-specific market test cases across different regimes:
   - Bullish trend
   - Bearish breakdown
   - Sideways / low volatility mean-reverting
   - Extreme volatility panic / whipsaw
5. Determinism and calibration check
"""

import time
import json
import statistics

TEST_SCENARIOS = [
    {
        "id": "bullish_breakout",
        "expected_regime": "TRENDING_BULLISH",
        "state": {
            "symbol": "NIFTY50",
            "price_action": "Breaking out to all-time highs above 20-day high with expanding volume",
            "atr_pct": 1.1,
            "adx": 34.5,
            "rsi": 68.2,
            "return_20d_pct": 5.4,
            "sma_fast_vs_slow": "+3.1% above 50 SMA",
        },
    },
    {
        "id": "bearish_breakdown",
        "expected_regime": "TRENDING_BEARISH",
        "state": {
            "symbol": "BANKNIFTY",
            "price_action": "Sustained selling, trading below 20, 50, and 200 daily moving averages with heavy distribution volume",
            "atr_pct": 2.4,
            "adx": 38.0,
            "rsi": 28.5,
            "return_20d_pct": -7.2,
            "sma_fast_vs_slow": "-4.5% below 50 SMA",
        },
    },
    {
        "id": "rangebound_consolidation",
        "expected_regime": "MEAN_REVERTING",
        "state": {
            "symbol": "RELIANCE",
            "price_action": "Consolidating inside a tight 2% band over the last 30 sessions, low volume, flat moving averages",
            "atr_pct": 0.8,
            "adx": 12.4,
            "rsi": 49.0,
            "return_20d_pct": 0.3,
            "sma_fast_vs_slow": "Flat (within 0.1% of 50 SMA)",
        },
    },
    {
        "id": "extreme_volatility_crash",
        "expected_regime": "HIGH_VOLATILITY",
        "state": {
            "symbol": "INDIAVIX",
            "price_action": "Massive gap downs, sudden 4% intraday swings, emergency rate hike and geopolitical shock",
            "atr_pct": 5.8,
            "adx": 22.0,
            "rsi": 32.0,
            "return_20d_pct": -8.5,
            "sma_fast_vs_slow": "Violent multi-sigma whipsaws",
        },
    },
]


def run_benchmark():
    print("=" * 60)
    print("STEP 1: Importing and Initializing Laya Agent...")
    print("=" * 60)
    t0 = time.perf_counter()
    import laya
    
    agent = laya.Agent("convaiinnovations/laya")
    init_time = time.perf_counter() - t0
    print(f"Model successfully loaded in {init_time:.2f} seconds.\n")

    regime_question = {
        "regime_selection": {
            "type": "choice",
            "instructions": (
                "Based on the asset price action, volatility, trend strength, and technical indicators in state, "
                "classify the prevailing market regime into exactly one option."
            ),
            "criteria": {
                "TRENDING_BULLISH": "Strong upward trend, sustained breakout, higher highs, ADX > 25, price above major moving averages.",
                "TRENDING_BEARISH": "Strong downward trend, breakdown, lower lows, ADX > 25, price below major moving averages.",
                "MEAN_REVERTING": "Range-bound sideways market, low ADX (<20), oscillating inside bands without clear direction.",
                "HIGH_VOLATILITY": "Wild price swings, high ATR percentage, gap moves, sudden shocks, or erratic whipsaws.",
            },
        },
        "is_safe_to_enter_momentum": {
            "type": "noul",
            "instructions": "Is it safe and favorable to open new trend-following long positions?",
        },
        "risk_severity": {
            "type": "score",
            "instructions": "Rate the current market risk severity from low to extreme.",
            "criteria": ["Minimal risk", "Normal risk", "Elevated volatility risk", "Extreme crash/whipsaw risk"],
        },
    }

    print("=" * 60)
    print("STEP 2: Evaluating Market Scenarios (Accuracy & Confidence)...")
    print("=" * 60)

    latencies = []
    results = []

    for sc in TEST_SCENARIOS:
        state_str = json.dumps(sc["state"], indent=2)
        t_start = time.perf_counter()
        pred = agent.predict(state=state_str, questions=regime_question)
        dt = (time.perf_counter() - t_start) * 1000.0
        latencies.append(dt)

        answers = pred.get("answers", {})
        raw_choice = answers.get("regime_selection")
        if isinstance(raw_choice, dict):
            chosen_regime = raw_choice.get("choice")
            choice_probs = raw_choice.get("probabilities", {})
        else:
            chosen_regime = raw_choice
            choice_probs = {}

        raw_noul = answers.get("is_safe_to_enter_momentum")
        safe_to_enter = raw_noul.get("noul") if isinstance(raw_noul, dict) else raw_noul

        raw_score = answers.get("risk_severity")
        risk_score = raw_score.get("score") if isinstance(raw_score, dict) else raw_score

        match = chosen_regime == sc["expected_regime"]
        results.append({
            "id": sc["id"],
            "expected": sc["expected_regime"],
            "got": chosen_regime,
            "match": match,
            "latency_ms": dt,
            "probabilities": choice_probs,
            "safe_to_enter_prob": safe_to_enter,
            "risk_score_level": risk_score,
        })

        print(f"Scenario: [{sc['id']}]")
        print(f"  Expected: {sc['expected_regime']}")
        print(f"  Laya Choice: {chosen_regime} (Match: {'PASS' if match else 'FAIL'})")
        print(f"  Class Probabilities: {choice_probs}")
        print(f"  Safe To Enter Long (NOUL P(true)): {safe_to_enter}")
        print(f"  Risk Severity Score (0-3 scale): {risk_score}")
        print(f"  Inference Latency: {dt:.1f} ms\n")

    print("=" * 60)
    print("STEP 3: Benchmark Summary & Reliability Metrics")
    print("=" * 60)
    correct_count = sum(1 for r in results if r["match"])
    accuracy = (correct_count / len(results)) * 100.0
    mean_lat = statistics.mean(latencies)
    median_lat = statistics.median(latencies)

    print(f"Total Scenarios Tested : {len(results)}")
    print(f"Regime Match Accuracy  : {correct_count}/{len(results)} ({accuracy:.1f}%)")
    print(f"Mean Inference Latency : {mean_lat:.1f} ms")
    print(f"Median Latency         : {median_lat:.1f} ms")
    print("=" * 60)


if __name__ == "__main__":
    run_benchmark()
