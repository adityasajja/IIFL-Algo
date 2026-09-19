"""Portfolio-level risk & capital allocation for cash-equity algos.

The enforced order path is::

    Strategy → Strategy Risk → Portfolio Risk → OMS → Paper/Live Venue

Strategy risk is the existing per-deployment gate (``LimitsRiskGate`` over the
deployment's own folded book). Portfolio risk is this module: one gate over the
*aggregate* book — every deployment, every strategy — consulted inside
``OrderService.validate`` after the strategy gate passes and before anything
reaches ``RISK_APPROVED``. There is no second path to submission, so neither a
strategy nor the learning tier can bypass it: both place through the OMS.

What this module owns, and what it explicitly does not
-------------------------------------------------------
* **Capital allocation** is the deployment row's ``capital`` (it already
  exists). This module reads it, sums it, and refuses to over-allocate a
  strategy — it never sets it.
* **Limits** live in one ``portfolio_policies`` row per user. No row means no
  portfolio limits: the gate passes everything through, which is the
  pre-policy behaviour, and every dashboard figure is still computed.
* **Conflicts are displayed always, enforced per policy.** Opposite holdings
  across deployments are shown whatever the mode; whether an incoming order
  against them fills is the operator's configured rule (``reject``,
  ``priority`` or ``net``). The system never picks a winner on its own: ties
  fail closed, and "net" still counts gross exposure against the size limits.
* **Exits are never trapped.** An order that strictly reduces its own arm's
  position bypasses the exposure and conflict checks — blocking an exit would
  lock a loss in place. The strategy gate still applies.
* **No statistics, no learning, no promotion.** Figures are sums, shares and
  counts. The learning tier later reads the retained entry-time context, but
  nothing here analyses it.

Units: notionals (exposure, stock, loss, capital) are rupees; sector,
correlated and concentration figures are fractions of total allocated capital
(0.40 = 40%). Percentage-style whole numbers (``40`` for 40%) are refused at
the door — that mixup is the most likely misconfiguration, and a 4000%
sector cap would silently disable the check.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from atr.research.learning_context import max_drawdown

logger = logging.getLogger("atr.services.portfolio")

IST = ZoneInfo("Asia/Kolkata")

#: Opposite-direction handling across deployments. ``reject`` fails closed;
#: ``priority`` lets a strictly higher-ranked strategy through; ``net`` allows
#: both books and counts net exposure (gross size limits still apply).
CONFLICT_MODES = ("reject", "priority", "net")

#: Event bound for the book and replay reads. The log is append-only and these
#: reads are dashboard-scale; the bound keeps one unbounded account from
#: turning a gate evaluation into a table scan.
MAX_EVENTS = 5000
MAX_ORDERS = 50000


class PolicyError(Exception):
    """A portfolio policy that cannot be stored."""

    def __init__(self, message: str, *, code: str = "invalid_policy", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioPolicy:
    """The validated portfolio policy. Every field ``None``-able: unset is off."""

    max_total_exposure: float | None = None
    max_daily_loss: float | None = None
    max_capital_per_strategy: float | None = None
    max_open_positions: int | None = None
    max_stock_exposure: float | None = None
    max_sector_exposure_pct: float | None = None
    max_correlated_exposure_pct: float | None = None
    correlation_groups: tuple[tuple[str, ...], ...] = ()
    conflict_mode: str = "reject"
    strategy_priorities: dict[str, float] = field(default_factory=dict)
    warn_at_pct_of_limit: float = 0.8

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_total_exposure": self.max_total_exposure,
            "max_daily_loss": self.max_daily_loss,
            "max_capital_per_strategy": self.max_capital_per_strategy,
            "max_open_positions": self.max_open_positions,
            "max_stock_exposure": self.max_stock_exposure,
            "max_sector_exposure_pct": self.max_sector_exposure_pct,
            "max_correlated_exposure_pct": self.max_correlated_exposure_pct,
            "correlation_groups": [list(g) for g in self.correlation_groups],
            "conflict_mode": self.conflict_mode,
            "strategy_priorities": dict(self.strategy_priorities),
            "warn_at_pct_of_limit": self.warn_at_pct_of_limit,
        }


def _positive_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PolicyError(f"{name} must be a number, got {value!r}") from None
    if not (number > 0):
        raise PolicyError(f"{name} must be positive, got {value!r}")
    return number


def _fraction(value: Any, name: str) -> float | None:
    number = _positive_number(value, name)
    if number is not None and number > 2.0:
        raise PolicyError(
            f"{name} is a fraction of capital (0.40 = 40%), got {value!r}; "
            "whole-number percentages are refused so 40 does not silently mean 4000%"
        )
    return number


def policy_from_dict(payload: dict[str, Any] | PortfolioPolicy | None) -> PortfolioPolicy:
    """Validate a policy payload. Unknown keys are refused, never ignored."""
    if isinstance(payload, PortfolioPolicy):
        return payload
    data = dict(payload or {})
    known = set(PortfolioPolicy.__dataclass_fields__)
    unknown = sorted(set(data) - known)
    if unknown:
        raise PolicyError(f"unknown policy field(s): {', '.join(unknown)}")

    mode = str(data.get("conflict_mode") or "reject").lower()
    if mode not in CONFLICT_MODES:
        raise PolicyError(
            f"conflict_mode must be one of {list(CONFLICT_MODES)}, got {data.get('conflict_mode')!r}"
        )

    groups: list[tuple[str, ...]] = []
    raw_groups = data.get("correlation_groups") or []
    if not isinstance(raw_groups, list):
        raise PolicyError("correlation_groups must be a list of symbol lists")
    for group in raw_groups:
        if not isinstance(group, list) or not group:
            raise PolicyError("correlation_groups must be a list of symbol lists")
        groups.append(tuple(str(s).strip().upper() for s in group if str(s).strip()))
    groups = [g for g in groups if g]

    priorities: dict[str, float] = {}
    raw_priorities = data.get("strategy_priorities") or {}
    if not isinstance(raw_priorities, dict):
        raise PolicyError("strategy_priorities must be {strategy_id: rank}")
    for key, rank in raw_priorities.items():
        try:
            priorities[str(key)] = float(rank)
        except (TypeError, ValueError):
            raise PolicyError(
                f"strategy_priorities[{key!r}] must be a number"
            ) from None

    max_open = data.get("max_open_positions")
    if max_open is not None:
        try:
            max_open = int(max_open)
        except (TypeError, ValueError):
            raise PolicyError("max_open_positions must be an integer") from None
        if max_open <= 0:
            raise PolicyError("max_open_positions must be positive")

    warn = data.get("warn_at_pct_of_limit", 0.8)
    try:
        warn = float(warn)
    except (TypeError, ValueError):
        raise PolicyError("warn_at_pct_of_limit must be a number") from None
    if not (0.0 < warn <= 1.0):
        raise PolicyError("warn_at_pct_of_limit must be in (0, 1]")

    return PortfolioPolicy(
        max_total_exposure=_positive_number(data.get("max_total_exposure"), "max_total_exposure"),
        max_daily_loss=_positive_number(data.get("max_daily_loss"), "max_daily_loss"),
        max_capital_per_strategy=_positive_number(
            data.get("max_capital_per_strategy"), "max_capital_per_strategy"
        ),
        max_open_positions=max_open,
        max_stock_exposure=_positive_number(data.get("max_stock_exposure"), "max_stock_exposure"),
        max_sector_exposure_pct=_fraction(
            data.get("max_sector_exposure_pct"), "max_sector_exposure_pct"
        ),
        max_correlated_exposure_pct=_fraction(
            data.get("max_correlated_exposure_pct"), "max_correlated_exposure_pct"
        ),
        correlation_groups=tuple(groups),
        conflict_mode=mode,
        strategy_priorities=priorities,
        warn_at_pct_of_limit=warn,
    )


def default_policy() -> PortfolioPolicy:
    """No limits, conflicts rejected. The pre-policy behaviour, stated."""
    return PortfolioPolicy()


# ---------------------------------------------------------------------------
# book — the aggregate paper book, folded from the order log
# ---------------------------------------------------------------------------


@dataclass
class BookPosition:
    """One symbol in one deployment: signed quantity, average cost, last mark."""

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    last_price: float = 0.0
    deployment_id: str | None = None
    strategy_id: str | None = None

    @property
    def value(self) -> float:
        price = self.last_price or self.avg_price or 0.0
        return self.qty * price


@dataclass
class DeploymentSlice:
    deployment_id: str
    strategy_id: str | None
    strategy_version: int | None
    mode: str | None
    status: str | None
    capital: float
    positions: dict[str, BookPosition] = field(default_factory=dict)
    #: Cash left after folding every fill against the allocation. Starts at
    #: ``capital`` — the number somebody chose — never at an invented default.
    cash: float = 0.0


@dataclass
class PortfolioBook:
    """Every deployment's lots, plus the capital they were allocated."""

    deployments: list[DeploymentSlice] = field(default_factory=list)
    total_capital: float = 0.0
    symbols_without_price: list[str] = field(default_factory=list)

    def arm_qty(self, deployment_id: str | None, symbol: str) -> float:
        if not deployment_id:
            return 0.0
        for dep in self.deployments:
            if dep.deployment_id == deployment_id:
                position = dep.positions.get(_clean(symbol))
                return position.qty if position else 0.0
        return 0.0

    def net_qty(self, symbol: str) -> float:
        wanted = _clean(symbol)
        return sum(
            position.qty
            for dep in self.deployments
            for position in dep.positions.values()
            if position.symbol == wanted
        )

    def symbol_value(self, symbol: str) -> float:
        wanted = _clean(symbol)
        return sum(
            position.value
            for dep in self.deployments
            for position in dep.positions.values()
            if position.symbol == wanted
        )

    def gross_exposure(self) -> float:
        return sum(abs(p.value) for dep in self.deployments for p in dep.positions.values())

    def open_symbols(self) -> list[str]:
        seen: dict[str, float] = {}
        for dep in self.deployments:
            for position in dep.positions.values():
                seen[position.symbol] = seen.get(position.symbol, 0.0) + position.qty
        return sorted(sym for sym, qty in seen.items() if abs(qty) > 1e-9)


def _clean(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def _side_sign(side: Any) -> int:
    return -1 if str(side or "").strip().upper() in ("SELL", "S", "SHORT") else 1


class PortfolioService:
    """Policy store, aggregate book, gate, dashboard and learning replay."""

    def __init__(self, db: Any = None) -> None:
        self._db = db

    @property
    def db(self) -> Any:
        if self._db is not None:
            return self._db
        from atr.appdb.engine import get_app_db

        return get_app_db()

    # ------------------------------------------------------------- policy store
    def get_policy(self, user_id: str) -> PortfolioPolicy | None:
        """The stored policy, or ``None`` when the user never set one.

        ``None`` — not a default object — is what tells the gate to pass
        everything through: "no policy" and "a policy with every limit off"
        are different states, and the dashboard shows which one holds.
        """
        from atr.appdb.schema import portfolio_policies

        with self.db.session() as session:
            from sqlalchemy import select

            row = session.execute(
                select(portfolio_policies).where(
                    portfolio_policies.c.user_id == user_id
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            payload = json.loads(row["policy"]) if isinstance(row["policy"], str) else {}
        except (TypeError, ValueError):
            logger.warning("portfolio: stored policy for %s is unreadable", user_id)
            return None
        try:
            return policy_from_dict(payload)
        except PolicyError:
            logger.warning("portfolio: stored policy for %s no longer validates", user_id)
            return None

    def set_policy(
        self, user_id: str, payload: dict[str, Any], *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Validate and store the policy. Wholesale replacement, reason required.

        Changing what the platform may risk is consequential, so — like the
        kill switch — it needs a who and a why. Unknown fields are refused
        rather than dropped: a limit the caller believes they set must not
        silently vanish.
        """
        from atr.appdb.engine import utcnow
        from atr.appdb.schema import portfolio_policies

        reason = (reason or "").strip()
        if not reason:
            raise PolicyError(
                "setting the portfolio policy requires a reason",
                code="reason_required",
                status=400,
            )
        policy = policy_from_dict(payload)
        now = utcnow()
        with self.db.session() as session:
            from sqlalchemy import select

            exists = (
                session.execute(
                    select(portfolio_policies.c.user_id).where(
                        portfolio_policies.c.user_id == user_id
                    )
                ).first()
                is not None
            )
            values = {
                "policy": json.dumps(policy.as_dict()),
                "updated_at": now,
                "updated_by": (actor or "")[:64] or None,
                "reason": reason,
            }
            if exists:
                from sqlalchemy import update

                session.execute(
                    update(portfolio_policies)
                    .where(portfolio_policies.c.user_id == user_id)
                    .values(**values)
                )
            else:
                from sqlalchemy import insert

                session.execute(
                    insert(portfolio_policies).values(user_id=user_id, **values)
                )
            session.commit()
        return {"user_id": user_id, "policy": policy.as_dict(), "updated_at": now}

    # ------------------------------------------------------------------- book
    def build_book(
        self, user_id: str, *, prices: dict[str, float] | None = None
    ) -> PortfolioBook:
        """Fold the user's fills into per-deployment lots, with capital attached.

        Marks come from ``prices`` where given, else the last fill price for
        the symbol — a documented approximation, and symbols with neither are
        named in ``symbols_without_price`` and excluded from value totals
        rather than valued at zero.
        """
        from atr.appdb.repositories import DeploymentRepository

        marks = {str(k).strip().upper(): float(v) for k, v in (prices or {}).items()}
        with self.db.session() as session:
            deployments = DeploymentRepository.list_for_user(session, user_id)
            fills = _user_fills(session, user_id)

        slices: dict[str, DeploymentSlice] = {}
        meta: dict[str, dict[str, Any]] = {}
        for dep in deployments:
            dep_id = str(dep.get("deployment_id"))
            meta[dep_id] = dep
            slices[dep_id] = DeploymentSlice(
                deployment_id=dep_id,
                strategy_id=dep.get("strategy_id"),
                strategy_version=dep.get("strategy_version"),
                mode=dep.get("mode"),
                status=dep.get("status"),
                capital=float(dep.get("capital") or 0.0),
            )

        for dep in slices.values():
            dep.cash = dep.capital
        for fill in fills:
            dep_id = fill.get("deployment_id")
            if not dep_id or dep_id not in slices:
                continue
            symbol = _clean(fill.get("symbol"))
            if not symbol:
                continue
            positions = slices[dep_id].positions
            position = positions.get(symbol)
            if position is None:
                position = BookPosition(
                    symbol=symbol,
                    deployment_id=dep_id,
                    strategy_id=slices[dep_id].strategy_id,
                )
                positions[symbol] = position
            _apply_fill(position, fill)
            try:
                notional = float(fill.get("qty") or 0.0) * float(fill.get("price") or 0.0)
                commission = float(fill.get("commission") or 0.0)
            except (TypeError, ValueError):
                notional, commission = 0.0, 0.0
            if _side_sign(fill.get("side")) > 0:
                slices[dep_id].cash -= notional + commission
            else:
                slices[dep_id].cash += notional - commission

        no_price: list[str] = []
        for dep in slices.values():
            for position in dep.positions.values():
                mark = marks.get(position.symbol)
                if mark is not None and mark > 0:
                    position.last_price = mark
                elif not position.last_price and position.symbol not in no_price:
                    no_price.append(position.symbol)

        total_capital = sum(
            dep.capital for dep in slices.values() if str(dep.status or "").upper() != "STOPPED"
        )
        return PortfolioBook(
            deployments=list(slices.values()),
            total_capital=total_capital,
            symbols_without_price=sorted(no_price),
        )

    # ------------------------------------------------------- dashboard figures
    def realized_by_strategy(self, user_id: str) -> dict[str, dict[str, Any]]:
        """Closed-trade P&L per strategy from the journal. Open episodes excluded:
        an unrealised figure is not a realised one, and the book already reports
        the open side separately."""
        from atr.appdb.repositories import TradeJournalRepository

        with self.db.session() as session:
            episodes, _total = TradeJournalRepository.list_for_user(
                session, user_id, closed_only=False, limit=1000
            )
        out: dict[str, dict[str, Any]] = {}
        for episode in episodes:
            key = str(episode.get("strategy_id") or "UNKNOWN")
            entry = out.setdefault(
                key, {"realized": 0.0, "trades_closed": 0, "trades_open": 0, "curve": []}
            )
            if episode.get("exit_ts") is None:
                entry["trades_open"] += 1
                continue
            net = episode.get("net_pnl")
            entry["trades_closed"] += 1
            if net is None:
                continue
            try:
                entry["realized"] += float(net)
            except (TypeError, ValueError):
                continue
            entry["curve"].append((episode.get("exit_ts"), float(net)))
        return out

    # ------------------------------------------------------- control center
    def control_center(
        self, user_id: str, *, prices: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """Everything the Portfolio Control Center renders, in one read.

        Objective figures only: per-strategy metrics are ordered by allocated
        capital, never ranked — there is no "best" here, and background text
        says so. Warnings fire on proximity to a *configured* limit; with no
        limit configured there is nothing to be proximate to, so nothing warns.
        """
        from atr.appdb.repositories import StrategyRepository

        policy = self.get_policy(user_id)
        effective = policy or default_policy()
        book = self.build_book(user_id, prices=prices)
        sector_fn = sector_resolver()
        realized = self.realized_by_strategy(user_id)
        today_realized = self.realized_today(user_id)

        names: dict[str, str] = {}
        try:
            with self.db.session() as session:
                for row in StrategyRepository.list_for_user(session, user_id):
                    names[str(row.get("strategy_id"))] = str(
                        row.get("name") or row.get("strategy_id")
                    )
        except Exception:  # noqa: BLE001 — names are display-only
            logger.warning("portfolio: strategy names unreadable")

        unrealized_by_strategy: dict[str, float] = {}
        for dep in book.deployments:
            for position in dep.positions.values():
                key = str(dep.strategy_id or "UNKNOWN")
                unrealized_by_strategy[key] = (
                    unrealized_by_strategy.get(key, 0.0)
                    + position.qty * ((position.last_price or 0.0) - (position.avg_price or 0.0))
                )
        unrealized_total = sum(unrealized_by_strategy.values())
        portfolio_total = (
            sum(v["realized"] for v in realized.values()) + unrealized_total
        )

        strategies: list[dict[str, Any]] = []
        for dep in sorted(book.deployments, key=lambda d: -d.capital):
            key = str(dep.strategy_id or "UNKNOWN")
            stats = realized.get(key, {"realized": 0.0, "trades_closed": 0, "trades_open": 0, "curve": []})
            used = sum(abs(p.value) for p in dep.positions.values())
            unrealized = unrealized_by_strategy.get(key, 0.0)
            total = stats["realized"] + unrealized
            curve = [net for _ts, net in sorted(stats["curve"], key=lambda t: str(t[0]))]
            if unrealized:
                curve = [*curve, unrealized]
            strategies.append(
                {
                    "strategy_id": key,
                    "strategy_name": names.get(key, key),
                    "allocated": dep.capital,
                    "used": round(used, 2),
                    "available": round(dep.capital - used, 2),
                    "exposure": round(used, 2),
                    "realized": round(stats["realized"], 2),
                    "unrealized": round(unrealized, 2),
                    "total_pnl": round(total, 2),
                    "return_pct": round(total / dep.capital * 100.0, 2) if dep.capital else None,
                    "drawdown": max_drawdown(curve),
                    "trades_closed": stats["trades_closed"],
                    "trades_open": stats["trades_open"],
                    "contribution": (
                        round(total / portfolio_total, 4) if portfolio_total else None
                    ),
                    "deployment_id": dep.deployment_id,
                    "deployment_status": dep.status,
                }
            )

        gross = book.gross_exposure()
        long_value = sum(
            p.value
            for dep in book.deployments
            for p in dep.positions.values()
            if p.value > 0
        )
        cash = sum(dep.cash for dep in book.deployments)
        capital = book.total_capital

        open_positions: list[dict[str, Any]] = []
        net_by_symbol: dict[str, float] = {}
        val_by_symbol: dict[str, float] = {}
        deps_by_symbol: dict[str, set[str]] = {}
        for dep in book.deployments:
            for position in dep.positions.values():
                net_by_symbol[position.symbol] = (
                    net_by_symbol.get(position.symbol, 0.0) + position.qty
                )
                val_by_symbol[position.symbol] = (
                    val_by_symbol.get(position.symbol, 0.0) + position.value
                )
                deps_by_symbol.setdefault(position.symbol, set()).add(dep.deployment_id)
        for symbol in sorted(val_by_symbol):
            if abs(net_by_symbol.get(symbol, 0.0)) <= 1e-9 and abs(val_by_symbol[symbol]) <= 1e-9:
                continue
            open_positions.append(
                {
                    "symbol": symbol,
                    "qty": round(net_by_symbol[symbol], 4),
                    "value": round(val_by_symbol[symbol], 2),
                    "sector": sector_fn(symbol),
                    "deployments": sorted(deps_by_symbol[symbol]),
                    "pct_of_capital": round(val_by_symbol[symbol] / capital, 4) if capital else None,
                }
            )

        sectors: dict[str, dict[str, Any]] = {}
        for symbol, value in val_by_symbol.items():
            sector = sector_fn(symbol)
            entry = sectors.setdefault(sector, {"value": 0.0})
            entry["value"] += abs(value)
        for entry in sectors.values():
            entry["value"] = round(entry["value"], 2)
            entry["pct"] = round(entry["value"] / capital, 4) if capital else None
        strat_exposure: dict[str, float] = {}
        for dep in book.deployments:
            key = str(dep.strategy_id or "UNKNOWN")
            strat_exposure[key] = strat_exposure.get(key, 0.0) + sum(
                abs(p.value) for p in dep.positions.values()
            )

        warnings = _concentration_warnings(
            val_by_symbol, sectors, strat_exposure, capital, effective
        )
        if book.symbols_without_price:
            warnings.append(
                f"{len(book.symbols_without_price)} symbol(s) have no price and are "
                f"excluded from value totals: {', '.join(book.symbols_without_price[:5])}"
            )

        conflicts = _describe_conflicts(book)
        limits = _active_limits(book, effective, capital, gross)

        today_total = today_realized + unrealized_total
        journal_curve = _realized_curve(self.db, user_id)
        if unrealized_total:
            journal_curve = [*journal_curve, unrealized_total]

        limitations: list[str] = []
        if policy is None:
            limitations.append(
                "no portfolio policy is configured: limits are off and the gate "
                "passes everything through; figures below are still computed"
            )
        if book.symbols_without_price:
            limitations.append(
                "value totals exclude unpriced symbols (last fill price unknown "
                "and no mark supplied)"
            )

        from atr.appdb.engine import utcnow

        return {
            "generated_at": utcnow().isoformat(),
            "policy_configured": policy is not None,
            "policy": (policy or default_policy()).as_dict(),
            "capital": {
                "total": round(capital, 2),
                "deployed": round(
                    sum(d.capital for d in book.deployments if str(d.status or '').upper() == "RUNNING"),
                    2,
                ),
                "used": round(sum(abs(p.value) for dep in book.deployments for p in dep.positions.values()), 2),
                "available": round(capital - gross, 2),
                "cash": round(cash, 2),
                "cash_utilization": round(1.0 - cash / capital, 4) if capital else None,
            },
            "exposure": {
                "gross": round(gross, 2),
                "long": round(long_value, 2),
                "short": round(gross - long_value, 2),
                "net": round(sum(val_by_symbol.values()), 2),
            },
            "today": {
                "realized": round(today_realized, 2),
                "unrealized": round(unrealized_total, 2),
                "total": round(today_total, 2),
            },
            "drawdown": {
                "value": max_drawdown(journal_curve),
                "method": (
                    "peak-to-trough of cumulative closed-trade net P&L with "
                    "current unrealized appended"
                ),
            },
            "open_positions": open_positions,
            "sectors": sectors,
            "strategies": strategies,
            "concentrations": {
                "stocks": _top_pct(val_by_symbol, capital, 5),
                "sectors": _top_pct(
                    {k: v["value"] for k, v in sectors.items()}, capital, 5
                ),
                "strategies": _top_pct(strat_exposure, capital, 5),
                "warnings": warnings,
            },
            "conflicts": conflicts,
            "limits": limits,
            "unpriced_symbols": book.symbols_without_price,
            "limitations": limitations,
            "advisory_only": True,
            "applies_changes": False,
        }

    def realized_today(self, user_id: str, *, now: datetime | None = None) -> float:
        """Today's closed-trade P&L across the book, in IST session date."""
        from atr.appdb.repositories import TradeJournalRepository

        moment = now or datetime.now(UTC)
        today = moment.astimezone(IST).date()
        total = 0.0
        with self.db.session() as session:
            episodes, _total = TradeJournalRepository.list_for_user(
                session, user_id, closed_only=True, limit=1000
            )
        for episode in episodes:
            exit_ts = episode.get("exit_ts")
            stamp = exit_ts.date() if hasattr(exit_ts, "date") else None
            if stamp != today or episode.get("net_pnl") is None:
                continue
            try:
                total += float(episode.get("net_pnl"))
            except (TypeError, ValueError):
                continue
        return total


def _user_fills(session: Any, user_id: str) -> list[dict[str, Any]]:
    """Every fill on every one of the user's orders, oldest first.

    Incremental quantities (cumulative minus previous), mirroring the paper
    ledger: a repeated cumulative figure is not a new fill, and recording it
    would double-count the position.
    """
    from atr.appdb.repositories import OrderEventRepository, OrderRepository

    orders = {
        o["order_id"]: o
        for o in OrderRepository.list_for_user(session, user_id, limit=MAX_ORDERS)[0]
    }
    out: list[dict[str, Any]] = []
    for order_id, order in orders.items():
        previous = 0.0
        for event in OrderEventRepository.fills(session, order_id):
            if event.get("filled_qty") is None:
                continue
            try:
                cumulative = float(event.get("filled_qty") or 0.0)
            except (TypeError, ValueError):
                continue
            incremental = cumulative - previous
            previous = cumulative
            if incremental <= 0:
                continue
            try:
                price = float(event.get("filled_price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            try:
                commission = float(event.get("commission") or 0.0)
            except (TypeError, ValueError):
                commission = 0.0
            out.append(
                {
                    "deployment_id": order.get("deployment_id"),
                    "strategy_id": order.get("strategy_id"),
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "qty": incremental,
                    "price": price,
                    "commission": commission,
                    "ts": event.get("fill_ts") or event.get("ts"),
                }
            )
    out.sort(key=_fill_sort_key)
    return out


def _fill_sort_key(fill: dict[str, Any]) -> str:
    ts = fill.get("ts")
    if hasattr(ts, "isoformat"):
        return str(ts.isoformat())
    return str(ts or "")


def _apply_fill(position: BookPosition, fill: dict[str, Any]) -> None:
    """Fold one incremental fill into a lot, average-cost.

    Buys add cost; sells reduce quantity at the running average (realised P&L
    is the journal's job, not the book's). A sell beyond the held quantity
    opens a short at the fill price — average-cost across the zero line would
    fabricate a basis, so the flip restarts it.
    """
    try:
        qty = float(fill.get("qty") or 0.0)
        price = float(fill.get("price") or 0.0)
    except (TypeError, ValueError):
        return
    if qty <= 0:
        return
    signed = qty if _side_sign(fill.get("side")) > 0 else -qty
    if price > 0:
        position.last_price = price
    new_qty = position.qty + signed
    if position.qty == 0 or (position.qty > 0) == (new_qty > 0) or new_qty == 0:
        if signed > 0 and price > 0:
            cost = position.avg_price * position.qty + price * signed
            position.avg_price = cost / new_qty if new_qty != 0 else 0.0
        position.qty = new_qty
        if new_qty == 0:
            position.avg_price = 0.0
    else:
        # Flipped through zero: the far side opens at this fill's price.
        position.qty = new_qty
        position.avg_price = price if price > 0 else 0.0


# ---------------------------------------------------------------------------
# gate — the portfolio check, after the strategy gate, before the OMS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioCheck:
    check: str
    allowed: bool
    detail: str
    limit: Any = None
    current: Any = None
    projected: Any = None


@dataclass(frozen=True)
class PortfolioDecision:
    allowed: bool
    reason: str | None
    code: str | None
    checks: tuple[PortfolioCheck, ...] = ()

    @classmethod
    def allow(
        cls, checks: list[PortfolioCheck], *, note: str | None = None
    ) -> PortfolioDecision:
        return cls(True, note, None, tuple(checks))

    @classmethod
    def reject(
        cls, reason: str, code: str, checks: list[PortfolioCheck]
    ) -> PortfolioDecision:
        return cls(False, reason, code, tuple(checks))


def resolve_price(draft: Any, book: PortfolioBook) -> float | None:
    """A price for the incoming order's notional, or ``None`` when unpriceable.

    Limit/requested first, then the book's last mark for the symbol. ``None``
    does not mean zero — callers skip the notional checks and say so, exactly
    like the strategy gate's unpriceable backstop.
    """
    for candidate in (
        getattr(draft, "limit_price", None),
        getattr(draft, "requested_price", None),
    ):
        try:
            if candidate is not None and float(candidate) > 0:
                return float(candidate)
        except (TypeError, ValueError):
            continue
    wanted = _clean(getattr(draft, "symbol", ""))
    for dep in book.deployments:
        position = dep.positions.get(wanted)
        if position is not None and position.last_price > 0:
            return position.last_price
    return None


def evaluate_order(
    draft: Any,
    *,
    book: PortfolioBook,
    policy: PortfolioPolicy,
    price: float | None,
    daily_pnl: float = 0.0,
    sector_of: Any = None,
) -> PortfolioDecision:
    """May this order reach the OMS? Pure: book + policy + price in, verdict out.

    ``draft`` needs ``symbol``, ``side``, ``quantity``, ``deployment_id`` and
    ``strategy_id``. ``sector_of`` maps a symbol to its sector (``"Unknown"``
    when unmapped); without it every symbol is its own one-member cluster.
    """
    checks: list[PortfolioCheck] = []
    symbol = _clean(getattr(draft, "symbol", ""))
    side = _side_sign(getattr(draft, "side", "BUY"))
    try:
        qty = float(getattr(draft, "quantity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return PortfolioDecision.reject(
            "order quantity is not a number", "portfolio_bad_order", checks
        )
    if qty <= 0:
        return PortfolioDecision.reject(
            "order quantity is not positive", "portfolio_bad_order", checks
        )

    arm_qty = book.arm_qty(getattr(draft, "deployment_id", None), symbol)
    if arm_qty != 0 and (arm_qty > 0) != (side > 0) and qty <= abs(arm_qty):
        # An exit (or partial exit): blocking it would lock a loss in place.
        # The strategy gate already passed; the portfolio layer steps aside.
        checks.append(
            PortfolioCheck(
                "exit_exempt",
                True,
                f"reduces the arm's own {abs(arm_qty):g} {symbol} position",
            )
        )
        return PortfolioDecision.allow(checks, note="exit exempt from portfolio limits")

    if not symbol:
        return PortfolioDecision.reject(
            "order has no symbol", "portfolio_bad_order", checks
        )

    notional = qty * price if price is not None and price > 0 else None
    net = book.net_qty(symbol)
    conflict = net != 0 and (net > 0) != (side > 0)

    if conflict:
        holders = sorted(
            {
                dep.strategy_id or dep.deployment_id
                for dep in book.deployments
                for position in dep.positions.values()
                if position.symbol == symbol
                and position.qty != 0
                and (position.qty > 0) != (side > 0)
            }
        )
        detail = (
            f"{symbol} is held {abs(net):g} opposite by "
            f"{', '.join(holders) or 'another arm'}"
        )
        if policy.conflict_mode == "reject":
            checks.append(PortfolioCheck("conflict", False, detail))
            return PortfolioDecision.reject(
                f"conflicting order rejected ({detail}; mode=reject)",
                "portfolio_conflict",
                checks,
            )
        if policy.conflict_mode == "priority":
            mine = _rank(policy, getattr(draft, "strategy_id", None))
            theirs = max(
                [_rank(policy, holder) for holder in holders] or [0.0]
            )
            if mine > theirs:
                checks.append(
                    PortfolioCheck(
                        "conflict",
                        True,
                        f"{detail}; strategy rank {mine:g} outranks {theirs:g}",
                    )
                )
            else:
                checks.append(
                    PortfolioCheck(
                        "conflict",
                        False,
                        f"{detail}; strategy rank {mine:g} does not outrank {theirs:g}",
                    )
                )
                return PortfolioDecision.reject(
                    f"conflicting order rejected ({detail}; mode=priority)",
                    "portfolio_conflict_priority",
                    checks,
                )
        else:  # "net" — both books stand; gross size limits still apply below.
            checks.append(
                PortfolioCheck("conflict", True, f"{detail}; netted (mode=net)")
            )
    else:
        checks.append(
            PortfolioCheck(
                "conflict",
                True,
                f"no opposite {symbol} holding across the book; exposure aggregates",
            )
        )

    sector_fn = sector_of or (lambda s: "Unknown")
    capital = book.total_capital
    gross = book.gross_exposure()

    def _deny(check: str, reason: str, code: str, **kw: Any) -> PortfolioDecision:
        checks.append(PortfolioCheck(check, False, reason, **kw))
        return PortfolioDecision.reject(reason, code, checks)

    def _pass(check: str, detail: str, **kw: Any) -> None:
        checks.append(PortfolioCheck(check, True, detail, **kw))

    if policy.max_total_exposure is not None:
        if notional is None:
            _pass(
                "total_exposure",
                "order cannot be priced; notional check skipped, counts still apply",
                limit=policy.max_total_exposure,
                current=round(gross, 2),
            )
        elif gross + notional > policy.max_total_exposure:
            return _deny(
                "total_exposure",
                f"portfolio exposure {gross + notional:,.0f} would exceed "
                f"{policy.max_total_exposure:,.0f}",
                "portfolio_total_exposure",
                limit=policy.max_total_exposure,
                current=round(gross, 2),
                projected=round(gross + notional, 2),
            )
        else:
            _pass(
                "total_exposure",
                f"{gross + notional:,.0f} of {policy.max_total_exposure:,.0f}",
                limit=policy.max_total_exposure,
                current=round(gross, 2),
                projected=round(gross + notional, 2),
            )

    if policy.max_stock_exposure is not None:
        current_stock = abs(book.symbol_value(symbol))
        if notional is None:
            _pass(
                "stock_exposure",
                "order cannot be priced; stock check skipped",
                limit=policy.max_stock_exposure,
                current=round(current_stock, 2),
            )
        elif current_stock + notional > policy.max_stock_exposure:
            return _deny(
                "stock_exposure",
                f"{symbol} exposure {current_stock + notional:,.0f} would exceed "
                f"{policy.max_stock_exposure:,.0f}",
                "portfolio_stock_exposure",
                limit=policy.max_stock_exposure,
                current=round(current_stock, 2),
                projected=round(current_stock + notional, 2),
            )
        else:
            _pass(
                "stock_exposure",
                f"{symbol} {current_stock + notional:,.0f} of "
                f"{policy.max_stock_exposure:,.0f}",
                limit=policy.max_stock_exposure,
                current=round(current_stock, 2),
                projected=round(current_stock + notional, 2),
            )

    if policy.max_sector_exposure_pct is not None:
        if capital <= 0:
            _pass(
                "sector_exposure",
                "no allocated capital; percentage check skipped",
                limit=policy.max_sector_exposure_pct,
            )
        elif notional is None:
            _pass(
                "sector_exposure",
                "order cannot be priced; sector check skipped",
                limit=policy.max_sector_exposure_pct,
            )
        else:
            sector = sector_fn(symbol)
            # Gross basis: a sector holding +100 long against −100 short is
            # 200 of exposure, not zero. Netting across arms for limit
            # purposes would let offsetting books hide concentration.
            sector_value = sum(
                abs(p.value)
                for dep in book.deployments
                for p in dep.positions.values()
                if sector_fn(p.symbol) == sector
            )
            add = notional if sector_fn(symbol) == sector else 0.0
            projected = (sector_value + add) / capital
            if projected > policy.max_sector_exposure_pct:
                return _deny(
                    "sector_exposure",
                    f"{sector} exposure {projected:.1%} would exceed "
                    f"{policy.max_sector_exposure_pct:.1%} of capital",
                    "portfolio_sector_exposure",
                    limit=policy.max_sector_exposure_pct,
                    current=round(sector_value / capital, 4),
                    projected=round(projected, 4),
                )
            _pass(
                "sector_exposure",
                f"{sector} {projected:.1%} of {policy.max_sector_exposure_pct:.1%}",
                limit=policy.max_sector_exposure_pct,
                projected=round(projected, 4),
            )

    if policy.max_correlated_exposure_pct is not None:
        if capital <= 0:
            _pass(
                "correlated_exposure",
                "no allocated capital; percentage check skipped",
                limit=policy.max_correlated_exposure_pct,
            )
        elif notional is None:
            _pass(
                "correlated_exposure",
                "order cannot be priced; correlated check skipped",
                limit=policy.max_correlated_exposure_pct,
            )
        else:
            cluster, members = _cluster_for(symbol, book, policy, sector_fn)
            cluster_value = sum(
                abs(book.symbol_value(member)) for member in members
            )
            projected = (cluster_value + notional) / capital
            if projected > policy.max_correlated_exposure_pct:
                return _deny(
                    "correlated_exposure",
                    f"correlated cluster ({cluster}) exposure {projected:.1%} "
                    f"would exceed {policy.max_correlated_exposure_pct:.1%} of capital",
                    "portfolio_correlated_exposure",
                    limit=policy.max_correlated_exposure_pct,
                    current=round(cluster_value / capital, 4),
                    projected=round(projected, 4),
                )
            _pass(
                "correlated_exposure",
                f"cluster ({cluster}) {projected:.1%} of "
                f"{policy.max_correlated_exposure_pct:.1%}",
                limit=policy.max_correlated_exposure_pct,
                projected=round(projected, 4),
            )

    if policy.max_open_positions is not None:
        seen: dict[str, float] = {}
        for dep in book.deployments:
            for position in dep.positions.values():
                seen[position.symbol] = seen.get(position.symbol, 0.0) + position.qty
        seen[symbol] = seen.get(symbol, 0.0) + side * qty
        projected_count = sum(1 for q in seen.values() if abs(q) > 1e-9)
        if projected_count > policy.max_open_positions:
            return _deny(
                "open_positions",
                f"{projected_count} open positions would exceed "
                f"{policy.max_open_positions}",
                "portfolio_open_positions",
                limit=policy.max_open_positions,
                projected=projected_count,
            )
        _pass(
            "open_positions",
            f"{projected_count} of {policy.max_open_positions}",
            limit=policy.max_open_positions,
            projected=projected_count,
        )

    if policy.max_daily_loss is not None:
        if daily_pnl <= -abs(policy.max_daily_loss):
            return _deny(
                "daily_loss",
                f"portfolio is down {abs(daily_pnl):,.0f} today (limit "
                f"{policy.max_daily_loss:,.0f}); new risk refused, exits still pass",
                "portfolio_daily_loss",
                limit=policy.max_daily_loss,
                current=round(daily_pnl, 2),
            )
        _pass(
            "daily_loss",
            f"today {daily_pnl:+,.0f} against a {policy.max_daily_loss:,.0f} limit",
            limit=policy.max_daily_loss,
            current=round(daily_pnl, 2),
        )

    return PortfolioDecision.allow(checks)


def _rank(policy: PortfolioPolicy, strategy_id: Any) -> float:
    try:
        return float(policy.strategy_priorities.get(str(strategy_id), 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _cluster_for(
    symbol: str, book: PortfolioBook, policy: PortfolioPolicy, sector_fn: Any
) -> tuple[str, list[str]]:
    """The correlated cluster a symbol belongs to, and its members.

    An explicitly declared ``correlation_groups`` basket wins — the operator
    naming two symbols correlated (index twins, ADR pairs) is a statement no
    price series needs to confirm. Otherwise the cluster is the symbol's
    sector: a documented proxy, not a return-correlation model, which this
    layer deliberately does not build.
    """
    wanted = _clean(symbol)
    for group in policy.correlation_groups:
        members = [_clean(s) for s in group]
        if wanted in members:
            return f"group:{'+'.join(sorted(set(members)))}", sorted(set(members))
    sector = sector_fn(wanted)
    members = sorted(
        {
            p.symbol
            for dep in book.deployments
            for p in dep.positions.values()
            if sector_fn(p.symbol) == sector
        }
        | {wanted}
    )
    return f"sector:{sector}", members


# ---------------------------------------------------------------------------
# control center — one read-only view of the whole paper book
# ---------------------------------------------------------------------------


def sector_resolver(data_root: Any = None) -> Any:
    """Symbol → sector, from the same universe mapping the dataset uses.

    The learning builder's ``sector`` column comes from this table, so
    concentration figures here agree with the learning axes by construction.
    Unknown symbols report ``"Unknown"`` — a real bucket in every breakdown,
    never a silent drop.
    """
    table: dict[str, str] = {}
    try:
        from atr.research.learning_enrich import build_sector_table

        table = build_sector_table(data_root) or {}
    except Exception:  # noqa: BLE001 — no mapping is a gap, not a failure
        table = {}

    def _sector(symbol: Any) -> str:
        return table.get(_clean(symbol), "Unknown")

    return _sector





# ---------------------------------------------------------------------------
# dashboard helpers (pure)
# ---------------------------------------------------------------------------


def _top_pct(values: dict[str, float], capital: float, n: int) -> list[dict[str, Any]]:
    ranked = sorted(values.items(), key=lambda kv: abs(kv[1]), reverse=True)[: max(n, 0)]
    return [
        {"name": name, "value": round(value, 2), "pct": round(value / capital, 4) if capital else None}
        for name, value in ranked
    ]


def _concentration_warnings(
    val_by_symbol: dict[str, float],
    sectors: dict[str, dict[str, Any]],
    strat_exposure: dict[str, float],
    capital: float,
    policy: PortfolioPolicy,
) -> list[str]:
    """Proximity warnings against *configured* limits only.

    No magic thresholds: with no limit configured there is nothing to be
    proximate to, so nothing warns. Each warning names the utilization, so a
    reader can tell 81% from 99%.
    """
    warnings: list[str] = []
    if capital <= 0:
        return warnings
    bar = policy.warn_at_pct_of_limit

    def _warn(label: str, value: float, limit: float | None) -> None:
        if limit is None or limit <= 0:
            return
        utilization = value / limit
        if utilization >= bar:
            warnings.append(
                f"{label} at {utilization:.0%} of its limit "
                f"({value:,.0f} of {limit:,.0f})"
            )

    for symbol, value in val_by_symbol.items():
        _warn(f"{symbol} exposure", abs(value), policy.max_stock_exposure)
    for sector, entry in sectors.items():
        limit = (
            policy.max_sector_exposure_pct * capital
            if policy.max_sector_exposure_pct is not None
            else None
        )
        _warn(f"{sector} sector exposure", abs(float(entry.get("value") or 0.0)), limit)
    for strategy, value in strat_exposure.items():
        _warn(f"strategy {strategy} exposure", abs(value), policy.max_total_exposure)
    _warn(
        "portfolio exposure",
        sum(abs(v) for v in val_by_symbol.values()),
        policy.max_total_exposure,
    )
    return warnings


def _describe_conflicts(book: PortfolioBook) -> list[dict[str, Any]]:
    """Symbols held in opposite directions by different deployments.

    Always computed, whatever the conflict mode: display is not enforcement,
    and an operator should see the disagreement even under mode=net. Each
    side names its deployments, so "who disagrees with whom" is answerable.
    """
    by_symbol: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for dep in book.deployments:
        for position in dep.positions.values():
            if abs(position.qty) <= 1e-9:
                continue
            sides = by_symbol.setdefault(position.symbol, {"long": [], "short": []})
            sides["long" if position.qty > 0 else "short"].append(
                {
                    "deployment_id": dep.deployment_id,
                    "strategy_id": dep.strategy_id,
                    "qty": round(position.qty, 4),
                }
            )
    conflicts = []
    for symbol in sorted(by_symbol):
        sides = by_symbol[symbol]
        if sides["long"] and sides["short"]:
            conflicts.append(
                {
                    "symbol": symbol,
                    "net_qty": round(
                        sum(r["qty"] for r in sides["long"])
                        - sum(r["qty"] for r in sides["short"]),
                        4,
                    ),
                    "long": sides["long"],
                    "short": sides["short"],
                }
            )
    return conflicts


def _active_limits(
    book: PortfolioBook, policy: PortfolioPolicy, capital: float, gross: float
) -> list[dict[str, Any]]:
    """Every knob the operator set, beside what it currently reads."""

    def _row(
        key: str, label: str, value: Any, current: Any, unit: str
    ) -> dict[str, Any]:
        utilization = None
        if isinstance(value, (int, float)) and isinstance(current, (int, float)) and value:
            utilization = round(current / value, 4)
        return {
            "key": key,
            "label": label,
            "configured": value,
            "current": current,
            "unit": unit,
            "utilization": utilization,
        }

    rows = [
        _row("max_total_exposure", "Total portfolio exposure", policy.max_total_exposure, round(gross, 2), "INR"),
        _row("max_daily_loss", "Daily portfolio loss", policy.max_daily_loss, None, "INR"),
        _row("max_capital_per_strategy", "Maximum capital per strategy", policy.max_capital_per_strategy, None, "INR"),
        _row(
            "max_open_positions",
            "Maximum open positions",
            policy.max_open_positions,
            len(book.open_symbols()),
            "count",
        ),
        _row("max_stock_exposure", "Maximum exposure per stock", policy.max_stock_exposure, None, "INR"),
        _row(
            "max_sector_exposure_pct",
            "Maximum sector exposure",
            policy.max_sector_exposure_pct,
            None,
            "fraction",
        ),
        _row(
            "max_correlated_exposure_pct",
            "Maximum correlated exposure",
            policy.max_correlated_exposure_pct,
            None,
            "fraction",
        ),
    ]
    rows.append(
        {
            "key": "conflict_mode",
            "label": "Opposite-direction rule",
            "configured": policy.conflict_mode,
            "current": len(_describe_conflicts(book)),
            "unit": "mode",
            "utilization": None,
        }
    )
    return rows


def _realized_curve(db: Any, user_id: str) -> list[float]:
    """Cumulative closed-trade net P&L by exit order, for the drawdown read."""
    from atr.appdb.repositories import TradeJournalRepository

    with db.session() as session:
        episodes, _total = TradeJournalRepository.list_for_user(
            session, user_id, closed_only=True, limit=1000
        )
    stamps: list[tuple[str, float]] = []
    for episode in episodes:
        if episode.get("net_pnl") is None:
            continue
        try:
            stamps.append((str(episode.get("exit_ts") or ""), float(episode.get("net_pnl"))))
        except (TypeError, ValueError):
            continue
    stamps.sort(key=lambda t: t[0])
    running = 0.0
    curve = []
    for _ts, net in stamps:
        running += net
        curve.append(round(running, 2))
    return curve


# ---------------------------------------------------------------------------
# gate factory — the callable the OMS consults inside validate()
# ---------------------------------------------------------------------------


def portfolio_gate_for(
    user_id: str,
    deployment_id: str | None,
    *,
    ledger: Any,
    prices: dict[str, float] | None = None,
    db: Any = None,
) -> Any:
    """Build the portfolio gate for one arm, or ``None`` when no policy exists.

    ``None`` preserves the pre-policy behaviour exactly: with no stored
    policy the gate does not exist and order flow is untouched. With a policy,
    the returned callable folds a fresh book on every call (validate-time
    state, never a cached one) and answers in the OMS ``RiskDecision`` shape.

    A gate that cannot read the book fails *closed*: a safety layer that
    silently vanishes on a store fault is the footgun this codebase refuses
    to build.
    """
    from atr.execution.oms import RiskDecision

    service = PortfolioService(db=db)
    policy = service.get_policy(user_id)
    if policy is None:
        return None

    def _gate(draft: Any) -> RiskDecision:
        try:
            book = service.build_book(user_id, prices=prices)
            price = resolve_price(draft, book)
            daily = service.realized_today(user_id) + _unrealized_now(book)
            decision = evaluate_order(
                draft,
                book=book,
                policy=policy,
                price=price,
                daily_pnl=daily,
                sector_of=sector_resolver(),
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, loudly
            logger.exception("portfolio gate unreadable; refusing order")
            return RiskDecision.reject(
                f"portfolio risk could not be evaluated ({exc}); refusing rather "
                "than trading unmeasured",
                code="portfolio_unavailable",
            )
        if decision.allowed:
            return RiskDecision.ok()
        return RiskDecision.reject(
            decision.reason or "rejected by portfolio risk",
            code=decision.code or "portfolio_rejected",
        )

    _gate.__name__ = "portfolio_gate"
    return _gate


def _unrealized_now(book: PortfolioBook) -> float:
    total = 0.0
    for dep in book.deployments:
        for position in dep.positions.values():
            total += position.qty * ((position.last_price or 0.0) - (position.avg_price or 0.0))
    return total


def check_strategy_capital(
    user_id: str,
    strategy_id: str,
    additional_capital: float,
    *,
    db: Any = None,
) -> tuple[bool, str | None]:
    """Would allocating more capital to this strategy breach its cap?

    Read by deployment creation, so the refusal happens before the row exists
    rather than as an order rejection later. No policy, or no cap in it, means
    no opinion.
    """
    service = PortfolioService(db=db)
    policy = service.get_policy(user_id)
    if policy is None or policy.max_capital_per_strategy is None:
        return True, None
    from atr.appdb.repositories import DeploymentRepository

    with service.db.session() as session:
        rows = DeploymentRepository.list_for_user(session, user_id)
    allocated = sum(
        float(r.get("capital") or 0.0)
        for r in rows
        if str(r.get("strategy_id") or "") == str(strategy_id)
        and str(r.get("status") or "").upper() != "STOPPED"
    )
    if allocated + float(additional_capital) > policy.max_capital_per_strategy:
        return (
            False,
            f"strategy {strategy_id} would hold {allocated + float(additional_capital):,.0f} "
            f"against a {policy.max_capital_per_strategy:,.0f} cap",
        )
    return True, None


# ---------------------------------------------------------------------------
# learning replay — portfolio context at each entry, from the order log
# ---------------------------------------------------------------------------


def snapshots_at_entry(
    entries: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    capitals: dict[str, float],
    sector_of: Any,
) -> dict[Any, dict[str, Any] | None]:
    """The portfolio as each entry saw it, replayed from fills.

    ``entries`` carry ``key``, ``ts``, ``deployment_id`` and ``symbol``;
    ``fills`` are incremental fills with ``ts``. A single chronological sweep
    maintains average-cost lots per (deployment, symbol); each entry snapshots
    the state at its own timestamp — fills *at or before* the entry count, so
    the episode's own entry fill is included: this is exposure *upon* entry,
    stated as such.

    Point-in-time by construction: only the append-only log is read, so a
    rebuild years later replays the same book. Anything unresolvable (no
    timestamp, no deployment, no symbol) yields ``None`` fields, never zeros —
    missing stays missing.
    """
    ordered_fills = sorted(fills, key=_fill_sort_key)
    ordered_entries = sorted(
        [e for e in entries if e.get("ts") is not None],
        key=lambda e: _fill_sort_key({"ts": e.get("ts")}),
    )
    lots: dict[tuple[str, str], list[float]] = {}
    snapshots: dict[Any, dict[str, Any] | None] = {}
    cursor = 0
    for entry in ordered_entries:
        key = entry.get("key")
        stamp = _fill_sort_key({"ts": entry.get("ts")})
        while cursor < len(ordered_fills) and _fill_sort_key(ordered_fills[cursor]) <= stamp:
            fill = ordered_fills[cursor]
            cursor += 1
            dep = fill.get("deployment_id")
            symbol = _clean(fill.get("symbol"))
            if not dep or not symbol:
                continue
            try:
                qty = float(fill.get("qty") or 0.0)
                price = float(fill.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            lot = lots.setdefault((str(dep), symbol), [0.0, 0.0])
            signed = qty if _side_sign(fill.get("side")) > 0 else -qty
            if lot[0] == 0 or (lot[0] > 0) == (lot[0] + signed > 0) or lot[0] + signed == 0:
                if signed > 0 and price > 0:
                    lot[1] = (lot[1] * lot[0] + price * signed) / (lot[0] + signed)
                lot[0] = lot[0] + signed
                if lot[0] == 0:
                    lot[1] = 0.0
            else:
                lot[0] = lot[0] + signed
                lot[1] = price if price > 0 else 0.0
        snapshots[key] = _snapshot_for(entry, lots, capitals, sector_of)
    for entry in entries:
        snapshots.setdefault(entry.get("key"), None)
    return snapshots


def _snapshot_for(
    entry: dict[str, Any],
    lots: dict[tuple[str, str], list[float]],
    capitals: dict[str, float],
    sector_of: Any,
) -> dict[str, Any] | None:
    dep = entry.get("deployment_id")
    symbol = _clean(entry.get("symbol"))
    if not dep or not symbol:
        return {"exposure": None, "allocation": None, "sector_pct": None, "concentration": None}
    values: dict[str, float] = {}
    for (_dep, sym), (qty, avg) in lots.items():
        if qty != 0 and avg > 0:
            values[sym] = values.get(sym, 0.0) + abs(qty) * avg
    gross = sum(values.values())
    allocation = capitals.get(str(dep))
    sector = sector_of(symbol)
    sector_value = sum(v for s, v in values.items() if sector_of(s) == sector)
    return {
        "exposure": round(gross, 2),
        "allocation": allocation,
        "sector_pct": round(sector_value / gross, 4) if gross else None,
        "concentration": round(values.get(symbol, 0.0) / gross, 4) if gross else None,
    }
