"""Strategy routes — ``/api/v1/strategies``.

The authoring half of the strategy workflow: create a strategy, append immutable
versions to it, read them back, and validate a definition *before* it becomes one.
Without these, the only strategy route was ``GET /strategies`` (the code registry),
so the ``(strategy_id, strategy_version)`` pair a paper deployment pins could only
be produced by a script against the repository — and the platform's headline chain
started at a row nobody could create.

Implemented from ``docs/API_CONTRACT.md`` §Strategies:

    GET    /                       list strategies (own)
    POST   /                       create { name, description, kind }
    GET    /{id}                   metadata + latest version
    GET    /{id}/versions          version list
    POST   /{id}/versions          create a NEW version (never mutates an old one)
    GET    /{id}/versions/{v}      the definition
    POST   /{id}/validate          structural validation without saving

Deliberately **not** built, and the routes are absent so a client gets a clean 404
rather than an endpoint that pretends:

* ``PATCH /{id}``, ``POST /{id}/compare``, ``POST /{id}/from-nl`` — renaming,
  metric comparison and natural-language authoring are separate pieces of work.
  ``from-nl`` in particular is not a stub worth shipping: a language model
  emitting a rule block is only as good as the validation behind it, and the
  validation is the part that exists.
* ``GET /{id}/versions/{v}/metrics`` — there is no per-version metric store. A
  version's results live on its backtest runs, which ``GET /api/v1/backtests``
  already serves.

``POST /seed`` is not in the contract. It is here because a fresh installation
otherwise cannot reach its own workflow: the first thing the chain needs is a
stored definition, and there was nowhere to get one.

Two shapes worth reading before the code:

* ``deployable`` on every version. It is computed by the *runner's own* resolver,
  so a version listed as deployable is one the loop will genuinely trade. The
  listing cannot drift from the behaviour, which is the only reason it is worth
  showing.
* ``statistical_validation`` on every validation response. ``ok: true`` means the
  definition will execute. It does not mean it will make money, and the payload
  says so rather than letting a green tick be read as a finding.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import CurrentPrincipal, require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.strategies import KINDS, StrategyError, StrategyService

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

_READ = Depends(require_permission(Permission.STRATEGY_READ))
_WRITE = Depends(require_permission(Permission.STRATEGY_WRITE))


def _service() -> StrategyService:
    return StrategyService()


def _fail(exc: StrategyError) -> HTTPException:
    detail: dict[str, Any] = {"detail": str(exc), "code": exc.code}
    if exc.detail:
        detail.update(exc.detail)
    return HTTPException(exc.status, detail=detail)


# --------------------------------------------------------------------- models
class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    kind: str = Field(default="rules", pattern="^(?i)(nocode|rules|code|options)$")
    #: For a strategy that wraps a registry class. ``kind: "code"`` normally has
    #: one; a ``rules`` strategy may carry one as well so the same version can be
    #: backtested through the engine it was derived from.
    engine_key: str | None = Field(default=None, max_length=64)


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Canonical JSON of the rules/params. ``Any`` rather than ``dict`` on
    #: purpose: the validator's job is to explain what is wrong with a definition,
    #: and a Pydantic schema would reject a string or a list with a message about
    #: a type rather than about the strategy.
    definition: Any
    change_note: str | None = Field(default=None, max_length=1000)
    #: Store a definition that fails structural validation. Off by default: a
    #: version is immutable, so a broken one is a permanent deployment that
    #: reports itself running and never trades.
    force: bool = False


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A draft definition to check without saving. When omitted, the stored
    #: version named by ``version`` (or the latest one) is checked instead.
    definition: Any = None
    version: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------- routes
# Declared before ``/{strategy_id}``: FastAPI matches in order, and ``seed``
# would otherwise be read as a strategy id.
@router.post("/seed", status_code=status.HTTP_201_CREATED)
def seed_example_strategy(
    principal: Principal = Depends(require_permission(Permission.STRATEGY_WRITE)),
    name: str = Query(default="Example breakout", min_length=1, max_length=128),
) -> dict[str, Any]:
    """Create the worked example strategy and its version 1.

    Idempotent, so it is safe in a setup script and safe behind a button: running
    it twice returns the existing version and writes nothing. The example is a
    breakout entry with a stop-loss and a take-profit exit, and it exists so a
    fresh installation can run the whole chain — create, validate, deploy, trade,
    forward observation — without anyone having to reverse the JSON shape out of
    the runner first.

    It has **not** been validated out of sample and makes no claim to an edge.
    The response's ``validation`` block says the same thing in the same words the
    validator uses.
    """
    try:
        return _service().seed_example(principal.user_id, name=name)
    except StrategyError as exc:
        raise _fail(exc) from exc


@router.get("", dependencies=[_READ])
@router.get("/", dependencies=[_READ], include_in_schema=False)
def list_strategies(
    principal: CurrentPrincipal, include_archived: bool = False
) -> dict[str, Any]:
    """The caller's own strategies. Never another account's — a strategy id from
    a different user behaves exactly like one that does not exist."""
    rows = _service().list(principal.user_id, include_archived=include_archived)
    return {"strategies": rows, "total": len(rows), "kinds": list(KINDS)}


@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_strategy(
    body: StrategyCreate,
    principal: Principal = Depends(require_permission(Permission.STRATEGY_WRITE)),
) -> dict[str, Any]:
    """Create a strategy. It has no version yet, and therefore cannot be deployed.

    That is a real state rather than an error — it is the strategy whose rules are
    still being written — and the row says ``latest_version: null`` so the
    difference is visible here instead of at deploy time.
    """
    try:
        return _service().create(
            principal.user_id,
            name=body.name,
            kind=body.kind,
            description=body.description,
            engine_key=body.engine_key,
        )
    except StrategyError as exc:
        raise _fail(exc) from exc


@router.get("/{strategy_id}", dependencies=[_READ])
def get_strategy(strategy_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    try:
        return _service().get(principal.user_id, strategy_id)
    except StrategyError as exc:
        raise _fail(exc) from exc


@router.get("/{strategy_id}/versions", dependencies=[_READ])
def list_versions(strategy_id: str, principal: CurrentPrincipal) -> dict[str, Any]:
    """Every version, newest first, each with ``deployable`` and why not.

    ``deployable`` is the runner's own answer, not a second opinion: a version
    that resolves to live entry/exit rules is one a paper deployment can trade,
    and one that does not would be a deployment that reports itself running and
    does nothing.
    """
    try:
        rows = _service().versions(principal.user_id, strategy_id)
    except StrategyError as exc:
        raise _fail(exc) from exc
    return {
        "strategy_id": strategy_id,
        "versions": rows,
        "total": len(rows),
        "deployable_versions": [r["version"] for r in rows if r["deployable"]],
    }


@router.post("/{strategy_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    strategy_id: str,
    body: VersionCreate,
    principal: Principal = Depends(require_permission(Permission.STRATEGY_WRITE)),
) -> dict[str, Any]:
    """Append a new immutable version. Never mutates an existing one.

    The definition is validated first. A structurally broken one is refused with
    the reasons attached, unless ``force`` is set — because a version cannot be
    edited afterwards, and its failure mode is silent: the deployment looks
    healthy and places no orders.
    """
    try:
        return _service().create_version(
            principal.user_id,
            strategy_id,
            definition=body.definition,
            change_note=body.change_note,
            force=body.force,
        )
    except StrategyError as exc:
        raise _fail(exc) from exc


@router.get("/{strategy_id}/versions/{version}", dependencies=[_READ])
def get_version(
    strategy_id: str, version: int, principal: CurrentPrincipal
) -> dict[str, Any]:
    """One version, read by exact number — never "latest".

    A deployment pins a number and a backtest scores a number; an endpoint that
    resolved "latest" would let a subject change under a run that already claims
    to have measured it.
    """
    try:
        return _service().version(principal.user_id, strategy_id, version)
    except StrategyError as exc:
        raise _fail(exc) from exc


@router.post("/{strategy_id}/validate")
def validate_strategy(
    strategy_id: str,
    body: ValidateRequest,
    principal: Principal = Depends(require_permission(Permission.STRATEGY_READ)),
) -> dict[str, Any]:
    """Check a definition is structurally runnable. Writes nothing.

    Two uses, and the draft one is the point: pass ``definition`` to check rules
    *before* they become an immutable version, or ``version`` (or nothing, meaning
    the latest) to check what is already stored.

    A 200 here is not a 200 anywhere else: the response always carries
    ``statistical_validation.performed: false`` with the reason, because
    "this will execute" and "this will make money" are different questions and a
    green tick gets read as the second one.
    """
    try:
        return _service().validate(
            principal.user_id,
            strategy_id,
            version=body.version,
            definition=body.definition,
        )
    except StrategyError as exc:
        raise _fail(exc) from exc


class SizingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entry_price: float = Field(..., gt=0)
    capital: float = Field(..., gt=0)
    available_capital: float | None = None
    stop_price: float | None = None
    stop_loss_pct: float | None = None
    atr: float | None = None
    current_stock_exposure: float = 0.0
    max_stock_exposure: float | None = None
    current_portfolio_exposure: float = 0.0
    max_total_portfolio_exposure: float | None = None
    current_sector_exposure: float = 0.0
    max_sector_exposure: float | None = None
    sizing: dict[str, Any] = Field(default_factory=dict)


@router.post("/sizing/preview", dependencies=[_READ])
def preview_position_sizing(body: SizingPreviewRequest) -> dict[str, Any]:
    """Calculate and preview position size and risk budgeting metrics."""
    from atr.strategy.sizing import PositionSizingEngine, SizingConfig

    cfg = SizingConfig.from_dict(body.sizing)
    result = PositionSizingEngine.calculate(
        cfg,
        entry_price=body.entry_price,
        capital=body.capital,
        available_capital=body.available_capital,
        stop_price=body.stop_price,
        stop_loss_pct=body.stop_loss_pct,
        atr=body.atr,
        current_stock_exposure=body.current_stock_exposure,
        max_stock_exposure=body.max_stock_exposure,
        current_portfolio_exposure=body.current_portfolio_exposure,
        max_total_portfolio_exposure=body.max_total_portfolio_exposure,
        current_sector_exposure=body.current_sector_exposure,
        max_sector_exposure=body.max_sector_exposure,
    )
    return result.as_dict()

