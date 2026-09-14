"""Capture an arbitrary panel and report its relative-time coverage.

Usage: ./.venv/Scripts/python.exe scripts/probe_panel_cdp.py signals
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

PORT = 9222
OUT_DIR = r"D:\ALGO\.workbuddy-ai\shots-ia"

JS = r"""
(() => {
  const body = document.body.innerText || '';
  const rel = [...document.querySelectorAll('span')]
    .map((s) => (s.innerText || '').replace(/\s+/g, ' ').trim())
    .filter((t) => /^\d+ (minute|hour|day|month)s? ago|^just now|^in \d/.test(t));
  const abs = [...document.querySelectorAll('span')]
    .map((s) => (s.innerText || '').replace(/\s+/g, ' ').trim())
    .filter((t) => /^\d{2}:\d{2}(:\d{2})?$/.test(t));
  return JSON.stringify({
    heading: (document.querySelector('h1,h2') || {}).innerText || null,
    relativeSpans: [...new Set(rel)].slice(0, 12),
    bareClockSpans: [...new Set(abs)].slice(0, 12),
    hasRelative: rel.length > 0,
  }, null, 2);
})()
"""


def find_browser():
    for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]:
        if os.path.exists(p):
            return p
    raise SystemExit("no browser")


async def main(panel):
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("backend down")

    proc = subprocess.Popen(
        [find_browser(), f"--remote-debugging-port={PORT}", "--headless=new",
         "--disable-gpu", "--no-first-run", "--window-size=1600,1400",
         "--user-data-dir=" + r"D:\ALGO\.cdp-profile2", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                    pages = json.load(r)
                c = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
                if c:
                    ws_url = c[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            raise SystemExit("no CDP")

        async with websockets.connect(ws_url, max_size=None) as ws:
            n = [0]
            pend: dict[int, asyncio.Future] = {}

            async def reader():
                async for raw in ws:
                    m = json.loads(raw)
                    mid = m.get("id")
                    if mid in pend and not pend[mid].done():
                        pend[mid].set_result(m)

            rt = asyncio.create_task(reader())

            async def cmd(method, params=None):
                n[0] += 1
                mid = n[0]
                f = asyncio.get_running_loop().create_future()
                pend[mid] = f
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                return await asyncio.wait_for(f, timeout=60)

            await cmd("Runtime.enable")
            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": f"http://127.0.0.1:8000/#{panel}"})

            for _ in range(45):
                await asyncio.sleep(1)
                p = await cmd("Runtime.evaluate", {"expression": "(document.body.innerText||'').length > 400", "returnByValue": True})
                if p.get("result", {}).get("result", {}).get("value"):
                    break
            await asyncio.sleep(6)

            inv = await cmd("Runtime.evaluate", {"expression": JS, "returnByValue": True})
            shot = await cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            rt.cancel()

        print(inv.get("result", {}).get("result", {}).get("value"))
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"14-{panel.replace(chr(47), chr(45))}.png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(shot["result"]["data"]))
        print("written:", path)
    finally:
        proc.terminate()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "signals"))
