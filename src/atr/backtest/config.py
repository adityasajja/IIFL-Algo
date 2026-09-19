"""Backtest run configuration — the schema, its validation, and its identity.

This module owns *what a backtest run is asked to do*. It does not run anything.

Design notes that matter:

* **The config is the record.** A run's `config` JSON is the only thing the
  result store keeps about the request, so every field that can change the
  outcome has to be in here. A field that lives only in a form's component
  state is a field that makes a result irreproducible.

* **Two hashes, two different jobs.** :func:`config_fingerprint` identifies the
  *request* (so "have I already run exactly this?" is answerable). The data
  fingerprint written next to the metrics identifies the *price series*, which
  is what actually changes under a config that looks identical. A metric
  without its data fingerprint is not reproducible, and the schema already says
  so — see ``backtest_runs.data_fingerprint``.

* **Costs are a first-class choice.** Every strategy measured in this repo
  before `IndianDeliveryCosts` existed was costed with an IBKR-style model that
  charges no statutory levy, which understates a delivery round trip in India
  by roughly 0.25% of turnover. Making it a selectable field with a documented
  default is the difference between a result and a fantasy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Limits. Enforced here rather than in the route so the CLI and the API agree.
# ---------------------------------------------------------------------------
MAX_SYMBOLS = 500
MAX_DAYS = 365 * 25
MIN_CAPITAL = 10_000.0
MAX_CAPITAL = 1_000_000_000.0

#: Cost models a run may select. Keys are the wire values.
COST_MODELS = ("india_delivery", "flat_per_share", "none")

#: Position sizing modes. `fixed_fraction` is the default because the other two
#: answer different questions: `fixed_quantity` ignores capital entirely (so its
#: return % is not the account's return), and `equal_weight` is only meaningful
#: when every symbol is traded every rebalance.
SIZING_MODES = ("fixed_fraction", "fixed_quantity", "equal_weight")

#: Timeframes a run may request. Only daily bars exist in the cache today; the
#: others are declared so the UI can disable them with a reason rather than
#: omit them and leave the user guessing.
SUPPORTED_TIMEFRAMES = ("1d",)
KNOWN_TIMEFRAMES = ("1d", "1h", "15m", "5m", "1m")

#: Where the bars come from.
SOURCES = ("cache", "synthetic")

#: Bars of history each built-in strategy needs before its rules can fire.
#:
#: This lives here rather than in the API layer because it is a property of
#: *running a strategy*, not of serving requests — the CLI needs it, the API
#: needs it, and the runner needs it. A missing entry means "no warmup", which
#: is correct for a strategy that only looks at the current bar.
WARMUP_BARS: dict[str, int] = {
    "sma_crossover": 50,
    "signals_entry": 110,
    "opening_range_breakout": 30,
    "cross_sectional_momentum": 130,
    # Paper models look back ~6 months and use a 200-day SMA.
    "momentum_breakout": 30,
    "paper_jegadeesh_titman": 260,
    "paper_avellaneda_lee": 260,
    "paper_volatility_breakout": 260,
    "paper_multi_factor_composite": 260,
    "paper_iima_nse_momentum": 260,
    "paper_nism_52w_high": 260,
    "paper_sehgal_low_vol": 260,
}


def warmup_bars_for(key: str | None) -> int:
    """Bars of warmup a strategy needs, or 0 if it has no lookback requirement."""
    return int(WARMUP_BARS.get(key or "", 0))


class ConfigError(ValueError):
    """A run configuration that cannot be executed as written."""

    def __init__(self, message: str, *, code: str = "invalid_config", field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass
class StopSpec:
    """Protective exit levels, all optional.

    Percentages are given as *percent* (2.0 means 2%), matching how every
    trading UI and the user's own notes express them. Converting to fractions
    happens in exactly one place — :meth:`as_fractions` — so a 100x error has
    one place to hide instead of four.
    """

    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None

    def as_fractions(self) -> dict[str, float | None]:
        return {
            "stop_loss_pct": (self.stop_loss_pct / 100.0) if self.stop_loss_pct is not None else None,
            "take_profit_pct": (self.take_profit_pct / 100.0) if self.take_profit_pct is not None else None,
            "trailing_stop_pct": (self.trailing_stop_pct / 100.0) if self.trailing_stop_pct is not None else None,
        }

    def validate(self) -> None:
        for name, value in (
            ("stop_loss_pct", self.stop_loss_pct),
            ("take_profit_pct", self.take_profit_pct),
            ("trailing_stop_pct", self.trailing_stop_pct),
        ):
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                raise ConfigError(f"{name} must be a number", field=name)
            if value <= 0:
                raise ConfigError(
                    f"{name} must be greater than 0 (got {value})", field=name
                )
            if value > 90:
                raise ConfigError(
                    f"{name} of {value}% would exit almost immediately; "
                    "keep it at or below 90",
                    field=name,
                )

    @property
    def any(self) -> bool:
        return any(
            v is not None
            for v in (self.stop_loss_pct, self.take_profit_pct, self.trailing_stop_pct)
        )


@dataclass
class SizingSpec:
    """How much of the account goes into a position."""

    mode: str = "fixed_fraction"
    #: Fraction of equity per position, as a percent (10.0 = 10%).
    percent: float = 10.0
    #: Used by `fixed_quantity` only. Shares per entry, ignoring capital.
    quantity: float = 100.0
    #: Cap so one position cannot exceed this share of equity.
    max_position_pct: float = 100.0

    def validate(self) -> None:
        if self.mode not in SIZING_MODES:
            raise ConfigError(
                f"unknown sizing mode {self.mode!r}; known: {', '.join(SIZING_MODES)}",
                code="unknown_sizing_mode",
                field="sizing.mode",
            )
        if self.mode == "fixed_fraction":
            if not isinstance(self.percent, (int, float)) or self.percent <= 0:
                raise ConfigError("sizing.percent must be greater than 0", field="sizing.percent")
            if self.percent > 100:
                raise ConfigError(
                    "sizing.percent cannot exceed 100 — a single position cannot be "
                    "more than the whole account without leverage",
                    field="sizing.percent",
                )
        if self.mode == "fixed_quantity" and (
            not isinstance(self.quantity, (int, float)) or self.quantity <= 0
        ):
            raise ConfigError("sizing.quantity must be greater than 0", field="sizing.quantity")
        if not isinstance(self.max_position_pct, (int, float)) or self.max_position_pct <= 0:
            raise ConfigError(
                "sizing.max_position_pct must be greater than 0",
                field="sizing.max_position_pct",
            )


@dataclass
class CostSpec:
    """Transaction costs and slippage."""

    model: str = "india_delivery"
    #: Slippage in basis points, applied against the trader on both legs.
    slippage_bps: float = 5.0

    def validate(self) -> None:
        if self.model not in COST_MODELS:
            raise ConfigError(
                f"unknown cost model {self.model!r}; known: {', '.join(COST_MODELS)}",
                code="unknown_cost_model",
                field="costs.model",
            )
        if not isinstance(self.slippage_bps, (int, float)):
            raise ConfigError("costs.slippage_bps must be a number", field="costs.slippage_bps")
        if self.slippage_bps < 0:
            raise ConfigError("costs.slippage_bps cannot be negative", field="costs.slippage_bps")
        if self.slippage_bps > 500:
            raise ConfigError(
                "costs.slippage_bps above 500 (5%) is not a slippage assumption, "
                "it is a fill failure",
                field="costs.slippage_bps",
            )


@dataclass
class BacktestRunConfig:
    """Everything a run needs. The single source of truth for reproducibility."""

    # --- what to run ---------------------------------------------------
    strategy: str = "sma_crossover"
    #: Registry key for a built-in strategy, OR a saved strategy version.
    engine_key: str | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None
    #: Parameters for the strategy constructor.
    params: dict = field(default_factory=dict)

    # --- over what -----------------------------------------------------
    universe: str | None = None
    symbols: list[str] = field(default_factory=list)
    exchange: str = "NSEEQ"
    timeframe: str = "1d"
    start: str | None = None
    end: str | None = None
    source: str = "cache"

    # --- with what -----------------------------------------------------
    initial_cash: float = 1_000_000.0
    sizing: SizingSpec = field(default_factory=SizingSpec)
    stops: StopSpec = field(default_factory=StopSpec)
    costs: CostSpec = field(default_factory=CostSpec)

    # --- engine knobs that change results ------------------------------
    allow_short: bool = False
    square_off_eod: bool = False
    risk_free_rate: float = 0.0
    benchmark: str | None = "NIFTYBEES"
    warmup_bars: int | None = None

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`ConfigError` if this cannot be run as written."""
        if not self.strategy and not self.engine_key:
            raise ConfigError("a run needs a strategy", code="missing_strategy", field="strategy")

        if self.timeframe not in KNOWN_TIMEFRAMES:
            raise ConfigError(
                f"unknown timeframe {self.timeframe!r}; known: {', '.join(KNOWN_TIMEFRAMES)}",
                code="unknown_timeframe",
                field="timeframe",
            )
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ConfigError(
                f"timeframe {self.timeframe!r} is not available yet — the local "
                f"cache holds daily bars only",
                code="unsupported_timeframe",
                field="timeframe",
            )

        if self.source not in SOURCES:
            raise ConfigError(
                f"unknown source {self.source!r}; known: {', '.join(SOURCES)}",
                code="unknown_source",
                field="source",
            )

        if self.source == "cache":
            if not self.symbols and not self.universe:
                raise ConfigError(
                    "choose a universe or list at least one symbol",
                    code="missing_universe",
                    field="symbols",
                )
            if len(self.symbols) > MAX_SYMBOLS:
                raise ConfigError(
                    f"{len(self.symbols)} symbols is above the {MAX_SYMBOLS} limit "
                    "for a single run; narrow the universe",
                    code="universe_too_large",
                    field="symbols",
                )

        if not isinstance(self.initial_cash, (int, float)):
            raise ConfigError("initial_cash must be a number", field="initial_cash")
        if self.initial_cash < MIN_CAPITAL:
            raise ConfigError(
                f"starting capital must be at least {MIN_CAPITAL:,.0f} — "
                "below that, brokerage dominates the result",
                field="initial_cash",
            )
        if self.initial_cash > MAX_CAPITAL:
            raise ConfigError(
                f"starting capital above {MAX_CAPITAL:,.0f} is out of range",
                field="initial_cash",
            )

        start = self._as_date(self.start, "start")
        end = self._as_date(self.end, "end")
        if start and end:
            if start >= end:
                raise ConfigError(
                    f"start ({start}) must be before end ({end})",
                    code="empty_date_range",
                    field="start",
                )
            if (end - start).days > MAX_DAYS:
                raise ConfigError(
                    f"a {MAX_DAYS}-day window is the maximum", field="start"
                )

        self.sizing.validate()
        self.stops.validate()
        self.costs.validate()

        if not isinstance(self.params, dict):
            raise ConfigError("params must be an object", field="params")

    # ------------------------------------------------------------------
    @staticmethod
    def _as_date(value: str | date | None, field_name: str) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except ValueError as exc:
            raise ConfigError(
                f"{field_name} must be an ISO date (YYYY-MM-DD), got {value!r}",
                code="bad_date",
                field=field_name,
            ) from exc

    @property
    def start_date(self) -> date | None:
        return self._as_date(self.start, "start")

    @property
    def end_date(self) -> date | None:
        return self._as_date(self.end, "end")

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Plain JSON-safe dict. This is what gets stored and replayed."""
        return json.loads(json.dumps(asdict(self), default=str))

    def fingerprint(self) -> str:
        """Stable identity of this *request*.

        Sorted keys and no incidental whitespace, so two runs with the same
        meaning hash the same even if one came from a form and the other from
        the CLI.
        """
        return config_fingerprint(self.to_dict())

    @property
    def label(self) -> str:
        """Short human description, used for auto-naming saved runs."""
        n = len(self.symbols)
        where = self.universe or (f"{n} symbol{'s' if n != 1 else ''}" if n else "universe")
        span = f"{self.start or 'all'}→{self.end or 'latest'}"
        return f"{self.strategy} · {where} · {span}"


def config_fingerprint(payload: dict) -> str:
    """sha256 of the canonical JSON of a config."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_series(frames: dict) -> str:
    """Fingerprint the price data a run actually consumed.

    Hashes shape, span and a per-symbol digest of the close column — not the
    whole frame. A run is reproducible if the closes and their timestamps are
    the same; open/high/low differences that never touch a fill would otherwise
    make two identical results look like different data.
    """
    parts: list[str] = []
    for symbol in sorted(frames):
        frame = frames[symbol]
        if frame is None or len(frame) == 0:
            parts.append(f"{symbol}:empty")
            continue
        close = frame["close"]
        digest = hashlib.sha256(str(close.to_numpy(dtype=float).tobytes()).encode("latin-1")).hexdigest()[:16]
        first = str(frame.index[0])[:19] if hasattr(frame, "index") else "?"
        last = str(frame.index[-1])[:19] if hasattr(frame, "index") else "?"
        parts.append(f"{symbol}:{len(frame)}:{first}:{last}:{digest}")
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def from_payload(payload: dict) -> BacktestRunConfig:
    """Build a config from untrusted JSON (an API body or a stored row)."""
    if not isinstance(payload, dict):
        raise ConfigError("config must be an object")

    known = {f for f in BacktestRunConfig.__dataclass_fields__}
    scalars = {k: v for k, v in payload.items() if k in known and k not in {"sizing", "stops", "costs"}}

    sizing_raw = payload.get("sizing") or {}
    stops_raw = payload.get("stops") or {}
    costs_raw = payload.get("costs") or {}

    def build(cls, raw):
        if not isinstance(raw, dict):
            raise ConfigError(f"{cls.__name__} must be an object")
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})

    config = BacktestRunConfig(
        **scalars,
        sizing=build(SizingSpec, sizing_raw),
        stops=build(StopSpec, stops_raw),
        costs=build(CostSpec, costs_raw),
    )
    config.validate()
    return config


__all__ = [
    "BacktestRunConfig",
    "COST_MODELS",
    "ConfigError",
    "CostSpec",
    "KNOWN_TIMEFRAMES",
    "MAX_SYMBOLS",
    "SIZING_MODES",
    "SOURCES",
    "SUPPORTED_TIMEFRAMES",
    "SizingSpec",
    "StopSpec",
    "WARMUP_BARS",
    "config_fingerprint",
    "fingerprint_series",
    "from_payload",
    "warmup_bars_for",
]
