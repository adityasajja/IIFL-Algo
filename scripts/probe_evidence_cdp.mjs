import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const BROWSER = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9334;
const OUT = "D:\\ALGO\\data\\shots";
const URL = "http://127.0.0.1:8123/";
mkdirSync(OUT, { recursive: true });

const proc = spawn(BROWSER, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
  "--no-first-run", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${OUT}\\profile`, "--window-size=1500,1400", "about:blank",
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await fetch(`http://127.0.0.1:${PORT}/json/list`).then(r => r.json());
      const page = list.find(t => t.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(500);
  }
  throw new Error("devtools never came up");
}

const ws = new WebSocket(await target());
await new Promise(res => (ws.onopen = res));

let id = 0;
const pending = new Map();
const errors = [];
let ready = false;

// ONE reader loop. Everything dispatches from here, so no event is swallowed.
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
    return;
  }
  if (m.method === "Runtime.exceptionThrown") {
    errors.push(m.params.exceptionDetails?.exception?.description ?? "exception");
  }
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
    errors.push(m.params.args.map(a => a.value ?? a.description).join(" "));
  }
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
    errors.push("LOG: " + m.params.entry.text);
  }
  if (m.method === "Page.frameNavigated" && m.params.frame.url.startsWith("http")) {
    ready = true;
  }
};

const send = (method, params = {}) => new Promise((res, rej) => {
  const myId = ++id;
  pending.set(myId, m => (m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)));
  ws.send(JSON.stringify({ id: myId, method, params }));
});

const evaluate = async expr =>
  (await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  }))?.result?.value;

const shot = async name => {
  const { data } = await send("Page.captureScreenshot", {
    format: "png", captureBeyondViewport: true,
  });
  writeFileSync(`${OUT}\\${name}.png`, Buffer.from(data, "base64"));
};

await send("Runtime.enable");
await send("Page.enable");
await send("Log.enable");

await send("Page.navigate", { url: URL });

// Gate on real content, not a fixed sleep.
for (let i = 0; i < 40; i++) {
  await sleep(1000);
  const len = await evaluate("(document.body.innerText || '').length");
  if (len && len > 400) break;
}

console.log("initial text length:", await evaluate("(document.body.innerText||'').length"));

// Navigate to Evidence via the sidebar, then the "Findings & verdicts" sub-tab.
const nav = await evaluate(`(() => {
  const b = [...document.querySelectorAll('button')].find(
    x => x.textContent.trim().toLowerCase() === 'evidence');
  if (b) { b.click(); return 'clicked evidence'; }
  return 'evidence button NOT FOUND: ' + [...document.querySelectorAll('button')]
    .map(x => x.textContent.trim()).slice(0, 40).join(' | ');
})()`);
console.log("nav:", nav);
await sleep(4000);

const subClicked = await evaluate(`(() => {
  const b = [...document.querySelectorAll('button')].find(
    x => x.textContent.includes('Findings'));
  if (b) { b.click(); return 'clicked findings'; }
  return 'findings sub-tab NOT FOUND: ' + [...document.querySelectorAll('button')]
    .map(x => x.textContent.trim()).slice(0, 40).join(' | ');
})()`);
console.log("subtab:", subClicked);

for (let i = 0; i < 25; i++) {
  await sleep(1000);
  const body = await evaluate("document.body.innerText || ''");
  if (body.includes("Rejected") || body.includes("Credible")) break;
}

const body = await evaluate("document.body.innerText || ''");
console.log("--- has 'Rejected':", body.includes("Rejected"));
console.log("--- has 'selection gap':", body.includes("selection gap"));
console.log("--- has 'leverage, not edge':", body.includes("leverage, not edge"));
console.log("--- has '+480':", body.includes("480"));
const idx = body.indexOf("Evidence");
console.log("--- excerpt ---");
console.log(body.slice(idx, idx + 900));

await shot("evidence-findings");
console.log("console errors:", errors.length);
for (const e of errors.slice(0, 10)) console.log("  !", String(e).slice(0, 220));

ws.close();
proc.kill();
process.exit(0);
