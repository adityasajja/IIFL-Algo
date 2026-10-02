// Browser smoke test: log in to a SEEDED demo server, open every page, and fail on any JS error or API
// error response. Also checks the home page's key promises. Run it with the server up:
//   python scripts/seed_demo.py && atr serve --no-browser     (empty data/ only)
//   bun run e2e                                               (BASE_URL, CHROME_PATH optional)
import { chromium } from "playwright-core";

const BASE = process.env.BASE_URL ?? "http://localhost:8000";
const ROUTES = [
  "dashboard", "strategies", "evidence/backtest", "evidence/research", "evidence/measured", "evidence/improve",
  "paper", "trading/portfolio", "trading/control-center", "learning/review", "learning/attribution",
  "markets/intelligence", "markets/scanner", "markets/screener", "markets/charts",
  "signals/today", "signals/queue", "signals/alerts", "signals/brief", "signals/context",
  "watchlist", "labs/custom-scan", "labs/alpha-hunt", "labs/episodic-pivot",
];

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  args: ["--no-sandbox"],
});
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage();
let current = "login";
const problems = [];
page.on("pageerror", (e) => problems.push(`${current}: JS error: ${e.message.slice(0, 160)}`));
page.on("response", (r) => {
  if (r.status() < 400 || !r.url().includes("/api")) return;
  if (current === "login" && r.status() === 401) return; // not signed in yet
  if (r.status() === 404 && r.url().includes("/reconciliation/runs/latest")) return; // "no run yet" is a 404 by design
  problems.push(`${current}: ${r.status()} ${r.url().replace(BASE, "")}`);
});

await page.goto(BASE + "/");
await page.locator("input").first().fill("demo");
await page.locator("input[type=password]").first().fill("DemoPassw0rd!");
await page.getByRole("button", { name: /sign in|log in|login/i }).first().click();
await page.waitForTimeout(2500);

for (const route of ROUTES) {
  current = route;
  await page.goto(`${BASE}/#${route}`);
  await page.reload();
  await page.waitForTimeout(1800);
}

current = "home";
await page.goto(`${BASE}/#dashboard`);
await page.reload();
await page.waitForTimeout(2000);
const text = await page.locator("body").innerText();
for (const must of [/DEMO DATA/i, /track record|paper trading/i]) {
  if (!must.test(text)) problems.push(`home: expected to find ${must}`);
}

await browser.close();
const unique = [...new Set(problems)];
console.log(unique.length ? `FAIL (${unique.length})\n${unique.join("\n")}` : `OK: ${ROUTES.length} pages, no errors`);
process.exit(unique.length ? 1 : 0);
