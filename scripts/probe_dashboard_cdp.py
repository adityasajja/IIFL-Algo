"""Capture the dashboard over CDP and inventory the seven refinements.

agent-browser does not support Windows, so this drives headless Edge/Chrome
directly over the DevTools protocol with `websockets`.

Two capture modes:
  * live     -- whatever the backend actually serves (empty book today)
  * populated -- the same page with /dashboard/summary intercepted and served
                 from a fixture, so the KPI sparkline / win-rate / drawdown
                 render paths can be seen at all.

Usage: ./.venv/Scripts/python.exe scripts/probe_dashboard_cdp.py [populated]
"""
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websockets

PORT = 9222
TARGET = "http://127.0.0.1:8000/#dashboard"
OUT_DIR = r"D:\ALGO\.workbuddy-ai\shots-ia"

# Fixture served in place of the real endpoint in `populated` mode. Only the
# inputs are synthetic -- the page composes it with the same code as live.
POPULATED = {
    "as_of": "2026-09-13T06:28:00+00:00",
    "exchange": "NSEEQ",
    "positions": {
        "count": 3, "value": 147944.5, "invested": 148270.0,
        "day_pnl": 129.5, "day_pnl_complete": True, "day_pnl_from_cache": 3,
        "day_pnl_pct": 0.087,
        "unrealized_pnl": -325.5, "last_flat_at": "2026-09-11T15:29:00+05:30",
    },
    "performance": {
        "trades": 36, "win_rate": 66.7, "wins": 20, "losses": 10, "sample": 30,
        "realized_pnl": 14520.0, "current_drawdown_pct": -6.4,
        "max_drawdown_pct": -35.54,
        "sparkline": [780.0, 2810.0, 4840.0, 5620.0, 7650.0, 9680.0, 10460.0],
        "equity_curve": [-430.0, 390.0, 1210.0, 780.0, 1600.0, 2420.0, 1990.0,
                         2810.0, 3630.0, 3200.0, 4020.0, 4840.0, 4410.0, 5230.0,
                         6050.0, 5620.0, 6440.0, 7260.0, 6830.0, 7650.0, 8470.0,
                         8040.0, 8860.0, 9680.0, 9250.0, 10070.0, 10890.0,
                         10460.0, 11280.0, 12100.0, 11670.0, 12490.0, 13310.0,
                         12880.0, 13700.0, 14520.0],
    },
    "breadth": {
        "series": [23.6, 22.9, 20.9, 20.4, 20.0],
        "dates": ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
                  "2026-09-11"],
        "current": 20.0, "delta_5d": -3.6, "expanding": False,
        "sampled": 600, "universe": 2672,
    },
}

INVENTORY_JS = r"""
(() => {
  const all = (s) => [...document.querySelectorAll(s)];
  // innerText is undefined on detached/void nodes, so never assume it exists.
  const txt = (el) => {
    if (!el) return null;
    const s = (el.innerText == null ? el.textContent : el.innerText) || '';
    const t = s.replace(/\s+/g, ' ').trim();
    return t || null;
  };

  const banner = all('[role="status"]').map(txt).filter(Boolean)[0] || null;

  const kpis = all('.grid.gap-4 > div').map((c) => {
    const kids = c.children;
    return {
      label: txt(kids[0]),
      value: txt(kids[1]),
      hasSvg: !!c.querySelector('svg'),
      svgAria: (c.querySelector('svg[aria-label]') || {})
        .getAttribute ? c.querySelector('svg[aria-label]').getAttribute('aria-label') : null,
      polylineDots: (() => {
        const pl = c.querySelector('svg polyline');
        return pl ? pl.getAttribute('points').trim().split(/\s+/).length : 0;
      })(),
    };
  });

  return JSON.stringify({
    banner,
    kpiCount: kpis.length,
    kpis,
    svgCount: all('svg').length,
    sparkAria: all('svg[aria-label]').map((s) => s.getAttribute('aria-label')),
    moversTitle: document.body.innerText.includes('Top movers'),
    momentumGone: !document.body.innerText.includes('Top momentum'),
    envRowGone: !document.body.innerText.includes('Environment: dev'),
    relativeTimes: all('*').map(txt)
      .filter((t) => t && /(\d+ (minute|hour|day)s? ago|just now)/.test(t))
      .slice(0, 10),
    alertsCta: txt(all('button').find((b) =>
      /No alerts armed/.test((b.innerText || b.textContent || '')))),
    flatStates: all('*').map(txt)
      .filter((t) => t && /No open positions|last flat|flat /.test(t)).slice(0, 5),
  }, null, 2);
})()
"""


def find_browser():
    import os
    for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        if os.path.exists(p):
            return p
    raise SystemExit("no Edge/Chrome found")


async def main(mode):
    exe = find_browser()

    # Wait for the backend to answer BEFORE the browser opens the URL. Pointing
    # Edge at a dead port makes Chromium render its own "refused to connect"
    # error page and cache it, so a later Page.reload can still resolve to the
    # stale error document and every assertion reads an empty DOM.
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health",
                                        timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("backend not answering on 127.0.0.1:8000")

    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={PORT}", "--headless=new",
         "--disable-gpu", "--no-first-run", "--window-size=1600,1400",
         "--user-data-dir=" + r"D:\ALGO\.cdp-profile", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json", timeout=2
                ) as r:
                    pages = json.load(r)
                cands = [p for p in pages if p.get("type") == "page"
                         and p.get("webSocketDebuggerUrl")]
                if cands:
                    ws_url = cands[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            raise SystemExit("CDP never came up")

        log: list[str] = []
        async with websockets.connect(ws_url, max_size=None) as ws:
            n = [0]
            pending: dict[int, asyncio.Future] = {}
            intercept = {"on": False}

            def fulfill(body_obj) -> None:
                body = base64.b64encode(
                    json.dumps(body_obj).encode()).decode()
                return body

            async def reader() -> None:
                """Single reader for the socket.

                Every consumer must go through this loop. Two coroutines both
                calling ws.recv() steal each other's frames -- which is why the
                first attempt's cmd() swallowed the Fetch.requestPaused events
                and the fixture never applied.
                """
                async for raw in ws:
                    msg = json.loads(raw)
                    method = msg.get("method")
                    if method == "Runtime.consoleAPICalled":
                        a = msg["params"].get("args", [{}])
                        log.append(str(a[0].get("value", ""))[:200])
                    elif method == "Fetch.requestPaused":
                        if intercept["on"]:
                            await ws.send(json.dumps({
                                "id": 900000 + len(pending),
                                "method": "Fetch.fulfillRequest",
                                "params": {
                                    "requestId": msg["params"]["requestId"],
                                    "responseCode": 200,
                                    "responseHeaders": [
                                        {"name": "Content-Type",
                                         "value": "application/json"},
                                        {"name": "Access-Control-Allow-Origin",
                                         "value": "*"}],
                                    "body": fulfill(POPULATED)}}))
                        else:
                            await ws.send(json.dumps({
                                "id": 900000 + len(pending),
                                "method": "Fetch.continueRequest",
                                "params": {
                                    "requestId": msg["params"]["requestId"]}}))
                    mid = msg.get("id")
                    if mid in pending and not pending[mid].done():
                        pending[mid].set_result(msg)

            async def cmd(method, params=None):
                n[0] += 1
                mid = n[0]
                fut = asyncio.get_running_loop().create_future()
                pending[mid] = fut
                await ws.send(json.dumps({"id": mid, "method": method,
                                          "params": params or {}}))
                return await asyncio.wait_for(fut, timeout=60)

            reader_task = asyncio.create_task(reader())

            await cmd("Runtime.enable")
            await cmd("Page.enable")

            # Arm interception FIRST, then navigate -- otherwise the initial
            # /dashboard/summary fired on mount escapes the Fetch domain.
            if mode == "populated":
                await cmd("Fetch.enable", {"patterns": [
                    {"urlPattern": "*dashboard/summary*",
                     "requestStage": "Request"}]})
                intercept["on"] = True

            await cmd("Page.navigate", {"url": TARGET})

            # Poll until the KPI strip actually exists, rather than sleeping a
            # fixed interval and hoping. Bounded so a real failure still exits.
            mounted = False
            for _ in range(60):
                await asyncio.sleep(1)
                probe = await cmd("Runtime.evaluate", {
                    "expression": "!!document.querySelector('.grid.gap-4')",
                    "returnByValue": True})
                if probe.get("result", {}).get("result", {}).get("value"):
                    mounted = True
                    break

            # Let NumberTicker / motion animations settle before shooting.
            await asyncio.sleep(5)
            print("mounted:", mounted)

            inv = await cmd("Runtime.evaluate",
                            {"expression": INVENTORY_JS, "returnByValue": True})
            shot = await cmd("Page.captureScreenshot",
                             {"format": "png", "captureBeyondViewport": True})

            reader_task.cancel()

        print("console:", log[:6] or "none")
        val = inv.get("result", {}).get("result", {}).get("value")
        print(val if isinstance(val, str) else json.dumps(inv)[:600])

        import os
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"13-dashboard-{mode}.png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(shot["result"]["data"]))
        print("written:", path)
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "live"))
