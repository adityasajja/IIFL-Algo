"""The market-intel computation runs once for concurrent callers, not once each."""

from __future__ import annotations

import threading
import time

from atr.market_intel.service import MarketIntelService


def test_concurrent_callers_share_one_computation(monkeypatch):
    service = MarketIntelService.__new__(MarketIntelService)
    service._lock = threading.Lock()
    service._compute_lock = threading.Lock()
    service._cached_summary = service._cached_sectors = service._cached_stocks = None
    service._cache_time = 0.0
    service._ttl_seconds = 60.0

    calls = []

    def slow(*, force_refresh=False):
        calls.append(force_refresh)
        time.sleep(0.2)
        with service._lock:
            service._cached_summary, service._cached_sectors, service._cached_stocks = (
                "summary", [], {},
            )
            service._cache_time = time.time()
        return service._cached_summary, service._cached_sectors, service._cached_stocks

    monkeypatch.setattr(service, "_compute_all_uncached", slow)

    threads = [threading.Thread(target=service.compute_all, kwargs={"force_refresh": True}) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f"expected one shared computation, got {len(calls)}"
