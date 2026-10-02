/**
 * The app's map: what the pages are, how they group, and how an old link finds its new home.
 * Pure data and functions so the routing can be tested without rendering anything.
 *
 * The product tells one story, "prove a strategy, then trade it":
 *
 *   Home -> Strategies (build) -> Test (prove it) -> Paper (run it, no money) -> Live -> Performance
 *
 * Markets, Signals and Watchlist are the world around that path. Labs holds the experimental
 * tools that are not part of the tested workflow.
 */

export type Tab =
  | "dashboard"
  | "strategies"
  | "evidence"
  | "paper"
  | "trading"
  | "learning"
  | "markets"
  | "signals"
  | "watchlist"
  | "labs";

export type NavGroup = { label: string | null; tabs: Tab[] };

export const NAV_GROUPS: NavGroup[] = [
  { label: "Prove, then trade", tabs: ["dashboard", "strategies", "evidence", "paper", "trading", "learning"] },
  { label: "Market", tabs: ["markets", "signals", "watchlist"] },
  { label: "Experimental", tabs: ["labs"] },
];

/** `name` is the sidebar label; `title` the page heading; `blurb` a few words under it. */
export const PAGES: Record<Tab, { name: string; title: string; blurb: string }> = {
  dashboard: { name: "Home", title: "Home", blurb: "Where you stand, and what needs you." },
  strategies: { name: "Strategies", title: "Strategies", blurb: "Build or pick your rules." },
  evidence: { name: "Test", title: "Test", blurb: "Does it hold up on unseen prices?." },
  paper: { name: "Paper", title: "Paper trading", blurb: "Practice money, live prices." },
  trading: { name: "Live", title: "Live trading", blurb: "Broker, limits, kill switch." },
  learning: { name: "Performance", title: "Performance", blurb: "What happened, and why." },
  markets: { name: "Markets", title: "Markets", blurb: "Mood, sectors, leaders, charts." },
  signals: { name: "Signals", title: "Signals", blurb: "What to do right now." },
  watchlist: { name: "Watchlist", title: "Watchlist", blurb: "Stocks you follow." },
  labs: { name: "Labs", title: "Labs", blurb: "Ideas, not evidence." },
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
    { id: "queue", label: "Trade queue" },
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
  trading: [
    { id: "portfolio", label: "Broker account" },
    { id: "control-center", label: "Risk & limits" },
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
  portfolio: { tab: "trading", sub: "portfolio" },
  risk: { tab: "trading", sub: "control-center" },
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
