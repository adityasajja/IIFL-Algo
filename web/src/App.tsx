import { BrandMark } from "./components/ui/brand-mark";
import {
  AnimatedSidebar,
  AnimatedSidebarContent,
  AnimatedSidebarFooter,
  AnimatedSidebarGroup,
  AnimatedSidebarGroupContent,
  AnimatedSidebarGroupLabel,
  AnimatedSidebarHeader,
  AnimatedSidebarInset,
  AnimatedSidebarMenu,
  AnimatedSidebarMenuButton,
  AnimatedSidebarMenuItem,
  AnimatedSidebarProvider,
  AnimatedSidebarTrigger,
} from "./components/motion/animated-sidebar";
import {
  type LucideIcon,
  Info,
  Bell,
  Briefcase,
  ChartPie,
  Eye,
  FlaskConical,
  House,
  Layers,
  Beaker,
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
import { useCallback, lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
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
const AlphaHuntPanel = lazy(() => import("./AlphaHuntPanel"));
const EpisodicPivotPanel = lazy(() => import("./EpisodicPivotPanel"));
import {
  appLogout,
  getDataStatus,
  getHealth,
  getLoginStatus,
  logoutSession,
  type DataStatus,
  type Health,
  type LoginStatus,
} from "./api";
import { Callout } from "./components/ui/stat";
import { DataPill } from "./DataTrust";
import { NAV_GROUPS, PAGES, SUBS, normaliseSub, parseRoute, type Tab } from "./lib/nav";
import { useSession } from "./lib/useSession";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";

/**
 * The sidebar follows the product's one story, "prove a strategy, then trade it":
 * Home, Strategies, Test, Paper, Live, Performance. Markets, Signals and Watchlist are the
 * world around that path, and Labs holds experimental tools that are not part of it.
 * The map itself (names, groups, sub-pages, old links) lives in lib/nav.ts.
 */
const PALETTE_KEYWORDS: Record<Tab, string[]> = {
  dashboard: ["home", "overview", "status", "start", "next step"],
  strategies: ["build", "registry", "rules", "models", "create"],
  evidence: ["test", "backtest", "validate", "stress test", "out of sample", "deflated sharpe", "results", "walk-forward"],
  paper: ["deploy", "deployment", "paper trading", "simulate", "practice", "monitor", "pause", "stop", "reset", "capital"],
  trading: ["live", "broker", "portfolio", "holdings", "positions", "order book", "trade book"],
  learning: ["performance", "review", "attribution", "learning", "trade history", "results"],
  markets: ["scanner", "momentum", "charts", "scan", "screener", "sectors"],
  signals: ["alerts", "buy", "sell", "rules", "notify", "briefing", "morning", "queue"],
  watchlist: ["watchlist", "my symbols", "columns", "favourites", "track"],
  labs: ["experimental", "custom scan", "alpha hunt", "episodic pivot"],
};

const TAB_ICON: Record<Tab, LucideIcon> = {
  dashboard: House,
  strategies: Layers,
  evidence: FlaskConical,
  paper: Radio,
  trading: Briefcase,
  learning: ChartPie,
  markets: TrendingUp,
  signals: Bell,
  watchlist: Eye,
  labs: Beaker,
};

/** Avatar tint per role, so authority is legible at a glance in the sidebar. */
const ROLE_TONE: Record<string, string> = {
  owner: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  admin: "border border-warning/20 bg-warning/[0.08] text-warning",
  trader: "border border-gain/20 bg-gain/[0.08] text-gain",
  researcher: "border border-primary/20 bg-primary/[0.08] text-primary",
  viewer: "bg-muted text-muted-foreground",
};

function getInitialRoute(): { tab: Tab; sub?: string } {
  if (typeof window !== "undefined") {
    const fromHash = parseRoute(window.location.hash);
    if (fromHash) return fromHash;
    const fromStore = parseRoute(localStorage.getItem("atr.tab") ?? "");
    if (fromStore) return fromStore;
  }
  return { tab: "dashboard" };
}

// ─── Markets: what is happening ───────────────────────────────────────────────
function MarketsTabContainer({
  sub,
  onSubChange,
  onOpenChart,
  theme,
}: {
  sub: string;
  onSubChange: (s: string) => void;
  onOpenChart: (symbol: string) => void;
  theme: "dark" | "light";
}) {
  return (
    <div className="space-y-4">
      <SubTabs tab="markets" value={sub} onChange={onSubChange} />
      {sub === "intelligence" && (
        <MarketIntelligencePanel
          onOpenScreen={(p) => {
            try {
              sessionStorage.setItem(SCREEN_PRESET_KEY, JSON.stringify(p.tree));
            } catch {
              /* private mode: the screener just opens with its default */
            }
            onSubChange("screener");
          }}
        />
      )}
      {sub === "scanner" && <ScannerPanel onOpenChart={onOpenChart} />}
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
  sub: string;
  onSubChange: (s: string) => void;
  onOpenChart: (symbol: string) => void;
}) {
  return (
    <div className="space-y-4">
      <SubTabs tab="signals" value={sub} onChange={onSubChange} />
      {sub === "today" && <TodayPanel onOpenChart={onOpenChart} />}
      {sub === "brief" && <BriefingPanel />}
      {sub === "alerts" && <AlertsPanel onOpenChart={onOpenChart} />}
      {sub === "queue" && <TradeSignalsPanel onOpenChart={onOpenChart} />}
      {sub === "context" && <SignalExplorerPanel />}
    </div>
  );
}

// ─── Live: your broker account, your limits, the kill switch ──────────────────
function TradingTabContainer({ sub, onSubChange }: { sub: string; onSubChange: (s: string) => void }) {
  const ex = useExecutionMode();
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SubTabs tab="trading" value={sub} onChange={onSubChange} />
        <ModePill ex={ex} />
      </div>
      <ModeDetails ex={ex} />
      {sub === "control-center" && <PortfolioControlCenter />}
      {sub === "portfolio" && <PortfolioPanel />}
    </div>
  );
}

// ─── Test: proof on prices the strategy has never seen ────────────────────────
function EvidenceTabContainer({ sub, onSubChange }: { sub: string; onSubChange: (s: string) => void }) {
  const [researchTab, setResearchTab] = useState<"harness" | "measured">("harness");
  return (
    <div className="space-y-4">
      <SubTabs tab="evidence" value={sub} onChange={onSubChange} />
      {sub === "backtest" && <BacktestWorkflowPanel />}
      {sub === "research" && <ResearchPanel forcedTab={researchTab} onTabChange={setResearchTab} />}
      {sub === "measured" && <ResearchPanel forcedTab="measured" onTabChange={setResearchTab} />}
      {sub === "improve" && <OptimizationPanel />}
    </div>
  );
}

// ─── Performance: what happened, and why ──────────────────────────────────────
function PerformanceTabContainer({ sub, onSubChange }: { sub: string; onSubChange: (s: string) => void }) {
  return (
    <div className="space-y-4">
      <SubTabs tab="learning" value={sub} onChange={onSubChange} />
      {sub === "review" && <LearningPanel />}
      {sub === "attribution" && <AnalyticsPanel />}
    </div>
  );
}

// ─── Labs: experimental, outside the tested path ──────────────────────────────
function LabsTabContainer({
  sub,
  onSubChange,
  onOpenChart,
}: {
  sub: string;
  onSubChange: (s: string) => void;
  onOpenChart: (symbol: string) => void;
}) {
  return (
    <div className="space-y-4">
      <Callout tone="warn">
        These tools are experimental and not part of the tested path. Their results are ideas to check in Test, not evidence.
      </Callout>
      <SubTabs tab="labs" value={sub} onChange={onSubChange} />
      {sub === "custom-scan" && <CustomScannerPanel onOpenChart={onOpenChart} />}
      {sub === "alpha-hunt" && <AlphaHuntPanel />}
      {sub === "episodic-pivot" && <EpisodicPivotPanel />}
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

const PATH_STEPS: Tab[] = ["strategies", "evidence", "paper", "trading", "learning"];

/** Where this page sits on the path, as five dots: shown on the path pages only. */
function StepDots({ tab }: { tab: Tab }) {
  const at = PATH_STEPS.indexOf(tab);
  if (at < 0) return null;
  return (
    <ol className="mt-1.5 flex items-center gap-1.5" aria-label={`Step ${at + 1} of ${PATH_STEPS.length}`}>
      {PATH_STEPS.map((t, i) => (
        <li
          key={t}
          title={PAGES[t].name}
          className={cn("h-1.5 rounded-full transition-all", i === at ? "w-6 bg-primary" : i < at ? "w-1.5 bg-gain" : "w-1.5 bg-border")}
        />
      ))}
    </ol>
  );
}

/** The second row of a page, built from the page's sub-pages in lib/nav.ts. */
function SubTabs({ tab, value, onChange }: { tab: Tab; value: string; onChange: (v: string) => void }) {
  return (
    <Tabs value={value} onValueChange={onChange} variant="pill">
      <TabsList>
        {(SUBS[tab] ?? []).map((o) => (
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
  const [route, setRoute] = useState<{ tab: Tab; sub?: string }>(initial);
  const tab = route.tab;
  const sub = route.sub;

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

  const setTab = useCallback((nextTab: Tab, nextSub?: string) => {
    const safeSub = normaliseSub(nextTab, nextSub);
    setRoute({ tab: nextTab, sub: safeSub });
    if (typeof window !== "undefined") {
      const target = safeSub ? `${nextTab}/${safeSub}` : nextTab;
      localStorage.setItem("atr.tab", target);
      window.location.hash = target;
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
      setTab("markets", "charts");
    },
    [setTab],
  );

  useEffect(() => {
    const handleHashChange = () => {
      const next = parseRoute(window.location.hash);
      if (!next) return;
      setRoute(next);
      localStorage.setItem("atr.tab", next.sub ? `${next.tab}/${next.sub}` : next.tab);
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
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null);
  const [dataError, setDataError] = useState(false);

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

  const refreshData = useCallback(async () => {
    try {
      setDataStatus(await getDataStatus());
      setDataError(false);
    } catch {
      // Keep the last answer: a failed check must not turn a known date into "unknown".
      setDataError(true);
    }
  }, []);

  useEffect(() => {
    void refreshData();
    const t = setVisibleInterval(() => void refreshData(), 60_000);
    return () => clearInterval(t);
  }, [refreshData]);

  useEffect(() => {
    void refreshHealth();
    void refreshAuth();
    const t = setVisibleInterval(() => {
      void refreshHealth();
      void refreshAuth();
    }, 15000);
    return () => clearInterval(t);
  }, [refreshHealth, refreshAuth]);

  const meta = PAGES[tab];
  const accountInitials = (principal?.display_name || principal?.username || "?").slice(0, 2);
  const [showLogin, setShowLogin] = useState(false);
  // Open by default: a first-time user needs the labels (the path, then the market) more than
  // the extra width. Whatever they choose afterwards is remembered.
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    try {
      return localStorage.getItem("atr.sidebar.open") !== "0";
    } catch {
      return true;
    }
  });
  const changeSidebar = useCallback((next: boolean) => {
    setSidebarOpen(next);
    try {
      localStorage.setItem("atr.sidebar.open", next ? "1" : "0");
    } catch {
      // remembering the choice is a convenience
    }
  }, []);
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
      ...NAV_GROUPS.flatMap((g) => g.tabs).map((id): CommandItem => {
        const Icon = TAB_ICON[id];
        return {
          id: `go-${id}`,
          label: `Go to ${PAGES[id].name}`,
          group: "Navigate",
          icon: Icon,
          keywords: PALETTE_KEYWORDS[id],
          onSelect: () => setTab(id),
        };
      }),
      { id: "go-risk", label: "Go to Risk & limits (kill switch)", group: "Navigate", icon: ShieldAlert, keywords: ["kill switch", "limits", "exposure", "halt", "stop", "risk", "policy", "control center"], onSelect: () => setTab("trading", "control-center") },
      { id: "go-improve", label: "Go to Improve (optimization)", group: "Navigate", icon: SlidersHorizontal, keywords: ["optimization", "optimisation", "adaptive", "parameters", "walk-forward", "robustness", "recommendation", "experiment"], onSelect: () => setTab("evidence", "improve") },
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
    <AnimatedSidebarProvider open={sidebarOpen} onOpenChange={changeSidebar}>
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
          {NAV_GROUPS.map((group) => (
            <AnimatedSidebarGroup key={group.label ?? "main"} className="pb-2">
              {group.label ? <AnimatedSidebarGroupLabel>{group.label}</AnimatedSidebarGroupLabel> : null}
              <AnimatedSidebarGroupContent>
                <AnimatedSidebarMenu>
                  {group.tabs.map((id) => {
                    const Icon = TAB_ICON[id];
                    return (
                      <AnimatedSidebarMenuItem key={id}>
                        <AnimatedSidebarMenuButton
                          isActive={tab === id}
                          icon={<Icon size={18} />}
                          onSelect={() => setTab(id)}
                        >
                          {PAGES[id].name}
                        </AnimatedSidebarMenuButton>
                      </AnimatedSidebarMenuItem>
                    );
                  })}
                </AnimatedSidebarMenu>
              </AnimatedSidebarGroupContent>
            </AnimatedSidebarGroup>
          ))}
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
            tab === "markets" && sub === "charts"
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
                <div className="flex items-center gap-2">
                  <div className="truncate text-heading text-foreground">{meta.title}</div>
                  <Tooltip content={meta.blurb} side="bottom" delay={150}>
                    <span className="grid size-6 shrink-0 cursor-help place-items-center rounded-full text-muted-foreground hover:text-foreground" role="img" aria-label={meta.blurb}>
                      <Info className="size-4" aria-hidden="true" />
                    </span>
                  </Tooltip>
                </div>
                <StepDots tab={tab} />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
              {dataStatus?.demo ? (
                <Tooltip content="This data is synthetic, made by the demo seeder. Nothing here is a real result." side="bottom" delay={200}>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-2.5 py-1 text-caption font-semibold text-warning">
                    DEMO DATA
                  </span>
                </Tooltip>
              ) : null}
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

              <DataPill status={dataStatus} onClick={() => setTab("dashboard")} />

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
        {tab === "dashboard" && <OverviewPanel onNavigate={(t, s) => setTab(t, s)} dataStatus={dataStatus} dataError={dataError} />}
        {tab === "watchlist" && (
          <WatchlistPanel permissions={principal?.permissions ?? []} onOpenChart={openChart} />
        )}
        {tab === "markets" && (
          <MarketsTabContainer
            sub={sub ?? "intelligence"}
            onSubChange={(s) => setTab("markets", s)}
            onOpenChart={openChart}
            theme={theme}
          />
        )}
        {tab === "strategies" && <StrategiesPanel onOpenPaper={() => setTab("paper")} />}
        {tab === "signals" && (
          <SignalsTabContainer sub={sub ?? "today"} onSubChange={(s) => setTab("signals", s)} onOpenChart={openChart} />
        )}
        {tab === "trading" && (
          <TradingTabContainer sub={sub ?? "portfolio"} onSubChange={(s) => setTab("trading", s)} />
        )}
        {tab === "paper" && <PaperDeploymentPanel onOpenStrategies={() => setTab("strategies")} />}
        {tab === "evidence" && (
          <EvidenceTabContainer sub={sub ?? "backtest"} onSubChange={(s) => setTab("evidence", s)} />
        )}
        {tab === "learning" && (
          <PerformanceTabContainer sub={sub ?? "review"} onSubChange={(s) => setTab("learning", s)} />
        )}
        {tab === "labs" && (
          <LabsTabContainer sub={sub ?? "custom-scan"} onSubChange={(s) => setTab("labs", s)} onOpenChart={openChart} />
        )}
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
