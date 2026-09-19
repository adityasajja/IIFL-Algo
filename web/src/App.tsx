import {
  AnimatedSidebar,
  AnimatedSidebarContent,
  AnimatedSidebarFooter,
  AnimatedSidebarGroup,
  AnimatedSidebarGroupContent,
  AnimatedSidebarHeader,
  AnimatedSidebarInset,
  AnimatedSidebarMenu,
  AnimatedSidebarMenuButton,
  AnimatedSidebarMenuItem,
  AnimatedSidebarProvider,
  AnimatedSidebarTrigger,
} from "./components/motion/animated-sidebar";
import {
  BarChart3,
  Bell,
  Ellipsis,
  Briefcase,
  FlaskConical,
  Home,
  ListChecks,
  LogIn,
  LogOut,
  Palette,
  PanelLeft,
  Radio,
  Search,
  Server,
  ShieldAlert,
  SlidersHorizontal,
} from "lucide-react";
import { useCallback, lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
const AlertsPanel = lazy(() => import("./AlertsPanel"));
import { PageLoader } from "./components/ui/loading";
import AuthGate from "./AuthGate";
const BacktestPanel = lazy(() => import("./BacktestPanel"));
const BacktestWorkflowPanel = lazy(() => import("./BacktestWorkflowPanel"));
const ChartsPanel = lazy(() => import("./ChartsPanel"));
const CustomScannerPanel = lazy(() => import("./CustomScannerPanel"));
const ExecutionModePanel = lazy(() => import("./ExecutionModePanel"));
const TradeSignalsPanel = lazy(() => import("./TradeSignalsPanel"));
const WatchlistPanel = lazy(() => import("./WatchlistPanel"));
import { Button } from "./components/ui/button";
import { CommandPalette, type CommandItem } from "./components/ui/command-palette";
import { GlobalTickerBar } from "./components/ui/global-ticker-bar";
import { EnvironmentBanner } from "./components/ui/environment-banner";
import { ThemeToggle } from "./components/ui/theme-toggle";
import LoginBanner from "./LoginBanner";
const PortfolioPanel = lazy(() => import("./PortfolioPanel"));
const PortfolioControlCenter = lazy(() => import("./PortfolioControlCenter"));
const BriefingPanel = lazy(() => import("./BriefingPanel"));
const TodayPanel = lazy(() => import("./TodayPanel"));
const AnalyticsPanel = lazy(() => import("./AnalyticsPanel"));
const LearningPanel = lazy(() => import("./LearningPanel").then((m) => ({ default: m.LearningPanel })));
const OptimizationPanel = lazy(() => import("./OptimizationPanel").then((m) => ({ default: m.OptimizationPanel })));
import OverviewPanel from "./OverviewPanel";
const PaperDeploymentPanel = lazy(() => import("./PaperDeploymentPanel"));
const ResearchPanel = lazy(() => import("./ResearchPanel"));
const EvidencePanel = lazy(() => import("./EvidencePanel"));
const RiskPanel = lazy(() => import("./RiskPanel"));
const StrategiesPanel = lazy(() => import("./StrategiesPanel"));
const ScannerPanel = lazy(() => import("./ScannerPanel"));
const ScreenerPanel = lazy(() => import("./ScreenerPanel"));
const MarketIntelligencePanel = lazy(() => import("./MarketIntelligencePanel"));
const SignalExplorerPanel = lazy(() => import("./SignalExplorerPanel"));
const SystemPanel = lazy(() => import("./SystemPanel"));
import {
  appLogout,
  getHealth,
  getLoginStatus,
  type Health,
  type LoginStatus,
} from "./api";
import { useSession } from "./lib/useSession";
import {
  IconBell,
  IconBrief,
  IconAttribution,
  IconBrain,
  IconFlask,
  IconHome,
  IconLayers,
  IconRadio,
  IconScan,
  IconServer,
  IconShield,
} from "./icons";
import { cn } from "./lib/utils";

export type Tab =
  | "dashboard"
  | "watchlist"
  | "markets"
  | "strategies"
  | "signals"
  | "trading"
  | "paper"
  | "evidence"
  | "learning"
  | "analytics"
  | "optimization"
  | "risk"
  | "system";

export type MarketsSub = "intelligence" | "scanner" | "custom" | "screener" | "charts";
export type SignalsSub = "today" | "brief" | "alerts" | "queue" | "context";
export type EvidenceSub = "backtest" | "workflow" | "research" | "measured" | "findings";
export type TradingSub = "portfolio" | "control-center" | "mode";

/**
 * Navigation follows the trader's loop, not the codebase's module list:
 * see the world → decide → act → track → learn → improve → maintain.
 *
 * Dashboard    = where am I
 * Markets      = what is happening
 * Strategies   = what I would do about it, and whether it works
 * Signals      = what it is telling me right now
 * Trading      = what I have done
 * Paper        = what the system is doing on its own, with my money as paper
 * Evidence     = proof, out of sample
 * Learning     = what the trade history says
 * Optimization = controlled parameter adaptation with user approval
 * Risk         = how much can go wrong
 * System       = is the machinery healthy
 *
 * Paper sits beside Trading under "Act" rather than inside it, because it is a
 * different activity: Trading is you placing orders, Paper is a deployed
 * strategy placing them itself. The two have separate books — a paper
 * deployment has its own capital allocation and its own P&L — so nesting one
 * under the other would present one account as the other.
 */
type NavItem = { id: Tab; name: string; icon: (p: { size?: number }) => ReactNode };

/**
 * Five pages carry the daily loop: how am I, what should I look at, what is the market
 * doing, what am I following, what do I hold. Everything else is the machinery behind
 * that (building and proving strategies, running them on paper, reviewing, limits,
 * plumbing) and lives under "More" so the rail is not thirteen items long. The command
 * palette still reaches every page.
 */
const PRIMARY: NavItem[] = [
  { id: "dashboard", name: "Dashboard", icon: IconHome },
  { id: "signals", name: "Signals", icon: IconBell },
  { id: "markets", name: "Markets", icon: IconScan },
  { id: "watchlist", name: "Watchlist", icon: ListChecks },
  { id: "trading", name: "Trading", icon: IconBrief },
];

const MORE: NavItem[] = [
  { id: "strategies", name: "Strategies", icon: IconLayers },
  { id: "paper", name: "Paper", icon: IconRadio },
  { id: "evidence", name: "Evidence", icon: IconFlask },
  { id: "learning", name: "Learning", icon: IconBrain },
  { id: "analytics", name: "Attribution", icon: IconAttribution },
  { id: "optimization", name: "Optimization", icon: SlidersHorizontal },
  { id: "risk", name: "Risk", icon: IconShield },
  { id: "system", name: "System", icon: IconServer },
];

const TITLES: Record<Tab, { title: string; sub: string }> = {
  dashboard: { title: "Dashboard", sub: "Where you stand right now" },
  watchlist: { title: "Watchlist", sub: "Your own symbols, your own columns" },
  markets: { title: "Markets", sub: "How the market is doing, plus scans and charts" },
  strategies: { title: "Strategies", sub: "What the system would do, and whether it has earned trust" },
  signals: { title: "Signals", sub: "What the market is doing and what is worth your attention" },
  trading: { title: "Trading", sub: "Positions, holdings, margin, order book — and what mode you are in" },
  paper: { title: "Paper", sub: "Deploy a strategy version, watch it trade, and stop it" },
  evidence: { title: "Evidence", sub: "Proof on prices the strategy has never seen" },
  learning: { title: "Learning", sub: "What the trade history says — findings only, never changes" },
  analytics: { title: "Attribution", sub: "What happened to each closed trade, why, and how well it was executed" },
  optimization: { title: "Optimization", sub: "Controlled adaptation, walk-forward OOS gating & robustness plateaus" },
  risk: { title: "Risk", sub: "Live limits, exposure and the kill switch" },
  system: { title: "System", sub: "History cache, contract files, broker session" },
};

/** Avatar tint per role, so authority is legible at a glance in the sidebar. */
const ROLE_TONE: Record<string, string> = {
  owner: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  admin: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  trader: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  researcher: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  viewer: "bg-muted text-muted-foreground",
};

const VALID_TABS = new Set<Tab>([
  "dashboard",
  "watchlist",
  "markets",
  "strategies",
  "signals",
  "trading",
  "paper",
  "evidence",
  "learning",
  "analytics",
  "optimization",
  "risk",
  "system",
]);

/** Old tab ids → their new home. Keeps saved links and bookmarks working. */
const LEGACY_TABS: Record<string, Tab> = {
  overview: "dashboard",
  watchlists: "watchlist",
  scanner: "markets",
  charts: "markets",
  alerts: "signals",
  signals: "signals",
  briefing: "signals",
  portfolio: "trading",
  research: "evidence",
  backtest: "evidence",
  // Anything that used to mean "a strategy running by itself" now belongs to
  // Paper rather than to Trading, which is a different book.
  deployments: "paper",
  deployment: "paper",
  live: "paper",
};

/**
 * Deep links look like `#markets/charts` or `#signals/queue`. A bare `#charts`
 * still arrives from the ticker bar and scanners, so legacy ids are mapped to
 * the right tab *and* the right sub-tab — otherwise "open chart" lands on the
 * Markets tab showing the scanner, which is not what the user clicked.
 */
const LEGACY_SUB: Partial<Record<string, string>> = {
  scanner: "scanner",
  charts: "charts",
  alerts: "alerts",
  signals: "queue",
  briefing: "brief",
  research: "research",
  backtest: "backtest",
};

function parseHash(raw: string): { tab: Tab; sub?: string } | null {
  const [head, sub] = raw.replace(/^#\/?/, "").toLowerCase().split("/");
  const tab = LEGACY_TABS[head] ?? (head as Tab);
  if (!VALID_TABS.has(tab)) return null;
  return { tab, sub: sub || LEGACY_SUB[head] };
}

function getInitialRoute(): { tab: Tab; sub?: string } {
  if (typeof window !== "undefined") {
    const fromHash = parseHash(window.location.hash);
    if (fromHash) return fromHash;
    const fromStore = parseHash(localStorage.getItem("atr.tab") ?? "");
    if (fromStore) return fromStore;
  }
  return { tab: "dashboard" };
}

// ─── Markets: scanner + charts under one roof ─────────────────────────────────
function MarketsTabContainer({
  sub,
  onSubChange,
  onOpenChart,
  theme,
}: {
  sub: MarketsSub;
  onSubChange: (s: MarketsSub) => void;
  onOpenChart: (symbol: string) => void;
  theme: "dark" | "light";
}) {
  return (
    <div className="space-y-4">
      <SubTabs
        value={sub}
        onChange={onSubChange}
        options={[
          { id: "intelligence" as MarketsSub, label: "Intelligence" },
          { id: "scanner" as MarketsSub, label: "Momentum scan" },
          { id: "custom" as MarketsSub, label: "Custom scan" },
          { id: "screener" as MarketsSub, label: "Screener" },
          { id: "charts" as MarketsSub, label: "Charts" },
        ]}
      />
      {sub === "intelligence" && <MarketIntelligencePanel />}
      {sub === "scanner" && <ScannerPanel onOpenChart={onOpenChart} />}
      {sub === "custom" && <CustomScannerPanel onOpenChart={onOpenChart} />}
      {sub === "screener" && <ScreenerPanel onOpenChart={onOpenChart} />}
      {sub === "charts" && <ChartsPanel theme={theme} />}
    </div>
  );
}

// ─── Signals: what the system is telling me right now ─────────────────────────
function SignalsTabContainer({
  sub,
  onSubChange,
  onOpenChart,
}: {
  sub: SignalsSub;
  onSubChange: (s: SignalsSub) => void;
  onOpenChart: (symbol: string) => void;
}) {
  return (
    <div className="space-y-4">
      <SubTabs
        value={sub}
        onChange={onSubChange}
        options={[
          { id: "today" as SignalsSub, label: "Today" },
          { id: "queue" as SignalsSub, label: "Trade queue" },
          { id: "alerts" as SignalsSub, label: "Alerts" },
          { id: "brief" as SignalsSub, label: "Morning brief" },
          { id: "context" as SignalsSub, label: "Signal context" },
        ]}
      />
      {sub === "today" && <TodayPanel onOpenChart={onOpenChart} />}
      {sub === "brief" && <BriefingPanel />}
      {sub === "alerts" && <AlertsPanel onOpenChart={onOpenChart} />}
      {sub === "queue" && <TradeSignalsPanel onOpenChart={onOpenChart} />}
      {sub === "context" && <SignalExplorerPanel />}
    </div>
  );
}

// ─── Trading: what I have done, and what mode I am in ─────────────────────────
function TradingTabContainer({
  sub,
  onSubChange,
}: {
  sub: TradingSub;
  onSubChange: (s: TradingSub) => void;
}) {
  return (
    <div className="space-y-4">
      <SubTabs
        value={sub}
        onChange={onSubChange}
        options={[
          { id: "control-center" as TradingSub, label: "Portfolio Control Center" },
          { id: "portfolio" as TradingSub, label: "Positions & orders" },
          { id: "mode" as TradingSub, label: "Execution mode" },
        ]}
      />
      {sub === "control-center" && <PortfolioControlCenter />}
      {sub === "portfolio" && <PortfolioPanel />}
      {sub === "mode" && <ExecutionModePanel />}
    </div>
  );
}

// ─── Evidence: proof on prices the strategy has never seen ────────────────────
function EvidenceTabContainer({
  sub,
  onSubChange,
}: {
  sub: EvidenceSub;
  onSubChange: (s: EvidenceSub) => void;
}) {
  const [researchTab, setResearchTab] = useState<"harness" | "measured">("harness");
  return (
    <div className="space-y-4">
      <SubTabs
        value={sub}
        onChange={onSubChange}
        options={[
          { id: "backtest" as EvidenceSub, label: "Backtest" },
          { id: "research" as EvidenceSub, label: "Walk-forward" },
          { id: "measured" as EvidenceSub, label: "Results" },
          { id: "findings" as EvidenceSub, label: "Findings" },
          { id: "workflow" as EvidenceSub, label: "Older backtest" },
        ]}
      />
      {sub === "backtest" && <BacktestWorkflowPanel />}
      {sub === "research" && <ResearchPanel forcedTab={researchTab} onTabChange={setResearchTab} />}
      {sub === "measured" && <ResearchPanel forcedTab="measured" onTabChange={setResearchTab} />}
      {sub === "findings" && <EvidencePanel />}
      {sub === "workflow" && <BacktestPanel />}
    </div>
  );
}

function SubTabs<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { id: T; label: string }[];
}) {
  return (
    <div className="flex w-fit overflow-hidden rounded-lg border border-border/60 text-sm font-semibold">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          onClick={() => onChange(o.id)}
          className={cn(
            "px-4 py-1.5 transition-colors",
            value === o.id
              ? "bg-primary text-primary-foreground"
              : "bg-background text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export default function App() {
  const initial = getInitialRoute();
  const [tab, setTabState] = useState<Tab>(initial.tab);
  const [marketsSub, setMarketsSub] = useState<MarketsSub>(
    (initial.sub as MarketsSub) ?? "intelligence",
  );
  const [signalsSub, setSignalsSub] = useState<SignalsSub>(
    (initial.sub as SignalsSub) ?? "today",
  );
  const [evidenceSub, setEvidenceSub] = useState<EvidenceSub>(
    (initial.sub as EvidenceSub) ?? "backtest",
  );
  const [tradingSub, setTradingSub] = useState<TradingSub>(
    (initial.sub as TradingSub) ?? "portfolio",
  );

  // Who is using the dashboard, and what they may do. This is the *platform*
  // account; the IIFL broker session below is a separate credential.
  const {
    state: sessionState,
    principal,
    error: sessionError,
    refresh: refreshSession,
    adopt: adoptSession,
    clear: clearSession,
  } = useSession();

  const signOut = useCallback(async () => {
    try {
      await appLogout();
    } catch {
      // A failed logout still clears the local view: the session may already be
      // revoked server-side, and leaving the UI looking signed in would be worse.
    }
    clearSession();
  }, [clearSession]);

  const setTab = useCallback((nextTab: Tab, sub?: string) => {
    setTabState(nextTab);
    if (typeof window !== "undefined") {
      localStorage.setItem("atr.tab", sub ? `${nextTab}/${sub}` : nextTab);
      window.location.hash = sub ? `${nextTab}/${sub}` : nextTab;
    }
  }, []);

  /** Open a symbol's chart from anywhere (scanner, ticker bar, alerts). */
  const openChart = useCallback(
    (symbol: string) => {
      if (symbol) {
        try {
          sessionStorage.setItem("atr.chartSymbol", symbol);
        } catch {
          /* ignore */
        }
      }
      setMarketsSub("charts");
      setTab("markets", "charts");
    },
    [setTab],
  );

  useEffect(() => {
    const handleHashChange = () => {
      const route = parseHash(window.location.hash);
      if (!route) return;
      setTabState(route.tab);
      localStorage.setItem("atr.tab", route.sub ? `${route.tab}/${route.sub}` : route.tab);
      if (route.tab === "markets" && route.sub) setMarketsSub(route.sub as MarketsSub);
      if (route.tab === "signals" && route.sub) setSignalsSub(route.sub as SignalsSub);
      if (route.tab === "evidence" && route.sub) setEvidenceSub(route.sub as EvidenceSub);
      if (route.tab === "trading" && route.sub) setTradingSub(route.sub as TradingSub);
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const [theme, setTheme] = useState<"dark" | "light">(
    () => (localStorage.getItem("atr.theme") as "dark" | "light") ?? "dark",
  );
  const [health, setHealth] = useState<Health | null>(null);
  const [auth, setAuth] = useState<LoginStatus | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await getHealth());
    } catch {
      setHealth(null);
    }
  }, []);

  // A transient failure must NOT blank the session state. The login modal reads
  // `login_url` off this object, so nulling it here left the user staring at a
  // disabled "Log in with IIFL" button — at exactly the moment (backend down or
  // restarting) they most needed it to work. Keep the last known answer; only
  // "unknown" if we never got one.
  const refreshAuth = useCallback(async () => {
    try {
      setAuth(await getLoginStatus());
    } catch {
      setAuth((prev) => prev);
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("atr.theme", theme);
  }, [theme]);

  useEffect(() => {
    void refreshHealth();
    void refreshAuth();
    const t = setInterval(() => {
      void refreshHealth();
      void refreshAuth();
    }, 15000);
    return () => clearInterval(t);
  }, [refreshHealth, refreshAuth]);

  const meta = TITLES[tab];
  const dbUp = health?.database === true;
  const accountInitials = (principal?.display_name || principal?.username || "?").slice(0, 2);
  const [showLogin, setShowLogin] = useState(false);
  const [moreOpen, setMoreOpen] = useState(() => {
    try {
      return localStorage.getItem("atr.sidebar.more") === "1";
    } catch {
      return false;
    }
  });
  const moreVisible = moreOpen || MORE.some((i) => i.id === tab);
  const toggleMore = () => {
    const next = !moreVisible;
    setMoreOpen(next);
    try {
      localStorage.setItem("atr.sidebar.more", next ? "1" : "0");
    } catch {
      // remembering the choice is a convenience
    }
  };
  const prompted = useRef(false);

  useEffect(() => {
    if (!auth) return;
    if (auth.session_active) {
      setShowLogin(false);
      prompted.current = false;
    } else if (!prompted.current) {
      prompted.current = true;
      // Ask once per browser session; after that the Log in button up top is enough.
      let seen = false;
      try {
        seen = sessionStorage.getItem("atr.login.prompted") === "1";
        sessionStorage.setItem("atr.login.prompted", "1");
      } catch {
        // storage can be blocked; then it simply asks again
      }
      if (!seen) setShowLogin(true);
    }
  }, [auth]);

  const paletteItems: CommandItem[] = useMemo(
    () => [
      { id: "go-dashboard", label: "Go to Dashboard", group: "Navigate", icon: Home, hint: "1", keywords: ["overview", "home", "status"], onSelect: () => setTab("dashboard") },
      { id: "go-watchlist", label: "Go to Watchlist", group: "Navigate", icon: ListChecks, hint: "2", keywords: ["watchlist", "my symbols", "columns", "favourites", "track"], onSelect: () => setTab("watchlist") },
      { id: "go-markets", label: "Go to Markets", group: "Navigate", icon: Search, hint: "3", keywords: ["scanner", "momentum", "charts", "scan"], onSelect: () => setTab("markets") },
      { id: "go-strategies", label: "Go to Strategies", group: "Navigate", icon: BarChart3, hint: "4", keywords: ["registry", "validated", "paper", "models"], onSelect: () => setTab("strategies") },
      { id: "go-signals", label: "Go to Signals", group: "Navigate", icon: Bell, hint: "5", keywords: ["alerts", "buy", "sell", "rules", "notify", "briefing", "morning"], onSelect: () => setTab("signals") },
      { id: "go-trading", label: "Go to Trading", group: "Navigate", icon: Briefcase, hint: "6", keywords: ["portfolio", "holdings", "positions", "limits", "order book", "trade book"], onSelect: () => setTab("trading") },
      { id: "go-paper", label: "Go to Paper", group: "Navigate", icon: Radio, hint: "7", keywords: ["deploy", "deployment", "paper trading", "simulate", "monitor", "strategy running", "pause", "stop", "reset", "capital"], onSelect: () => setTab("paper") },
      { id: "go-evidence", label: "Go to Evidence (walk-forward)", group: "Navigate", icon: FlaskConical, hint: "8", keywords: ["research", "validate", "out of sample", "deflated sharpe", "backtest", "measured"], onSelect: () => setTab("evidence") },
      { id: "go-optimization", label: "Go to Strategy Optimization", group: "Navigate", icon: SlidersHorizontal, hint: "opt", keywords: ["optimization", "adaptive", "parameters", "walk-forward", "robustness", "recommendation"], onSelect: () => setTab("optimization") },
      { id: "go-risk", label: "Go to Risk", group: "Navigate", icon: ShieldAlert, hint: "9", keywords: ["kill switch", "limits", "exposure", "halt", "stop"], onSelect: () => setTab("risk") },
      { id: "go-system", label: "Go to System", group: "Navigate", icon: Server, hint: "0", keywords: ["cache", "contracts", "session", "health", "history"], onSelect: () => setTab("system") },
      { id: "toggle-theme", label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode", group: "View", icon: Palette, keywords: ["appearance"], onSelect: () => setTheme((t) => (t === "dark" ? "light" : "dark")) },
      { id: "login", label: "Log in with IIFL", group: "Session", icon: LogIn, keywords: ["auth", "session", "broker"], onSelect: () => setShowLogin(true) },
      { id: "sign-out", label: "Sign out of ATR", group: "Session", icon: LogOut, keywords: ["logout", "account", "leave", "end session"], onSelect: () => void signOut() },
    ],
    [theme, setTab, signOut],
  );

  // ── the gate ───────────────────────────────────────────────────────────────
  // Placed after every hook so the hook order is stable across renders, and
  // before the shell so an unauthenticated visitor never sees a dashboard frame
  // they cannot populate.
  //
  // `offline` is passed through rather than folded into `anonymous`: a backend
  // that is down must not be presented as a rejected login, or the user retypes a
  // correct password and is told it is wrong.
  if (sessionState === "loading") {
    return (
      <div className="grid min-h-screen place-items-center bg-background text-muted-foreground">
        <div className="grid gap-2 text-center">
          <div className="mx-auto grid size-11 place-items-center rounded-xl bg-gradient-to-br from-primary to-violet-500 text-lg font-extrabold text-white">
            A
          </div>
          <span className="text-xs">Checking your session…</span>
        </div>
      </div>
    );
  }
  if (sessionState === "anonymous") {
    return <AuthGate onAuthenticated={adoptSession} />;
  }
  if (sessionState === "offline") {
    return (
      <AuthGate
        offline={{ error: sessionError, onRetry: () => void refreshSession() }}
        onAuthenticated={adoptSession}
      />
    );
  }

  return (
    <AnimatedSidebarProvider>
      <AnimatedSidebar ariaLabel="ATR navigation" collapsible="icon">
        <AnimatedSidebarHeader className="p-3 pb-2">
          <div className="flex min-h-11 items-center gap-3 overflow-hidden px-2">
            <div className="grid size-7 shrink-0 place-items-center rounded-[9px] bg-gradient-to-br from-primary to-violet-500 text-sm font-extrabold text-white shadow-sm">
              A
            </div>
            <span className="truncate text-[15px] font-semibold tracking-tight text-foreground group-data-[state=collapsed]/sidebar:hidden">
              ATR
            </span>
          </div>
        </AnimatedSidebarHeader>

        <AnimatedSidebarContent className="px-2 pt-1">
          <AnimatedSidebarGroup className="pb-2">
            <AnimatedSidebarGroupContent>
              <AnimatedSidebarMenu>
                {[...PRIMARY].map((it) => {
                  const Icon = it.icon;
                  return (
                    <AnimatedSidebarMenuItem key={it.id}>
                      <AnimatedSidebarMenuButton
                        isActive={tab === it.id}
                        icon={<Icon size={16} />}
                        onSelect={() => setTab(it.id)}
                      >
                        {it.name}
                      </AnimatedSidebarMenuButton>
                    </AnimatedSidebarMenuItem>
                  );
                })}
                <AnimatedSidebarMenuItem>
                  <AnimatedSidebarMenuButton icon={<Ellipsis size={16} />} onSelect={toggleMore}>
                    {moreVisible ? "Less" : "More"}
                  </AnimatedSidebarMenuButton>
                </AnimatedSidebarMenuItem>
                {moreVisible &&
                  MORE.map((it) => {
                    const Icon = it.icon;
                    return (
                      <AnimatedSidebarMenuItem key={it.id}>
                        <AnimatedSidebarMenuButton
                          isActive={tab === it.id}
                          icon={<Icon size={16} />}
                          onSelect={() => setTab(it.id)}
                        >
                          {it.name}
                        </AnimatedSidebarMenuButton>
                      </AnimatedSidebarMenuItem>
                    );
                  })}
              </AnimatedSidebarMenu>
            </AnimatedSidebarGroupContent>
          </AnimatedSidebarGroup>
        </AnimatedSidebarContent>

        <AnimatedSidebarFooter className="gap-3 border-none p-3">
          {/* Who is signed in. Distinct from the broker session below it: this is
              the platform account (what you may change), that is the IIFL session
              (what you may trade). Both matter, neither implies the other. */}
          <div className="flex min-h-11 w-full items-center gap-3 overflow-hidden rounded-xl p-1">
            <span
              title={`${principal?.username ?? "?"} · ${principal?.role ?? ""}`}
              className={cn(
                "grid size-9 shrink-0 place-items-center rounded-full text-[11px] font-bold uppercase",
                ROLE_TONE[principal?.role ?? ""] ?? "bg-muted text-muted-foreground",
              )}
            >
              {accountInitials}
            </span>
            <span className="min-w-0 flex-1 group-data-[state=collapsed]/sidebar:hidden">
              <span className="block truncate text-sm font-medium text-foreground">
                {principal?.display_name || principal?.username}
              </span>
              <span className="block truncate text-xs text-muted-foreground">
                {principal?.auth_method === "anonymous" ? "auth disabled" : principal?.role}
              </span>
            </span>
            <button
              type="button"
              onClick={() => void signOut()}
              title="Sign out of ATR"
              aria-label="Sign out of ATR"
              className="grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive group-data-[state=collapsed]/sidebar:hidden"
            >
              <LogOut aria-hidden="true" className="size-4" />
            </button>
          </div>

          <div className="flex min-h-11 w-full items-center gap-3 overflow-hidden rounded-xl p-1">
            <span
              className={cn(
                "grid size-9 shrink-0 place-items-center rounded-full text-[10px] font-bold group-data-[state=collapsed]/sidebar:hidden",
                health?.session_active
                  ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {health?.session_active ? "ON" : "OFF"}
            </span>
            <span className="min-w-0 flex-1 group-data-[state=collapsed]/sidebar:hidden">
              <span className="block truncate text-sm font-medium text-foreground">
                {health?.session_active ? "Session live" : "No session"}
              </span>
              <span className="block truncate text-xs text-muted-foreground">
                {health ? health.env : "offline"}
              </span>
            </span>
            <AnimatedSidebarTrigger
              title="Collapse sidebar (Ctrl+B)"
              aria-label="Collapse sidebar"
              className="grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              <PanelLeft aria-hidden="true" className="size-4" />
            </AnimatedSidebarTrigger>
          </div>
        </AnimatedSidebarFooter>
      </AnimatedSidebar>

      <AnimatedSidebarInset>
        {/* Environment banner sits above everything, including the ticker bar —
            the whole point is that it cannot be missed or scrolled past. */}
        <EnvironmentBanner
          env={health?.env}
          executionMode={health?.execution_mode}
          killSwitch={health?.kill_switch}
        />
        <GlobalTickerBar onSelectSymbol={openChart} />
        <main
          className={cn(
            "min-w-0 pb-16",
            tab === "markets" && marketsSub === "charts"
              ? "px-4 max-md:px-2 pb-4"
              : "px-8 max-md:px-4",
          )}
        >
        <div className="mb-6 flex h-14 items-center justify-between gap-4 border-b border-border/60">
          <div className="flex min-w-0 items-center gap-3">
            <div className="min-w-0">
              <div className="truncate text-base font-semibold tracking-tight text-foreground">{meta.title}</div>
              <div className="truncate text-xs text-muted-foreground">{meta.sub}</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {/* Unified Stripe-style status badge */}
            <div
              title={`Environment: ${health?.env || "offline"} | DB: ${dbUp ? "Online" : "Offline"} | Session: ${health?.session_active ? "Active" : "None"}`}
              className="flex items-center gap-2 rounded-full border border-border/80 bg-card/60 px-3 py-1 text-xs font-medium text-foreground/90 transition-colors"
            >
              <span
                className={cn(
                  "h-2 w-2 rounded-full",
                  health?.session_active
                    ? "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
                    : health
                      ? "bg-amber-500"
                      : "bg-red-500"
                )}
              />
              <span className="hidden sm:inline text-muted-foreground text-[11.5px]">
                {health?.session_active ? "Live Session" : health ? `${health.env}` : "Offline"}
              </span>
            </div>

            {!health?.session_active && (
              <Button size="sm" variant="outline" onClick={() => setShowLogin(true)} className="h-7 text-xs px-2.5">
                Log in
              </Button>
            )}

            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              title="Command palette (Ctrl+K)"
              className="hidden items-center gap-1.5 rounded-full border border-border/80 bg-card/60 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground sm:inline-flex"
            >
              <Search className="h-3 w-3" />
              <kbd className="rounded border border-border/80 bg-muted/50 px-1 text-[10px]">⌘K</kbd>
            </button>

            <ThemeToggle
              isDark={theme === "dark"}
              onToggle={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              className="grid h-7 w-7 place-items-center rounded-full border border-border/80 bg-card/60 text-muted-foreground transition-colors hover:text-foreground"
              iconClassName="h-3.5 w-3.5"
            />
          </div>
        </div>

        <Suspense fallback={<PageLoader />}>
        {tab === "dashboard" && <OverviewPanel onNavigate={(t) => setTab(t as Tab)} />}
        {tab === "watchlist" && (
          <WatchlistPanel permissions={principal?.permissions ?? []} onOpenChart={openChart} />
        )}
        {tab === "markets" && (
          <MarketsTabContainer
            sub={marketsSub}
            onSubChange={(s) => setTab("markets", s)}
            onOpenChart={openChart}
            theme={theme}
          />
        )}
        {tab === "strategies" && <StrategiesPanel onOpenResearch={() => setTab("evidence", "measured")} />}
        {tab === "signals" && (
          <SignalsTabContainer
            sub={signalsSub}
            onSubChange={(s) => setTab("signals", s)}
            onOpenChart={openChart}
          />
        )}
        {tab === "trading" && (
          <TradingTabContainer sub={tradingSub} onSubChange={(s) => setTab("trading", s)} />
        )}
        {tab === "paper" && <PaperDeploymentPanel onOpenStrategies={() => setTab("strategies")} />}
        {tab === "evidence" && (
          <EvidenceTabContainer sub={evidenceSub} onSubChange={(s) => setTab("evidence", s)} />
        )}
        {tab === "learning" && <LearningPanel />}
        {tab === "analytics" && <AnalyticsPanel />}
        {tab === "optimization" && <OptimizationPanel />}
        {tab === "risk" && <RiskPanel />}
        {tab === "system" && <SystemPanel />}
        </Suspense>
        </main>
      </AnimatedSidebarInset>

      <CommandPalette items={paletteItems} open={paletteOpen} onOpenChange={setPaletteOpen} />

      {showLogin && (
        <LoginBanner
          status={auth}
          onLoggedIn={() => {
            void refreshHealth();
            void refreshAuth();
          }}
          onClose={() => setShowLogin(false)}
        />
      )}
    </AnimatedSidebarProvider>
  );
}
