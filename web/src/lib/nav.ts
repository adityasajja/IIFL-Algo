/**
 * The app's map: what the pages are, how they group, and how an old link finds its new home.
 * Pure data and functions so the routing can be tested without rendering anything.
 *
 * The product is paper trading and information only. It never places a real order. The story:
 *
 *   Home -> Strategies (build) -> Test (prove it) -> Paper (run it, practice money) -> Performance
 *
 * Markets, Signals and Watchlist are the world around that path. Labs holds the experimental
 * tools that are not part of the tested workflow.
 */

export type Tab =
  | "dashboard"
  | "strategies"
  | "evidence"
  | "paper"
  | "learning"
  | "markets"
  | "signals"
  | "watchlist"
  | "labs";

export type NavGroup = { label: string | null; tabs: Tab[] };

export const NAV_GROUPS: NavGroup[] = [
  { label: "Prove it on paper", tabs: ["dashboard", "strategies", "evidence", "paper", "learning"] },
  { label: "Market", tabs: ["markets", "signals", "watchlist"] },
  { label: "Experimental", tabs: ["labs"] },
];

/** `name` is the sidebar label; `title` the page heading; `blurb` one plain sentence under it. */
export const PAGES: Record<Tab, { name: string; title: string; blurb: string }> = {
  dashboard: { name: "Home", title: "Home", blurb: "What your strategies have actually done on paper, and what to do next." },
  strategies: { name: "Strategies", title: "Strategies", blurb: "Step 1. Build or pick a strategy: the rules for when to buy and sell." },
  evidence: { name: "Test", title: "Test", blurb: "Step 2. Does it hold up on prices it has never seen?" },
  paper: { name: "Paper", title: "Paper trading", blurb: "Step 3. Run it on live prices with practice money. No real order is ever sent." },
  learning: { name: "Performance", title: "Performance", blurb: "Step 4. What actually happened, what it cost, and where the results came from." },
  markets: { name: "Markets", title: "Markets", blurb: "What the market is doing: mood, sectors, leaders, scans and charts." },
  signals: { name: "Signals", title: "Signals", blurb: "What your strategies are telling you to do right now." },
  watchlist: { name: "Watchlist", title: "Watchlist", blurb: "The stocks you follow, with live prices." },
  labs: { name: "Labs", title: "Labs", blurb: "Experimental tools. They are not part of the tested path above, so treat their output as ideas, not evidence." },
};

export type SubPage = { id: string; label: string };

/** Pages that have sub-pages. The first is the default. */
export const SUBS: Partial<Record<Tab, SubPage[]>> = {
  markets: [
    { id: "intelligence", label: "Overview" },
    { id: "scanner", label: "Momentum scan" },
    { id: "screener", label: "Screener" },
    { id: "charts", label: "Charts" },
  ],
  signals: [
    { id: "today", label: "Today" },
    { id: "queue", label: "Trade ideas" },
    { id: "alerts", label: "Alerts" },
    { id: "brief", label: "Morning brief" },
    { id: "context", label: "Signal quality" },
  ],
  evidence: [
    { id: "backtest", label: "Backtest" },
    { id: "research", label: "Stress test" },
    { id: "measured", label: "Results" },
    { id: "improve", label: "Improve" },
  ],
  paper: [
    { id: "runs", label: "Runs" },
    { id: "risk", label: "Risk & limits" },
  ],
  learning: [
    { id: "review", label: "Trade review" },
    { id: "attribution", label: "Attribution" },
  ],
  labs: [
    { id: "custom-scan", label: "Custom scan" },
    { id: "alpha-hunt", label: "Alpha hunt" },
    { id: "episodic-pivot", label: "Episodic pivot" },
  ],
};

export function defaultSub(tab: Tab): string | undefined {
  return SUBS[tab]?.[0]?.id;
}

/** A sub-page id that exists on this tab, else the tab's default. */
export function normaliseSub(tab: Tab, sub?: string): string | undefined {
  const subs = SUBS[tab];
  if (!subs) return undefined;
  return subs.some((s) => s.id === sub) ? sub : subs[0].id;
}

export const VALID_TABS = new Set<Tab>(Object.keys(PAGES) as Tab[]);

/** Old page ids (from bookmarks, saved state, deep links) and where they live now. */
const LEGACY: Record<string, { tab: Tab; sub?: string }> = {
  overview: { tab: "dashboard" },
  watchlists: { tab: "watchlist" },
  scanner: { tab: "markets", sub: "scanner" },
  charts: { tab: "markets", sub: "charts" },
  alerts: { tab: "signals", sub: "alerts" },
  briefing: { tab: "signals", sub: "brief" },
  // Live trading is not part of the product. Its old links land on the paper pages that remain.
  trading: { tab: "paper", sub: "runs" },
  portfolio: { tab: "paper", sub: "runs" },
  risk: { tab: "paper", sub: "risk" },
  research: { tab: "evidence", sub: "research" },
  backtest: { tab: "evidence", sub: "backtest" },
  optimization: { tab: "evidence", sub: "improve" },
  analytics: { tab: "learning", sub: "attribution" },
  deployments: { tab: "paper" },
  deployment: { tab: "paper" },
  live: { tab: "paper" },
};

/** A sub-page that moved to another tab: `tab/sub` -> its new home. */
const MOVED_SUBS: Record<string, { tab: Tab; sub: string }> = {
  "markets/custom": { tab: "labs", sub: "custom-scan" },
  "trading/control-center": { tab: "paper", sub: "risk" },
  "trading/portfolio": { tab: "paper", sub: "runs" },
};

/**
 * `#markets/charts`, `#charts`, `#optimization` ... -> a tab and a valid sub-page.
 * `signals` was both a tab and an old alias for the trade queue; the tab wins, and a bare
 * `#signals` opens its default page.
 */
export function parseRoute(raw: string): { tab: Tab; sub?: string } | null {
  const [head, sub] = raw.replace(/^#\/?/, "").toLowerCase().split("/");
  if (!head) return null;
  const moved = MOVED_SUBS[`${head}/${sub}`];
  if (moved) return moved;
  if (VALID_TABS.has(head as Tab)) {
    const tab = head as Tab;
    return { tab, sub: normaliseSub(tab, sub) };
  }
  const legacy = LEGACY[head];
  if (!legacy) return null;
  return { tab: legacy.tab, sub: normaliseSub(legacy.tab, sub ?? legacy.sub) };
}
