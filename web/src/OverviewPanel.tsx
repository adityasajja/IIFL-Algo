import { PnlCalendar } from "./components/ui/pnl-calendar";
import { RefreshCw } from "lucide-react";
import { IconChevR } from "./icons";
import { useCallback, useEffect, useState } from "react";
import {
  getDashboardSummary,
  getHealth,
  getPositions,
  getRunnerStatus,
  getTradeSignals,
  listDeployments,
  listSavedStrategies,
  seedExampleStrategy,
  getResearchedStocks,
  createDeployment,
  startDeployment,
  type DashboardSummary,
  type DataStatus,
  type Deployment,
  type Health,
  type Position,
  type RunnerStatus,
  type SavedStrategy,
  type TradeSignalsResponse,
} from "./api";
import { Button } from "./components/ui/button";
import { Tooltip } from "./components/motion/tooltip";
import { Card, Hint } from "./components/ui/card";
import { AnimatedBadge } from "./components/motion/animated-badge";
import { TiltCard } from "./components/motion/tilt-card";
import { useLiveTicks } from "./lib/useLiveTicks";
import { DataTrust } from "./DataTrust";
import { JourneyCard } from "./JourneyCard";
import { TrackRecordCard } from "./TrackRecordCard";
import { toneFill, type Tone } from "./lib/tone";
import { journeySteps, nextStep } from "./lib/journey";
import type { Tab } from "./lib/nav";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";
import { formatInr as inrFmt, TYPOGRAPHY } from "./lib/theme";

interface Props {
  onNavigate: (tab: Tab, sub?: string) => void;
  dataStatus?: DataStatus | null;
  dataError?: boolean;
}

// ─── Status Badges ────────────────────────────────────────────────────────────
type StatusType = "HEALTHY" | "WARNING" | "STALE" | "ERROR" | "NOT_ACTIVE";

// ─── Main OverviewPanel ───────────────────────────────────────────────────────
export default function OverviewPanel({ onNavigate, dataStatus, dataError }: Props) {
  const [health, setHealth] = useState<Health | null>(null);
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [signals, setSignals] = useState<TradeSignalsResponse | null>(null);
  const [strategies, setStrategies] = useState<SavedStrategy[]>([]);

  const [, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

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
        apply(listDeployments(), (v) => setDeployments(v?.deployments ?? [])),
        apply(getTradeSignals(), setSignals),
        apply(listSavedStrategies(), (v) => setStrategies(v?.strategies ?? [])),
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

  /**
   * One click from an empty app to a paper run: the worked-example strategy (made if it is not
   * there yet), put on practice money over the stocks it was researched on, and started.
   */
  const startStarter = useCallback(async () => {
    setStarting(true);
    setStartError(null);
    try {
      const seeded = await seedExampleStrategy();
      const universe = await getResearchedStocks();
      if (!universe.symbols.length) throw new Error("No researched stocks are available yet to run it on.");
      const made = await createDeployment({
        strategy_id: seeded.strategy.strategy_id,
        strategy_version: seeded.version.version,
        capital: 500_000,
        mode: "PAPER",
        config: { symbols: universe.symbols, exchange: "NSEEQ", timeframe: "1d", max_open_positions: 10 },
      });
      await startDeployment(made.deployment_id);
      await loadData(true);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : "Could not start it");
    } finally {
      setStarting(false);
    }
  }, [loadData]);

  useEffect(() => {
    void loadData();
    const interval = setVisibleInterval(() => {
      void loadData();
    }, 20_000);
    return () => clearInterval(interval);
  }, [loadData]);

  // Derived Values
  const openPosCount = summary?.positions?.count ?? positions?.length ?? 0;
  // `total_day_pnl` folds in whatever was already realized by closing a
  // position earlier today — `day_pnl` alone only prices what is *still*
  // open, so a trade opened and closed for a profit today used to vanish
  // from "Today" the moment it was flattened.
  const dayPnl = summary?.positions?.total_day_pnl ?? summary?.positions?.day_pnl ?? null;
  // The backend's percentage is unrealized-P&L-over-invested-capital, which
  // is meaningless once nothing is left open (it reads 0.00% next to a real
  // non-zero rupee figure) — so it only makes sense to show alongside an
  // actual open book.
  const dayPnlPct = openPosCount > 0 ? (summary?.positions?.day_pnl_pct ?? null) : null;

  // Market hours determination
  const isMarketHours = runner?.in_market_hours ?? true;
  // A healthy socket only means our backend and the broker bridge are up. When
  // the market itself is closed there are no live prices to stream, so the
  // light must not claim "Streaming" next to a "Market Closed" light.
  const feedStatus: StatusType =
  !isMarketHours ? "NOT_ACTIVE" : wsConnected && bridgeActive ? "HEALTHY" : wsConnected ? "WARNING" : "ERROR";
  const brokerStatus: StatusType = health?.session_active ? "HEALTHY" : "ERROR";

  const tradingOn: boolean | null = health ? !health.kill_switch : null;
  const runningDeployments = deployments.filter((d) => d.status === "RUNNING");
  const runningPnl = runningDeployments.reduce((acc, d) => acc + (d.pnl?.total_pnl ?? 0), 0);
  const waiting = (signals?.signals ?? []).filter((s) => s.status === "PENDING");

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
    { key: "prices", label: "Live prices", status: feedStatus, word: !isMarketHours ? "Market closed" : feedStatus === "HEALTHY" ? "Streaming" : feedStatus === "WARNING" ? "Waiting" : "Off" },
    { key: "auto", label: "Strategies", status: auto.status, word: auto.word },
  ];

  // Plain-language things that need the person, most important first.
  const todo: { id: string; tone: "bad" | "warn"; text: string; action?: { label: string; tab: Tab } }[] = [];
  // The safety switch is already on the app-wide banner and the Trading tile
  // (which links to Risk), and a disconnected broker is in the status strip
  // with Log in in the app bar, so neither gets its own row here.
  if (health?.session_active && feedStatus === "ERROR") {
    todo.push({ id: "prices", tone: "warn", text: "Live prices off" });
  }

  const steps = journeySteps({
    strategies: strategies.length,
    deployable: strategies.filter((x) => x.deployable).length,
    paperRuns: deployments.length,
    executionMode: health ? (health.execution_mode === "live" ? "live" : "paper") : null,
    brokerConnected: !!health?.session_active,
  });
  const next = nextStep(steps);

  const pnlTone = dayPnl === null ? "text-muted-foreground" : dayPnl > 0 ? "text-gain" : dayPnl < 0 ? "text-loss" : "text-foreground";

  const toneOfStatus: Record<StatusType, Tone> = { HEALTHY: "good", WARNING: "warn", ERROR: "bad", STALE: "warn", NOT_ACTIVE: "flat" };

  return (
    <div className="space-y-6 pb-12">
      {/* The page leads with the evidence: what the strategies did, against the market. */}
      <TrackRecordCard
        running={runningDeployments.length}
        starting={starting}
        startError={startError}
        onStartStarter={() => void startStarter()}
        onBuild={() => onNavigate("strategies")}
        onSeePast={() => onNavigate("evidence", "backtest")}
      />

      <JourneyCard steps={steps} next={next} onNavigate={onNavigate} />

      {/* Four lights for whether the app can do its job right now. */}
      <Card padding="none" className="overflow-hidden">
        <div className="grid grid-cols-2 divide-border lg:grid-cols-[repeat(4,1fr)_auto] lg:divide-x">
          {lights.map((l) => (
            <div key={l.key} className="flex items-center gap-3 px-5 py-3">
              <i className={cn("size-2.5 shrink-0 rounded-full", toneFill[toneOfStatus[l.status]], l.status === "HEALTHY" && "animate-pulse")} />
              <div className="min-w-0">
                <div className="text-body font-medium text-foreground">{l.label}</div>
                <div className="truncate text-xs text-muted-foreground">{l.word}</div>
              </div>
            </div>
          ))}
          <div className="flex items-center justify-end px-3 py-3">
            <Tooltip content={lastRefreshedAt ? `Updated ${lastRefreshedAt}` : "Refresh"} side="bottom" delay={400}>
              <Button variant="quiet" size="icon-sm" onClick={() => loadData(true)} disabled={refreshing} aria-label="Refresh">
                <RefreshCw className={cn(refreshing && "animate-spin")} />
              </Button>
            </Tooltip>
          </div>
        </div>
      </Card>

      <DataTrust status={dataStatus ?? null} brokerConnected={!!health?.session_active} error={dataError} />

      {/* Today: only once there is something to say (a day P&L or an open trade). */}
      {(dayPnl !== null || openPosCount > 0) && (
      <TiltCard
          onClick={() => onNavigate("trading")}
        className="cursor-pointer rounded-xl border border-border bg-white p-6 text-left shadow-[rgba(0,55,112,0.08)_0_1px_3px] transition-colors hover:border-primary/40 sm:p-7 dark:bg-card"
        >
        <div className="flex h-full flex-col justify-between gap-6">
          <div className="flex items-center justify-between gap-3">
            <div className={TYPOGRAPHY.eyebrow}>Today</div>
            <span className="flex items-center gap-0.5 text-xs font-medium text-primary dark:text-primary-subdued">Open trading <IconChevR size={12} /></span>
          </div>
          <div>
            <div className={cn(TYPOGRAPHY.metricHero, pnlTone)}>
            {dayPnl === null ? "—" : `${dayPnl > 0 ? "+" : ""}${inrFmt(dayPnl)}`}
          </div>
            <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-border/60 pt-4">
              <div>
              <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Day P&amp;L</div>
              <div className="mt-0.5 text-sm font-medium tabular-nums text-foreground">{dayPnlPct !== null ? `${dayPnlPct > 0 ? "+" : ""}${dayPnlPct.toFixed(2)}%` : "—"}</div>
          </div>
              <div>
              <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Open trades</div>
              <div className="mt-0.5 text-sm font-medium tabular-nums text-foreground">{openPosCount}</div>
            </div>
          </div>
            </div>
          </div>
      </TiltCard>
      )}

      {/* Only when something needs a person. */}
      {todo.length > 0 ? (
        <Card className="divide-y divide-border overflow-hidden">
          {todo.map((t) => (
            <div key={t.id} className="flex items-center justify-between gap-4 px-5 py-3.5">
              <div className="flex items-center gap-3 text-sm">
              <span className={cn("size-2 shrink-0 rounded-full", t.tone === "bad" ? "bg-loss" : "bg-warning")} />
                {t.text}
              </div>
              {t.action ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onNavigate(t.action!.tab)}
                  className="rounded-full"
                >
                  {t.action.label}
                </Button>
              ) : null}
            </div>
          ))}
        </Card>
      ) : null}

      <div className="grid gap-5 md:grid-cols-2">
        {/* Strategies: a count, not a table. */}
        <Card className="p-6">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Strategies running</span>
            <Button
              size="inline"
              variant="link"
              className="text-xs"
              onClick={() => onNavigate("paper")}
            >
              {runningDeployments.length > 0 ? "Manage" : "Start one"} <IconChevR size={12} />
            </Button>
          </div>
          <div className="mt-3 flex items-baseline gap-3">
            <span className="text-4xl font-semibold tracking-tight tabular-nums">{runningDeployments.length}</span>
            {runningDeployments.length > 0 ? (
              <span
                className={cn(
                  "text-sm font-medium tabular-nums",
                  runningPnl > 0 ? "text-gain" : runningPnl < 0 ? "text-loss" : "text-muted-foreground",
                )}
              >
                {runningPnl > 0 ? "+" : ""}
                {inrFmt(runningPnl)}
              </span>
            ) : null}
          </div>
          {runningDeployments.length === 0 ? <Hint className="mt-2">None</Hint> : null}
        </Card>

        {/* Signals: who is waiting for a decision. */}
        <Card className="p-6">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Signals waiting for you</span>
            <Button
              size="inline"
              variant="link"
              className="text-xs"
              onClick={() => onNavigate("signals")}
            >
              Review <IconChevR size={12} />
            </Button>
          </div>
          <div className="mt-3 text-4xl font-semibold tracking-tight tabular-nums">{waiting.length}</div>
          {waiting.length > 0 ? (
            <div className="mt-3 space-y-1.5">
              {waiting.slice(0, 3).map((s) => (
                <div key={s.id} className="flex items-center gap-2.5 text-sm">
                <AnimatedBadge
                status={s.action === "BUY" ? "success" : "danger"}
                size="sm"
                  >
                    {s.action === "BUY" ? "Buy" : "Sell"}
                  </AnimatedBadge>
                  <span className="font-medium">{s.symbol.replace("-EQ", "")}</span>
                  <span className="ml-auto tabular-nums text-muted-foreground">₹{s.entry_price.toFixed(2)}</span>
                </div>
              ))}
            </div>
          ) : (
            <Hint className="mt-2">None</Hint>
          )}
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <PnlCalendar scope="paper" title="Paper trades, all strategies" note="₹1,00,000 a trade" />
        <PnlCalendar scope="real" title="My portfolio" />
      </div>

    </div>
  );
}
