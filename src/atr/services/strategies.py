"""Authoring strategies and their immutable versions.

The gap this closes
-------------------

``strategies`` and ``strategy_versions`` have existed for a while, with a
repository that creates, reads and refuses to update them. What did not exist was
any way to *reach* it: ``GET /strategies`` was the only strategy route and it
reads the code registry, so the only way to get a stored version was to write a
script against ``StrategyRepository``. A paper deployment pins
``(strategy_id, strategy_version)`` and the runner refuses to trade when that
version cannot be read — correctly, because the alternative is stamping a
strategy's name onto rules it never ran — so in practice the platform's headline
chain started at a row nobody could create.

Three properties this service is built to preserve, each of which the runner and
the backtest runner already depend on:

* **A version is immutable.** Nothing here updates ``definition``. Creating a
  version appends; editing means creating the next one. ``is_deployed`` is
  metadata and is the only column that moves.
* **A version is the subject.** A deployment pins a number, never "latest", and
  this service reads versions by exact number for the same reason.
* **A stored version must be runnable.** Creation refuses a definition with
  structural errors rather than storing one that will make the loop refuse to
  trade later. Immutability is what makes this the right place to be strict: a
  bad version is permanent, and its symptom — a deployment that reports itself
  running and never trades — is indistinguishable from a quiet market.

Validation is *structural*. It answers "will this execute", not "does this make
money". The response says so explicitly, because a green tick next to a strategy
name is otherwise read as a finding, and nothing here measured anything.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select

from atr.appdb.engine import AppDatabase, get_app_db
from atr.appdb.repositories import (
    DuplicateDefinition,
    StrategyRepository,
    canonical_definition,
    definition_hash,
)
from atr.appdb.schema import (
    backtest_runs,
    deployments,
    learning_observations,
    order_intents,
    orders,
    signal_contexts,
    strategies,
    trade_attributions,
    trade_journal,
)
from atr.strategy.definition import (
    parse_definition,
    resolve_rules,
    validate_definition,
)
from atr.strategy.example import EXAMPLE_DEFINITION, EXAMPLE_DESCRIPTION, EXAMPLE_NAME

logger = logging.getLogger("atr.services.strategies")

#: The definition kinds the store accepts. Mirrors ``StrategyRepository.KINDS``;
#: restated as a tuple so the API can publish it in its own error message.
KINDS = ("nocode", "rules", "code", "options")

#: Hard ceiling on a stored definition. Not a security boundary — the request body
#: limit is — but a version that is megabytes of JSON is a mistake, and refusing it
#: at the door is cheaper than discovering it when a runner parses it on every pass.
MAX_DEFINITION_BYTES = 64 * 1024


class StrategyError(Exception):
    """A strategy action was refused. Carries the code the API returns."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "strategy_error",
        status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        #: Extra machine-readable context. The validator puts its ``errors`` and
        #: ``warnings`` here so a 422 is actionable without a second request.
        self.detail = detail


class StrategyService:
    """Create strategies, append versions, read them back, validate definitions."""

    def __init__(self, db: AppDatabase | None = None) -> None:
        self._db = db

    @property
    def db(self) -> AppDatabase:
        return self._db or get_app_db()

    # ------------------------------------------------------------------ create
    def create(
        self,
        user_id: str,
        *,
        name: str,
        kind: str = "rules",
        description: str | None = None,
        engine_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a strategy. Versions are added separately, on purpose.

        A strategy with no version cannot be deployed, and that is a real state
        rather than an error: it is the strategy whose rules are still being
        written. The listing says ``latest_version: null`` so the difference is
        visible instead of being inferred from a failure at deploy time.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise StrategyError("a strategy needs a name", code="name_required", status=422)
        clean_kind = (kind or "").strip().lower()
        if clean_kind not in KINDS:
            raise StrategyError(
                f"kind must be one of {list(KINDS)}, got {kind!r}",
                code="invalid_kind",
                status=422,
            )
        if engine_key is not None:
            engine_key = str(engine_key).strip() or None

        with self.db.session() as session:
            if StrategyRepository.name_taken(session, user_id, clean_name):
                raise StrategyError(
                    f"a strategy called {clean_name!r} already exists",
                    code="name_taken",
                    status=409,
                )
            row = StrategyRepository.create(
                session,
                user_id=user_id,
                name=clean_name,
                kind=clean_kind,
                description=description,
                engine_key=engine_key,
            )
        logger.info("strategy %s created (%s)", row["strategy_id"][:8], clean_kind)
        return {**row, "latest_version": None, "version_count": 0}

    # -------------------------------------------------------------------- read
    def list(self, user_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = StrategyRepository.list_for_user(
                session, user_id, include_archived=include_archived
            )
            return [self._decorate(session, row) for row in rows]

    def get(self, user_id: str, strategy_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = StrategyRepository.get(session, strategy_id, user_id)
            if row is None:
                raise self._not_found(strategy_id)
            return self._decorate(session, row)

    #: Deployment states in which a paper run is still using the strategy's version.
    LIVE_DEPLOYMENT_STATES = ("RUNNING", "PENDING")

    #: Tables whose rows name the strategy they came from. Removing a strategy erases
    #: these rows too. The audit trail is left alone: it is an append-only record.
    OWNED_TABLES = (
        order_intents,
        orders,
        trade_journal,
        signal_contexts,
        learning_observations,
        backtest_runs,
        deployments,
    )

    def delete(self, user_id: str, strategy_id: str) -> dict[str, Any]:
        """Erase a strategy and everything that came from it.

        Its versions, backtests and paper runs all go. Children that hang off a row
        (versions, order events, backtest curves) go through the foreign-key cascade.

        Refused while a paper run is still using it: the runner reads the pinned version
        on every pass, and pulling it out from under a live run would be a surprise.
        """
        with self.db.session() as session:
            row = StrategyRepository.get(session, strategy_id, user_id)
            if row is None:
                raise self._not_found(strategy_id)
            active = session.execute(
                select(func.count())
                .select_from(deployments)
                .where(
                    deployments.c.user_id == user_id,
                    deployments.c.strategy_id == strategy_id,
                    deployments.c.status.in_(self.LIVE_DEPLOYMENT_STATES),
                )
            ).scalar_one()
            if active:
                raise StrategyError(
                    f"{row['name']} is still running on paper. Stop it on the Paper page first.",
                    code="strategy_in_use",
                    status=409,
                    detail={"active_deployments": int(active)},
                )
            run_ids = select(deployments.c.deployment_id).where(deployments.c.strategy_id == strategy_id)
            session.execute(delete(trade_attributions).where(trade_attributions.c.deployment_id.in_(run_ids)))
            for table in self.OWNED_TABLES:
                session.execute(delete(table).where(table.c.strategy_id == strategy_id))
            session.execute(delete(strategies).where(strategies.c.strategy_id == strategy_id))
        return {"strategy_id": strategy_id, "deleted": True}

    def versions(self, user_id: str, strategy_id: str) -> list[dict[str, Any]]:
        """Every version, newest first, each saying whether it can be deployed.

        ``deployable`` is computed by asking the *runner's own* resolver, so a
        version listed as deployable is one the loop will actually trade. A list
        that called an unresolvable version "ready" would send an operator to a
        deployment that silently does nothing.
        """
        with self.db.session() as session:
            if StrategyRepository.get(session, strategy_id, user_id) is None:
                raise self._not_found(strategy_id)
            rows = StrategyRepository.versions(session, strategy_id)
        return [self._describe_version(row) for row in rows]

    def version(self, user_id: str, strategy_id: str, version: int) -> dict[str, Any]:
        """One version, with its definition parsed rather than re-encoded."""
        with self.db.session() as session:
            if StrategyRepository.get(session, strategy_id, user_id) is None:
                raise self._not_found(strategy_id)
            row = StrategyRepository.version(session, strategy_id, version)
        if row is None:
            raise StrategyError(
                f"strategy {strategy_id} has no version {version}",
                code="version_not_found",
                status=404,
            )
        described = self._describe_version(row)
        definition, reason = parse_definition(row.get("definition"))
        described["definition"] = definition
        described["definition_parse_error"] = reason
        described["canonical_definition"] = row.get("definition")
        described["author_user_id"] = row.get("author_user_id")
        return described

    # ---------------------------------------------------------------- versions
    def create_version(
        self,
        user_id: str,
        strategy_id: str,
        *,
        definition: Any,
        change_note: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append an immutable version.

        The definition is validated first and a structurally broken one is
        refused (422) with the reasons attached, unless ``force`` is set. Storing
        it anyway is possible — an author may be mid-way through writing rules
        that reference a strategy not yet registered — but it takes saying so,
        because the consequence of getting it wrong is a deployment that reports
        itself running and never trades.
        """
        parsed, reason = parse_definition(definition)
        if parsed is None:
            raise StrategyError(
                f"the definition is not usable: {reason}",
                code="invalid_definition",
                status=422,
                detail={"errors": [{"code": "unreadable_definition", "message": reason}]},
            )

        canonical = canonical_definition(parsed)
        if len(canonical.encode("utf-8")) > MAX_DEFINITION_BYTES:
            raise StrategyError(
                f"the definition is {len(canonical.encode('utf-8'))} bytes; the limit is "
                f"{MAX_DEFINITION_BYTES}",
                code="definition_too_large",
                status=413,
            )

        report = validate_definition(parsed)
        if not report["ok"] and not force:
            raise StrategyError(
                "the definition has structural errors, so it would not run; fix them "
                "or pass force=true to store it anyway",
                code="invalid_definition",
                status=422,
                detail={
                    "errors": report["errors"],
                    "warnings": report["warnings"],
                    "paper": report["paper"],
                    "backtest": report["backtest"],
                },
            )

        with self.db.session() as session:
            if StrategyRepository.get(session, strategy_id, user_id) is None:
                raise self._not_found(strategy_id)
            try:
                row = StrategyRepository.create_version(
                    session,
                    strategy_id=strategy_id,
                    author_user_id=user_id,
                    definition=parsed,
                    change_note=change_note,
                )
            except DuplicateDefinition as exc:
                # Not a conflict to shrug at: identical definitions are the same
                # strategy, and storing them as v4 and v5 would make two names for
                # one subject. The existing version is named so the caller can use it.
                raise StrategyError(
                    f"an identical definition already exists as version "
                    f"{exc.existing_version}",
                    code="duplicate_definition",
                    status=409,
                    detail={"existing_version": exc.existing_version},
                ) from exc
        described = self._describe_version(row)
        described["validation"] = report
        logger.info(
            "strategy %s version %s created by %s",
            strategy_id[:8],
            row["version"],
            user_id[:8],
        )
        return described

    # -------------------------------------------------------------- validation
    def validate(
        self,
        user_id: str,
        strategy_id: str,
        *,
        version: int | None = None,
        definition: Any = None,
    ) -> dict[str, Any]:
        """Validate a definition — a draft one, or a stored version.

        Draft validation is the useful half: it is what lets an author find the
        typo *before* it becomes an immutable row. Passing ``definition`` checks
        that and nothing is written; passing ``version`` (or neither, which means
        the latest) checks what is actually stored.
        """
        with self.db.session() as session:
            if StrategyRepository.get(session, strategy_id, user_id) is None:
                raise self._not_found(strategy_id)
            target = (
                StrategyRepository.version(session, strategy_id, version)
                if version is not None
                else StrategyRepository.latest_version(session, strategy_id)
            )

        if definition is not None:
            report = validate_definition(definition)
            report.update(
                {
                    "strategy_id": strategy_id,
                    "version": None,
                    "checked": "draft",
                    "stored": False,
                }
            )
            return report

        if target is None:
            raise StrategyError(
                (
                    f"strategy {strategy_id} has no version {version} to validate"
                    if version is not None
                    else f"strategy {strategy_id} has no versions to validate"
                ),
                code="version_not_found",
                status=404,
            )

        parsed, reason = parse_definition(target.get("definition"))
        report = validate_definition(parsed if parsed is not None else target.get("definition"))
        report.update(
            {
                "strategy_id": strategy_id,
                "version": int(target["version"]),
                "checked": "stored",
                "stored": True,
                "definition_hash": target.get("definition_hash"),
                "created_at": _iso(target.get("created_at")),
                "parse_error": reason,
            }
        )
        return report

    # -------------------------------------------------------------------- seed
    def seed_example(
        self, user_id: str, *, name: str = EXAMPLE_NAME
    ) -> dict[str, Any]:
        """Create the worked example strategy and its version 1, idempotently.

        Idempotent because the caller is a fresh installation: a command that
        fails the second time it is run is a command nobody can put in a setup
        script. If the strategy exists and already holds this exact definition the
        existing version is returned with ``created: False``.
        """
        wanted = canonical_definition(EXAMPLE_DEFINITION)
        digest = definition_hash(wanted)

        with self.db.session() as session:
            existing = next(
                (
                    row
                    for row in StrategyRepository.list_for_user(session, user_id)
                    if row["name"].strip().lower() == name.strip().lower()
                ),
                None,
            )
            if existing is not None:
                match = next(
                    (
                        v
                        for v in StrategyRepository.versions(session, existing["strategy_id"])
                        if v["definition_hash"] == digest
                    ),
                    None,
                )
                if match is not None:
                    return {
                        "strategy": existing,
                        "version": self._describe_version(match),
                        "created": False,
                        "note": (
                            "this strategy already holds the example definition; "
                            "nothing was written"
                        ),
                    }

        try:
            strategy = self.create(
                user_id,
                name=name,
                kind="rules",
                description=EXAMPLE_DESCRIPTION,
                engine_key=EXAMPLE_DEFINITION.get("engine_key"),
            )
        except StrategyError as exc:
            if exc.code != "name_taken":
                raise
            # The name is taken but by something else — a strategy the operator
            # wrote, or an older seed. Refusing is the honest answer; silently
            # creating a second one under a different name would leave two.
            raise StrategyError(
                f"a strategy called {name!r} already exists but does not hold the example "
                "definition; pass --name to seed under a different name",
                code="seed_name_taken",
                status=409,
            ) from exc

        version = self.create_version(
            user_id,
            strategy["strategy_id"],
            definition=EXAMPLE_DEFINITION,
            change_note="seeded example: breakout entry, stop-loss and take-profit exits",
        )
        return {"strategy": strategy, "version": version, "created": True, "note": None}

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _describe_version(row: dict[str, Any]) -> dict[str, Any]:
        """A version row plus the two things a caller always needs next."""
        parsed, reason = parse_definition(row.get("definition"))
        resolution = resolve_rules(parsed) if parsed is not None else None
        return {
            "strategy_id": row.get("strategy_id"),
            "version": int(row.get("version")),
            "created_at": _iso(row.get("created_at")),
            "change_note": row.get("change_note"),
            "definition_hash": row.get("definition_hash"),
            "is_deployed": bool(row.get("is_deployed")),
            #: Whether the paper runner can turn this into live rules. Answered by
            #: the runner's own resolver, so this cannot disagree with what happens
            #: after a deployment is started.
            "deployable": bool(resolution and resolution.ok),
            "not_deployable_reason": None if (resolution and resolution.ok) else (
                resolution.reason if resolution else reason
            ),
        }

    def _decorate(self, session, row: dict[str, Any]) -> dict[str, Any]:
        """Attach the version summary the listing would otherwise need N calls for."""
        latest = StrategyRepository.latest_version(session, row["strategy_id"])
        return {
            **row,
            "latest_version": None if latest is None else int(latest["version"]),
            "version_count": StrategyRepository.version_count(session, row["strategy_id"]),
            "deployable": bool(latest is not None and self._describe_version(latest)["deployable"]),
        }

    @staticmethod
    def _not_found(strategy_id: str) -> StrategyError:
        """Another account's strategy is indistinguishable from a missing one."""
        return StrategyError(
            f"no strategy {strategy_id}",
            code="not_found",
            status=404,
        )


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def get_strategy_service() -> StrategyService:
    return StrategyService()


__all__ = [
    "KINDS",
    "MAX_DEFINITION_BYTES",
    "StrategyError",
    "StrategyService",
    "get_strategy_service",
]
