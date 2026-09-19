"""Adaptive strategy parameters specification and extraction.

Only parameters explicitly marked as adaptive may be optimized.
The optimizer is strictly forbidden from modifying arbitrary strategy logic.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AdaptiveParameter:
    """Specification for a strategy parameter that is permitted to be adapted."""

    name: str
    current: float
    minimum: float
    maximum: float
    step: float
    adaptive: bool = True
    block: str = "entry"  # "entry", "exit", or "params"
    description: str = ""

    def validate_bounds(self) -> None:
        if self.minimum >= self.maximum:
            raise ValueError(f"Parameter {self.name}: minimum ({self.minimum}) must be less than maximum ({self.maximum})")
        if self.step <= 0:
            raise ValueError(f"Parameter {self.name}: step ({self.step}) must be strictly positive")
        if not (self.minimum <= self.current <= self.maximum):
            # If current is slightly out of bounds, we allow it but clamp when generating candidates
            pass

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Well-known default specifications for parameters if adaptive=True is toggled without explicit bounds
DEFAULT_PARAMETER_SPECS: dict[str, dict[str, Any]] = {
    "volume_multiple": {
        "minimum": 1.0,
        "maximum": 3.5,
        "step": 0.1,
        "block": "entry",
        "description": "Relative volume threshold required for breakout entry",
    },
    "relative_volume_threshold": {
        "minimum": 1.0,
        "maximum": 3.5,
        "step": 0.1,
        "block": "entry",
        "description": "Relative volume threshold required for entry",
    },
    "rsi_overbought": {
        "minimum": 60.0,
        "maximum": 85.0,
        "step": 2.0,
        "block": "exit",
        "description": "RSI threshold for overbought profit-taking / exit",
    },
    "oversold_rsi": {
        "minimum": 20.0,
        "maximum": 45.0,
        "step": 2.0,
        "block": "entry",
        "description": "RSI threshold for oversold dip-buying entry",
    },
    "stop_loss_pct": {
        "minimum": 0.5,
        "maximum": 5.0,
        "step": 0.25,
        "block": "exit",
        "description": "Percentage trailing or fixed stop loss threshold",
    },
    "take_profit_pct": {
        "minimum": 1.0,
        "maximum": 15.0,
        "step": 0.5,
        "block": "exit",
        "description": "Percentage take profit threshold",
    },
    "trailing_stop_pct": {
        "minimum": 0.5,
        "maximum": 6.0,
        "step": 0.25,
        "block": "exit",
        "description": "Percentage trailing stop threshold",
    },
    "breakout_proximity_pct": {
        "minimum": 0.5,
        "maximum": 5.0,
        "step": 0.25,
        "block": "entry",
        "description": "Proximity to high/low required to trigger breakout entry",
    },
    "breakout_lookback": {
        "minimum": 10.0,
        "maximum": 50.0,
        "step": 5.0,
        "block": "entry",
        "description": "Lookback bar period for channel breakout high/low",
    },
    "long_sma": {
        "minimum": 20.0,
        "maximum": 200.0,
        "step": 10.0,
        "block": "entry",
        "description": "Period of long trend moving average",
    },
}


def extract_adaptive_parameters(definition: dict[str, Any]) -> dict[str, AdaptiveParameter]:
    """Extract all parameters explicitly declared as adaptive from a strategy definition.

    Parameters must be declared under `adaptive_parameters`:
    either as a list of dicts:
    [
        {
            "name": "volume_multiple",
            "current": 1.5,
            "minimum": 1.2,
            "maximum": 3.0,
            "step": 0.1,
            "adaptive": true
        }
    ]
    or as a dictionary:
    {
        "volume_multiple": {
            "current": 1.5,
            "minimum": 1.2,
            "maximum": 3.0,
            "step": 0.1,
            "adaptive": true
        }
    }

    If a parameter has `adaptive: false` or is omitted, it is NOT extracted.
    """
    if not isinstance(definition, dict):
        return {}

    raw_adaptive = definition.get("adaptive_parameters")
    if not raw_adaptive:
        return {}

    # Locate current values in rules/params if not explicitly supplied
    rules = definition.get("rules") if isinstance(definition.get("rules"), dict) else definition
    entry_block = rules.get("entry") or rules.get("entries") or {}
    exit_block = rules.get("exit") or rules.get("exits") or {}
    params_block = definition.get("params") or {}

    def _find_current(name: str, preferred_block: str | None = None) -> tuple[float | None, str]:
        if preferred_block == "exit" and name in exit_block:
            val = exit_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "exit"
        if preferred_block == "entry" and name in entry_block:
            val = entry_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "entry"
        if preferred_block == "params" and name in params_block:
            val = params_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "params"

        # Fallback check across blocks
        if name in entry_block:
            val = entry_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "entry"
        if name in exit_block:
            val = exit_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "exit"
        if name in params_block:
            val = params_block[name]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val), "params"

        return None, preferred_block or "entry"

    result: dict[str, AdaptiveParameter] = {}

    items: list[dict[str, Any]] = []
    if isinstance(raw_adaptive, list):
        items = [item for item in raw_adaptive if isinstance(item, dict)]
    elif isinstance(raw_adaptive, dict):
        for k, v in raw_adaptive.items():
            if isinstance(v, dict):
                item = {"name": k, **v}
                items.append(item)

    for item in items:
        is_adaptive = item.get("adaptive", True)
        if not is_adaptive:
            continue

        name = item.get("name")
        if not name or not isinstance(name, str):
            continue

        defaults = DEFAULT_PARAMETER_SPECS.get(name, {})
        preferred_block = item.get("block") or defaults.get("block", "entry")
        current_val, block = _find_current(name, preferred_block)

        if "current" in item and isinstance(item["current"], (int, float)) and not isinstance(item["current"], bool):
            current_val = float(item["current"])

        if current_val is None:
            # Cannot adapt a parameter whose current baseline value is unknown
            continue

        minimum = float(item.get("minimum", defaults.get("minimum", current_val * 0.5)))
        maximum = float(item.get("maximum", defaults.get("maximum", current_val * 2.0)))
        step = float(item.get("step", defaults.get("step", 0.1)))
        desc = str(item.get("description", defaults.get("description", "")))

        param = AdaptiveParameter(
            name=name,
            current=current_val,
            minimum=minimum,
            maximum=maximum,
            step=step,
            adaptive=True,
            block=block,
            description=desc,
        )
        try:
            param.validate_bounds()
            result[name] = param
        except ValueError:
            continue

    return result


def apply_parameter_to_definition(
    definition: dict[str, Any],
    parameter_name: str,
    new_value: float,
    block: str | None = None,
) -> dict[str, Any]:
    """Return a new deep-copied strategy definition with the parameter updated.

    Keeps the baseline structure completely intact and updates the target value
    in rules.entry, rules.exit, top-level entry/exit, or params.
    Also updates the `current` field inside `adaptive_parameters` if present.
    """
    updated = copy.deepcopy(definition)

    # Determine which block to write to
    target_block = block
    if not target_block:
        if "rules" in updated and isinstance(updated["rules"], dict):
            if "entry" in updated["rules"] and parameter_name in updated["rules"]["entry"]:
                target_block = "entry"
            elif "exit" in updated["rules"] and parameter_name in updated["rules"]["exit"]:
                target_block = "exit"
        if not target_block:
            if "entry" in updated and isinstance(updated["entry"], dict) and parameter_name in updated["entry"]:
                target_block = "entry"
            elif "exit" in updated and isinstance(updated["exit"], dict) and parameter_name in updated["exit"]:
                target_block = "exit"
            elif "params" in updated and isinstance(updated["params"], dict) and parameter_name in updated["params"]:
                target_block = "params"

    # Default to entry if still unknown
    target_block = target_block or "entry"

    # Round to reasonable precision based on step/float
    rounded_val: float | int = round(new_value, 4)
    if rounded_val.is_integer():
        rounded_val = int(rounded_val)

    if target_block == "params":
        if "params" not in updated or not isinstance(updated["params"], dict):
            updated["params"] = {}
        updated["params"][parameter_name] = rounded_val
    else:
        # Write to rules block if rules exists
        if "rules" in updated and isinstance(updated["rules"], dict):
            if target_block not in updated["rules"] or not isinstance(updated["rules"][target_block], dict):
                updated["rules"][target_block] = {}
            updated["rules"][target_block][parameter_name] = rounded_val
        else:
            if target_block not in updated or not isinstance(updated[target_block], dict):
                updated[target_block] = {}
            updated[target_block][parameter_name] = rounded_val

    # Update adaptive_parameters entry if present
    if "adaptive_parameters" in updated:
        raw_ad = updated["adaptive_parameters"]
        if isinstance(raw_ad, list):
            for item in raw_ad:
                if isinstance(item, dict) and item.get("name") == parameter_name:
                    item["current"] = rounded_val
        elif isinstance(raw_ad, dict) and parameter_name in raw_ad:
            if isinstance(raw_ad[parameter_name], dict):
                raw_ad[parameter_name]["current"] = rounded_val

    return updated
