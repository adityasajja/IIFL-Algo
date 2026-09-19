"""Price-source wiring that belongs to the API layer.

Why this module exists
----------------------

The paper engine needs live tick prices, and the live ticks live behind
:class:`atr.api.stream.TickBroadcaster`. But ``services`` sits *below* ``api`` in
this codebase's layering — ``tests/test_architecture.py`` enforces that a service
must never import the transport layer, not even inside a function. So the service
cannot reach for the broadcaster itself.

The dependency therefore runs one way only: this module imports the broadcaster
**and** the service, and hands the service a callable. The service stays ignorant
of where its prices come from, which is also what lets a test substitute a
deterministic source without touching either package.

Wire it once, at startup. Until it is wired the paper engine prices off the daily
cache — the previous behaviour, not a crash.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("atr.api.price_sources")


def install_live_price_source() -> None:
    """Point the paper engine's default price source at the tick stream.

    Called from ``main``'s startup. Best-effort: if the broadcaster cannot be
    built, the paper engine keeps working against the daily cache, and
    ``GET /api/v1/paper/runner`` will show no signals rather than the API failing
    to boot.

    **Two wirings, not one, and the second is the one that mattered.** Reading a
    price out of the stream (the source) does nothing unless the symbols are
    actually *on* the stream (the subscriber). The broadcaster resolves a symbol
    to a contract only from ``subscribe()`` — the browser path — so a paper
    deployment with no dashboard open was never subscribed to anything, its price
    lookups all returned ``None``, and the venue silently fell back to yesterday's
    close. Both seams are installed here, from the one layer allowed to see both
    sides.
    """
    try:
        from atr.api.stream import get_broadcaster
        from atr.services import paper as paper_service

        broadcaster = get_broadcaster()
        paper_service.install_live_source(
            paper_service.live_tick_source(broadcaster=broadcaster)
        )
        paper_service.install_live_subscriber(broadcaster.ensure_symbols)
        logger.info("paper engine price source: live ticks (cache fallback)")
    except Exception as exc:  # noqa: BLE001 - never fatal
        logger.warning("live price source not installed: %s", exc)


def clear_live_price_source() -> None:
    """Unregister it. For tests and for a clean shutdown."""
    from atr.services import paper as paper_service

    paper_service.install_live_source(None)
    paper_service.install_live_subscriber(None)
