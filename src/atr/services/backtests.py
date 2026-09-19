"""Backtest service — the one place a run's lifecycle is defined.

The API route and the CLI both call in here. Nothing above this layer knows
that a backtest is executed on a thread, and nothing below it knows that a run
has a status.

Why a thread and not a task queue: a run is CPU-bound (pandas over thousands of
bars), so an asyncio task on the request loop would block every other request
for the duration. A thread releases the GIL inside pandas' C code, which is
where nearly all the time goes. A real queue (Celery/RQ) is the right answer
when runs outlive the process, and this is explicitly not that yet — see the
note in ``run_backtest`` on what happens to a run left RUNNING by a crash.

The lifecycle contract, which the tests assert against:

    submit()  → QUEUED, returns immediately with a run id
    worker    → RUNNING, then COMPLETED with artefacts, or FAILED with an error
    a failed run stores no partial artefacts (so aggregate metrics can never be
    read against a half-written trade list)
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from atr.backtest.config import BacktestRunConfig, ConfigError, from_payload
from atr.backtest.runner import (
    CANCELLED,
    COMPLETED,
    FAILED,
    PROGRESS_QUEUED,
    PROGRESS_RUNNING,
    QUEUED,
    RUNNING,
    BacktestError,
    BacktestRunner,
)

#: Statuses a run can be polled in, re-exported so callers need one import.
STATUSES = (QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED)

#: Bounded worker pool. A backtest is CPU-bound: more workers than cores makes
#: every run slower, and an unbounded pool lets a scripted loop of submissions
#: exhaust memory with queued work.
MAX_WORKERS = 2


class BacktestService:
    """Submit, execute and read back backtest runs."""

    def __init__(self, db: Any = None, *, workers: int = MAX_WORKERS) -> None:
        self._db = db
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atr-backtest")
        self._lock = threading.Lock()
        self._futures: dict[str, Any] = {}

    # ------------------------------------------------------------------ db
    @property
    def db(self) -> Any:
        """Resolve the live database handle on every access.

        Caching it in ``__init__`` (or in a property's first call) pins an
        ``AppDatabase`` whose engine may be disposed by the time the next call
        arrives — a background worker that outlives a test's teardown holds a
        handle to a closed database, and the failure surfaces as an
        intermittent foreign-key or "no such table" error somewhere unrelated.
        """
        if self._db is not None:
            return self._db
        from atr.appdb.engine import get_app_db

        return get_app_db()

    # -------------------------------------------------------------- options
    def options(self, *, owner_user_id: str | None = None) -> dict[str, Any]:
        """Everything the configuration form needs, with availability flagged.

        Timeframes other than daily are listed as *unsupported* rather than
        omitted. A dropdown that silently lacks an option reads as a bug; one
        that offers it greyed out with "the local cache holds daily bars only"
        answers the question the user was about to ask.

        ``owner_user_id`` scopes the saved-strategy list. When it is absent the
        CLI path falls back to any user, because a CLI run has no principal —
        but the API always passes one, since showing one account another
        account's strategies is both wrong and, for the deploy form, misleading:
        only saved strategies carry a version, so a mis-scoped list makes the
        form look empty while the strategies exist.
        """
        from atr.backtest.config import (
            COST_MODELS,
            KNOWN_TIMEFRAMES,
            SIZING_MODES,
            SOURCES,
            SUPPORTED_TIMEFRAMES,
        )

        labels = {
            "india_delivery": "Indian delivery — STT, stamp duty, GST (realistic)",
            "flat_per_share": "IBKR-style flat per share (understates Indian costs)",
            "none": "No costs (A/B comparison only)",
        }
        sizing_labels = {
            "fixed_fraction": "Fixed fraction of equity per position",
            "fixed_quantity": "Fixed number of shares",
            "equal_weight": "Equal weight",
        }

        return {
            "strategies": self.strategy_registry(owner_user_id=owner_user_id),
            "timeframes": [
                {
                    "value": tf,
                    "label": tf,
                    "available": tf in SUPPORTED_TIMEFRAMES,
                    "reason": None
                    if tf in SUPPORTED_TIMEFRAMES
                    else "the local cache holds daily bars only",
                }
                for tf in KNOWN_TIMEFRAMES
            ],
            "cost_models": [
                {"value": m, "label": labels.get(m, m)} for m in COST_MODELS
            ],
            "sizing_modes": [
                {"value": m, "label": sizing_labels.get(m, m)} for m in SIZING_MODES
            ],
            "sources": [{"value": s, "label": s} for s in SOURCES],
            "exchanges": ["NSEEQ", "BSEEQ"],
            "universes": self._universes(),
            "limits": {
                "max_symbols": 500,
                "min_capital": 10_000.0,
                "max_persisted_trades": 20_000,
            },
        }

    def _universes(self) -> list[dict[str, Any]]:
        try:
            from atr.screener.service import get_screener_service

            return get_screener_service().universes()
        except Exception as exc:  # noqa: BLE001 — options must render even if data is cold
            logger.warning("could not list universes: {}", exc)
            return []

    def strategy_registry(self, *, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        """Strategies that can be backtested, with their version counts.

        Merges the built-in registry with the user's saved strategies, because a
        backtest can name either and a form that only showed one would make the
        other unreachable from the UI.

        ``owner_user_id`` selects whose saved strategies are listed. It is
        keyword-only and optional so the CLI keeps working, but the API always
        passes it: a list built from an arbitrary row would disclose one user's
        strategies to another.
        """
        from atr.appdb.repositories import StrategyRepository
        from atr.strategy.strategies import STRATEGIES

        rows: list[dict[str, Any]] = []
        for name in sorted(STRATEGIES):
            rows.append(
                {
                    "kind": "builtin",
                    "key": name,
                    "name": name,
                    "strategy_id": None,
                    "versions": [],
                }
            )

        # Saved strategies are optional context: an unreachable database must
        # not stop the built-in list rendering.
        #
        # `StrategyRepository.list_for_user` returns a *plain list* — unlike
        # `OrderRepository.list_for_user`, which returns `(rows, total)`. This
        # call unpacked it as a pair, and the broad `except` below swallowed the
        # resulting ValueError, so the saved half of the registry was silently
        # empty for every user, in every build. Only the built-ins ever appeared.
        try:
            with self.db.session() as session:
                owner = owner_user_id or self._any_user_id(session)
                saved = (
                    StrategyRepository.list_for_user(session, owner) if owner else []
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("saved strategies unavailable: {}", exc)
            saved = []

        for row in saved:
            try:
                with self.db.session() as session:
                    versions = StrategyRepository.versions(session, row["strategy_id"])
            except Exception:  # noqa: BLE001
                versions = []
            rows.append(
                {
                    "kind": "saved",
                    "key": row.get("engine_key"),
                    "name": row.get("name"),
                    "strategy_id": row["strategy_id"],
                    "description": row.get("description"),
                    "versions": [
                        {
                            "version": v["version"],
                            "created_at": _iso(v.get("created_at")),
                            "change_note": v.get("change_note"),
                        }
                        for v in versions
                    ],
                }
            )
        return rows

    @staticmethod
    def _any_user_id(session) -> str:
        """The account whose strategies are listed when none is specified.

        Single-tenant in practice today; when there is a real principal the
        route passes it and this is only a fallback for the CLI.
        """
        from sqlalchemy import select

        from atr.appdb.schema import users

        found = session.execute(select(users.c.user_id).limit(1)).scalar()
        return str(found or "")

    # ---------------------------------------------------------------- submit
    def submit(self, payload: dict, *, user_id: str) -> dict[str, Any]:
        """Validate, persist a QUEUED run, and start it. Returns immediately."""
        try:
            config = from_payload(payload)
        except ConfigError as exc:
            raise BacktestError(str(exc), code=exc.code, status=422) from exc

        # An equal-weight run over one symbol with a 10% target is a
        # contradiction the user cannot see: they asked to spread risk over a
        # set that has one member. Say so rather than silently dividing by one.
        if config.sizing.mode == "equal_weight" and len(self._resolve_for_check(config)) == 1:
            raise BacktestError(
                "equal-weight sizing needs more than one symbol to spread across",
                code="equal_weight_needs_many",
                status=422,
            )

        from atr.appdb.repositories import BacktestRunRepository

        with self.db.session() as session:
            row = BacktestRunRepository.create(
                session,
                user_id=user_id,
                config=config.to_dict(),
                engine_key=config.engine_key or config.strategy,
                strategy_id=config.strategy_id,
                strategy_version=config.strategy_version,
            )

        run_id = row["run_id"]
        future = self._pool.submit(self._execute, run_id, user_id, config)
        with self._lock:
            self._futures[run_id] = future
        logger.info("backtest {} submitted ({} symbols)", run_id, len(config.symbols))
        return {
            "run_id": run_id,
            "status": QUEUED,
            "progress": PROGRESS_QUEUED,
            "config": config.to_dict(),
            "fingerprint": config.fingerprint(),
        }

    def _resolve_for_check(self, config: BacktestRunConfig) -> list[str]:
        """Symbol count without touching the cache."""
        if config.symbols:
            return [s for s in config.symbols if str(s).strip()]
        if config.universe:
            try:
                from atr.screener.service import get_screener_service

                return get_screener_service().symbols_for(config.universe, config.exchange)
            except Exception:  # noqa: BLE001
                return []
        return []

    # --------------------------------------------------------------- execute
    def _version_definition(self, config: BacktestRunConfig) -> dict | None:
        """Read a saved strategy version's definition, or None for a built-in.

        A version is loaded by its exact number, never "latest": a run whose
        subject can change after the fact is not reproducible, which is the
        whole point of persisting the config.
        """
        if not (config.strategy_id and config.strategy_version is not None):
            return None
        from atr.appdb.repositories import StrategyRepository

        with self.db.session() as session:
            row = StrategyRepository.version(
                session, config.strategy_id, int(config.strategy_version)
            )
        if row is None:
            raise BacktestError(
                f"strategy version {config.strategy_id}#{config.strategy_version} "
                "does not exist",
                code="version_not_found",
                status=404,
            )
        raw = row.get("definition") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise BacktestError(
                f"strategy version {config.strategy_version} has an unreadable "
                "definition",
                code="bad_version_definition",
                status=422,
            ) from exc
        return parsed if isinstance(parsed, dict) else {}

    def _universe_resolver(self, universe: str, exchange: str) -> list[str]:
        """Expand a named universe. Injected into the runner.

        The runner is a compute layer and cannot import the screener, so the
        resolution is handed to it as a callable rather than reached for.
        """
        from atr.screener.service import get_screener_service

        return get_screener_service().symbols_for(universe, exchange)

    def _instrument_master(self):
        try:
            from atr.instruments.service import get_instrument_master

            return get_instrument_master()
        except Exception as exc:  # noqa: BLE001 — a runner falls back to raw paths
            logger.debug("instrument master unavailable: {}", exc)
            return None

    def _execute(self, run_id: str, user_id: str, config: BacktestRunConfig) -> None:
        """Worker body. Never raises: every failure is recorded on the run."""
        from atr.appdb.repositories import BacktestArtefactRepository, BacktestRunRepository

        try:
            with self.db.session() as session:
                BacktestRunRepository.mark_running(session, run_id, user_id)
                BacktestRunRepository.set_progress(
                    session, run_id, user_id, PROGRESS_RUNNING
                )
        except Exception as exc:  # noqa: BLE001
            # A run whose status row cannot be updated must not be silently
            # abandoned: record why, so the caller sees FAILED rather than a run
            # parked at QUEUED forever.
            logger.error("could not mark backtest {} running: {}", run_id, exc)
            self._fail(run_id, user_id, f"run could not be started: {exc}")
            return

        try:
            outcome = BacktestRunner(
                config,
                version_definition=self._version_definition(config),
                universe_resolver=self._universe_resolver,
                instrument_master=self._instrument_master(),
            ).execute()
        except BacktestError as exc:
            self._fail(run_id, user_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — a worker must record, not crash
            detail = f"{type(exc).__name__}: {exc}"
            logger.error("backtest {} failed: {}\n{}", run_id, detail, traceback.format_exc())
            self._fail(run_id, user_id, detail)
            return

        try:
            with self.db.session() as session:
                BacktestArtefactRepository.save_curve(session, run_id, "equity", outcome.equity)
                BacktestArtefactRepository.save_curve(session, run_id, "drawdown", outcome.drawdown)
                BacktestArtefactRepository.save_curve(session, run_id, "exposure", outcome.exposure)
                BacktestArtefactRepository.save_trades(session, run_id, outcome.trades)
                BacktestArtefactRepository.save_monthly(session, run_id, outcome.monthly)

                metrics = dict(outcome.metrics)
                metrics["warnings"] = outcome.warnings
                metrics["signal_context_model_version"] = _context_model_version()
                written = BacktestRunRepository.complete(
                    session,
                    run_id,
                    user_id,
                    metrics=metrics,
                    data_fingerprint=outcome.data_fingerprint,
                )
            if not written:
                # The guard rejected the write: the run was cancelled or already
                # terminal. That is a legitimate outcome, not an error, but the
                # artefacts now belong to a run that is not COMPLETED, so they
                # are removed rather than left to be read as a result.
                logger.info("backtest {} finished but was no longer running", run_id)
                with self.db.session() as session:
                    BacktestArtefactRepository.delete_for_run(session, run_id)
                return
            logger.info(
                "backtest {} completed: {} trades", run_id, metrics.get("num_trades")
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("could not persist backtest {}: {}", run_id, exc)
            self._fail(run_id, user_id, f"results could not be stored: {exc}")
            return

        self._enrich_signal_context(run_id, user_id)

    def _enrich_signal_context(self, run_id: str, user_id: str) -> None:
        """Best-effort, post-completion context annotation for a run's trades.

        Deliberately outside the persistence try/except: enrichment is an
        annotation on an already-COMPLETED run and must never turn it FAILED.
        """
        try:
            from atr.signal_context.service import get_signal_context_service

            get_signal_context_service().enrich_run(run_id, user_id)
        except Exception as exc:  # noqa: BLE001 — annotation is not a gate
            logger.warning("signal context enrichment for backtest %s failed: %s", run_id, exc)

    def _fail(self, run_id: str, user_id: str, error: str) -> None:
        from atr.appdb.repositories import BacktestArtefactRepository, BacktestRunRepository

        try:
            with self.db.session() as session:
                # No partial artefacts under a FAILED run: a metrics reader must
                # never find a trade list that stopped mid-write.
                BacktestArtefactRepository.delete_for_run(session, run_id)
                BacktestRunRepository.fail(session, run_id, user_id, error=error)
        except Exception as exc:  # noqa: BLE001
            logger.error("could not record failure for backtest {}: {}", run_id, exc)

    # ------------------------------------------------------------------ read
    def run(self, run_id: str, user_id: str) -> dict[str, Any]:
        from atr.appdb.repositories import BacktestArtefactRepository, BacktestRunRepository

        with self.db.session() as session:
            row = BacktestRunRepository.get(session, run_id, user_id)
            if row is None:
                raise BacktestError("backtest run not found", code="not_found", status=404)
            trades = BacktestArtefactRepository.trade_count(session, run_id) if row["status"] == COMPLETED else 0

        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "progress": float(row.get("progress") or 0.0),
            "engine_key": row.get("engine_key"),
            "strategy_id": row.get("strategy_id"),
            "strategy_version": row.get("strategy_version"),
            "created_at": _iso(row.get("created_at")),
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(row.get("finished_at")),
            "data_fingerprint": row.get("data_fingerprint"),
            "error": row.get("error"),
            "config": _loads(row.get("config")),
            "metrics": _loads(row.get("metrics")),
            "num_trades": trades,
            "reproducible": bool(row.get("data_fingerprint")),
        }

    def history(
        self, user_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        from atr.appdb.repositories import BacktestRunRepository

        if status and status.upper() not in STATUSES:
            raise BacktestError(
                f"unknown status {status!r}; known: {', '.join(STATUSES)}",
                code="unknown_status",
                status=422,
            )

        with self.db.session() as session:
            rows, total = BacktestRunRepository.list_for_user(
                session,
                user_id,
                status=status.upper() if status else None,
                limit=limit,
                offset=offset,
            )

        runs = []
        for row in rows:
            metrics = _loads(row.get("metrics")) or {}
            config = _loads(row.get("config")) or {}
            runs.append(
                {
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "progress": float(row.get("progress") or 0.0),
                    "strategy": row.get("engine_key") or config.get("strategy"),
                    "strategy_id": row.get("strategy_id"),
                    "strategy_version": row.get("strategy_version"),
                    "created_at": _iso(row.get("created_at")),
                    "finished_at": _iso(row.get("finished_at")),
                    "symbols": len(config.get("symbols") or []) or config.get("universe"),
                    "total_return_pct": metrics.get("total_return_pct"),
                    "sharpe": metrics.get("sharpe"),
                    "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                    "num_trades": metrics.get("num_trades"),
                    "error": row.get("error"),
                }
            )
        return {"runs": runs, "total": total}

    def metrics(self, run_id: str, user_id: str) -> dict[str, Any]:
        row = self._completed(run_id, user_id)
        payload = _loads(row.get("metrics")) or {}
        return {
            "run_id": run_id,
            "metrics": payload,
            "data_fingerprint": row.get("data_fingerprint"),
        }

    def equity(self, run_id: str, user_id: str) -> dict[str, Any]:
        """Equity plus the drawdown and exposure curves, each as {ts, value}."""
        self._completed(run_id, user_id)
        from atr.appdb.repositories import BacktestArtefactRepository

        with self.db.session() as session:
            equity = BacktestArtefactRepository.curve(session, run_id, "equity")
            drawdown = BacktestArtefactRepository.curve(session, run_id, "drawdown")
            exposure = BacktestArtefactRepository.curve(session, run_id, "exposure")

        return {
            "run_id": run_id,
            "equity": [_point(r) for r in equity],
            "drawdown": [_point(r) for r in drawdown],
            "exposure": [_point(r) for r in exposure],
        }

    def trades(
        self, run_id: str, user_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        self._completed(run_id, user_id)
        from atr.appdb.repositories import BacktestArtefactRepository

        with self.db.session() as session:
            total = BacktestArtefactRepository.trade_count(session, run_id)
            rows = BacktestArtefactRepository.trades(session, run_id, limit=limit, offset=offset)

        return {
            "run_id": run_id,
            "trades": [_trade(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _effective_params(self, row: dict[str, Any]) -> dict[str, Any]:
        """The parameters that actually ran, for the trade-detail read.

        Reading ``config["params"]`` alone is wrong for a run pinned to a saved
        version: those parameters live in the version's stored definition and
        never appear in the run's own config, so the trade detail reported ``{}``
        — a run that says nothing about the parameters it used, which is exactly
        the question requirement 4 asks the trade view to answer.

        The precedence mirrors ``BacktestRunner.build_strategy``: the stored
        definition is the baseline, and explicit run params win on top of it, so
        a user can vary one value without forking a version.
        """
        config = _loads(row.get("config")) or {}
        params = dict(config.get("params") or {})
        strategy_id = row.get("strategy_id")
        version = row.get("strategy_version")
        if not (strategy_id and version is not None):
            return params
        try:
            definition = self._version_definition(
                BacktestRunConfig(
                    strategy_id=strategy_id,
                    strategy_version=int(version),
                    params=params,
                )
            )
        except BacktestError:
            # The run exists, so its version did too. If it has since been
            # removed, report what the run recorded rather than failing a read
            # of a completed run over missing provenance.
            return params
        stored = (definition or {}).get("params")
        return {**stored, **params} if isinstance(stored, dict) else params

    def trade(self, run_id: str, user_id: str, seq: int) -> dict[str, Any]:
        """One trade in full, including the signal conditions behind its entry."""
        row = self._completed(run_id, user_id)
        from atr.appdb.repositories import BacktestArtefactRepository

        with self.db.session() as session:
            trade = BacktestArtefactRepository.trade(session, run_id, seq)
        if trade is None:
            raise BacktestError("trade not found in this run", code="not_found", status=404)

        config = _loads(row.get("config")) or {}
        out = _trade(trade)
        out["strategy"] = {
            "engine_key": row.get("engine_key"),
            "strategy_id": row.get("strategy_id"),
            "version": row.get("strategy_version"),
            # The effective parameters, not just the run's overrides: a version
            # backtest must be able to say what the version contained.
            "params": self._effective_params(row),
        }
        out["exits"] = {
            "stop_loss_pct": (config.get("stops") or {}).get("stop_loss_pct"),
            "take_profit_pct": (config.get("stops") or {}).get("take_profit_pct"),
            "trailing_stop_pct": (config.get("stops") or {}).get("trailing_stop_pct"),
        }
        out["costs"] = config.get("costs") or {}
        out["sizing"] = config.get("sizing") or {}
        return out

    def monthly(self, run_id: str, user_id: str) -> dict[str, Any]:
        self._completed(run_id, user_id)
        from atr.appdb.repositories import BacktestArtefactRepository

        with self.db.session() as session:
            rows = BacktestArtefactRepository.monthly(session, run_id)

        years: dict[int, dict[int, float | None]] = {}
        for row in rows:
            years.setdefault(int(row["year"]), {})[int(row["month"])] = row.get("return_pct")

        return {
            "run_id": run_id,
            "matrix": [
                {
                    "year": year,
                    "months": [months.get(m) for m in range(1, 13)],
                    "year_total": _compound(months.values()),
                }
                for year, months in sorted(years.items())
            ],
        }

    def cancel(self, run_id: str, user_id: str) -> dict[str, Any]:
        from atr.appdb.repositories import BacktestRunRepository

        with self.db.session() as session:
            changed = BacktestRunRepository.cancel(session, run_id, user_id)
        if not changed:
            raise BacktestError(
                "run is not cancellable (already finished, or not yours)",
                code="not_cancellable",
                status=409,
            )
        return {"run_id": run_id, "status": CANCELLED}

    # ------------------------------------------------------------------
    def _completed(self, run_id: str, user_id: str) -> dict[str, Any]:
        """Read a run that must be COMPLETED, with a specific error if not.

        A results reader asking for metrics on a RUNNING run gets told it is
        still running, rather than an empty metric set that looks like a
        completed run which made nothing.
        """
        from atr.appdb.repositories import BacktestRunRepository

        with self.db.session() as session:
            row = BacktestRunRepository.get(session, run_id, user_id)
        if row is None:
            raise BacktestError("backtest run not found", code="not_found", status=404)
        if row["status"] != COMPLETED:
            raise BacktestError(
                f"run is {row['status'].lower()}, not completed",
                code="not_completed",
                status=409,
            )
        return row

    # ------------------------------------------------------------------
    def shutdown(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting work and let in-flight runs finish.

        ``drain`` is the default because a worker that outlives its service
        writes into a database that may already have been torn down — which in
        tests surfaced as an intermittent error on an unrelated test, and in
        production would mean a run completing against a database that had been
        rotated. Waiting a bounded time costs a shutdown a few seconds; not
        waiting costs correctness.

        ``cancel_futures`` drops work that has not started, since there is no
        point running a queued backtest against a service being shut down. Work
        already executing is allowed to finish rather than being interrupted
        mid-write, which would leave a half-written artefact set.

        After the timeout the threads are abandoned — the old behaviour — so a
        genuinely hung run cannot block process exit forever.
        """
        self._pool.shutdown(wait=False, cancel_futures=True)
        if not drain:
            return
        deadline = time.monotonic() + timeout
        with self._lock:
            pending = list(self._futures.values())
        for future in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "backtest shutdown timed out with {} run(s) still executing",
                    sum(1 for f in pending if not f.done()),
                )
                return
            try:
                future.result(timeout=remaining)
            except Exception:  # noqa: BLE001 — a failed run must not block exit
                pass
        with self._lock:
            self._futures.clear()


def _point(row: dict[str, Any]) -> dict[str, Any]:
    return {"ts": _iso(row.get("ts")), "value": row.get("value")}


def _trade(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": row.get("seq"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "quantity": row.get("quantity"),
        "entry_ts": _iso(row.get("entry_ts")),
        "entry_price": row.get("entry_price"),
        "exit_ts": _iso(row.get("exit_ts")),
        "exit_price": row.get("exit_price"),
        "gross_pnl": row.get("gross_pnl"),
        "commission": row.get("commission"),
        "net_pnl": row.get("net_pnl"),
        "return_pct": row.get("return_pct"),
        "duration_days": row.get("duration_days"),
        "exit_reason": row.get("exit_reason"),
        "signal_reason": row.get("signal_reason"),
        "strategy_version": row.get("strategy_version"),
    }


def _compound(values) -> float | None:
    """Compound a sequence of monthly percentage returns."""
    total = 1.0
    seen = False
    for value in values:
        if value is None:
            continue
        total *= 1.0 + (float(value) / 100.0)
        seen = True
    return (total - 1.0) * 100.0 if seen else None


def _iso(value: Any) -> str | None:
    """Naive-UTC datetimes go out with a Z, matching the rest of the API."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return f"{value.isoformat()}Z"
    return str(value)


def _loads(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    import json

    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _context_model_version() -> str | None:
    """The signal-context scoring model this run's trades were tagged with.

    Stamped on the run's metrics so a reader knows which model produced the
    contexts; ``None`` if the engine cannot be imported (it is an annotation).
    """
    try:
        from atr.signal_context import DEFAULT_CONTEXT_MODEL_V1

        return DEFAULT_CONTEXT_MODEL_V1.version
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
_service: BacktestService | None = None
_service_lock = threading.Lock()


def get_backtest_service() -> BacktestService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BacktestService()
        return _service


def reset_backtest_service() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.shutdown()
        _service = None


__all__ = [
    "BacktestService",
    "STATUSES",
    "get_backtest_service",
    "reset_backtest_service",
]
