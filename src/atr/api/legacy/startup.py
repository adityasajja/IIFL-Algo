"""Startup work: warm the caches, start the paper runner and the background loops."""

from __future__ import annotations

import asyncio
import logging



from atr.api.legacy.dashboard import _breadth_trend
from atr.api.legacy.scanner import _SCAN_CACHE

logger = logging.getLogger("atr.api")


async def on_startup() -> None:
    from atr.alerts.intelligent import get_intelligent_monitor
    from atr.api.stream import get_broadcaster

    get_broadcaster().set_loop(asyncio.get_running_loop())
    await get_intelligent_monitor().start()
    # Price the paper engine off live ticks before the runner starts, so the
    # first fill is priced from the feed rather than yesterday's close.
    from atr.api.price_sources import install_live_price_source

    install_live_price_source()
    _start_paper_runner()
    _warm_breadth_cache()
    _warm_market_intel()
    _warm_instrument_master()
    asyncio.create_task(_insights_loop())
    asyncio.create_task(_eod_refresh_loop())
    asyncio.create_task(_jobs_loop())


async def _jobs_loop() -> None:
    """Run the once-a-day jobs (currently the nightly backup)."""
    from atr.config.settings import get_settings
    from atr.infra.backup import backup_now
    from atr.jobs.daily import DailyJob, JobStore, run_forever
    from atr.market_intel.service import DATA_ROOT

    settings = get_settings()
    backup = DailyJob(
        "backup",
        lambda: backup_now(DATA_ROOT, settings.backup_dir, keep=settings.backup_keep),
        at=(2, 30),
    )
    from atr.research.forward_tracker import run_daily

    # After the 16:30 price top-up, so the day's bars are in before signals are read.
    tracker = DailyJob("forward_tracker", lambda: run_daily(DATA_ROOT), at=(17, 30), weekdays_only=True)
    from atr.research import gap_plan

    gap = DailyJob("gap_plan", lambda: gap_plan.run_daily(DATA_ROOT), at=(17, 45), weekdays_only=True)
    await run_forever([backup, tracker, gap], JobStore(DATA_ROOT))


async def _eod_refresh_loop() -> None:
    """Top the tracked names up from public daily bars once a day, after the close.

    Runs without a broker session, so the price history keeps moving when the login lapses.
    """
    from atr.data.eod_refresh import due, refresh
    from atr.market_intel.service import DATA_ROOT, get_market_intel_service

    def run() -> None:
        if not due(DATA_ROOT):
            return
        summary = refresh(DATA_ROOT, get_market_intel_service().get_universe_symbols())
        if summary["ok"]:
            # New bars exist: the next read must recompute instead of serving the old day.
            get_market_intel_service().invalidate()
            _SCAN_CACHE["data"] = None

    await asyncio.sleep(60)  # let startup finish first
    while True:
        try:
            await asyncio.to_thread(run)
        except Exception:  # noqa: BLE001 - a failed refresh must not stop the loop
            logger.exception("end-of-day refresh failed")
        await asyncio.sleep(1800)


async def _insights_loop() -> None:
    """Send the day's read once, after the close. Checked every five minutes."""
    from atr.insights.service import get_insights_service

    def run():
        try:
            from atr.services.broker_access import authed_client

            client = authed_client()
        except Exception:  # noqa: BLE001 - no session: the digest uses the saved holdings
            client = None
        return get_insights_service().maybe_send_daily(client=client)

    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(run)
        except Exception:  # noqa: BLE001 - a failed send must not stop the loop
            logger.exception("daily insights send failed")


def _start_paper_runner() -> None:
    """Start the continuous paper-trading loop.

    The runner is what turns the paper engine from something a request drives
    into something that runs: without it, a deployment marked RUNNING evaluates
    nothing and fills nothing, and the dashboard shows a live strategy that is
    actually inert.

    Best-effort, never fatal. A runner that cannot start must not take the API
    down with it — the deployment routes and the health endpoint are how an
    operator finds out what is wrong, and ``GET /api/v1/paper/runner`` reports
    ``running: false`` rather than pretending the loop is up.
    """
    try:
        from atr.services.runner import get_runner

        get_runner().start()
        logger.info("paper runner started")
    except Exception as exc:  # noqa: BLE001 — the API must still serve
        logger.warning("paper runner could not start: %s", exc)


def _warm_instrument_master() -> None:
    """Build the instrument index off the request path.

    A cold build reads 3,000+ parquet footers and takes ~8s. Paying that on the
    first watchlist or search request would look like a hung page, so it is paid
    at startup on a daemon thread — the same pattern as the breadth cache below.
    """
    try:
        from atr.instruments.service import get_instrument_master

        get_instrument_master().warm()
    except Exception as exc:  # noqa: BLE001 — best effort, never fatal
        logger.warning("instrument master warm-up could not start: %s", exc)


def _warm_market_intel() -> None:
    """Run the full-universe market pass once at startup, off the request path.

    The first Markets or Today visit otherwise pays ~5s reading ~500 parquet files.
    """
    import threading

    def work() -> None:
        try:
            from atr.market_intel.service import get_market_intel_service

            get_market_intel_service().compute_all()
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            logger.warning("market intelligence warm-up failed: %s", exc)

    threading.Thread(target=work, daemon=True, name="atr-market-intel-warm").start()


def _warm_breadth_cache() -> None:
    """Compute the 5-session breadth series once, off the request path.

    Scoring a universe sample five times takes ~15s. Paying that on the first
    dashboard poll would look like a hung page, so it is paid at startup on a
    daemon thread instead — by the time a browser connects, the answer is
    usually already cached.
    """
    import threading

    def work() -> None:
        try:
            import time as _t

            t0 = _t.monotonic()
            _breadth_trend("NSEEQ")
            logger.info("breadth cache warmed in %.1fs", _t.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 — best effort, never fatal
            logger.warning("breadth warm-up failed: %s", exc)

    threading.Thread(target=work, daemon=True, name="atr-breadth-warm").start()
