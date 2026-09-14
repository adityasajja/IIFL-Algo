"""Risk state: the kill switch, the execution mode, and the limits.

Everything here used to be an in-process dict in ``atr/api/main.py``. That had
two consequences worth naming, because both are safety failures rather than
cosmetic ones:

* **A restart re-armed trading.** The kill switch and the paper/live mode lived in
  memory, so bouncing the process silently cleared a decision an operator had made
  *because something was wrong*.
* **There was one reader per route.** Each endpoint looked at the dict and decided
  for itself, so "is the kill switch engaged?" had as many answers as there were
  routes. The manual order route did not ask at all.

So the state is durable (``system_state``, a key/value table) and it is read
through one service. The gate the OMS uses is built from it, which is what makes
the kill switch apply to every order path rather than to the two that remembered.

The three transitions that can lose real money — engaging the kill switch,
releasing it, and going live — require a non-empty reason. That is deliberate
friction: the sentence is what shows up in the audit trail when someone asks why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from atr.appdb.engine import AppDatabase, get_app_db
from atr.appdb.repositories import SystemStateRepository
from atr.execution.risk import RiskLimits

logger = logging.getLogger("atr.services.risk")

#: Keys in ``system_state``. Namespaced so a future key cannot collide with these.
KEY_KILL_SWITCH = "risk.kill_switch"
KEY_EXECUTION_MODE = "risk.execution_mode"
KEY_MODE_META = "risk.execution_mode.meta"
KEY_LIMITS = "risk.limits"

MODES = ("paper", "live")

#: Limits that may be set through the API. Anything not listed is not configurable
#: from a request body — a route that accepted arbitrary ``RiskLimits`` fields
#: would let a caller relax a limit the product does not intend to expose.
CONFIGURABLE_LIMITS: tuple[str, ...] = (
    "max_gross_exposure",
    "max_position_notional",
    "max_position_per_symbol",
    "max_daily_loss",
    "max_daily_trades",
    "max_open_positions",
    "max_order_notional",
    "allow_short",
    "allowed_symbols",
    "trading_window",
)


class RiskStateError(Exception):
    """A risk-state change was refused. Carries the code the API returns."""

    def __init__(self, message: str, *, code: str = "risk_state_error", status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class RiskStateSnapshot:
    """What the risk controls currently are. Frozen: a caller cannot mutate state."""

    kill_switch: bool
    execution_mode: str
    changed_at: str | None = None
    changed_by: str | None = None
    reason: str | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)

    @property
    def live(self) -> bool:
        return self.execution_mode == "live"

    def as_dict(self) -> dict[str, Any]:
        """The shape the dashboard reads. ``limits`` is flattened for the UI."""
        return {
            "kill_switch": self.kill_switch,
            "execution_mode": self.execution_mode,
            "live": self.live,
            "paper": not self.live,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
            "reason": self.reason,
            "limits": _limits_as_dict(self.limits),
        }


def _limits_as_dict(limits: RiskLimits) -> dict[str, Any]:
    """Serialise limits, turning ``inf`` into ``None``.

    ``max_position_notional`` defaults to ``float("inf")``, and JSON has no
    infinity — it would come out as the literal string ``Infinity``, which is not
    valid JSON and which a JavaScript client would parse to ``null`` or fail on.
    ``None`` means "no limit", which is what ``inf`` means.
    """
    out: dict[str, Any] = {}
    for name in CONFIGURABLE_LIMITS:
        value = getattr(limits, name, None)
        if isinstance(value, float) and value == float("inf"):
            out[name] = None
        elif isinstance(value, set):
            out[name] = sorted(value)
        elif isinstance(value, tuple):
            out[name] = [v.isoformat() if hasattr(v, "isoformat") else v for v in value]
        else:
            out[name] = value
    return out


def limits_from_dict(payload: dict[str, Any] | None) -> RiskLimits:
    """Rebuild :class:`RiskLimits` from stored JSON, ignoring unknown keys.

    Unknown keys are dropped rather than passed through: ``RiskLimits`` is a
    dataclass, and a stored key that no longer exists would raise on construction
    and take the whole risk state with it.
    """
    if not payload:
        return RiskLimits()
    known = {f for f in RiskLimits.__dataclass_fields__}
    kwargs: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in known or key not in CONFIGURABLE_LIMITS:
            continue
        if key == "allowed_symbols":
            kwargs[key] = set(value) if value else None
        elif key == "trading_window":
            kwargs[key] = _parse_window(value)
        elif key == "allow_short":
            kwargs[key] = bool(value)
        elif value is None:
            # `None` means "no limit". Only the optional limits accept it.
            continue
        else:
            kwargs[key] = float(value) if not isinstance(value, bool) else value
    return RiskLimits(**kwargs)


def _parse_window(value: Any) -> Any:
    """``["09:15", "15:30"]`` → ``(time(9,15), time(15,30))``. Bad input → no window."""
    from datetime import time

    if not value or not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        parsed = tuple(time.fromisoformat(str(v)) for v in value)
    except ValueError:
        logger.warning("ignoring unparseable trading window %r", value)
        return None
    return parsed if parsed[0] <= parsed[1] else None


@dataclass
class RiskStateService:
    """Reads and writes the durable risk state.

    Every write that changes what the platform will do to real money demands a
    reason, and every write records the actor. The *service* enforces that rather
    than the route, so a second caller cannot skip it.
    """

    db: AppDatabase = field(default_factory=get_app_db)

    # ------------------------------------------------------------------ reads
    def snapshot(self) -> RiskStateSnapshot:
        with self.db.session() as session:
            return self._snapshot_in(session)

    def _snapshot_in(self, session: Any) -> RiskStateSnapshot:
        meta = SystemStateRepository.get_value(session, KEY_MODE_META, {}) or {}
        raw_limits = SystemStateRepository.get_value(session, KEY_LIMITS, {}) or {}
        return RiskStateSnapshot(
            kill_switch=bool(SystemStateRepository.get_value(session, KEY_KILL_SWITCH, False)),
            execution_mode=str(SystemStateRepository.get_value(session, KEY_EXECUTION_MODE, "paper")),
            changed_at=meta.get("changed_at"),
            changed_by=meta.get("changed_by"),
            reason=meta.get("reason"),
            limits=limits_from_dict(raw_limits),
        )

    def limits(self) -> RiskLimits:
        return self.snapshot().limits

    def kill_switch_engaged(self) -> bool:
        return self.snapshot().kill_switch

    def execution_mode(self) -> str:
        return self.snapshot().execution_mode

    # ----------------------------------------------------------------- writes
    def set_kill_switch(
        self, engaged: bool, *, reason: str, actor: str
    ) -> RiskStateSnapshot:
        """Engage or release the global kill switch.

        Both directions require a reason. Releasing it is arguably the more
        consequential of the two — it re-enables trading — and a release with no
        recorded reason is indistinguishable from someone clearing it by accident.
        """
        reason = (reason or "").strip()
        if not reason:
            raise RiskStateError(
                "changing the kill switch requires a reason", code="reason_required"
            )
        with self.db.session() as session:
            previous = bool(SystemStateRepository.get_value(session, KEY_KILL_SWITCH, False))
            SystemStateRepository.set(
                session, KEY_KILL_SWITCH, bool(engaged), actor=actor, reason=reason
            )
            if previous != bool(engaged):
                logger.warning(
                    "kill switch %s by %s (%s)",
                    "ENGAGED" if engaged else "RELEASED",
                    actor,
                    reason,
                )
            return self._snapshot_in(session)

    def set_execution_mode(self, mode: str, *, reason: str, actor: str) -> RiskStateSnapshot:
        """Switch between ``paper`` and ``live``.

        Going live requires a reason; coming back to paper does not, because
        reducing risk should never be harder than taking it. The asymmetry is
        deliberate.
        """
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            raise RiskStateError(
                f"mode must be one of {list(MODES)}", code="bad_mode"
            )
        reason = (reason or "").strip()
        if mode == "live" and not reason:
            raise RiskStateError(
                "switching to live requires a reason — it is recorded in the audit trail",
                code="reason_required",
            )
        with self.db.session() as session:
            previous = str(SystemStateRepository.get_value(session, KEY_EXECUTION_MODE, "paper"))
            SystemStateRepository.set(session, KEY_EXECUTION_MODE, mode, actor=actor, reason=reason or None)
            from atr.appdb.engine import utcnow

            SystemStateRepository.set(
                session,
                KEY_MODE_META,
                {
                    "changed_at": utcnow().isoformat(timespec="seconds") + "Z",
                    "changed_by": actor,
                    "reason": reason or None,
                    "previous": previous,
                },
                actor=actor,
                reason=reason or None,
            )
            if previous != mode:
                logger.warning("execution mode %s -> %s by %s (%s)", previous, mode, actor, reason or "no reason")
            return self._snapshot_in(session)

    def set_limits(self, payload: dict[str, Any], *, actor: str) -> RiskStateSnapshot:
        """Replace the configured limits.

        Wholesale replacement rather than a merge, so "set this limit back to
        unlimited" is expressible — a merge cannot distinguish "absent" from
        "cleared".
        """
        unknown = sorted(set(payload) - set(CONFIGURABLE_LIMITS))
        if unknown:
            raise RiskStateError(
                f"unknown limit(s): {', '.join(unknown)}", code="unknown_limit"
            )
        try:
            # Construct once to validate before storing: a limit set that cannot
            # be rebuilt would poison every later read of the risk state.
            candidate = limits_from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise RiskStateError(f"invalid limits: {exc}", code="invalid_limit") from exc
        with self.db.session() as session:
            SystemStateRepository.set(
                session, KEY_LIMITS, _limits_as_dict(candidate), actor=actor, reason=None
            )
            return self._snapshot_in(session)

    # ------------------------------------------------------------------- gate
    def gate(self, *, portfolio: Any = None, instruments: Any = None) -> Any:
        """Build the OMS risk gate from the current state.

        Returns a :class:`~atr.execution.oms.LimitsRiskGate` over a
        :class:`~atr.execution.risk.RiskEngine` whose ``kill_switch`` is the
        durable flag. This is the single place the kill switch enters the order
        path, which is what makes it apply to every route instead of the two that
        remembered to ask.

        ``portfolio`` and ``instruments`` are injected because only the caller
        knows whether it is the paper engine or the live broker asking.
        """
        from atr.execution.oms import LimitsRiskGate
        from atr.execution.risk import RiskEngine

        snapshot = self.snapshot()
        limits = snapshot.limits
        # The kill switch is read at gate-construction time; the OMS builds a gate
        # per request, so engaging it takes effect on the next order.
        limits.kill_switch = snapshot.kill_switch
        return LimitsRiskGate(
            engine=RiskEngine(limits=limits),
            portfolio=portfolio,
            instruments=instruments,
        )


def reset_risk_state_cache() -> None:
    """Kept for symmetry with the other singletons; the service holds no cache.

    ``RiskStateService`` reads through to the database on every call, so there is
    nothing to clear. This exists so a future cache can be added without every
    test fixture needing to change.
    """
    return None


__all__ = [
    "CONFIGURABLE_LIMITS",
    "KEY_EXECUTION_MODE",
    "KEY_KILL_SWITCH",
    "KEY_LIMITS",
    "KEY_MODE_META",
    "MODES",
    "RiskStateError",
    "RiskStateService",
    "RiskStateSnapshot",
    "limits_from_dict",
    "reset_risk_state_cache",
]
