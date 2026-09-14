"""Drive the Phase 1 UI through Chrome DevTools Protocol and screenshot it.

This is the end-to-end proof that the auth gate and the watchlist panel actually
render and work in a real browser — the API contract tests prove the *server*
side, and this proves the client side: React mounting, the gate gating, the form
submitting, the panel drawing a table.

It drives the real DOM rather than asserting on markup, because the two failure
modes worth catching are "React never hydrated" and "a controlled input did not
receive the value" — both invisible to a static check.

Usage (with the server already up on 8899):

    ./.venv/Scripts/python.exe scripts/probe_phase1_ui.py

Screenshots land in ``.workbuddy-ai/shots-phase1/``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request

import websockets

PORT = 9222
BASE = "http://127.0.0.1:8899"
OUT_DIR = r"D:\ALGO\.workbuddy-ai\shots-phase1"
#: Unique per run, so a stale session cookie from the previous run cannot make the
#: gate invisible and quietly skip the first thing this script exists to check.
PROFILE = rf"D:\ALGO\.cdp-profile-phase1-{int(time.time())}"

#: Kept out of the f-strings below so the quoting stays readable.
SET_LIST_NAME_JS = "window.__atr.setValue('input[placeholder=\"Name\"]', \"{name}\")"
SET_SYMBOLS_JS = "window.__atr.setValue('input[placeholder^=\"RELIANCE\"]', \"RELIANCE, TCS\")"
SET_IDENTIFIER_JS = (
    "window.__atr.setValue('input[placeholder=\"you@example.com\"]', \"aditya\")"
)
SET_PASSWORD_JS = (
    "window.__atr.setValue('input[type=password]', \"Str0ngPassw0rd\")"
)

# ── the DOM helpers, evaluated in the page ───────────────────────────────────
# React owns the input value, so assigning `.value` directly is ignored: React's
# onChange only fires when the *native* setter runs. Calling the prototype setter
# and then dispatching a bubbling `input` is what makes the component see it.
HELPERS = r"""
window.__atr = {
  setValue(selector, value) {
    const el = document.querySelector(selector);
    if (!el) return 'no element for ' + selector;
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  },
  clickText(text) {
    const target = text.toLowerCase();
    const norm = (b) => (b.innerText || '').replace(/\s+/g, ' ').trim();
    const all = [...document.querySelectorAll('button')].filter((b) => norm(b));
    // Exact first. `StatefulButton` renders its label twice (an sr-only copy plus
    // the animated one), and "Create" is a prefix of "Create the owner account",
    // so a naive `includes` would click the wrong thing.
    const exact = all.find((b) => norm(b).toLowerCase() === target);
    const partial = all
      .filter((b) => norm(b).toLowerCase().includes(target))
      .sort((a, b) => norm(a).length - norm(b).length)[0];
    const btn = exact || partial;
    if (!btn) return 'no button: ' + text + ' | have: ' + all.map(norm).join(' / ');
    if (btn.disabled) return 'button is DISABLED: ' + norm(btn);
    btn.click();
    return 'ok';
  },
  clickTitle(title) {
    const el = document.querySelector('[title="' + title + '"]');
    if (!el) return 'no [title="' + title + '"]';
    if (el.disabled) return 'title button is DISABLED: ' + title;
    el.click();
    return 'ok';
  },
  text() { return (document.body.innerText || '').replace(/\s+/g, ' ').trim(); },
  buttons() {
    return [...document.querySelectorAll('button')].map((b) => ({
      label: (b.innerText || '').replace(/\s+/g, ' ').trim(),
      title: b.getAttribute('title'),
      disabled: b.disabled,
    })).filter((b) => b.label || b.title);
  },
  table() {
    const t = document.querySelector('table');
    if (!t) return null;
    return {
      headers: [...t.querySelectorAll('thead th')].map((th) => (th.innerText || '').trim()),
      rows: [...t.querySelectorAll('tbody tr')].map((tr) =>
        [...tr.querySelectorAll('td')].map((td) => (td.innerText || '').replace(/\s+/g, ' ').trim()),
      ),
    };
  },
  theme() { return document.documentElement.dataset.theme || '(unset)'; },
  errors() {
    return [...document.querySelectorAll('[role="alert"]')]
      .map((e) => (e.innerText || '').trim()).filter(Boolean);
  },
};
'helpers installed'
"""


def find_browser() -> str:
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.exists(path):
            return path
    raise SystemExit("no Chromium-based browser found")


class Page:
    """A very small CDP client — enough to evaluate JS and take screenshots."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self._n = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def reader(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            mid = msg.get("id")
            if mid in self._pending and not self._pending[mid].done():
                self._pending[mid].set_result(msg)

    async def cmd(self, method: str, params: dict | None = None):
        self._n += 1
        mid = self._n
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout=90)

    async def js(self, expression: str):
        res = await self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return res.get("result", {}).get("result", {}).get("value")

    async def wait_for(self, expression: str, seconds: int = 40, label: str = "") -> bool:
        for _ in range(seconds * 2):
            try:
                if await self.js(expression):
                    return True
            except Exception:  # noqa: BLE001 - the page is mid-navigation
                pass
            await asyncio.sleep(0.5)
        print(f"    ! timed out waiting for {label or expression}")
        return False

    async def shot(self, name: str) -> str:
        res = await self.cmd(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
        )
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"{name}.png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(res["result"]["data"]))
        print(f"    screenshot: {path}")
        return path


async def main() -> int:
    # ── the backend must be up ───────────────────────────────────────────────
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        raise SystemExit(f"backend not reachable at {BASE}")

    proc = subprocess.Popen(
        [
            find_browser(),
            f"--remote-debugging-port={PORT}",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1500,1000",
            f"--user-data-dir={PROFILE}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                    pages = json.load(r)
                candidates = [
                    p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")
                ]
                if candidates:
                    ws_url = candidates[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                continue
        if not ws_url:
            raise SystemExit("no CDP endpoint")

        async with websockets.connect(ws_url, max_size=None) as ws:
            page = Page(ws)
            reader = asyncio.create_task(page.reader())
            await page.cmd("Runtime.enable")
            await page.cmd("Page.enable")

            # ── 1. the gate ──────────────────────────────────────────────────
            # Whether the gate offers "set up" or "sign in" depends on whether the
            # store already has an account. Asking the API first keeps the probe
            # re-runnable against the same database instead of demanding a wipe.
            print("\n[1] First visit — the gate should stand in front of the app")
            with urllib.request.urlopen(f"{BASE}/api/v1/auth/bootstrap", timeout=5) as r:
                state = json.load(r)
            needs_setup = bool(state["needs_setup"])
            print(f"    store needs setup: {needs_setup}")

            await page.cmd("Page.navigate", {"url": f"{BASE}/"})
            await page.wait_for(
                "!!document.querySelector('input[type=password]')", 40, "the gate form"
            )
            await asyncio.sleep(1.5)
            await page.js(HELPERS)
            print(f"    theme: {await page.js('window.__atr.theme()')}")
            print(f"    visible: {(await page.js('window.__atr.text()') or '')[:200]!r}")
            # The dashboard must NOT be reachable while signed out.
            leaked = await page.js("!!document.querySelector('[aria-label=\"ATR navigation\"]')")
            print(f"    dashboard shell present while signed out: {leaked}")
            print(f"    buttons: {await page.js('JSON.stringify(window.__atr.buttons())')}")
            await page.shot("1-auth-gate")

            # ── 2. get in through the UI ─────────────────────────────────────
            if needs_setup:
                print("\n[2] Fill the first-run form and submit")
                for selector, value in (
                    ("input[type=email]", "aditya@example.com"),
                    ('input[placeholder="how you sign in"]', "aditya"),
                    ('input[placeholder="optional"]', "Aditya"),
                    ("input[type=password]", "Str0ngPassw0rd"),
                ):
                    print(
                        f"    {selector} <- {value!r}: "
                        f"{await page.js(f'window.__atr.setValue({selector!r}, {value!r})')}"
                    )
                await asyncio.sleep(0.6)
                submit_label = "Create the owner account"
            else:
                print("\n[2] Sign in with the existing account")
                print(
                    f"    identifier: {await page.js(SET_IDENTIFIER_JS)}"
                )
                print(f"    password: {await page.js(SET_PASSWORD_JS)}")
                await asyncio.sleep(0.6)
                submit_label = "Sign in"

            print(
                f"    submit: "
                f"{await page.js(f'window.__atr.clickText({submit_label!r})')}"
            )

            signed_in = await page.wait_for(
                "!!document.querySelector('[aria-label=\"ATR navigation\"]')", 40, "the dashboard shell"
            )
            await asyncio.sleep(3)
            print(f"    signed in: {signed_in}")
            print(f"    alerts: {await page.js('JSON.stringify(window.__atr.errors())')}")
            print(f"    visible: {(await page.js('window.__atr.text()') or '')[:240]!r}")
            await page.shot("2-signed-in")

            # ── 3. the watchlist panel ───────────────────────────────────────
            print("\n[3] Navigate to the watchlist tab")
            await page.cmd("Page.navigate", {"url": f"{BASE}/#watchlist"})
            await page.wait_for(
                "!!document.querySelector('[title=\"New watchlist\"]')", 40, "the list rail"
            )
            await asyncio.sleep(2)
            await page.js(HELPERS)
            print(f"    visible: {(await page.js('window.__atr.text()') or '')[:220]!r}")
            await page.shot("3-watchlist-empty")

            print("\n[4] Create a list and add a symbol, through the DOM")
            print(f"    new: {await page.js('window.__atr.clickTitle(\"New watchlist\")')}")
            await asyncio.sleep(1)
            # A timestamped name so a second run does not collide with the first —
            # watchlist names are unique per user and a duplicate is a 409.
            list_name = f"Probe {int(time.time()) % 100000}"
            print(
                f"    name: "
                f"{await page.js(SET_LIST_NAME_JS.replace('{name}', list_name))}"
            )
            await asyncio.sleep(0.4)
            print(f"    create: {await page.js('window.__atr.clickText(\"Create\")')}")
            added_list = await page.wait_for(
                f"document.body.innerText.includes({list_name!r})", 30, "the new list"
            )
            print(f"    list created: {added_list}")
            await asyncio.sleep(2)

            print(f"    symbol: {await page.js(SET_SYMBOLS_JS)}")
            await asyncio.sleep(0.6)
            print(f"    add: {await page.js('window.__atr.clickText(\"Add\")')}")
            await page.wait_for("!!document.querySelector('table')", 40, "the quote table")
            await asyncio.sleep(4)
            print(f"    table: {await page.js('JSON.stringify(window.__atr.table())')}")
            print(f"    alerts: {await page.js('JSON.stringify(window.__atr.errors())')}")
            # The rail badge must agree with the table. It is loaded before the
            # symbols are added, so a stale count here is the specific bug this
            # check exists for.
            body_text = await page.js("window.__atr.text()") or ""
            print(f"    rail badge in step: {f'{list_name} 2' in body_text}")
            print(f"    visible: {body_text[:260]!r}")
            await page.shot("4-watchlist-populated")

            reader.cancel()

        print("\ndone")
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
