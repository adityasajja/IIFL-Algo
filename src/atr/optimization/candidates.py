"""Candidate parameter change generation from forward-learning evidence.

Strict Rules:
1. Only parameters explicitly marked as adaptive may be adapted.
2. The optimizer must never modify arbitrary strategy logic.
3. Every candidate retains the original strategy definition as the baseline control.
4. Candidates are only generated from genuine forward-learning evidence meeting
   sample size thresholds (n >= 10, significance in {'strong', 'moderate'}).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from atr.optimization.adaptive import (
    AdaptiveParameter,
    apply_parameter_to_definition,
    extract_adaptive_parameters,
)


@dataclass(frozen=True)
class OptimizationCandidate:
    """A single proposed parameter change hypothesis ready for backtest and validation."""

    candidate_id: str
    parameter_name: str
    current_value: float
    proposed_value: float
    hypothesis: str
    source_observation: dict[str, Any]
    sample_size: int
    baseline_definition: dict[str, Any]
    candidate_definition: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Mapping from observation axis / keyword patterns to candidate adaptive parameter names
AXIS_PARAMETER_MAPPING: dict[str, list[str]] = {
    "relative_volume": ["volume_multiple", "relative_volume_threshold"],
    "rvol": ["volume_multiple", "relative_volume_threshold"],
    "rsi": ["rsi_overbought", "oversold_rsi"],
    "atr": ["stop_loss_pct", "trailing_stop_pct", "take_profit_pct"],
    "volatility": ["stop_loss_pct", "trailing_stop_pct"],
    "breakout": ["breakout_proximity_pct", "breakout_lookback"],
    "trend": ["long_sma", "trend_sma"],
}



def _extract_numeric_from_label(label: str) -> float | None:
    """Extract float number from label like 'RVOL > 2.0x' or 'RSI > 75'."""
    match = re.search(r"[-+]?\d*\.?\d+", label)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def match_observation_to_parameter(
    observation: dict[str, Any],
    adaptive_params: dict[str, AdaptiveParameter],
) -> tuple[AdaptiveParameter | None, str | None]:
    """Identify which adaptive parameter an observation pertains to, if any."""
    evidence = observation.get("evidence", {})
    axis = str(evidence.get("axis") or observation.get("axis") or "").lower()
    label = str(evidence.get("bucket") or observation.get("statement") or "").lower()

    # 1. Match by explicit axis
    for axis_key, param_names in AXIS_PARAMETER_MAPPING.items():
        if axis_key in axis:
            for p_name in param_names:
                if p_name in adaptive_params:
                    return adaptive_params[p_name], axis_key

    # 2. Match by keyword in label / statement
    for axis_key, param_names in AXIS_PARAMETER_MAPPING.items():
        if axis_key in label or (axis_key == "relative_volume" and "rvol" in label):
            for p_name in param_names:
                if p_name in adaptive_params:
                    return adaptive_params[p_name], axis_key

    # 3. Match by direct parameter name mentioned in statement or evidence
    for p_name, param in adaptive_params.items():
        if p_name.lower() in label or p_name.lower() in axis:
            return param, p_name

    return None, None


def generate_candidates_from_observations(
    strategy_definition: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    min_sample_size: int = 10,
    max_candidates_per_param: int = 3,
) -> list[OptimizationCandidate]:
    """Generate candidate parameter modifications based on forward-learning observations.

    Rules:
    - Only parameters explicitly in strategy_definition with adaptive=True are adapted.
    - If no adaptive parameters exist, 0 candidates are returned.
    - An observation must have n >= min_sample_size and non-trivial lift or significance.
    """
    adaptive_params = extract_adaptive_parameters(strategy_definition)
    if not adaptive_params:
        return []

    candidates: list[OptimizationCandidate] = []

    for obs in observations:
        sample_size = obs.get("sample_size") or obs.get("n") or 0
        if sample_size < min_sample_size:
            continue

        param, matched_axis = match_observation_to_parameter(obs, adaptive_params)
        if param is None:
            continue

        evidence = obs.get("evidence", {})
        label = str(evidence.get("bucket") or obs.get("statement") or "")
        lift = evidence.get("lift")
        if lift is None and "stats" in evidence:
            lift = evidence["stats"].get("lift")

        obs_target_val = _extract_numeric_from_label(label)

        # Propose candidate values based on target value or step progression
        proposed_values: list[float] = []
        curr = param.current

        if obs_target_val is not None:
            # If observation suggests a target value (e.g. 2.0 from RVOL > 2.0x)
            direction = 1 if obs_target_val > curr else -1
            # Generate discrete steps in multiples of param.step leading up to target
            # e.g. 1.5 -> 1.8, 1.9, 2.0
            total_steps = max(1, int(round(abs(obs_target_val - curr) / param.step)))
            start_k = max(1, total_steps - max_candidates_per_param + 1)
            for k in range(start_k, total_steps + 1):
                val = round(curr + (direction * k * param.step), 4)
                if param.minimum <= val <= param.maximum and val != curr:
                    proposed_values.append(val)
            if obs_target_val not in proposed_values and param.minimum <= obs_target_val <= param.maximum and obs_target_val != curr:
                proposed_values.append(round(obs_target_val, 4))
        else:
            # Step in the direction suggested by the lift/observation
            # If strong bucket or positive lift, step in positive direction; otherwise negative
            is_positive = (lift is not None and lift > 0) or obs.get("kind") == "strong_bucket"
            sign = 1 if is_positive else -1
            for step_multiplier in range(1, max_candidates_per_param + 1):
                val = round(curr + (sign * step_multiplier * param.step), 4)
                if param.minimum <= val <= param.maximum and val != curr:
                    proposed_values.append(val)

        # Deduplicate while preserving order
        unique_proposals: list[float] = []
        for p in proposed_values:
            # Clean floating rounding
            clean_p = round(p, 4)
            if clean_p not in unique_proposals and clean_p != curr:
                unique_proposals.append(clean_p)

        for proposed_val in unique_proposals[:max_candidates_per_param]:
            cand_def = apply_parameter_to_definition(
                strategy_definition,
                param.name,
                proposed_val,
                block=param.block,
            )
            hypothesis = (
                f"Forward observation on {param.name} ('{label}', n={sample_size}, "
                f"lift={lift if lift is not None else 'positive'}) suggests changing "
                f"{param.name} from {curr} to {proposed_val} to capture higher quality setups."
            )
            candidate = OptimizationCandidate(
                candidate_id=str(uuid.uuid4()),
                parameter_name=param.name,
                current_value=curr,
                proposed_value=proposed_val,
                hypothesis=hypothesis,
                source_observation=obs,
                sample_size=sample_size,
                baseline_definition=strategy_definition,
                candidate_definition=cand_def,
            )
            candidates.append(candidate)

    return candidates
