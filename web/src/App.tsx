import { BrandMark } from "./components/ui/brand-mark";
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
  Brain,
  Briefcase,
  ChartPie,
  Ellipsis,
  Eye,
  FlaskConical,
  Home,
  House,
  Layers,
  LogIn,
  LogOut,
  Palette,
  PanelLeft,
  Radio,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  TrendingUp,
} from "lucide-react";
import { useCallback, lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
const AlertsPanel = lazy(() => import("./AlertsPanel"));
import { PageLoader } from "./components/ui/loading";
import AuthGate from "./AuthGate";
const BacktestWorkflowPanel = lazy(() => import("./BacktestWorkflowPanel"));
const ChartsPanel = lazy(() => import("./ChartsPanel"));
const CustomScannerPanel = lazy(() => import("./CustomScannerPanel"));
const TradeSignalsPanel = lazy(() => import("./TradeSignalsPanel"));
const WatchlistPanel = lazy(() => import("./WatchlistPanel"));
import { Button } from "./components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { Tooltip } from "./components/motion/tooltip";
import { CommandPalette, type CommandItem } from "./components/ui/command-palette";
import { EnvironmentBanner } from "./components/ui/environment-banner";
import { ThemeToggle } from "./components/ui/theme-toggle";
import LoginBanner from "./LoginBanner";
import { ModeDetails, ModePill, useExecutionMode } from "./ModeSwitch";
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
const StrategiesPanel = lazy(() => import("./StrategiesPanel"));
const ScannerPanel = lazy(() => import("./ScannerPanel"));
const ScreenerPanel = lazy(() => import("./ScreenerPanel"));
import { SCREEN_PRESET_KEY } from "./MarketMood";
const MarketIntelligencePanel = lazy(() => import("./MarketIntelligencePanel"));
const SignalExplorerPanel = lazy(() => import("./SignalExplorerPanel"));
import {
  appLogout,
  getHealth,
  getLoginStatus,
  logoutSession,
  type Health,
  type LoginStatus,
} from "./api";
import { useSession } from "./lib/useSession";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";

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
  | "optimization";

export type MarketsSub = "intelligence" | "scanner" | "custom" | "screener" | "charts";
export type SignalsSub = "today" | "brief" | "alerts" | "queue" | "context";
export type EvidenceSub = "backtest" | "research" | "measured";
export type TradingSub = "portfolio" | "control-center";

/**
 * Navigation follows the trader's loop, not the codebase's module list:
 * see the world → decide → act → track → learn → improve → maintain.
 *
 * Dashboard = where am I
 * Markets = what is happening
 * Strategies = what I would do about it, and whether it works
 * Signals = what it is telling me right now
 * Trading = what I have done (positions, control center & risk policies, execution mode)
 * Paper = what the system is doing on its own, with my money as paper
 * Evidence = proof, out of sample
 * Learning = what the trade history says
 * Optimization = controlled parameter adaptation with user approval
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
  { id: "dashboard", name: "Dashboard", icon: House },
  { id: "signals", name: "Signals", icon: Bell },
  { id: "markets", name: "Markets", icon: TrendingUp },
  { id: "watchlist", name: "Watchlist", icon: Eye },
  { id: "trading", name: "Trading", icon: Briefcase },
];

const MORE: NavItem[] = [
  { id: "strategies", name: "Strategies", icon: Layers },
  { id: "paper", name: "Paper", icon: Radio },
  { id: "evidence", name: "Evidence", icon: FlaskConical },
  { id: "learning", name: "Learning", icon: Brain },
  { id: "analytics", name: "Attribution", icon: ChartPie },
  { id: "optimization", name: "Optimization", icon: SlidersHorizontal },
];

const TITLES: Record<Tab, { title: string }> = {
  dashboard: { title: "Dashboard" },
  watchlist: { title: "Watchlist" },
  markets: { title: "Markets" },
  strategies: { title: "Strategies" },
  signals: { title: "Signals" },
  trading: { title: "Trading" },
  paper: { title: "Paper" },
  evidence: { title: "Evidence" },
  learning: { title: "Learning" },
  analytics: { title: "Attribution" },
  optimization: { title: "Optimization" },
};

/** Avatar tint per role, so authority is legible at a glance in the sidebar. */
const ROLE_TONE: Record<string, string> = {
  owner: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  admin: "border border-warning/20 bg-warning/[0.08] text-warning",
  trader: "border border-gain/20 bg-gain/[0.08] text-gain",
  researcher: "border border-primary/20 bg-primary/[0.08] text-primary",
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
  risk: "trading",
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
  risk: "control-center",
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
      {sub === "intelligence" && (
        <MarketIntelligencePanel
          onOpenScreen={(p) => {
            try {
              sessionStorage.setItem(SCREEN_PRESET_KEY, JSON.stringify(p.tree));
            } catch {
              /* private mode: the screener just opens with its default */
            }
            onSubChange("screener" as MarketsSub);
          }}
        />
      )}
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

// ─── Trading: what I have done, plus the mode switch in the header row ─────
// "mode" used to be a third tab; stale #trading/mode links fall back to portfolio.
function asTradingSub(s?: string): TradingSub {
  return s === "control-center" || s === "portfolio" ? s : "portfolio";
}
function TradingTabContainer({
  sub,
  onSubChange,
}: {
  sub: TradingSub;
  onSubChange: (s: TradingSub) => void;
}) {
  const safe = asTradingSub(sub);
  const ex = useExecutionMode();
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
      <SubTabs
          value={safe}
        onChange={onSubChange}
        options={[
            { id: "control-center" as TradingSub, label: "Paper trading" },
            { id: "portfolio" as TradingSub, label: "Broker account" },
        ]}
      />
        <ModePill ex={ex} />
      </div>
      <ModeDetails ex={ex} />
      {safe === "control-center" && <PortfolioControlCenter />}
      {safe === "portfolio" && <PortfolioPanel />}
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
  const safe: EvidenceSub = sub === "backtest" || sub === "research" || sub === "measured" ? sub : "backtest";
  return (
    <div className="space-y-4">
      <SubTabs
        value={safe}
        onChange={onSubChange}
        options={[
          { id: "backtest" as EvidenceSub, label: "Backtest" },
          { id: "research" as EvidenceSub, label: "Validate" },
          { id: "measured" as EvidenceSub, label: "Results" },
        ]}
      />
      {safe === "backtest" && <BacktestWorkflowPanel />}
      {safe === "research" && <ResearchPanel forcedTab={researchTab} onTabChange={setResearchTab} />}
      {safe === "measured" && <ResearchPanel forcedTab="measured" onTabChange={setResearchTab} />}
    </div>
  );
}

/** IST wall clock, ticking every second — the market's own timezone regardless
 * of where the browser is, since a session/kill-switch timestamp only means
 * something next to the clock the market itself runs on. */
function LiveClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const time = now.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return (
    <span className="hidden items-center gap-1.5 rounded-full border border-border/80 bg-card/60 px-2.5 py-1 text-xs text-muted-foreground tabular-nums sm:inline-flex">
      {time} <span className="text-muted-foreground/60">IST</span>
    </span>
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
    <Tabs value={value} onValueChange={(v) => onChange(v as T)} variant="pill">
      <TabsList>
      {options.map((o) => (
          <TabsTrigger key={o.id} value={o.id}>
          {o.label}
          </TabsTrigger>
      ))}
      </TabsList>
    </Tabs>
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
    initial.sub === "backtest" || initial.sub === "research" || initial.sub === "measured" ? initial.sub : "backtest",
  );
  const [tradingSub, setTradingSub] = useState<TradingSub>(asTradingSub(initial.sub));

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
      if (route.tab === "evidence" && route.sub) setEvidenceSub(route.sub === "backtest" || route.sub === "research" || route.sub === "measured" ? route.sub : "backtest");
      if (route.tab === "trading" && route.sub) setTradingSub(asTradingSub(route.sub));
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const [theme, setTheme] = useState<"dark" | "light">(
    () => (localStorage.getItem("atr.theme") as "dark" | "light") ?? "light",
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

  const disconnectBroker = useCallback(async () => {
    try {
      await logoutSession();
    } catch {
      // Same reasoning as signOut above: refresh health regardless so the UI
      // never keeps claiming "Broker connected" after the user asked to disconnect.
    }
    void refreshHealth();
  }, [refreshHealth]);

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
    const t = setVisibleInterval(() => {
      void refreshHealth();
      void refreshAuth();
    }, 15000);
    return () => clearInterval(t);
  }, [refreshHealth, refreshAuth]);

  const meta = TITLES[tab];
  const accountInitials = (principal?.display_name || principal?.username || "?").slice(0, 2);
  const [showLogin, setShowLogin] = useState(false);
  const [moreOpen, setMoreOpen] = useState(() => {
    try {
      return localStorage.getItem("atr.sidebar.more") === "1";
    } catch {
      return false;
    }
  });
  // The list is open or closed as the person chose. The old rule also held it open whenever
  // the current page belonged to it, so "Less" did nothing while you were on, say, System.
  // Folded, it still shows the page you are on, so you can tell where you are.
  const shownMore = moreOpen ? MORE : MORE.filter((i) => i.id === tab);
  const toggleMore = () => {
    const next = !moreOpen;
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
      { id: "go-watchlist", label: "Go to Watchlist", group: "Navigate", icon: Eye, hint: "2", keywords: ["watchlist", "my symbols", "columns", "favourites", "track"], onSelect: () => setTab("watchlist") },
      { id: "go-markets", label: "Go to Markets", group: "Navigate", icon: Search, hint: "3", keywords: ["scanner", "momentum", "charts", "scan"], onSelect: () => setTab("markets") },
      { id: "go-strategies", label: "Go to Strategies", group: "Navigate", icon: BarChart3, hint: "4", keywords: ["registry", "validated", "paper", "models"], onSelect: () => setTab("strategies") },
      { id: "go-signals", label: "Go to Signals", group: "Navigate", icon: Bell, hint: "5", keywords: ["alerts", "buy", "sell", "rules", "notify", "briefing", "morning"], onSelect: () => setTab("signals") },
      { id: "go-trading", label: "Go to Trading", group: "Navigate", icon: Briefcase, hint: "6", keywords: ["portfolio", "holdings", "positions", "limits", "order book", "trade book"], onSelect: () => setTab("trading") },
      { id: "go-paper", label: "Go to Paper", group: "Navigate", icon: Radio, hint: "7", keywords: ["deploy", "deployment", "paper trading", "simulate", "monitor", "strategy running", "pause", "stop", "reset", "capital"], onSelect: () => setTab("paper") },
      { id: "go-evidence", label: "Go to Evidence (walk-forward)", group: "Navigate", icon: FlaskConical, hint: "8", keywords: ["research", "validate", "out of sample", "deflated sharpe", "backtest", "measured"], onSelect: () => setTab("evidence") },
      { id: "go-optimization", label: "Go to Strategy Optimization", group: "Navigate", icon: SlidersHorizontal, hint: "opt", keywords: ["optimization", "adaptive", "parameters", "walk-forward", "robustness", "recommendation"], onSelect: () => setTab("optimization") },
      { id: "go-risk", label: "Go to Portfolio Controls & Risk", group: "Navigate", icon: ShieldAlert, hint: "9", keywords: ["kill switch", "limits", "exposure", "halt", "stop", "risk", "policy"], onSelect: () => { setTradingSub("control-center"); setTab("trading", "control-center"); } },
      { id: "toggle-theme", label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode", group: "View", icon: Palette, keywords: ["appearance"], onSelect: () => setTheme((t) => (t === "dark" ? "light" : "dark")) },
      { id: "login", label: "Log in with IIFL", group: "Session", icon: LogIn, keywords: ["auth", "session", "broker"], onSelect: () => setShowLogin(true) },
      { id: "sign-out", label: "Sign out", group: "Session", icon: LogOut, keywords: ["logout", "account", "leave", "end session"], onSelect: () => void signOut() },
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
          <BrandMark className="mx-auto size-11" />
          <span className="text-xs">Checking your session…</span>
        </div>
      </div>
    );
  }
  if (sessionState === "anonymous" && (typeof window === "undefined" || !new URLSearchParams(window.location.search).has("preview"))) {
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
    <AnimatedSidebarProvider defaultOpen={false}>
      <AnimatedSidebar ariaLabel="Forward navigation" collapsible="icon">
        <AnimatedSidebarHeader className="p-3 pb-2">
          <div className="flex min-h-11 items-center gap-3 overflow-hidden px-2">
            <BrandMark className="size-7" />
            <span className="truncate text-sm font-semibold tracking-tight text-foreground group-data-[state=collapsed]/sidebar:hidden">
              Forward
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
                        icon={<Icon size={18} />}
                        onSelect={() => setTab(it.id)}
                      >
                        {it.name}
                      </AnimatedSidebarMenuButton>
                    </AnimatedSidebarMenuItem>
                  );
                })}
                <AnimatedSidebarMenuItem>
                  <AnimatedSidebarMenuButton icon={<Ellipsis size={18} />} onSelect={toggleMore}>
                    {moreOpen ? "Less" : "More"}
                  </AnimatedSidebarMenuButton>
                </AnimatedSidebarMenuItem>
                {shownMore.map((it) => {
                  const Icon = it.icon;
                  return (
                    <AnimatedSidebarMenuItem key={it.id}>
                      <AnimatedSidebarMenuButton
                        isActive={tab === it.id}
                        icon={<Icon size={18} />}
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
          <div className="flex min-h-11 w-full items-center gap-3 overflow-hidden rounded p-1 group-data-[state=collapsed]/sidebar:justify-center">
          <Tooltip content={`${principal?.username ?? "?"} · ${principal?.role ?? ""}`} side="right" delay={400}>
            <span
              className={cn(
                  "grid size-9 shrink-0 place-items-center rounded-full text-caption font-semibold uppercase",
                ROLE_TONE[principal?.role ?? ""] ?? "bg-muted text-muted-foreground",
              )}
            >
              {accountInitials}
            </span>
            </Tooltip>
            <span className="min-w-0 flex-1 group-data-[state=collapsed]/sidebar:hidden">
              <span className="block truncate text-sm font-medium text-foreground">
                {principal?.display_name || principal?.username}
              </span>
              <span className="block truncate text-xs text-muted-foreground">
                {principal?.auth_method === "anonymous" ? "auth disabled" : principal?.role}
              </span>
            </span>
            <Tooltip content="Sign out" side="right" delay={400}>
            <Button
              size="icon-sm"
              variant="plain"
              className="hover:bg-destructive/10 hover:text-destructive shrink-0 group-data-[state=collapsed]/sidebar:hidden"
              onClick={() => void signOut()}
              aria-label="Sign out"
            >
              <LogOut aria-hidden="true" />
            </Button>
            </Tooltip>
          </div>

          <div className="flex min-h-11 w-full items-center gap-3 overflow-hidden rounded p-1 group-data-[state=collapsed]/sidebar:justify-center">
            <span className="flex min-w-0 flex-1 items-center gap-2.5 px-1 group-data-[state=collapsed]/sidebar:hidden">
              <i
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  health?.session_active ? "bg-gain" : "bg-muted-foreground/40",
                )}
              />
              <span className="truncate text-sm text-muted-foreground">
                {health?.session_active ? "Broker connected" : "Broker not connected"}
              </span>
            </span>
            {health?.session_active && (
              <Tooltip content="Disconnect broker" side="right" delay={400}>
                <Button
                  size="icon-sm"
                  variant="plain"
                  className="hover:bg-destructive/10 hover:text-destructive shrink-0 group-data-[state=collapsed]/sidebar:hidden"
                  onClick={() => void disconnectBroker()}
                  aria-label="Disconnect broker"
                >
                  <LogOut aria-hidden="true" />
                </Button>
              </Tooltip>
            )}
            <Tooltip content="Collapse sidebar (Ctrl+B)" side="right" delay={400}>
            <AnimatedSidebarTrigger
              aria-label="Collapse sidebar"
                className="grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring group-data-[state=collapsed]/sidebar:size-9 group-data-[state=collapsed]/sidebar:rounded-xl"
            >
              <PanelLeft aria-hidden="true" className="size-4" />
            </AnimatedSidebarTrigger>
            </Tooltip>
          </div>
        </AnimatedSidebarFooter>
      </AnimatedSidebar>

      <AnimatedSidebarInset>
        {/* Environment banner sits above everything else — the whole point is
 that it cannot be missed or scrolled past. */}
        <EnvironmentBanner
          env={health?.env}
          executionMode={health?.execution_mode}
          killSwitch={health?.kill_switch}
        />
        <main
          className={cn(
            "mx-auto min-w-0 w-full max-w-[1200px] px-6 pb-16",
            tab === "markets" && marketsSub === "charts"
            ? "max-md:px-4 pb-8"
            : "max-md:px-4",
          )}
        >
          <div className="mb-8 flex min-h-[72px] items-center justify-between gap-4 border-b border-border bg-white/80 py-4 dark:bg-transparent">
          <div className="flex min-w-0 items-center gap-3">
              {/* The sidebar's own trigger lives in its footer, which is exactly
 what a closed mobile drawer hides — nothing could ever open it.
 This is the only way in on a phone-width viewport. */}
              <Tooltip content="Open menu" side="bottom" delay={400}>
                <AnimatedSidebarTrigger
                  aria-label="Open menu"
                  className="grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring md:hidden"
                >
                  <PanelLeft aria-hidden="true" className="size-4" />
                </AnimatedSidebarTrigger>
              </Tooltip>
            <div className="min-w-0">
                <div className="truncate text-heading text-foreground">{meta.title}</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
              {health?.execution_mode === "live" ? (
                <Tooltip content="System is transmitting live orders with real capital to IIFL" side="bottom" delay={400}>
                  <button
                    type="button"
                    onClick={() => setTab("trading")}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-destructive/30 bg-destructive/[0.08] px-2.5 py-1 text-caption font-semibold text-destructive animate-pulse"
                  >
                    <span className="size-1.5 rounded-full bg-loss" />
                    REAL / LIVE
                  </button>
                </Tooltip>
              ) : (
                <Tooltip content="Paper mode active: Signals and orders are simulated locally. No real money or broker orders are placed." side="bottom" delay={400}>
                  <button
                    type="button"
                    onClick={() => setTab("trading")}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-gain/20 bg-gain/[0.08] px-2.5 py-1 text-caption font-semibold text-gain"
                  >
                    <FlaskConical className="size-3" />
                    PAPER MODE
                  </button>
                </Tooltip>
              )}

              <LiveClock />

            {!health?.session_active && (
              <Button
                size="xs"
                variant="outline"
                onClick={() => setShowLogin(true)}
              >
                Log in
              </Button>
            )}

              <Tooltip content="Command palette (Ctrl+K)" side="bottom" delay={400}>
            <Button
              size="xs"
              variant="quiet"
              className="hidden sm:inline-flex"
              aria-label="Open command palette"
              onClick={() => setPaletteOpen(true)}
            >
              <Search className="size-3" />
              <kbd className="rounded-md border border-border bg-muted/50 px-1 text-micro">⌘K</kbd>
            </Button>
              </Tooltip>

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
        {tab === "strategies" && (
          <StrategiesPanel
            onOpenPaper={() => setTab("paper")}
          />
        )}
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
