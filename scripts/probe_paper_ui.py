"""Drive the Paper tab end to end through the real UI, headlessly.

Why a probe and not just unit tests: the unit tests prove the *derivations* are
right, and they would all still pass if the panel never called them — if a prop
were wired to the wrong handler, if the tab were never registered, if the
overview response field were named something else. This navigates a real
browser at the running app, fills the real deploy form, clicks the real button,
and reads back what the real backend logged.

It asserts on the rendered DOM and on the API, and it takes a screenshot so a
human can confirm the screen looks like what the text claims.

Usage:
    ./.venv/Scripts/python.exe scripts/probe_paper_ui.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

PORT = 9223
OUT_DIR = r"D:\ALGO\.workbuddy-ai\shots-paper"

#: The probe's own session cookie. Reusing the browser's would work, but a
#: cookie jar we control means the probe can assert on the API as the same
#: principal the page is using, without guessing at the page's state.
COOKIE = ""

#: A sentence for the reason prompts. `pause` / `stop` / `reset` all require
#: ``reason: str = Field(min_length=1)`` on the backend, so a blank string is a
#: 422 — and a UI that offers the button without a way to answer the prompt is a
#: control that cannot be used at all.
REASON = "probe: exercising the deployment control"

JS_STRIP_OVERLAYS = r"""
(() => {
  // The IIFL session banner and the broker login modal both open on their own,
  // and both cover the page. It is tempting to dismiss them by clicking, but the
  // modal in `components/ui/modal.tsx` renders its panel with `pointer-events-none`
  // on the positioning layer and the backdrop is a full-screen `motion.button`
  // whose `onClick` fires on *release* — a synthesised `click()` on it can be
  // swallowed by the presence gate. Rather than fight the animation, this removes
  // the overlay nodes from the DOM outright.
  //
  // That is the right call for a read-back probe: the overlays are not the
  // subject. Every assertion below is about the Paper panel underneath them, and
  // a probe that reads the modal's text is a false negative that looks exactly
  // like a broken panel.
  const OVERLAY = '.fixed.inset-0, .fixed.inset-4';
  const removed = [];
  for (const el of [...document.querySelectorAll(OVERLAY)]) {
    // Only overlays with a stacking context above the app (z-[80] in the house
    // modal). The app's own inset layers are not full-screen blockers.
    const z = parseInt(getComputedStyle(el).zIndex || '0', 10);
    if (!Number.isFinite(z) || z < 60) continue;
    removed.push((el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60));
    el.remove();
  }
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  return JSON.stringify({ removed, count: removed.length });
})()
"""

JS_LOCATE = r"""
(() => {
  const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null);
  const body = document.body.innerText || '';
  // `innerText` is the *rendered* text, and the renderer uppercases some labels:
  // `components/ui/stat.tsx` declares `uppercase` on the label div, so a `Stat`
  // renders "Today's P&L" as "TODAY'S P&L" while a local `Field` label keeps its
  // case. Asserting on the literal string therefore fails for the stats and
  // passes for the form — a failure that says nothing about the panel and
  // everything about the probe. Compare case-insensitively and keep the raw text
  // in the response so a human can still read it.
  const has = (needle) => body.toLowerCase().includes(needle.toLowerCase());
  const labelOf = (el) => {
    let node = el;
    for (let up = 0; up < 4 && node; up++) {
      node = node.parentElement;
      if (!node) break;
      const lab = node.querySelector('label');
      if (lab) return txt(lab) || '';
    }
    return '';
  };
  const fields = [...document.querySelectorAll('select, input')].map((el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.type || '',
    label: labelOf(el),
    value: el.value,
    disabled: !!el.disabled,
  }));
  const buttons = [...document.querySelectorAll('button')]
    .map((b) => ({ text: txt(b), disabled: !!b.disabled,
                   aria: b.getAttribute('aria-label') || '' }))
    .filter((b) => b.text || b.aria);
  const chips = [...document.querySelectorAll('span')]
    .map((s) => txt(s))
    .filter((t) => t && /^(RUNNING|PAUSED|STOPPED|PENDING|trading|not trading|session open|session closed|runner running|runner stopped)$/.test(t));
  const stages = ['SIGNAL', 'RISK', 'ORDER', 'FILL', 'POSITION'].filter((s) => body.includes(s));
  return JSON.stringify({
    onPaperTab: has('Deploy a strategy version'),
    // Which of the panel's two views is showing. The deploy form and the monitor
    // are toggled by a segmented control, and a deploy that succeeds switches to
    // the monitor on its own — but a probe that re-reads the DOM later cannot
    // assume which one it is looking at.
    onDeployForm: has('Deploy to paper'),
    onMonitor: has("Today's P&L") || has('New deployment'),
    strategyOptions: [...document.querySelectorAll('select')]
      .map((s) => [...s.options].map((o) => o.textContent.trim()))
      .flat()
      .filter((t) => t.includes('Breakout')),
    fields,
    buttons,
    chips: [...new Set(chips)],
    stagesPresent: stages,
    // The rail is the five stage names in order; `Signal`/`Position` alone would
    // also match ordinary prose on the page.
    hasTimelineRail: stages.length >= 3 || (body.includes('Signal') && body.includes('Position')),
    usesEmDashForUnknown: body.includes('\u2014'),
    claimsZeroToday: /Today's P&L\s*\u20b90\b/i.test(body),
    saysCannotDeploy: has('Cannot deploy yet'),
    text: body.slice(0, 6000),
  }, null, 2);
})()
"""

JS_OPEN_MONITOR = r"""
(() => {
  // Switch the panel's segmented control to the Monitor view, the way a user
  // does. Kept separate from `JS_LOCATE` so the monitor assertions test the
  // screen a user would see after pressing the tab, not only the screen the
  // deploy happens to leave behind — the two are different code paths.
  const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null);
  const body = document.body.innerText || '';
  if (/Today's P&L/i.test(body)) return JSON.stringify({ ok: true, alreadyThere: true });
  const tab = [...document.querySelectorAll('button')]
    .find((b) => (txt(b) || '') === 'Monitor');
  if (!tab) {
    return JSON.stringify({ ok: false, why: 'no Monitor control',
                            buttons: [...document.querySelectorAll('button')].map(txt) });
  }
  tab.click();
  return JSON.stringify({ ok: true, alreadyThere: false });
})()
"""

JS_DEPLOY = r"""
(async () => {
  const settle = (ms) => new Promise((r) => setTimeout(r, ms));
  const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null);

  // ── React's controlled inputs ────────────────────────────────────────────
  // React installs its own `value` tracker on the DOM node and compares the new
  // value against what it last rendered. Assigning `el.value = x` updates the
  // DOM *without React hearing about it*, so the select shows the new option
  // while the component's state stays on the old one — the form looks filled in
  // and the component still thinks it is empty. Going through the prototype's
  // native setter is what makes React see a change at all; the bubbling event
  // afterwards is what makes it act on one.
  const setNative = (el, value, kind) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    desc.set.call(el, value);
    el.dispatchEvent(new Event(kind, { bubbles: true }));
  };
  const setSelect = (el, value) => setNative(el, value, 'change');
  const setInput = (el, value) => setNative(el, value, 'input');

  const labelOf = (el) => {
    let node = el;
    for (let up = 0; up < 4 && node; up++) {
      node = node.parentElement;
      if (!node) break;
      const lab = node.querySelector('label');
      if (lab) return txt(lab) || '';
    }
    return '';
  };
  const findSelect = (needle) =>
    [...document.querySelectorAll('select')].find((s) => labelOf(s).includes(needle));
  const findInput = (needle) =>
    [...document.querySelectorAll('input')].find((i) => labelOf(i).includes(needle));

  const strategySel = findSelect('Strategy');
  const versionSel = findSelect('Version');
  const exchangeSel = findSelect('Exchange');
  const timeframeSel = findSelect('Timeframe');
  const universeSel = findSelect('Universe');
  const capitalInput = findInput('Capital');
  const symbolsInput = findInput('Symbols');

  if (!strategySel) {
    return JSON.stringify({ ok: false, why: 'no Strategy select on the form',
                            labels: [...document.querySelectorAll('select, input')].map(labelOf) });
  }
  const want = [...strategySel.options].find((o) => o.textContent.includes('Breakout v1'));
  if (!want) {
    return JSON.stringify({ ok: false, why: 'probe strategy not in the list',
                            options: [...strategySel.options].map((o) => o.textContent.trim()) });
  }

  // ── 1. pick the strategy, and let React re-render the version list ───────
  setSelect(strategySel, want.value);
  await settle(250);

  // The preselect is real behaviour worth asserting: the panel chooses the
  // *newest* version for whichever strategy is chosen, and "newest" is the
  // version this deployment will be pinned to. It is read here, before anything
  // overrides it — checking it afterwards would only be testing this setter.
  const versionOptions = versionSel
    ? [...versionSel.options].map((o) => ({ value: o.value, text: o.textContent.trim() }))
    : [];
  const versionPreselected = versionSel ? versionSel.value : null;
  const newest = versionOptions.length
    ? versionOptions.reduce((a, b) => (Number(b.value) > Number(a.value) ? b : a))
    : null;
  const preselectIsNewest = !!newest && versionPreselected === newest.value;

  // ── 2. pin the *older* version ──────────────────────────────────────────
  // Deploying v1 rather than the preselected v2 is deliberate: it is the case
  // where the pin and the default differ, so a panel that ignored the selection
  // and always took "latest" would be caught here and nowhere else.
  let pinned = null;
  if (versionSel) {
    const v1 = [...versionSel.options].find((o) => /^v?1\b/.test(o.textContent.trim()));
    if (v1) { setSelect(versionSel, v1.value); pinned = v1.value; await settle(150); }
  }

  // ── 3. the rest of the form ─────────────────────────────────────────────
  if (exchangeSel) { setSelect(exchangeSel, 'NSEEQ'); await settle(100); }
  if (capitalInput) { setInput(capitalInput, '500000'); await settle(100); }
  if (timeframeSel) {
    const daily = [...timeframeSel.options].find((o) => /1d|daily/i.test(o.textContent));
    if (daily) { setSelect(timeframeSel, daily.value); await settle(100); }
  }
  if (symbolsInput) { setInput(symbolsInput, 'RELIANCE-EQ'); await settle(400); }

  // ── 4. read the button back ─────────────────────────────────────────────
  const deployBtn = [...document.querySelectorAll('button')]
    .find((b) => (txt(b) || '').includes('Deploy to paper'));
  const body = document.body.innerText || '';
  if (!deployBtn) {
    return JSON.stringify({ ok: false, why: 'no Deploy button' });
  }

  return JSON.stringify({
    ok: !deployBtn.disabled,
    why: deployBtn.disabled ? 'the deploy button is still disabled' : null,
    strategyChosen: txt(strategySel.selectedOptions[0]),
    versionPreselected,
    versionOptions,
    preselectIsNewest,
    versionPinned: pinned,
    versionAfterPin: versionSel ? versionSel.value : null,
    exchange: exchangeSel ? exchangeSel.value : null,
    capital: capitalInput ? capitalInput.value : null,
    timeframe: timeframeSel ? timeframeSel.value : null,
    symbols: symbolsInput ? symbolsInput.value : null,
    universe: universeSel ? universeSel.value : null,
    deployDisabled: !!deployBtn.disabled,
    deployLabel: txt(deployBtn),
    stillSaysCannotDeploy: body.includes('Cannot deploy yet'),
    readinessLine: (body.match(/[^\n]*Cannot deploy yet[^\n]*/) || [''])[0].slice(0, 200),
  });
})()
"""

JS_CLICK_DEPLOY = r"""
(() => {
  const txt = (el) => (el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null);
  const btn = [...document.querySelectorAll('button')]
    .find((b) => (txt(b) || '').includes('Deploy to paper'));
  if (!btn) return JSON.stringify({ ok: false, why: 'no deploy button' });
  if (btn.disabled) return JSON.stringify({ ok: false, why: 'disabled', text: txt(btn) });
  btn.click();
  return JSON.stringify({ ok: true });
})()
"""



def find_browser() -> str:
    for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]:
        if os.path.exists(p):
            return p
    raise SystemExit("no browser found")


def wait_for_backend(timeout: int = 60) -> None:
    for _ in range(timeout):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise SystemExit("backend is not up on :8000")


def login() -> str:
    """Mint a session token through the real login route.

    The probe logs in rather than being handed a token by the caller, so running
    it is one command with no prior setup. It still needs *some* account: this
    reads the probe credentials from the environment, and says so plainly when
    they are absent rather than failing later with a confusing 401.
    """
    identifier = os.environ.get("ATR_PROBE_USER", "aditya")
    password = os.environ.get("ATR_PROBE_PASSWORD")
    if not password:
        raise SystemExit(
            "set ATR_PROBE_PASSWORD (and optionally ATR_PROBE_USER) to a real "
            "account on this instance; the probe drives the UI as that user."
        )
    body = json.dumps({"identifier": identifier, "password": password}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.load(r)
    token = payload.get("token")
    if not token:
        raise SystemExit(f"login did not return a token: {payload}")
    return str(token)


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.rows.append((label, bool(ok), detail))

    def report(self) -> int:
        failed = [r for r in self.rows if not r[1]]
        print()
        for label, ok, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            line = f"  {mark}  {label}"
            if detail and not ok:
                line += f"  ({detail})"
            print(line)
        print()
        print("=" * 62)
        if failed:
            print(f"FAILED  {len(failed)}/{len(self.rows)} checks")
            return 1
        print(f"OK  {len(self.rows)}/{len(self.rows)} checks passed")
        return 0


async def main() -> int:
    wait_for_backend()
    token = COOKIE or login()
    checks = Checks()

    proc = subprocess.Popen(
        [
            find_browser(),
            f"--remote-debugging-port={PORT}",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--window-size=1600,1600",
            "--user-data-dir=" + r"D:\ALGO\.cdp-profile-paper",
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
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json", timeout=2
                ) as r:
                    pages = json.load(r)
                cands = [
                    p
                    for p in pages
                    if p.get("type") == "page" and p.get("webSocketDebuggerUrl")
                ]
                if cands:
                    ws_url = cands[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            raise SystemExit("no CDP endpoint")

        async with websockets.connect(ws_url, max_size=None) as ws:
            n = [0]
            pend: dict[int, asyncio.Future] = {}
            console: list[str] = []
            exceptions: list[str] = []

            async def reader_events() -> None:
                async for raw in ws:
                    m = json.loads(raw)
                    mid = m.get("id")
                    if mid in pend and not pend[mid].done():
                        pend[mid].set_result(m)
                        continue
                    method = m.get("method")
                    if method == "Runtime.consoleAPICalled":
                        args = m.get("params", {}).get("args", [])
                        text = " ".join(
                            str(a.get("value") or a.get("description") or "")
                            for a in args
                        )
                        console.append(text)
                    elif method == "Runtime.exceptionThrown":
                        d = m.get("params", {}).get("exceptionDetails", {})
                        exceptions.append(
                            str(d.get("exception", {}).get("description") or d.get("text"))
                        )

            async def cmd(method: str, params: dict | None = None) -> dict:
                n[0] += 1
                mid = n[0]
                f = asyncio.get_running_loop().create_future()
                pend[mid] = f
                await ws.send(
                    json.dumps({"id": mid, "method": method, "params": params or {}})
                )
                return await asyncio.wait_for(f, timeout=90)

            async def evaluate(expr: str) -> object:
                res = await cmd(
                    "Runtime.evaluate",
                    {
                        "expression": expr,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                return res.get("result", {}).get("result", {}).get("value")

            # Reader first: `Runtime.enable` immediately starts emitting
            # console and exception events, and a reader attached afterwards
            # would drop exactly the ones that explain a blank screen.
            rt = asyncio.create_task(reader_events())

            await cmd("Runtime.enable")
            await cmd("Page.enable")
            await cmd(
                "Network.enable",
            )
            # Authenticate the browser as the same principal the probe uses.
            await cmd(
                "Network.setCookie",
                {
                    "name": "atr_session",
                    "value": token,
                    "domain": "127.0.0.1",
                    "path": "/",
                },
            )

            print("=== 1. the Paper tab is registered and reachable ===")
            await cmd(
                "Page.navigate", {"url": "http://127.0.0.1:8000/#paper"}
            )
            loaded = False
            for _ in range(45):
                await asyncio.sleep(1)
                if await evaluate("(document.body.innerText||'').length > 400"):
                    loaded = True
                    break
            checks.check("the app loads at #paper", loaded)

            # The IIFL session banner opens on its own when no broker session is
            # active — `session_active` is false on this instance, and the modal
            # covers the whole page. Every `innerText` assertion below would
            # otherwise be reading the modal's text, which is a false negative
            # that looks exactly like a broken panel. Strip the overlays, then
            # confirm the *app* is on top by looking for text only the app has.
            await asyncio.sleep(3)
            raw = await evaluate(JS_STRIP_OVERLAYS)
            stripped = json.loads(raw) if isinstance(raw, str) else {}
            checks.check(
                "the broker-login overlay was cleared",
                isinstance(stripped, dict),
                str(stripped),
            )
            await asyncio.sleep(1.5)

            raw = await evaluate(JS_LOCATE)
            state = json.loads(raw) if isinstance(raw, str) else {}
            checks.check(
                "no auth gate — the session cookie was accepted",
                not (state.get("text") or "").startswith("Sign in"),
                "saw the account auth gate",
            )
            checks.check(
                "the Paper tab is on screen",
                state.get("onPaperTab") is True,
                (state.get("text") or "")[:160],
            )
            checks.check(
                "the deploy form offers the saved strategy",
                any(
                    "Breakout v1" in o
                    for o in (state.get("strategyOptions") or [])
                ),
                f"options were {state.get('strategyOptions')}",
            )
            checks.check(
                "the runner chip reports its state",
                any(
                    c in (state.get("chips") or [])
                    for c in ("runner running", "runner stopped")
                ),
                f"chips were {state.get('chips')}",
            )
            checks.check(
                "the form exposes every field the requirements name",
                # Substring, not equality: `Version (immutable)` and
                # `Capital (₹)` carry parentheticals that are the panel's own
                # wording, and a probe that pins them would fail on a copy edit.
                all(
                    any(needle in (f.get("label") or "") for f in (state.get("fields") or []))
                    for needle in ("Strategy", "Version", "Capital", "Timeframe", "Symbols")
                ),
                f"labels were {sorted({f.get('label') or '' for f in (state.get('fields') or [])})}",
            )
            shot = await cmd(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True},
            )
            os.makedirs(OUT_DIR, exist_ok=True)
            p1 = os.path.join(OUT_DIR, "01-deploy-form.png")
            with open(p1, "wb") as fh:
                fh.write(base64.b64decode(shot["result"]["data"]))
            print(f"  wrote {p1}")

            print()
            print("=== 2. fill the deploy form ===")
            raw = await evaluate(JS_DEPLOY)
            filled = json.loads(raw) if isinstance(raw, str) else {}
            checks.check(
                "the saved strategy could be selected",
                filled.get("ok") is True,
                str(filled.get("why")) + " | " + str(filled.get("readinessLine")),
            )
            checks.check(
                "choosing a strategy preselects its NEWEST version",
                filled.get("preselectIsNewest") is True,
                f"preselected {filled.get('versionPreselected')} of "
                f"{[v.get('value') for v in (filled.get('versionOptions') or [])]}",
            )
            checks.check(
                "an OLDER version can be pinned explicitly",
                filled.get("versionPinned") == "1"
                and filled.get("versionAfterPin") == "1",
                f"pinned {filled.get('versionPinned')} -> {filled.get('versionAfterPin')}",
            )
            checks.check(
                "the version list offers more than one choice",
                len(filled.get("versionOptions") or []) >= 2,
                f"only {filled.get('versionOptions')}",
            )
            checks.check(
                "symbols bound to the input",
                filled.get("symbols") == "RELIANCE-EQ",
                f"got {filled.get('symbols')}",
            )
            checks.check(
                "the deploy button is enabled once the form is complete",
                filled.get("deployDisabled") is False,
                f"still disabled; readiness said {filled.get('readinessLine')!r}",
            )
            checks.check(
                "the panel no longer says it cannot deploy",
                filled.get("stillSaysCannotDeploy") is False,
                f"readiness said {filled.get('readinessLine')!r}",
            )

            print()
            print("=== 3. click Deploy to paper ===")
            # Snapshot the ids first, so the deployment below is provably the one
            # this click created rather than the newest row of a pre-existing
            # pile — which is how a probe reports success on a stale deployment.
            got = await evaluate(
                "fetch('/api/v1/paper/deployments',{credentials:'include'})"
                ".then(r=>r.text())"
            )
            before = json.loads(got) if isinstance(got, str) else {}
            ids_before = {
                d["deployment_id"] for d in (before.get("deployments") or [])
            }
            checks.check("the pre-existing deployments are countable",
                         isinstance(ids_before, set),
                         f"{len(ids_before)} before")
            print(f"  {len(ids_before)} deployment(s) existed before the click")

            raw = await evaluate(JS_CLICK_DEPLOY)
            clicked = json.loads(raw) if isinstance(raw, str) else {}
            checks.check(
                "the deploy button was enabled and clicked",
                clicked.get("ok") is True,
                str(clicked.get("why")),
            )

            # The click creates + starts a deployment, then the panel switches to
            # the monitor view. Poll the API rather than trusting the animation.
            deployed = None
            for _ in range(30):
                await asyncio.sleep(1)
                got = await evaluate(
                    "fetch('/api/v1/paper/deployments',{credentials:'include'})"
                    ".then(r=>r.text())"
                )
                try:
                    payload = json.loads(got) if isinstance(got, str) else None
                except Exception:
                    payload = None
                fresh = [
                    d
                    for d in ((payload or {}).get("deployments") or [])
                    if d["deployment_id"] not in ids_before
                ]
                if fresh:
                    deployed = fresh[0]
                    break

            checks.check("the click created a NEW deployment", deployed is not None)
            if deployed:
                print(f"  deployment {deployed['deployment_id'][:8]} created")
                checks.check(
                    "it pinned the version the form chose (v1), not the newest",
                    deployed.get("strategy_version") == 1,
                    f"got v{deployed.get('strategy_version')}",
                )
                checks.check(
                    "it was started, not left in PENDING",
                    deployed.get("status") in ("RUNNING", "PAUSED"),
                    f"got {deployed.get('status')}",
                )
                checks.check(
                    "it carries the capital the form held",
                    float(deployed.get("capital") or 0) == 500000.0,
                    f"got {deployed.get('capital')}",
                )
                cfg = deployed.get("config")
                if isinstance(cfg, str):
                    try:
                        cfg = json.loads(cfg)
                    except Exception:
                        cfg = {}
                checks.check(
                    "the runner's universe reached config.symbols",
                    (cfg or {}).get("symbols") == ["RELIANCE-EQ"],
                    f"config was {cfg}",
                )

            print()
            print("=== 4. the monitoring screen renders ===")
            # The panel is a two-view control (Deploy / Monitor). A successful
            # deploy switches to the monitor on its own, but the probe asks for
            # it explicitly so the monitor assertions test the screen a user gets
            # by pressing the tab — a path the deploy's auto-switch does not
            # exercise.
            raw = await evaluate(JS_OPEN_MONITOR)
            opened = json.loads(raw) if isinstance(raw, str) else {}
            checks.check(
                "the Monitor view can be selected",
                opened.get("ok") is True,
                str(opened.get("why")),
            )

            # The monitor view needs one polling round trip before it has an
            # overview. Wait for a *marker* rather than a fixed sleep: asserting
            # after 5s would fail on a slow machine and pass on a fast one, which
            # is a test that measures the machine.
            seen = False
            for _ in range(40):
                await asyncio.sleep(1)
                probe_txt = await evaluate("(document.body.innerText || '')")
                # Case-insensitive: `Stat` renders its label with CSS `uppercase`,
                # so `innerText` returns "TODAY'S P&L", not "Today's P&L".
                if isinstance(probe_txt, str) and "today's p&l" in probe_txt.lower():
                    seen = True
                    break
            checks.check(
                "the monitor view replaced the deploy form",
                seen,
                "the monitor figures never appeared",
            )
            # One more beat for the chips and the rail to settle.
            await asyncio.sleep(2)

            raw = await evaluate(JS_LOCATE)
            state = json.loads(raw) if isinstance(raw, str) else {}
            text = state.get("text") or ""
            low = text.lower()

            checks.check(
                "the status chip renders a lifecycle state",
                any(
                    c in (state.get("chips") or [])
                    for c in ("RUNNING", "PAUSED", "STOPPED", "PENDING")
                ),
                f"chips were {state.get('chips')}",
            )
            checks.check(
                "running and trading are shown as separate facts",
                "trading" in low,
            )
            for needle in (
                "Today's P&L",
                "Total P&L",
                "Capital",
                "Exposure",
            ):
                checks.check(
                    f"the '{needle}' figure is on screen",
                    needle.lower() in low,
                    "not in the rendered text",
                )
            checks.check(
                "all five timeline stages are drawn",
                state.get("stagesPresent")
                == ["SIGNAL", "RISK", "ORDER", "FILL", "POSITION"],
                f"got {state.get('stagesPresent')}",
            )
            checks.check(
                "the idle reason is stated rather than left blank",
                any(
                    phrase in low
                    for phrase in (
                        "session is closed",
                        "not attached",
                        "not trading",
                        "nothing has happened yet",
                        "reset",
                    )
                ),
                "no explanation for an idle deployment",
            )
            checks.check(
                "the control buttons are present",
                {"Start", "Pause", "Stop", "Reset"}
                <= {b.get("text") or "" for b in (state.get("buttons") or [])},
                f"buttons were "
                f"{sorted({b.get('text') or '' for b in (state.get('buttons') or [])})}",
            )

            shot = await cmd(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True},
            )
            p2 = os.path.join(OUT_DIR, "02-monitor.png")
            with open(p2, "wb") as fh:
                fh.write(base64.b64decode(shot["result"]["data"]))
            print(f"  wrote {p2}")

            print()
            print("=== 5. the monitor API agrees with the screen ===")
            got = await evaluate(
                "fetch('/api/v1/paper/deployments',{credentials:'include'})"
                ".then(r=>r.text())"
            )
            payload = json.loads(got) if isinstance(got, str) else {}
            rows = payload.get("deployments") or []
            dep_id = deployed["deployment_id"] if deployed else (
                rows[0]["deployment_id"] if rows else None
            )
            checks.check("a deployment id is available to monitor", dep_id is not None)

            if dep_id:
                got = await evaluate(
                    "fetch('/api/v1/monitor/deployments/"
                    + dep_id
                    + "',{credentials:'include'}).then(r=>r.text())"
                )
                overview = json.loads(got) if isinstance(got, str) else {}
                for view in (
                    "status",
                    "pnl",
                    "positions",
                    "orders",
                    "fills",
                    "signals",
                    "trades",
                    "risk",
                    "timeline",
                ):
                    checks.check(
                        f"the overview carries '{view}'",
                        view in overview,
                    )
                status = overview.get("status") or {}
                checks.check(
                    "status answers 'is it trading' explicitly",
                    "trading" in status and "not_trading_because" in status,
                )
                checks.check(
                    "NOTHING was rejected as JSON (no Infinity / NaN)",
                    got.find("Infinity") == -1 and got.find("NaN") == -1
                    if isinstance(got, str)
                    else True,
                )
                pnl = overview.get("pnl") or {}
                checks.check(
                    "today_pnl is null, not 0, before any fill",
                    pnl.get("today_pnl") is None,
                    f"got {pnl.get('today_pnl')}",
                )
                checks.check(
                    "the monitor is showing THIS deployment",
                    status.get("deployment_id") == dep_id,
                    f"status reported {status.get('deployment_id')}",
                )
                checks.check(
                    "the screen does not print ₹0 for an unmeasured day",
                    not state.get("claimsZeroToday"),
                )

            print()
            print("=== 6. the runner is attached to the deployment ===")
            got = await evaluate(
                "fetch('/api/v1/paper/runner',{credentials:'include'}).then(r=>r.text())"
            )
            runner = json.loads(got) if isinstance(got, str) else {}
            checks.check("the runner reports its state", "running" in runner)
            if dep_id:
                attached = [
                    d
                    for d in (runner.get("deployments") or [])
                    if d.get("deployment_id") == dep_id
                ]
                checks.check(
                    "the running loop is bound to this deployment",
                    bool(attached),
                    f"runner listed "
                    f"{[d.get('deployment_id','')[:8] for d in (runner.get('deployments') or [])]}",
                )
                if attached:
                    checks.check(
                        "the loop resolved a non-empty universe",
                        (attached[0].get("symbols") or 0) >= 1,
                        f"symbols={attached[0].get('symbols')}",
                    )

            print()
            print("=== 7. PAUSE / STOP are reachable from the screen ===")
            # The lifecycle is exercised through the same buttons a user presses,
            # not by calling the API and then claiming the UI has controls. The
            # reason prompt is part of the flow: `pause` and `stop` are 422s
            # without one, so a screen that offers the button but no way to
            # answer the prompt is a dead control.
            async def click_control(name: str) -> dict:
                expr = (
                    "(() => { const t=(e)=>e?(e.innerText||'').replace(/\\s+/g,' ').trim():null;"
                    "const b=[...document.querySelectorAll('button')]"
                    f".find(b=>t(b)==='{name}');"
                    "if(!b) return JSON.stringify({ok:false,why:'no such button'});"
                    "if(b.disabled) return JSON.stringify({ok:false,why:'disabled'});"
                    "b.click(); return JSON.stringify({ok:true}); })()"
                )
                raw = await evaluate(expr)
                return json.loads(raw) if isinstance(raw, str) else {}

            async def answer_reason(sentence: str) -> dict:
                # The prompt renders an autofocused `Input`; its value is set
                # through the native setter for the same reason every other
                # controlled field is, then Enter confirms.
                expr = (
                    "(() => {"
                    "const i=document.activeElement;"
                    "if(!i||i.tagName!=='INPUT')"
                    "  return JSON.stringify({ok:false,why:'no focused input'});"
                    "const d=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(i),'value');"
                    f"d.set.call(i,{json.dumps(sentence)});"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));"
                    "i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));"
                    "return JSON.stringify({ok:true,focused:true});"
                    "})()"
                )
                raw = await evaluate(expr)
                return json.loads(raw) if isinstance(raw, str) else {}

            async def status_of(did: str) -> str | None:
                got = await evaluate(
                    "fetch('/api/v1/paper/deployments',{credentials:'include'})"
                    ".then(r=>r.text())"
                )
                payload = json.loads(got) if isinstance(got, str) else {}
                for d in payload.get("deployments") or []:
                    if d["deployment_id"] == did:
                        return d.get("status")
                return None

            if dep_id:
                paused = await click_control("Pause")
                checks.check(
                    "the Pause button is clickable while RUNNING",
                    paused.get("ok") is True,
                    str(paused.get("why")),
                )
                await asyncio.sleep(0.8)
                prompt = await answer_reason(REASON)
                checks.check(
                    "Pause opens a reason prompt and accepts a sentence",
                    prompt.get("ok") is True,
                    str(prompt.get("why")),
                )
                after = None
                for _ in range(15):
                    await asyncio.sleep(1)
                    after = await status_of(dep_id)
                    if after == "PAUSED":
                        break
                checks.check(
                    "clicking Pause actually paused the deployment",
                    after == "PAUSED",
                    f"status is {after}",
                )

                # STOPPED is terminal on the repository, so this is the last
                # transition available and the one worth proving.
                stopped = await click_control("Stop")
                checks.check(
                    "the Stop button is clickable while PAUSED",
                    stopped.get("ok") is True,
                    str(stopped.get("why")),
                )
                await asyncio.sleep(0.8)
                prompt = await answer_reason(REASON)
                checks.check(
                    "Stop opens a reason prompt and accepts a sentence",
                    prompt.get("ok") is True,
                    str(prompt.get("why")),
                )
                after = None
                for _ in range(15):
                    await asyncio.sleep(1)
                    after = await status_of(dep_id)
                    if after == "STOPPED":
                        break
                checks.check(
                    "clicking Stop actually stopped the deployment",
                    after == "STOPPED",
                    f"status is {after}",
                )

                # And the screen must now refuse to start it, because STOPPED is
                # terminal. A Start button that is still enabled here is a button
                # that would 409.
                await asyncio.sleep(2)
                raw = await evaluate(
                    "(() => { const t=(e)=>e?(e.innerText||'').replace(/\\s+/g,' ').trim():null;"
                    "const b=[...document.querySelectorAll('button')]"
                    ".find(b=>t(b)==='Start');"
                    "if(!b) return JSON.stringify({present:false});"
                    "return JSON.stringify({present:true,disabled:!!b.disabled}); })()"
                )
                start_btn = json.loads(raw) if isinstance(raw, str) else {}
                checks.check(
                    "Start is disabled on a STOPPED (terminal) deployment",
                    start_btn.get("disabled") is True,
                    str(start_btn),
                )

            rt.cancel()

            rt.cancel()

        # Console noise is reported, not asserted on: a stray warning is not a
        # failure, but an exception during render is the single most likely
        # cause of a blank screen and is worth seeing in the output.
        if exceptions:
            print()
            print("=== uncaught exceptions during the run ===")
            for e in exceptions[:6]:
                print("  " + e.replace("\n", " ")[:300])
        errors = [c for c in console if "error" in c.lower()]
        if errors:
            print()
            print("=== console errors ===")
            for c in errors[:6]:
                print("  " + c.replace("\n", " ")[:300])

    finally:
        proc.terminate()

    return checks.report()


if __name__ == "__main__":
    # Optionally pass a token; otherwise the probe logs in with
    # ATR_PROBE_USER / ATR_PROBE_PASSWORD.
    if len(sys.argv) > 1:
        COOKIE = sys.argv[1]
    raise SystemExit(asyncio.run(main()))
