"""What a stored strategy version *means*, decided in exactly one place.

A ``strategy_versions.definition`` is a JSON column, which means it is untrusted
input: whoever wrote the row may have spelled a field ``stop_loss`` instead of
``stop_loss_pct``, or stored ``"3%"`` where a number belongs. Two consumers read
it — the paper runner, which turns it into live entry/exit rules, and the
backtest runner, which turns it into a registry strategy plus constructor
parameters — and if each had its own opinion about what is acceptable, then
"validate before you deploy" would be a promise the platform cannot keep: the
validator would approve a definition the loop then refuses, or reject one it
would happily run.

So the coercion lives here, in the compute layer, and both consumers delegate to
it. This is not a new abstraction over the strategy engine; it is the *existing*
coercion moved to the only layer that both callers may import. It used to live
in ``atr/services/runner.py``, and ``atr/strategy`` may not import
``atr/services`` (``tests/test_architecture.py``), so leaving it there would have
forced the validator to re-implement it — which is how the two would drift.

Two definition shapes are accepted, unchanged from before this module existed:

* an explicit rule block — ``{"rules": {"entry": {...}, "exit": {...}}}`` or the
  same two blocks at the top level. This is what the live loop runs.
* ``engine_key`` (+ optional ``params``) — a key into the ``STRATEGIES``
  registry, which is what a backtest runs.

A version may carry both, and when it does the two must agree: the rules block is
what trades live, ``engine_key``/``params`` is what the backtest scores, and a
backtest of rules the deployment does not run would validate the wrong subject.
:func:`validate_definition` reports a disagreement rather than resolving it,
because only the author can say which of the two was meant.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import Any

from atr.signals.models import EntryRules, ExitRules

logger = logging.getLogger("atr.strategy.definition")

#: Rule fields that must be numeric, and the ones that may be ``None`` to mean
#: "off". Kept as an explicit set rather than inferred from the dataclass: the
#: check belongs where the value is read, not where it is used, because a
#: dataclass constructor does not type-check and would accept ``"3%"`` happily
#: and only explode inside ``eval_exit`` once a position existed to compare it
#: against.
NUMERIC_RULE_FIELDS = frozenset(
    {
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "trend_sma",
        "trend_confirm_bars",
        "rsi_overbought",
        "min_history_bars",
        "trend_fast_sma",
        "trend_slow_sma",
        "pullback_rsi_low",
        "pullback_rsi_high",
        "breakout_lookback",
        "breakout_proximity_pct",
        "volume_multiple",
        "volume_lookback",
        "oversold_rsi",
        "long_sma",
        "rsi_period",
        "triple_rsi_period",
        "triple_rsi_below",
        "triple_rsi_prior_below",
        "triple_rsi_trend_sma",
    }
)

#: The two rule blocks, and the aliases ``_rules_from_definition`` always accepted.
BLOCK_ENTRY = ("entry", "entries")
BLOCK_EXIT = ("exit", "exits")

#: Top-level keys something actually reads. Anything else is silently dropped by
#: both consumers, so it is reported rather than ignored — a field the author set
#: and nobody honours is a definition that does not mean what it says.
KNOWN_DEFINITION_KEYS = frozenset(
    {
        "rules",
        "entry",
        "entries",
        "exit",
        "exits",
        "engine_key",
        "params",
        "name",
        "description",
        "notes",
        "change_note",
        "adaptive_parameters",
        "sizing",
    }
)


def invalid_rule_values(*blocks: dict[str, Any]) -> dict[str, Any]:
    """Return the fields whose stored value cannot be used as a number.

    Empty when every recognised field is either a number or ``None``. ``bool`` is
    rejected explicitly because it is an ``int`` subclass and ``True`` as a stop
    percentage would silently mean 1%.
    """
    bad: dict[str, Any] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            if name not in NUMERIC_RULE_FIELDS:
                continue
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                bad[name] = value
    return bad


@dataclass(frozen=True)
class Resolution:
    """The outcome of reading a definition as live entry/exit rules.

    ``reason`` is ``None`` exactly when the pair was produced. It is a sentence
    rather than a code because its two callers want it for different things — the
    runner logs it, the validator returns it — and neither wants to map codes
    back to prose.
    """

    entry: Any | None = None
    exit_rules: Any | None = None
    #: ``"rules"`` when the explicit block resolved it, ``"engine_key"`` when the
    #: registry did, ``None`` when neither was even present.
    source: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.entry is not None and self.exit_rules is not None


def parse_definition(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """A stored definition as a dict. Returns ``(definition, reason)``.

    ``raw`` may be a JSON string (the column's normal form), an already-parsed
    dict, or something else entirely. A caller that cannot get a dict must not
    proceed on a guess.
    """
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None, "the definition is not readable JSON"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "the definition is JSON but not an object"
    if raw is None:
        return None, "the definition is empty"
    return None, f"the definition is {type(raw).__name__}, not a JSON object"


def rule_blocks(definition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``(entry, exit)`` blocks as stored, without constructing anything.

    Precedence is the one the live loop has always used: an explicit ``rules``
    object if there is one, otherwise the definition's own top level. The ``or``
    between the singular and plural spellings is deliberate and preserved — an
    empty dict is treated as "not given", which is what an author who wrote
    ``"entry": {}`` meant.
    """
    blocks = definition.get("rules")
    if not isinstance(blocks, dict):
        blocks = definition
    entry_raw = blocks.get(BLOCK_ENTRY[0]) or blocks.get(BLOCK_ENTRY[1])
    exit_raw = blocks.get(BLOCK_EXIT[0]) or blocks.get(BLOCK_EXIT[1])
    return (
        entry_raw if isinstance(entry_raw, dict) else {},
        exit_raw if isinstance(exit_raw, dict) else {},
    )


def resolve_rules(definition: dict[str, Any]) -> Resolution:
    """Coerce a stored definition into live ``(EntryRules, ExitRules)``.

    Explicit rule blocks win over the registry, because a saved version that
    spells out its rules is more specific than the class it was derived from: the
    version is the subject, the engine is only how it is executed.

    Returns a :class:`Resolution` whose ``reason`` explains a refusal. Refusing
    matters more than it looks: the alternative — falling back to
    ``EntryRules()``/``ExitRules()`` defaults — is a deployment that trades a
    generic ruleset while stamping every order with the name of a strategy whose
    rules never ran. Nothing raises, the loop trades, and the artefacts are
    internally consistent and wrong.
    """
    if not isinstance(definition, dict):
        return Resolution(reason="the definition is not a JSON object")

    entry_raw, exit_raw = rule_blocks(definition)
    has_blocks = bool(entry_raw) or bool(exit_raw)

    if has_blocks:
        try:
            entry = EntryRules(**entry_raw)
            exit_rules = ExitRules(**exit_raw)
        except TypeError as exc:
            return Resolution(source="rules", reason=f"the rule block is malformed: {exc}")

        malformed = invalid_rule_values(entry_raw, exit_raw)
        if malformed:
            return Resolution(
                source="rules",
                reason=f"the rule block has non-numeric values: {malformed}",
            )
        return Resolution(entry=entry, exit_rules=exit_rules, source="rules")

    engine_key = definition.get("engine_key")
    if not engine_key:
        return Resolution(
            reason=(
                "the definition declares neither an entry/exit rule block nor an "
                "engine_key, so there is nothing to evaluate"
            )
        )

    from atr.strategy.strategies import STRATEGIES

    meta = STRATEGIES.get(str(engine_key))
    if meta is None:
        return Resolution(
            source="engine_key",
            reason=f"engine_key {engine_key!r} is not a registered strategy",
        )
    rules = getattr(meta, "signal_rules", None)
    if callable(rules):
        produced = rules()
        if isinstance(produced, tuple) and len(produced) == 2:
            return Resolution(entry=produced[0], exit_rules=produced[1], source="engine_key")
    return Resolution(
        source="engine_key",
        reason=(
            f"strategy {engine_key!r} defines no signal_rules(), so it has no live "
            "entry/exit rules; give the version an explicit entry/exit block to "
            "make it deployable"
        ),
    )


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------
def _issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _entry_rule_fields() -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(EntryRules))


def _exit_rule_fields() -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(ExitRules))


def _constructor_params(engine_key: str) -> frozenset[str] | None:
    """Accepted constructor parameter names, or ``None`` when unknowable.

    ``None`` means the class takes ``**kwargs``, so an unrecognised parameter
    name cannot be called wrong — guessing would produce false alarms on a
    legitimate definition.
    """
    import inspect

    from atr.strategy.strategies import STRATEGIES

    cls = STRATEGIES.get(engine_key)
    if cls is None:
        return None
    try:
        signature = inspect.signature(cls.__init__)
    except (TypeError, ValueError):  # pragma: no cover - builtin or C class
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return None
    return frozenset(
        name for name in signature.parameters if name not in ("self", "kwargs")
    )


def _numeric_range_warnings(
    entry_raw: dict[str, Any], exit_raw: dict[str, Any]
) -> list[dict[str, str]]:
    """Warnings for values that are usable but mean something the author may not intend."""
    if not (entry_raw or exit_raw):
        return []

    out: list[dict[str, str]] = []

    # Check if any exit rule can actually fire
    live_exits = [
        name for name in ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trend_sma")
        if exit_raw.get(name)
    ]
    if exit_raw.get("rsi_overbought") is not None:
        live_exits.append("rsi_overbought")
    if not live_exits:
        out.append(_issue("no_exit_rule", "exit",
            "no exit rule can ever fire (a falsy threshold disables its rule), so a "
            "position this strategy opens can never be closed by it — the deployment "
            "would produce no closed trade and therefore no forward observation"))

    # Individual field warnings
    confirm = exit_raw.get("trend_confirm_bars")
    if _is_number(confirm) and confirm < 1:
        out.append(_issue("trend_confirm_bars_clamped", "exit.trend_confirm_bars",
            f"trend_confirm_bars is {confirm} and will be clamped to 1; a single close "
            "below the average fires constantly in ordinary noise"))

    fast, slow = entry_raw.get("trend_fast_sma"), entry_raw.get("trend_slow_sma")
    if _is_number(fast) and _is_number(slow) and fast >= slow:
        out.append(_issue("trend_sma_order_inverted", "entry.trend_fast_sma",
            f"trend_fast_sma ({fast}) is not below trend_slow_sma ({slow}), so the "
            "trend_pullback rule can never fire (it requires fast > slow)"))

    min_history = entry_raw.get("min_history_bars")
    if _is_number(min_history) and min_history > 250:
        out.append(_issue("min_history_bars_exceeds_typical_warmup", "entry.min_history_bars",
            f"min_history_bars is {min_history}; the runner loads ~400 daily bars by "
            "default, and a rule that needs more than the history it is given "
            "produces no trades at all"))

    return out


def _is_number(value: Any) -> bool:
    """True for int/float, False for bool (which is an int subclass)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_definition(definition: Any) -> dict[str, Any]:
    """Check that a definition is *structurally runnable*, and say what that excludes.

    Two questions, kept apart on purpose:

    * **Can this be executed?** A rule block that parses, fields that exist,
      values that are numbers, an ``engine_key`` the registry knows. Answered
      here, definitively.
    * **Does it make money?** Not answered here, not answerable here, and the
      response says so in ``statistical_validation`` rather than letting a green
      tick imply it. A definition that passes this is a definition that will run,
      and nothing more.

    ``ok`` means "no errors": the definition will execute on both paths it
    claims. ``warnings`` are things that are executable but probably not what was
    meant, and they are worth reading before deploying.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    parsed, reason = parse_definition(definition)
    if parsed is None:
        return {
            "ok": False,
            "errors": [_issue("unreadable_definition", "definition", reason or "unreadable")],
            "warnings": [],
            "paper": {"resolvable": False, "reason": reason},
            "backtest": {"resolvable": False, "reason": reason},
            "statistical_validation": _not_measured(),
        }

    unknown_keys = sorted(set(parsed) - KNOWN_DEFINITION_KEYS)
    for key in unknown_keys:
        warnings.append(
            _issue(
                "unknown_definition_key",
                key,
                f"{key!r} is not read by the live loop or the backtest runner, so it "
                "will be silently ignored — remove it or spell it correctly",
            )
        )

    entry_raw, exit_raw = rule_blocks(parsed)
    entry_fields = _entry_rule_fields()
    exit_fields = _exit_rule_fields()

    for label, block, known in (
        ("entry", entry_raw, entry_fields),
        ("exit", exit_raw, exit_fields),
    ):
        for name in sorted(set(block) - known):
            errors.append(
                _issue(
                    "unknown_rule_field",
                    f"{label}.{name}",
                    f"{name!r} is not a field of the {label} rules "
                    f"({sorted(known)}), so the rule block cannot be constructed",
                )
            )
        for name, value in sorted(invalid_rule_values(block).items()):
            errors.append(
                _issue(
                    "non_numeric_rule_value",
                    f"{label}.{name}",
                    f"{name!r} is {value!r}; it must be a number (or null to disable "
                    "the rule)",
                )
            )

    warnings.extend(_numeric_range_warnings(entry_raw, exit_raw))

    # --- sizing block validation ------------------------------------------
    sizing_raw = parsed.get("sizing")
    if sizing_raw is not None:
        if not isinstance(sizing_raw, dict):
            errors.append(
                _issue(
                    "sizing_not_an_object",
                    "sizing",
                    f"sizing is {type(sizing_raw).__name__}; must be a configuration object",
                )
            )
        else:
            from atr.strategy.sizing import SizingMethod
            known_sizing_keys = {
                "method",
                "capital_allocation",
                "risk_per_trade_pct",
                "risk_per_trade_rupees",
                "fixed_quantity",
                "fixed_rupee_value",
                "capital_fraction",
                "max_quantity",
                "max_position_value",
                "max_portfolio_exposure_pct",
                "min_quantity",
                "rounding_rule",
                "atr_multiplier",
            }
            for k in sorted(set(sizing_raw) - known_sizing_keys):
                warnings.append(
                    _issue(
                        "unknown_sizing_key",
                        f"sizing.{k}",
                        f"{k!r} is not a known sizing parameter; supported: {sorted(known_sizing_keys)}",
                    )
                )
            method_val = sizing_raw.get("method")
            if method_val:
                try:
                    SizingMethod(str(method_val).upper())
                except ValueError:
                    valid_methods = [m.value for m in SizingMethod]
                    warnings.append(
                        _issue(
                            "unknown_sizing_method",
                            "sizing.method",
                            f"method {method_val!r} is not standard; valid methods: {valid_methods}",
                        )
                    )


    # --- the live path ----------------------------------------------------
    resolution = resolve_rules(parsed)
    paper: dict[str, Any] = {
        "resolvable": resolution.ok,
        "source": resolution.source,
        "reason": resolution.reason,
    }
    if resolution.ok:
        authored = set(entry_raw) | set(exit_raw)
        # Which fields the author actually chose, and which are the module's own
        # starting values. ``EntryRules``/``ExitRules`` ship defaults that nothing
        # has validated, so a version that sets three fields is mostly running
        # them — worth knowing before it is deployed, not after.
        paper["authored_fields"] = sorted(authored)
        paper["default_fields"] = sorted((entry_fields | exit_fields) - authored)

    # --- the backtest path ------------------------------------------------
    engine_key = parsed.get("engine_key")
    params = parsed.get("params")
    backtest: dict[str, Any] = {"engine_key": engine_key}
    if engine_key:
        from atr.strategy.strategies import STRATEGIES

        if str(engine_key) not in STRATEGIES:
            errors.append(
                _issue(
                    "unknown_engine_key",
                    "engine_key",
                    f"{engine_key!r} is not a registered strategy, so a backtest of this "
                    f"version cannot run (have: {sorted(STRATEGIES)})",
                )
            )
            backtest["resolvable"] = False
            backtest["reason"] = f"unknown engine_key {engine_key!r}"
        else:
            backtest["resolvable"] = True
            backtest["reason"] = None
    elif params is not None:
        errors.append(
            _issue(
                "params_without_engine_key",
                "params",
                "params were given without an engine_key, so nothing will read them",
            )
        )
        backtest["resolvable"] = False
        backtest["reason"] = "params without an engine_key"
    else:
        backtest["resolvable"] = False
        backtest["reason"] = (
            "the version names no engine_key, so it can be deployed but not backtested"
        )

    if isinstance(params, dict):
        backtest["params"] = sorted(params)
    elif params is not None:
        errors.append(
            _issue(
                "params_not_an_object",
                "params",
                f"params is {type(params).__name__}; the backtest runner merges it into "
                "the strategy constructor and needs an object",
            )
        )

    # --- the two paths must describe the same strategy ---------------------
    if engine_key and isinstance(params, dict) and (entry_raw or exit_raw):
        accepted = _constructor_params(str(engine_key))
        if accepted is not None:
            for name in sorted(set(params) - accepted):
                errors.append(
                    _issue(
                        "unknown_strategy_param",
                        f"params.{name}",
                        f"{engine_key!r} does not accept a {name!r} parameter "
                        f"(tunable: {sorted(accepted)}), so a backtest of this version "
                        "would raise instead of running",
                    )
                )
        #: Qualified with its block, like every other ``field`` in this payload —
        #: a bare rule name would not say *which* of the two paths disagrees.
        for label, block in (("entry", entry_raw), ("exit", exit_raw)):
            for name in sorted(set(block) & set(params)):
                if block[name] == params[name]:
                    continue
                warnings.append(
                    _issue(
                        "rules_and_params_disagree",
                        f"{label}.{name}",
                        f"the rules block says {name}={block[name]!r} but params says "
                        f"{params[name]!r}. The live loop runs the rules block and a "
                        "backtest runs params, so the two would be testing different "
                        "strategies",
                    )
                )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "paper": paper,
        "backtest": backtest,
        "statistical_validation": _not_measured(),
    }


def _not_measured() -> dict[str, Any]:
    """The clause that stops a green tick from being read as a finding.

    This project's whole premise is that a number nobody measured must not be
    presented as one. Structural validation is a real and useful check — it
    catches the definition that would refuse to trade at 09:15 tomorrow — and it
    is not evidence that the rules have an edge. Saying so in the payload is
    cheaper than hoping nobody conflates the two.
    """
    return {
        "performed": False,
        "reason": (
            "structural validation only: it says the definition will execute, not "
            "that it will make money. No out-of-sample result is implied."
        ),
        "how_to_measure": (
            "POST /api/v1/backtests (out-of-sample walk-forward with deflated-Sharpe "
            "correction), or `atr research`"
        ),
    }


__all__ = [
    "BLOCK_ENTRY",
    "BLOCK_EXIT",
    "KNOWN_DEFINITION_KEYS",
    "NUMERIC_RULE_FIELDS",
    "Resolution",
    "invalid_rule_values",
    "parse_definition",
    "resolve_rules",
    "rule_blocks",
    "validate_definition",
]
