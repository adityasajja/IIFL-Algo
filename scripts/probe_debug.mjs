import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const BROWSER = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9335;
const OUT = "D:\\ALGO\\data\\shots";
const URL = "http://127.0.0.1:8123/";
mkdirSync(OUT, { recursive: true });

const proc = spawn(BROWSER, [
  "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
  "--no-first-run", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${OUT}\\profile2`, "--window-size=1500,1200", "about:blank",
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
const logs = [];

ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") {
    logs.push("EXC: " + (m.params.exceptionDetails?.exception?.description ?? "?"));
  }
  if (m.method === "Runtime.consoleAPICalled") {
    logs.push(m.params.type + ": " + m.params.args.map(a => a.value ?? a.description).join(" "));
  }
  if (m.method === "Log.entryAdded") logs.push("LOG[" + m.params.entry.level + "]: " + m.params.entry.text);
};

const send = (method, params = {}) => new Promise((res, rej) => {
  const myId = ++id;
  pending.set(myId, m => (m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)));
  ws.send(JSON.stringify({ id: myId, method, params }));
});

const evaluate = async expr =>
  (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }))
    ?.result?.value;

await send("Runtime.enable");
await send("Page.enable");
await send("Log.enable");
await send("Page.navigate", { url: URL });
await sleep(12000);

console.log("URL:", await evaluate("location.href"));
console.log("body length:", await evaluate("(document.body.innerText||'').length"));
console.log("body text:", JSON.stringify(await evaluate("document.body.innerText")));
console.log("root html len:", await evaluate("(document.getElementById('root')?.innerHTML||'').length"));
console.log("root html head:", (await evaluate("(document.getElementById('root')?.innerHTML||'').slice(0,600)")));
console.log("scripts:", await evaluate("[...document.querySelectorAll('script')].map(s=>s.src).join(' | ')"));

// Direct fetch from inside the page, exercising the relative-URL path.
console.log("fetch /evidence:", await evaluate(`
  fetch('/evidence').then(r => r.status + ' ' + r.headers.get('content-type'))
    .catch(e => 'ERR ' + e.message)
`));
console.log("fetch /health:", await evaluate(`
  fetch('/health').then(r => r.status).catch(e => 'ERR ' + e.message)
`));

const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
writeFileSync(`${OUT}\\debug.png`, Buffer.from(data, "base64"));

console.log("--- console ---");
for (const l of logs.slice(0, 25)) console.log("  " + String(l).slice(0, 250));

ws.close(); proc.kill(); process.exit(0);
