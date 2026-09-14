"""Reconciliation: compare what the platform believes against what the broker reports.

Why this is a safety control and not a report
---------------------------------------------
Everything else in this platform reasons from its own records. That is fine until
a record is wrong, and then every conclusion drawn from it is wrong in the same
direction and with no symptom. Reconciliation is the only thing that compares the
platform's belief against an outside source, which is what makes it the control
that catches everything else.

The dangerous direction is asymmetric, and the code reflects that:

* **An order the broker holds and the platform does not know about** is the worst
  case. Something is live that nothing is managing — no stop, no square-off, no
  risk check. It is `critical`.
* **An order the platform thinks is live and the broker does not have** is also
  `critical`, but for a different reason: the platform will keep waiting for a fill
  that can never come, and its position arithmetic is already wrong.
* **A position that disagrees** is `critical`. Every risk limit is measured against
  positions, so a wrong position means the limits are guarding the wrong thing.
* **A holding that disagrees** is a `warning`. On a T+1 market a holding and a
  position legitimately differ for a day after every trade; calling that critical
  would train an operator to ignore the alarm.
* **A comparison that cannot be made** is a `warning` with a reason, never `ok`.
  The platform has no cash ledger for a live account, so `funds` is not comparable
  today — and a run that says "ok" while skipping a scope is the silent failure
  this module exists to prevent.

Nothing here swallows a failure. A run that could not read the broker records the
error and a severity that is not `ok`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from atr.appdb.engine import AppDatabase, get_app_db, utcnow
from atr.appdb.repositories import OrderRepository, ReconciliationRepository

logger = logging.getLogger("atr.services.reconcile")

SCOPE_ORDERS = "orders"
SCOPE_POSITIONS = "positions"
SCOPE_HOLDINGS = "holdings"
SCOPE_FUNDS = "funds"
SCOPE_ALL = "all"

SCOPES: tuple[str, ...] = (SCOPE_ORDERS, SCOPE_POSITIONS, SCOPE_HOLDINGS, SCOPE_FUNDS)

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: Quantities are floats; a share is not a continuous quantity, so anything below
#: this is the same quantity arrived at by two different routes.
QUANTITY_TOLERANCE = 1e-6
#: Cash is compared with a tolerance because brokers report rounded figures.
CASH_TOLERANCE = 1.0


@dataclass(frozen=True)
class Difference:
    """One discrepancy, as a fact with a shape rather than a log line."""

    scope: str
    key: str
    kind: str
    severity: str
    detail: str
    platform: Any = None
    broker: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "platform": self.platform,
            "broker": self.broker,
        }


@dataclass
class ScopeResult:
    """What one comparison found, including whether it could run at all."""

    scope: str
    compared: bool
    platform_count: int = 0
    broker_count: int = 0
    differences: list[Difference] = field(default_factory=list)
    #: Why the comparison did not happen. Set exactly when ``compared`` is False.
    skipped_reason: str | None = None

    @property
    def severity(self) -> str:
        if not self.compared:
            return SEVERITY_WARNING
        if any(d.severity == SEVERITY_CRITICAL for d in self.differences):
            return SEVERITY_CRITICAL
        if self.differences:
            return SEVERITY_WARNING
        return SEVERITY_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "compared": self.compared,
            "skipped_reason": self.skipped_reason,
            "platform_count": self.platform_count,
            "broker_count": self.broker_count,
            "severity": self.severity,
            "differences": [d.as_dict() for d in self.differences],
        }


@dataclass
class ReconciliationReport:
    """The result of one run, and the thing that gets persisted."""

    scope: str
    severity: str
    results: list[ScopeResult]
    run_id: str | None = None
    duration_ms: int | None = None
    error: str | None = None

    @property
    def differences(self) -> list[Difference]:
        return [d for r in self.results for d in r.differences]

    @property
    def difference_count(self) -> int:
        """Real discrepancies: the platform and the broker disagree."""
        return len(self.differences)

    @property
    def mismatch_count(self) -> int:
        """Everything that stops this run being ``ok``: discrepancies **and**
        scopes that could not be compared.

        A scope that was skipped is a reason not to report a clean run, so it
        belongs in the same count — and it has to be the same number that gets
        stored, or the row and the response would disagree about how much was
        wrong. ``difference_count`` is the strict count for a caller that wants
        only the disagreements.
        """
        return self.difference_count + len(self.incomparable)

    @property
    def incomparable(self) -> list[dict[str, str]]:
        """Scopes that could not be compared, with the reason.

        Surfaced rather than hidden: a clean-looking run that silently skipped half
        its scopes is worse than one that reports a problem.
        """
        return [
            {"scope": r.scope, "reason": r.skipped_reason or "not compared"}
            for r in self.results
            if not r.compared
        ]

    @property
    def critical(self) -> list[Difference]:
        return [d for d in self.differences if d.severity == SEVERITY_CRITICAL]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": self.scope,
            "severity": self.severity,
            # `mismatches` is what the stored row counts, so the two agree.
            "mismatches": self.mismatch_count,
            "differences": self.difference_count,
            "critical": len(self.critical),
            "incomparable": self.incomparable,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "results": [r.as_dict() for r in self.results],
            "detail": [d.as_dict() for d in self.differences],
        }


# =========================================================================== comparisons
def compare_orders(
    platform_orders: Iterable[dict[str, Any]], broker_orders: Iterable[Any]
) -> ScopeResult:
    """Compare open orders by broker order id.

    Platform orders with no ``broker_order_id`` are excluded: one that has not been
    acknowledged is not yet expected at the broker, and counting it would report a
    mismatch every time an order was in flight — which is most of the time, and
    would make the control useless.
    """
    platform = {
        str(o["broker_order_id"]): o
        for o in platform_orders
        if o.get("broker_order_id")
    }
    broker = {
        str(getattr(o, "broker_order_id", "") or ""): o
        for o in broker_orders
        if getattr(o, "broker_order_id", None)
    }

    differences: list[Difference] = []
    for order_id, order in broker.items():
        if order_id not in platform:
            differences.append(
                Difference(
                    scope=SCOPE_ORDERS,
                    key=order_id,
                    kind="unknown_to_platform",
                    severity=SEVERITY_CRITICAL,
                    detail=(
                        "the broker holds an order the platform has no record of — "
                        "nothing is managing it"
                    ),
                    broker=getattr(getattr(order, "instrument", None), "symbol", None),
                )
            )
    for order_id, order in platform.items():
        if order_id not in broker:
            differences.append(
                Difference(
                    scope=SCOPE_ORDERS,
                    key=order_id,
                    kind="missing_at_broker",
                    severity=SEVERITY_CRITICAL,
                    detail=(
                        "the platform is waiting on an order the broker does not have; "
                        "its position arithmetic is already wrong"
                    ),
                    platform=order.get("symbol"),
                )
            )
    return ScopeResult(
        scope=SCOPE_ORDERS,
        compared=True,
        platform_count=len(platform),
        broker_count=len(broker),
        differences=differences,
    )


def compare_positions(
    platform_positions: dict[str, float], broker_positions: Iterable[Any]
) -> ScopeResult:
    """Compare net quantity per symbol. Any difference is critical.

    Every risk limit is measured against positions, so a position that disagrees
    means the limits are guarding a book that does not exist.
    """
    broker = {
        p.instrument.symbol: float(p.quantity)
        for p in broker_positions
        if abs(float(p.quantity)) > QUANTITY_TOLERANCE
    }
    differences: list[Difference] = []
    for symbol in sorted(set(platform_positions) | set(broker)):
        ours = float(platform_positions.get(symbol, 0.0))
        theirs = float(broker.get(symbol, 0.0))
        if abs(ours - theirs) > QUANTITY_TOLERANCE:
            differences.append(
                Difference(
                    scope=SCOPE_POSITIONS,
                    key=symbol,
                    kind="quantity",
                    severity=SEVERITY_CRITICAL,
                    detail=(
                        f"platform {ours:g} vs broker {theirs:g} — every risk limit is "
                        "measured against the platform's figure"
                    ),
                    platform=ours,
                    broker=theirs,
                )
            )
    return ScopeResult(
        scope=SCOPE_POSITIONS,
        compared=True,
        platform_count=len(platform_positions),
        broker_count=len(broker),
        differences=differences,
    )


def compare_holdings(
    platform_positions: dict[str, float], broker_holdings: Iterable[Any]
) -> ScopeResult:
    """Compare settled holdings against the platform's positions.

    A ``warning`` rather than critical, and the reason is mechanical: on a T+1
    market a purchase is a position today and a holding tomorrow, so the two differ
    legitimately for a day after every trade. Treating that as critical is how an
    alarm gets ignored.
    """
    broker = {
        h.instrument.symbol: float(h.quantity)
        for h in broker_holdings
        if abs(float(h.quantity)) > QUANTITY_TOLERANCE
    }
    differences: list[Difference] = []
    for symbol in sorted(set(platform_positions) | set(broker)):
        ours = float(platform_positions.get(symbol, 0.0))
        theirs = float(broker.get(symbol, 0.0))
        if abs(ours - theirs) > QUANTITY_TOLERANCE:
            differences.append(
                Difference(
                    scope=SCOPE_HOLDINGS,
                    key=symbol,
                    kind="quantity",
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"platform {ours:g} vs broker holding {theirs:g} — usually "
                        "settlement, occasionally a trade the platform did not place"
                    ),
                    platform=ours,
                    broker=theirs,
                )
            )
    return ScopeResult(
        scope=SCOPE_HOLDINGS,
        compared=True,
        platform_count=len(platform_positions),
        broker_count=len(broker),
        differences=differences,
    )


def compare_funds(platform_cash: float | None, broker_funds: Any) -> ScopeResult:
    """Compare cash, when both sides have a figure.

    ``platform_cash`` is ``None`` on the live path because the platform keeps no
    cash ledger for a live account — that is a real gap, and it is reported as
    "not comparable" with the reason rather than as a clean comparison. A run that
    says ``ok`` while skipping a scope is the silent failure this module exists to
    catch.
    """
    if platform_cash is None:
        return ScopeResult(
            scope=SCOPE_FUNDS,
            compared=False,
            broker_count=1,
            skipped_reason=(
                "the platform keeps no cash ledger for a live account, so there is "
                "nothing to compare the broker's balance against"
            ),
        )
    if broker_funds is None:
        return ScopeResult(
            scope=SCOPE_FUNDS,
            compared=False,
            platform_count=1,
            skipped_reason="the broker did not report funds",
        )

    theirs = float(getattr(broker_funds, "available_cash", 0.0))
    difference = abs(float(platform_cash) - theirs)
    differences: list[Difference] = []
    if difference > CASH_TOLERANCE:
        differences.append(
            Difference(
                scope=SCOPE_FUNDS,
                key="available_cash",
                kind="cash",
                severity=SEVERITY_CRITICAL,
                detail=f"platform {platform_cash:,.2f} vs broker {theirs:,.2f}",
                platform=float(platform_cash),
                broker=theirs,
            )
        )
    return ScopeResult(
        scope=SCOPE_FUNDS,
        compared=True,
        platform_count=1,
        broker_count=1,
        differences=differences,
    )


# =========================================================================== the service
@dataclass
class ReconciliationService:
    """Runs the comparisons, persists the result, and refuses to report a clean run
    when it could not actually check something."""

    db: AppDatabase = field(default_factory=get_app_db)

    def run(
        self,
        user_id: str,
        *,
        broker: Any,
        scopes: Iterable[str] | None = None,
        platform_cash: float | None = None,
        persist: bool = True,
        actor: str | None = None,
        notifier: Callable[[ReconciliationReport], Any] | None = None,
    ) -> ReconciliationReport:
        """Compare the platform's records against the broker's.

        A failure reading the broker does **not** raise: it produces a report whose
        severity is not ``ok`` and whose ``error`` names the cause. Reconciliation
        that throws is reconciliation that stops running, and this is the control
        that has to keep running.
        """
        started = utcnow()
        # A bare string is accepted as one scope. Iterating it would split it into
        # characters and produce "unknown scope(s): d, e, o, r, s", which tells a
        # caller nothing about the mistake they actually made.
        if isinstance(scopes, str):
            scopes = [scopes]
        wanted = tuple(scopes) if scopes else SCOPES
        unknown = sorted(set(wanted) - set(SCOPES))
        if unknown:
            raise ValueError(f"unknown reconciliation scope(s): {', '.join(unknown)}")

        results: list[ScopeResult] = []
        error: str | None = None
        try:
            results = self._compare(
                user_id, broker=broker, scopes=wanted, platform_cash=platform_cash
            )
        except Exception as exc:  # noqa: BLE001 - a run that threw must still be recorded
            logger.exception("reconciliation against the broker failed")
            error = f"{type(exc).__name__}: {exc}"
            results = [
                ScopeResult(
                    scope=scope,
                    compared=False,
                    skipped_reason="the run failed before this scope was reached",
                )
                for scope in wanted
            ]

        severity = _roll_up(results, error)
        duration_ms = max(0, int((utcnow() - started).total_seconds() * 1000))
        report = ReconciliationReport(
            scope=SCOPE_ALL if len(wanted) == len(SCOPES) else ",".join(wanted),
            severity=severity,
            results=results,
            duration_ms=duration_ms,
            error=error,
        )

        if persist:
            self._persist(user_id, report, actor=actor)

        if severity == SEVERITY_CRITICAL:
            logger.critical(
                "reconciliation found %d critical difference(s): %s",
                len(report.critical),
                [f"{d.scope}/{d.key} ({d.kind})" for d in report.critical],
            )
        if notifier is not None:
            self._notify(notifier, report)
        return report

    def _compare(
        self,
        user_id: str,
        *,
        broker: Any,
        scopes: tuple[str, ...],
        platform_cash: float | None,
    ) -> list[ScopeResult]:
        platform_positions = self.platform_positions(user_id)
        results: list[ScopeResult] = []

        for scope in scopes:
            if scope == SCOPE_ORDERS:
                results.append(
                    compare_orders(self.open_orders(user_id), broker.open_orders())
                )
            elif scope == SCOPE_POSITIONS:
                results.append(compare_positions(platform_positions, broker.positions()))
            elif scope == SCOPE_HOLDINGS:
                results.append(self._holdings(platform_positions, broker))
            elif scope == SCOPE_FUNDS:
                results.append(self._funds(platform_cash, broker))
        return results

    @staticmethod
    def _holdings(platform_positions: dict[str, float], broker: Any) -> ScopeResult:
        """Holdings, or an explicit "the broker cannot report them"."""
        try:
            return compare_holdings(platform_positions, broker.holdings())
        except NotImplementedError as exc:
            return ScopeResult(
                scope=SCOPE_HOLDINGS, compared=False, skipped_reason=str(exc)
            )

    @staticmethod
    def _funds(platform_cash: float | None, broker: Any) -> ScopeResult:
        try:
            funds = broker.funds()
        except NotImplementedError as exc:
            return ScopeResult(
                scope=SCOPE_FUNDS, compared=False, skipped_reason=str(exc)
            )
        return compare_funds(platform_cash, funds)

    # ------------------------------------------------------------------ platform side
    def platform_positions(self, user_id: str) -> dict[str, float]:
        """The platform's net quantity per symbol, folded from ``order_events``.

        The same fold the paper ledger uses, and the same one the risk gate is
        handed. It is mode-agnostic: it reads the order log, which is written
        identically for paper and live. (``PaperLedger`` is named for its first
        caller; it is really an order-event ledger.)
        """
        from atr.services.paper import PaperLedger

        portfolio = PaperLedger(self.db).portfolio(user_id)
        return {
            symbol: float(position.quantity)
            for symbol, position in portfolio.positions.items()
            if abs(float(position.quantity)) > QUANTITY_TOLERANCE
        }

    def open_orders(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return OrderRepository.open_orders(session, user_id)

    # ------------------------------------------------------------------ persistence
    def _persist(
        self, user_id: str, report: ReconciliationReport, *, actor: str | None
    ) -> None:
        details: list[dict[str, Any]] = [d.as_dict() for d in report.differences]
        # Incomparable scopes go in the same list so a reader sees one array of
        # "things that were not right", and the severity rule keeps them from
        # being recorded as ok.
        details.extend(
            {
                "scope": entry["scope"],
                "key": entry["scope"],
                "kind": "not_comparable",
                "severity": SEVERITY_WARNING,
                "detail": entry["reason"],
            }
            for entry in report.incomparable
        )
        try:
            with self.db.session() as session:
                row = ReconciliationRepository.record(
                    session,
                    user_id=user_id,
                    scope=report.scope,
                    mismatches=details,
                    severity=report.severity,
                    internal_count=sum(r.platform_count for r in report.results),
                    broker_count=sum(r.broker_count for r in report.results),
                    error=report.error,
                    duration_ms=report.duration_ms,
                )
                report.run_id = row["run_id"]
                self._audit(session, user_id, report, actor=actor)
        except Exception:  # noqa: BLE001 - failing to record must not lose the report
            logger.exception("could not persist the reconciliation run")

    @staticmethod
    def _audit(
        session: Any,
        user_id: str,
        report: ReconciliationReport,
        *,
        actor: str | None,
    ) -> None:
        """Write the audit row, and never let it break the run.

        A critical mismatch is an event worth a permanent record with a name
        attached, which is what makes it something somebody can be asked about.
        """
        try:
            from atr.appdb.repositories import AuditRepository

            AuditRepository.append(
                session,
                action="reconciliation.run",
                result="failure" if report.severity != SEVERITY_OK else "success",
                user_id=user_id,
                actor=actor or "system",
                target_type="reconciliation",
                target_id=report.run_id,
                detail={
                    "scope": report.scope,
                    "severity": report.severity,
                    "mismatches": report.mismatch_count,
                    "incomparable": report.incomparable,
                    "critical": [f"{d.scope}/{d.key}" for d in report.critical],
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not write the reconciliation audit row")

    @staticmethod
    def _notify(notifier: Callable[[ReconciliationReport], Any], report: ReconciliationReport) -> None:
        """Hand the report to a notifier, which must not be able to break the run."""
        try:
            notifier(report)
        except Exception:  # noqa: BLE001 - a failed notification is not a failed check
            logger.exception("reconciliation notifier failed")

    # ------------------------------------------------------------------ reads
    def latest(self, user_id: str, *, scope: str | None = None) -> dict[str, Any] | None:
        with self.db.session() as session:
            return ReconciliationRepository.latest(session, user_id, scope=scope)

    def history(
        self, user_id: str, *, severity: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        with self.db.session() as session:
            return ReconciliationRepository.list_for_user(
                session, user_id, severity=severity, limit=limit
            )

    def status(self, user_id: str) -> dict[str, Any]:
        """The dashboard surface: the most recent run that was not clean.

        Returns ``None`` once a clean run supersedes it, so a banner clears when
        the problem is fixed rather than waiting to be dismissed.
        """
        with self.db.session() as session:
            worst = ReconciliationRepository.worst_unacknowledged(session, user_id)
        if worst is None:
            return {"clear": True, "run": None}
        return {"clear": False, "run": worst}


def _roll_up(results: list[ScopeResult], error: str | None) -> str:
    """The run's severity: the worst scope, and never ``ok`` when something failed.

    An error is at least a warning. A run that could not read the broker has not
    established that anything is right, and saying ``ok`` would be a claim it
    cannot support.
    """
    if any(r.severity == SEVERITY_CRITICAL for r in results):
        return SEVERITY_CRITICAL
    if error:
        return SEVERITY_WARNING
    if any(r.severity == SEVERITY_WARNING for r in results):
        return SEVERITY_WARNING
    return SEVERITY_OK


__all__ = [
    "CASH_TOLERANCE",
    "QUANTITY_TOLERANCE",
    "SCOPES",
    "SCOPE_ALL",
    "SCOPE_FUNDS",
    "SCOPE_HOLDINGS",
    "SCOPE_ORDERS",
    "SCOPE_POSITIONS",
    "SEVERITY_CRITICAL",
    "SEVERITY_OK",
    "SEVERITY_WARNING",
    "Difference",
    "ReconciliationReport",
    "ReconciliationService",
    "ScopeResult",
    "compare_funds",
    "compare_holdings",
    "compare_orders",
    "compare_positions",
]
