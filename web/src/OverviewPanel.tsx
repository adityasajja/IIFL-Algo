import { PnlCalendar } from "./components/ui/pnl-calendar";
import { AlertTriangle, ChevronRight, RefreshCw, Shield } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getDashboardSummary,
  getForwardEvidenceCounts,
  getHealth,
  getPositions,
  getRiskStatus,
  getRunnerStatus,
  getTradeSignals,
  listDeployments,
  type DashboardSummary,
  type Deployment,
  type ForwardEvidenceCounts,
  type Health,
  type Position,
  type RiskStatus,
  type RunnerStatus,
  type TradeSignalsResponse,
} from "./api";
import { useLiveTicks } from "./lib/useLiveTicks";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";

interface Props {
  onNavigate: (tab: string) => void;
}

const inrFmt = (n: number | null | undefined): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
};

// ─── Status Badges ────────────────────────────────────────────────────────────
type StatusType = "HEALTHY" | "WARNING" | "STALE" | "ERROR" | "NOT_ACTIVE";

// ─── Main OverviewPanel ───────────────────────────────────────────────────────
export default function OverviewPanel({ onNavigate }: Props) {
  const [health, setHealth] = useState<Health | null>(null);
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [signals, setSignals] = useState<TradeSignalsResponse | null>(null);
  const [forwardCounts, setForwardCounts] = useState<ForwardEvidenceCounts | null>(null);

  const [, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);

  // Live feed websocket subscription
  const { connected: wsConnected, bridgeActive } = useLiveTicks(["NIFTYBEES-EQ", "RELIANCE-EQ"]);

  const loadData = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) setRefreshing(true);
    try {
      // Each card fills in as its own request lands. Waiting for all of them
      // meant the whole page stayed blank behind the slowest endpoint.
      const apply = <T,>(request: Promise<T>, set: (value: T) => void) =>
        request.then(set).catch(() => undefined);
      await Promise.all([
        apply(getHealth(), setHealth),
        apply(getRunnerStatus(), setRunner),
        apply(getDashboardSummary(), setSummary),
        apply(getPositions(), setPositions),
        apply(getRiskStatus(), setRisk),
        apply(listDeployments(), (v) => setDeployments(v?.deployments ?? [])),
        apply(getTradeSignals(), setSignals),
        apply(getForwardEvidenceCounts(), setForwardCounts),
      ]);

      const now = new Date();
      setLastRefreshedAt(
        now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
      );
    } catch {
      // Ignored: all wrapped in settled
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const interval = setVisibleInterval(() => {
      void loadData();
    }, 20_000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Derived Values
  const openPosCount = summary?.positions?.count ?? positions?.length ?? 0;
  const dayPnl = summary?.positions?.day_pnl ?? null;
  const dayPnlPct = summary?.positions?.day_pnl_pct ?? null;
  const investedCapital = summary?.positions?.invested ?? null;
  const grossExposurePct =
    risk?.limits?.capital && investedCapital !== null
      ? (investedCapital / risk.limits.capital) * 100
      : null;

  // Market hours determination
  const isMarketHours = runner?.in_market_hours ?? true;
  const feedStatus: StatusType =
    wsConnected && bridgeActive ? "HEALTHY" : wsConnected ? "WARNING" : "ERROR";
  const brokerStatus: StatusType = health?.session_active ? "HEALTHY" : "ERROR";

  const tradingOn: boolean | null = health ? !health.kill_switch : null;
  const runningDeployments = deployments.filter((d) => d.status === "RUNNING");
  const runningPnl = runningDeployments.reduce((acc, d) => acc + (d.pnl?.total_pnl ?? 0), 0);
  const waiting = (signals?.signals ?? []).filter((s) => s.status === "PENDING");
  const forwardTotal = forwardCounts?.total_genuine_forward ?? 0;
  const drawdown = summary?.performance?.current_drawdown_pct ?? 0;
  const inUsePct = grossExposurePct ?? 0;

  // `runner.running` only means the background loop is alive, not that anything
  // is being traded. Say what it is actually doing, in order of what blocks it.
  const auto: { status: StatusType; word: string } = !runner
    ? { status: "NOT_ACTIVE", word: "Checking…" }
    : !runner.running
      ? { status: "NOT_ACTIVE", word: "Off" }
      : runningDeployments.length === 0
        ? { status: "NOT_ACTIVE", word: "Nothing to run" }
        : tradingOn === false
          ? { status: "WARNING", word: "Paused" }
          : !isMarketHours
            ? { status: "NOT_ACTIVE", word: "Market closed" }
            : feedStatus === "ERROR"
              ? { status: "WARNING", word: "No prices" }
              : { status: "HEALTHY", word: "Trading" };

  // The four things that decide whether the app can do its job, as lights.
  const lights: { key: string; label: string; status: StatusType; word: string }[] = [
    { key: "market", label: "Market", status: isMarketHours ? "HEALTHY" : "NOT_ACTIVE", word: isMarketHours ? "Open" : "Closed" },
    { key: "broker", label: "Broker", status: health ? brokerStatus : "NOT_ACTIVE", word: !health ? "Checking…" : health.session_active ? "Connected" : "Offline" },
    { key: "prices", label: "Live prices", status: feedStatus, word: feedStatus === "HEALTHY" ? "Streaming" : feedStatus === "WARNING" ? "Waiting" : "Off" },
    { key: "auto", label: "Strategies", status: auto.status, word: auto.word },
  ];

  // Plain-language things that need the person, most important first.
  const todo: { id: string; tone: "bad" | "warn"; text: string; action?: { label: string; tab: string } }[] = [];
  // The safety switch is already on the app-wide banner and the Trading tile
  // (which links to Risk), and a disconnected broker is in the status strip
  // with Log in in the app bar, so neither gets its own row here.
  if (health?.session_active && feedStatus === "ERROR") {
    todo.push({ id: "prices", tone: "warn", text: "Live prices are off right now." });
  }

  const pnlTone = dayPnl === null ? "text-muted-foreground" : dayPnl > 0 ? "text-emerald-500" : dayPnl < 0 ? "text-rose-500" : "text-foreground";
  const ringTone = inUsePct >= 90 ? "stroke-rose-500" : inUsePct >= 70 ? "stroke-amber-500" : "stroke-primary";

  return (
    <div className="space-y-4 pb-12">
      {/* Mode + refresh: the only chrome above the content. */}
      <div className="flex items-center justify-between">
        <span
          title="Signals are generated and tracked, but no real order is sent to the broker."
          className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-[11px] font-medium text-amber-500"
        >
          Practice mode
        </span>
        <button
          onClick={() => loadData(true)}
          disabled={refreshing}
          title={lastRefreshedAt ? `Updated ${lastRefreshedAt}` : "Refresh"}
          aria-label="Refresh"
          className="grid size-8 place-items-center rounded-full border border-border text-muted-foreground transition-colors hover:text-foreground"
        >
          <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />
        </button>
      </div>

      {/* Status strip: four lights instead of a table of rows. */}
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-border bg-border lg:grid-cols-4">
        {lights.map((l) => (
          <div key={l.key} className="flex items-center gap-3 bg-card px-5 py-4">
            <span className="relative grid size-3 place-items-center">
              {l.status === "HEALTHY" && (
                <span className="absolute inline-flex size-3 animate-ping rounded-full bg-emerald-500/40" />
              )}
              <span
                className={cn(
                  "relative size-2.5 rounded-full",
                  l.status === "HEALTHY" ? "bg-emerald-500" : l.status === "NOT_ACTIVE" ? "bg-muted-foreground/40" : l.status === "WARNING" ? "bg-amber-500" : "bg-rose-500",
                )}
              />
            </span>
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-foreground">{l.label}</div>
              <div className="truncate text-xs text-muted-foreground">{l.word}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Three big answers: how am I doing, how much is at work, is trading on. */}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <button
          type="button"
          onClick={() => onNavigate("trading")}
          className="rounded-2xl border border-border bg-card p-5 text-left transition-colors hover:border-foreground/20"
        >
          <div className="text-xs text-muted-foreground">Today</div>
          <div className={cn("mt-3 text-4xl font-semibold tracking-tight tabular-nums", pnlTone)}>
            {dayPnl === null ? "—" : `${dayPnl > 0 ? "+" : ""}${inrFmt(dayPnl)}`}
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            {dayPnl === null
              ? "Nothing to report yet"
              : `${dayPnlPct !== null ? `${dayPnlPct > 0 ? "+" : ""}${dayPnlPct.toFixed(2)}% · ` : ""}${openPosCount} open ${openPosCount === 1 ? "trade" : "trades"}`}
          </div>
        </button>

        <button
          type="button"
          onClick={() => onNavigate("risk")}
          className="flex items-center gap-5 rounded-2xl border border-border bg-card p-5 text-left transition-colors hover:border-foreground/20"
        >
          <div className="relative size-20 shrink-0">
            <svg viewBox="0 0 100 100" className="size-20 -rotate-90">
              <circle cx="50" cy="50" r="42" fill="none" strokeWidth="9" className="stroke-muted" />
              <circle
                cx="50"
                cy="50"
                r="42"
                fill="none"
                strokeWidth="9"
                strokeLinecap="round"
                strokeDasharray={`${(2 * Math.PI * 42 * Math.min(Math.max(inUsePct, 0), 100)) / 100} ${2 * Math.PI * 42}`}
                className={cn("transition-all duration-500", ringTone)}
              />
            </svg>
            <div className="absolute inset-0 grid place-items-center text-base font-semibold tabular-nums">
              {Math.round(inUsePct)}%
            </div>
          </div>
          <div className="min-w-0">
            <div className="text-xs text-muted-foreground">Money in use</div>
            <div className="mt-1 text-sm font-medium text-foreground">
              {investedCapital ? inrFmt(investedCapital) : "Nothing invested"}
            </div>
            {risk?.limits?.capital ? (
              <div className="text-xs text-muted-foreground">of {inrFmt(risk.limits.capital)}</div>
            ) : null}
            {drawdown < 0 ? (
              <div className="mt-1 text-xs text-rose-500">Down {Math.abs(drawdown).toFixed(1)}% from peak</div>
            ) : null}
          </div>
        </button>

        <button
          type="button"
          onClick={() => onNavigate("risk")}
          className="flex items-center gap-4 rounded-2xl border border-border bg-card p-5 text-left transition-colors hover:border-foreground/20 md:col-span-2 xl:col-span-1"
        >
          <span
            className={cn(
              "grid size-14 shrink-0 place-items-center rounded-2xl",
              tradingOn === null ? "bg-muted text-muted-foreground" : tradingOn ? "bg-emerald-500/10 text-emerald-500" : "bg-rose-500/10 text-rose-500",
            )}
          >
            {tradingOn === false ? <AlertTriangle className="size-7" /> : <Shield className="size-7" />}
          </span>
          <div>
            <div className="text-xs text-muted-foreground">Trading</div>
            <div className="mt-1 text-xl font-semibold tracking-tight">
              {tradingOn === null ? "Checking…" : tradingOn ? "On" : "Paused"}
            </div>
            <div className="text-xs text-muted-foreground">
              {tradingOn === null ? "\u00a0" : tradingOn ? "Safety switch is off" : "Safety switch is on"}
            </div>
          </div>
        </button>
      </div>

      {/* Only when something needs a person. */}
      {todo.length > 0 ? (
        <div className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card">
          {todo.map((t) => (
            <div key={t.id} className="flex items-center justify-between gap-4 px-5 py-3.5">
              <div className="flex items-center gap-3 text-sm">
                <span className={cn("size-2 shrink-0 rounded-full", t.tone === "bad" ? "bg-rose-500" : "bg-amber-500")} />
                {t.text}
              </div>
              {t.action ? (
                <button
                  type="button"
                  onClick={() => onNavigate(t.action!.tab)}
                  className="shrink-0 rounded-full border border-border px-3 py-1 text-xs font-medium transition-colors hover:bg-muted"
                >
                  {t.action.label}
                </button>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        {/* Strategies: a count, not a table. */}
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Strategies running</span>
            <button
              type="button"
              onClick={() => onNavigate("paper")}
              className="flex items-center gap-0.5 text-xs text-primary hover:underline"
            >
              {runningDeployments.length > 0 ? "Manage" : "Start one"} <ChevronRight className="size-3" />
            </button>
          </div>
          <div className="mt-3 flex items-baseline gap-3">
            <span className="text-4xl font-semibold tracking-tight tabular-nums">{runningDeployments.length}</span>
            {runningDeployments.length > 0 ? (
              <span
                className={cn(
                  "text-sm font-medium tabular-nums",
                  runningPnl > 0 ? "text-emerald-500" : runningPnl < 0 ? "text-rose-500" : "text-muted-foreground",
                )}
              >
                {runningPnl > 0 ? "+" : ""}
                {inrFmt(runningPnl)}
              </span>
            ) : null}
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            {runningDeployments.length > 0 ? "Practising with live prices" : "None yet"}
          </div>
        </div>

        {/* Signals: who is waiting for a decision. */}
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Signals waiting for you</span>
            <button
              type="button"
              onClick={() => onNavigate("signals")}
              className="flex items-center gap-0.5 text-xs text-primary hover:underline"
            >
              Review <ChevronRight className="size-3" />
            </button>
          </div>
          <div className="mt-3 text-4xl font-semibold tracking-tight tabular-nums">{waiting.length}</div>
          {waiting.length > 0 ? (
            <div className="mt-3 space-y-1.5">
              {waiting.slice(0, 3).map((s) => (
                <div key={s.id} className="flex items-center gap-2.5 text-sm">
                  <span
                    className={cn(
                      "w-11 rounded-full py-0.5 text-center text-[10px] font-semibold",
                      s.action === "BUY" ? "bg-emerald-500/10 text-emerald-500" : "bg-rose-500/10 text-rose-500",
                    )}
                  >
                    {s.action === "BUY" ? "Buy" : "Sell"}
                  </span>
                  <span className="font-medium">{s.symbol.replace("-EQ", "")}</span>
                  <span className="ml-auto tabular-nums text-muted-foreground">₹{s.entry_price.toFixed(2)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-2 text-xs text-muted-foreground">Nothing waiting</div>
          )}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <PnlCalendar scope="paper" title="Paper trades, all strategies" note="Some paper results are sized at ₹1,00,000 a trade." />
        <PnlCalendar scope="real" title="My portfolio" note="Daily change in what you hold." />
      </div>

      {/* Proof: a progress bar rather than a paragraph. */}
      <div className="rounded-2xl border border-border bg-card p-5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">Real practice trades collected</span>
          <span className="tabular-nums text-foreground">{forwardTotal} of 50</span>
        </div>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-all duration-500"
            style={{ width: `${Math.min((forwardTotal / 50) * 100, 100)}%` }}
          />
        </div>
      </div>
    </div>
  );
}
