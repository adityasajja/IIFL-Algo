import { motion } from "motion/react";
import {
  Activity,
  BarChart3,
  Bell,
  Briefcase,
  FlaskConical,
  Home,
  LogIn,
  Mail,
  PanelLeft,
  Palette,
  Search,
  Server,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import AlertsPanel from "./AlertsPanel";
import BacktestPanel from "./BacktestPanel";
import BriefingPanel from "./BriefingPanel";
import ChartsPanel from "./ChartsPanel";
import { Button } from "./components/ui/button";
import { CommandPalette, type CommandItem } from "./components/ui/command-palette";
import { ThemeToggle } from "./components/ui/theme-toggle";
import { Tooltip } from "./components/ui/tooltip";
import LoginBanner from "./LoginBanner";
import PortfolioPanel from "./PortfolioPanel";
import OverviewPanel from "./OverviewPanel";
import ResearchPanel from "./ResearchPanel";
import RiskPanel from "./RiskPanel";
import ScannerPanel from "./ScannerPanel";
import SignalsPanel from "./SignalsPanel";
import SystemPanel from "./SystemPanel";
import {
  API_URL,
  getHealth,
  getLoginStatus,
  type Health,
  type LoginStatus,
} from "./api";
import {
  IconBell,
  IconBrief,
  IconChart,
  IconChevL,
  IconChevR,
  IconFlask,
  IconHome,
  IconLayers,
  IconMail,
  IconPulse,
  IconScan,
  IconServer,
  IconShield,
} from "./icons";
import { cn } from "./lib/utils";

export type Tab =
  | "overview"
  | "scanner"
  | "charts"
  | "signals"
  | "alerts"
  | "briefing"
  | "research"
  | "backtest"
  | "portfolio"
  | "risk"
  | "system";

const GROUPS: { label: string; items: { id: Tab; name: string; icon: (p: { size?: number }) => ReactNode }[] }[] = [
  {
    label: "Markets",
    items: [
      { id: "overview", name: "Overview", icon: IconHome },
      { id: "scanner", name: "Scanner", icon: IconScan },
      { id: "charts", name: "Charts", icon: IconChart },
    ],
  },
  {
    label: "Signals",
    items: [
      { id: "signals", name: "Signals", icon: IconPulse },
      { id: "alerts", name: "Alerts", icon: IconBell },
      { id: "briefing", name: "Briefing", icon: IconMail },
    ],
  },
  {
    label: "Evidence",
    items: [
      { id: "research", name: "Research", icon: IconFlask },
      { id: "backtest", name: "Backtest", icon: IconLayers },
    ],
  },
  {
    label: "Account",
    items: [
      { id: "portfolio", name: "Portfolio", icon: IconBrief },
      { id: "risk", name: "Risk", icon: IconShield },
    ],
  },
  {
    label: "System",
    items: [{ id: "system", name: "Caches & session", icon: IconServer }],
  },
];

const TITLES: Record<Tab, { title: string; sub: string }> = {
  overview: { title: "Overview", sub: "Everything that matters, at a glance" },
  scanner: { title: "Market scanner", sub: "Momentum across 2600+ NSE names" },
  charts: { title: "Charts", sub: "OHLCV, indicators and a synced RSI pane" },
  signals: { title: "Signals", sub: "Risk exits on your book, entries as a watchlist" },
  alerts: { title: "Alerts", sub: "Price and indicator watch, delivered to Telegram" },
  briefing: { title: "Briefing", sub: "The morning list, configured and automated" },
  research: { title: "Research", sub: "Walk-forward validation — the only number that counts" },
  backtest: { title: "Backtest", sub: "Single-run replay on synthetic or real data" },
  portfolio: { title: "Portfolio", sub: "Limits, positions, holdings, order and trade books" },
  risk: { title: "Risk", sub: "Engine status and the kill switch" },
  system: { title: "Caches & session", sub: "History cache, contract files, broker session" },
};

function StatusPill({
  dot,
  pulse,
  title,
  children,
}: {
  dot: string;
  pulse?: boolean;
  title: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground"
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", dot, pulse && "animate-pulse")} />
      {children}
    </span>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");
  const [theme, setTheme] = useState<"dark" | "light">(
    () => (localStorage.getItem("atr.theme") as "dark" | "light") ?? "dark",
  );
  const [health, setHealth] = useState<Health | null>(null);
  const [auth, setAuth] = useState<LoginStatus | null>(null);
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem("atr.sidebar") === "closed",
  );
  const [paletteOpen, setPaletteOpen] = useState(false);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await getHealth());
    } catch {
      setHealth(null);
    }
  }, []);

  const refreshAuth = useCallback(async () => {
    try {
      setAuth(await getLoginStatus());
    } catch {
      setAuth(null);
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("atr.theme", theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem("atr.sidebar", collapsed ? "closed" : "open");
  }, [collapsed]);

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
  const [showLogin, setShowLogin] = useState(false);
  const prompted = useRef(false);

  useEffect(() => {
    if (!auth) return;
    if (auth.session_active) {
      setShowLogin(false);
      prompted.current = false;
    } else if (!prompted.current) {
      setShowLogin(true);
      prompted.current = true;
    }
  }, [auth]);

  const paletteItems: CommandItem[] = useMemo(
    () => [
      { id: "go-overview", label: "Go to Overview", group: "Navigate", icon: Home, hint: "1", keywords: ["dashboard", "home"], onSelect: () => setTab("overview") },
      { id: "go-scanner", label: "Go to Scanner", group: "Navigate", icon: Search, hint: "2", keywords: ["momentum", "scan"], onSelect: () => setTab("scanner") },
      { id: "go-charts", label: "Go to Charts", group: "Navigate", icon: BarChart3, hint: "3", keywords: ["candles", "plot"], onSelect: () => setTab("charts") },
      { id: "go-signals", label: "Go to Signals", group: "Navigate", icon: Activity, hint: "4", keywords: ["buy", "sell", "rules"], onSelect: () => setTab("signals") },
      { id: "go-alerts", label: "Go to Alerts", group: "Navigate", icon: Bell, hint: "5", keywords: ["notify", "rules"], onSelect: () => setTab("alerts") },
      { id: "go-briefing", label: "Go to Briefing", group: "Navigate", icon: Mail, hint: "6", keywords: ["morning", "brief"], onSelect: () => setTab("briefing") },
      { id: "go-research", label: "Go to Research (walk-forward)", group: "Navigate", icon: FlaskConical, hint: "7", keywords: ["validate", "out of sample", "deflated sharpe"], onSelect: () => setTab("research") },
      { id: "go-backtest", label: "Go to Backtest", group: "Navigate", icon: FlaskConical, hint: "8", keywords: ["strategy", "replay"], onSelect: () => setTab("backtest") },
      { id: "go-portfolio", label: "Go to Portfolio", group: "Navigate", icon: Briefcase, hint: "9", keywords: ["holdings", "limits", "order book", "trade book"], onSelect: () => setTab("portfolio") },
      { id: "go-risk", label: "Go to Risk", group: "Navigate", icon: ShieldCheck, hint: "0", keywords: ["kill switch", "limits"], onSelect: () => setTab("risk") },
      { id: "go-system", label: "Go to Caches & session", group: "Navigate", icon: Server, keywords: ["history", "contracts", "cache", "sync"], onSelect: () => setTab("system") },
      { id: "toggle-sidebar", label: collapsed ? "Expand sidebar" : "Collapse sidebar", group: "View", icon: PanelLeft, keywords: ["nav", "rail"], onSelect: () => setCollapsed((c) => !c) },
      { id: "toggle-theme", label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode", group: "View", icon: Palette, keywords: ["appearance"], onSelect: () => setTheme((t) => (t === "dark" ? "light" : "dark")) },
      { id: "login", label: "Log in with IIFL", group: "Session", icon: LogIn, keywords: ["auth", "session", "broker"], onSelect: () => setShowLogin(true) },
    ],
    [collapsed, theme],
  );

  return (
    <div className={cn("grid min-h-screen", collapsed ? "grid-cols-[62px_1fr]" : "grid-cols-[226px_1fr]")}>
      <aside className="sticky top-0 flex h-screen flex-col border-r border-border bg-card px-3.5 py-[18px]">
        <div className={cn("flex items-center gap-2.5 pb-[18px]", collapsed ? "justify-center px-0" : "px-1.5")}>
          <div className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-[9px] bg-gradient-to-br from-primary to-violet-500 text-sm font-extrabold text-white shadow-sm">
            A
          </div>
          {!collapsed && (
            <div>
              <div className="text-[15px] font-bold tracking-tight">ATR</div>
              <div className="text-[11px] text-muted-foreground">algo trading</div>
            </div>
          )}
        </div>

        {GROUPS.map((g) => (
          <div className="mt-[18px] first:mt-1" key={g.label}>
            {!collapsed && (
              <div className="px-2.5 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground/70">
                {g.label}
              </div>
            )}
            {g.items.map((it) => {
              const Icon = it.icon;
              const active = tab === it.id;
              return (
                <button
                  key={it.id}
                  onClick={() => setTab(it.id)}
                  title={it.name}
                  className={cn(
                    "relative mb-0.5 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] font-medium transition-colors",
                    collapsed && "justify-center px-2",
                    active ? "text-foreground" : "text-muted-foreground hover:bg-primary/[0.05] hover:text-foreground",
                  )}
                >
                  {active && (
                    <motion.span
                      layoutId="nav-active"
                      className="absolute inset-0 rounded-lg bg-primary/[0.09]"
                      transition={{ type: "spring", stiffness: 420, damping: 34 }}
                    />
                  )}
                  <span className={cn("relative z-10 inline-flex", active && "text-primary")}>
                    <Icon />
                  </span>
                  {!collapsed && <span className="relative z-10">{it.name}</span>}
                </button>
              );
            })}
          </div>
        ))}

        {!collapsed && (
          <div className="mt-auto border-t border-border px-2.5 pb-0.5 pt-3.5 text-[11.5px] leading-relaxed text-muted-foreground/70">
            <div>Sandbox · no auto-orders</div>
            <div>
              <span
                className="cursor-pointer text-primary hover:underline"
                onClick={() => window.open(`${API_URL}/docs`, "_blank")}
              >
                API docs ↗
              </span>
            </div>
          </div>
        )}
      </aside>

      <main className="min-w-0 px-7 pb-15 max-md:px-3.5">
        <div className="mb-[22px] flex h-15 items-center justify-between gap-4 border-b border-border">
          <div className="flex min-w-0 items-center gap-3">
            <Tooltip content={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
              <button
                type="button"
                onClick={() => setCollapsed((c) => !c)}
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-expanded={!collapsed}
                className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted hover:text-foreground max-md:hidden"
              >
                {collapsed ? <IconChevR size={14} /> : <IconChevL size={14} />}
              </button>
            </Tooltip>
            <div className="min-w-0">
              <div className="truncate text-[17px] font-bold tracking-tight">{meta.title}</div>
              <div className="truncate text-xs text-muted-foreground">{meta.sub}</div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2.5">
            <StatusPill
              title="backend"
              dot={health ? (health.session_active ? "bg-emerald-500" : "bg-amber-500") : "bg-red-500"}
              pulse={!!health && !health.session_active}
            >
              {health ? health.env : "offline"}
            </StatusPill>
            <StatusPill title="database" dot={health ? (dbUp ? "bg-emerald-500" : "bg-red-500") : "bg-red-500"}>
              {dbUp ? "db up" : "db off"}
            </StatusPill>
            {health?.session_active ? (
              <StatusPill title="IIFL session" dot="bg-emerald-500">
                session
              </StatusPill>
            ) : (
              <Button size="sm" variant="outline" onClick={() => setShowLogin(true)}>
                Log in
              </Button>
            )}
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              title="Command palette (Ctrl+K)"
              className="hidden items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground sm:inline-flex"
            >
              <Search className="h-3 w-3" />
              <kbd className="rounded border border-border bg-background px-1 text-[10px]">⌘K</kbd>
            </button>
            <ThemeToggle
              isDark={theme === "dark"}
              onToggle={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              className="grid h-[30px] w-[30px] place-items-center rounded-full border border-border bg-card text-muted-foreground transition-colors hover:text-foreground"
              iconClassName="h-3.5 w-3.5"
            />
          </div>
        </div>

        {tab === "overview" && <OverviewPanel onNavigate={(t) => setTab(t as Tab)} />}
        {tab === "scanner" && <ScannerPanel onOpenChart={(t) => setTab(t as Tab)} />}
        {tab === "charts" && <ChartsPanel theme={theme} />}
        {tab === "signals" && <SignalsPanel />}
        {tab === "alerts" && <AlertsPanel onOpenChart={(t) => setTab(t as Tab)} />}
        {tab === "briefing" && <BriefingPanel />}
        {tab === "research" && <ResearchPanel />}
        {tab === "backtest" && <BacktestPanel />}
        {tab === "portfolio" && <PortfolioPanel />}
        {tab === "risk" && <RiskPanel />}
        {tab === "system" && <SystemPanel />}
      </main>

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
    </div>
  );
}
