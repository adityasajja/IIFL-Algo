import {
  Activity,
  ArrowRight,
  BarChart2,
  Bell,
  BellOff,
  BrainCircuit,
  CheckCircle2,
  Crosshair,
  Minus,
  TrendingDown,
  TrendingUp,
  XCircle,
  Zap,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import {
  API_URL,
  getAlertEvents,
  getAlertRules,
  getBriefingConfig,
  getDashboardSummary,
  getHealth,
  getPositions,
  getRiskStatus,
  getSelfLearningStatus,
  getTradeSignals,
  type AlertEvent,
  type AlertRule,
  type DashboardSummary,
  type Health,
  type Position,
  type RiskStatus,
  type SelfLearningStatus,
  type TradeSignalsResponse,
} from "./api";
import { AnimatedNumber } from "./components/ui/animated-number";
import { NumberTicker } from "./components/ui/number-ticker";
import { Sparkline, TrendArrow } from "./components/ui/sparkline";
import { RelativeTime } from "./lib/time";
import { useLiveTicks } from "./lib/useLiveTicks";
import { cn } from "./lib/utils";

interface ScanAllResponse {
  as_of: string;
  universe: number;
  scored: number;
  breadth_up: number;
  rows: { symbol: string; last: number; day_chg_pct: number; rsi: number; trend: string }[];
}

interface Props {
  onNavigate: (tab: string) => void;
}

const inrFmt = (n: number) =>
  `₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

// ─── Inline stat row ─────────────────────────────────────────────────────────
function StatRow({
  label,
  value,
  tone,
  sub,
  trailing,
}: {
  label: string;
  value: ReactNode;
  tone?: "pos" | "neg" | "warn";
  sub?: ReactNode;
  /** Optional inline visual (sparkline) that sits between value and edge. */
  trailing?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-2.5 border-b border-border/40 last:border-0">
      <span className="shrink-0 text-sm text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center justify-end gap-2.5">
        {trailing ? <span className="shrink-0">{trailing}</span> : null}
        <div className="min-w-0 text-right">
          <span
            className={cn(
              "text-sm font-semibold tabular-nums",
              tone === "pos" && "text-emerald-500",
              tone === "neg" && "text-destructive",
              tone === "warn" && "text-amber-500",
              !tone && "text-foreground",
            )}
          >
            {value}
          </span>
          {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
        </div>
      </div>
    </div>
  );
}

// ─── Section card ─────────────────────────────────────────────────────────────
function Section({
  icon,
  title,
  children,
  action,
  onAction,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
  action?: string;
  onAction?: () => void;
}) {
  return (
    <div className="rounded-2xl border border-border/60 bg-card/40">
      {/* header */}
      <div className="flex items-center gap-2.5 border-b border-border/40 px-5 py-3.5">
        <span className="text-muted-foreground">{icon}</span>
        <span className="text-sm font-semibold text-foreground">{title}</span>
      </div>
      {/* body */}
      <div className="px-5 py-1">{children}</div>
      {/* footer */}
      {action && onAction && (
        <div className="border-t border-border/40 px-5 py-3">
          <button
            type="button"
            onClick={onAction}
            className="group inline-flex items-center gap-1.5 text-xs font-semibold text-primary"
          >
            {action}
            <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Dot status ───────────────────────────────────────────────────────────────
function StatusDot({ ok }: { ok: boolean | null }) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-full",
        ok === true && "bg-emerald-500",
        ok === false && "bg-destructive",
        ok === null && "bg-muted-foreground/40",
      )}
    />
  );
}

// ─── KPI tile ────────────────────────────────────────────────────────────────
function Kpi({
  label,
  value,
  sub,
  tone,
  series,
  sparkTone,
  icon,
  footnote,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "pos" | "neg" | "warn" | "muted";
  series?: number[];
  sparkTone?: "auto" | "up" | "down" | "neutral";
  icon?: ReactNode;
  footnote?: ReactNode;
}) {
  return (
    <div className="flex flex-col justify-between rounded-2xl border border-border/60 bg-card/40 px-4 py-3.5">
      <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {icon}
        {label}
      </div>

      <div className="mt-2 flex items-end justify-between gap-2">
        <div className="min-w-0">
          <div
            className={cn(
              "truncate text-xl font-semibold tabular-nums",
              tone === "pos" && "text-emerald-500",
              tone === "neg" && "text-destructive",
              tone === "warn" && "text-amber-500",
              tone === "muted" && "text-muted-foreground",
              !tone && "text-foreground",
            )}
          >
            {value}
          </div>
          {sub ? (
            <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{sub}</div>
          ) : null}
        </div>

        {series && series.length > 1 ? (
          <Sparkline
            data={series}
            tone={sparkTone ?? "auto"}
            width={68}
            height={26}
            className="mb-0.5 shrink-0"
          />
        ) : null}
      </div>

      {footnote ? <div className="mt-1.5 text-[10.5px]">{footnote}</div> : null}
    </div>
  );
}

// ─── Flat empty state ────────────────────────────────────────────────────────
/**
 * A deliberate visual state, not a gap. "—" reads as *loading* and leaves you
 * wondering whether data is missing or the value is genuinely nothing. "Flat"
 * says the book is empty on purpose, and when that last happened.
 */
function FlatState({
  icon,
  title,
  since,
  detail,
}: {
  icon: ReactNode;
  title: string;
  since?: string | null;
  detail?: string;
}) {
  return (
    <div className="rounded-2xl border border-dashed border-border/70 bg-muted/30 px-4 py-5 text-center">
      <div className="mx-auto mb-2 grid size-9 place-items-center rounded-full bg-muted text-muted-foreground">
        {icon}
      </div>
      <div className="text-sm font-semibold text-foreground/90">{title}</div>
      {since ? (
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          last flat <RelativeTime value={since} lead="relative" />
        </div>
      ) : (
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          no flat timestamp on record
        </div>
      )}
      {detail ? <div className="mt-1 text-[11px] text-muted-foreground/80">{detail}</div> : null}
    </div>
  );
}

// ─── RSI band ────────────────────────────────────────────────────────────────
function rsiBand(rsi: number): { label: string; cls: string } {
  if (rsi >= 70) return { label: "Overbought", cls: "text-destructive" };
  if (rsi <= 30) return { label: "Oversold", cls: "text-emerald-500" };
  if (rsi >= 60) return { label: "Firm", cls: "text-amber-600 dark:text-amber-400" };
  if (rsi <= 40) return { label: "Soft", cls: "text-muted-foreground" };
  return { label: "Neutral", cls: "text-muted-foreground" };
}

export default function OverviewPanel({ onNavigate }: Props) {
  const [health, setHealth] = useState<Health | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [events, setEvents] = useState<AlertEvent[] | null>(null);
  const [briefCfg, setBriefCfg] = useState<{ last_sent: { sent_at: string; channel: string } | null } | null>(null);
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [scan, setScan] = useState<ScanAllResponse | null>(null);
  const [tradeQueue, setTradeQueue] = useState<TradeSignalsResponse | null>(null);
  const [learningStatus, setLearningStatus] = useState<SelfLearningStatus | null>(null);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);

  useEffect(() => {
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void getPositions().then(setPositions).catch(() => setPositions(null));
    void getAlertRules().then(setRules).catch(() => setRules(null));
    void getAlertEvents().then(setEvents).catch(() => setEvents(null));
    void getBriefingConfig().then(setBriefCfg).catch(() => setBriefCfg(null));
    void getRiskStatus().then(setRisk).catch(() => setRisk(null));
    void getTradeSignals().then(setTradeQueue).catch(() => setTradeQueue(null));
    void getSelfLearningStatus().then(setLearningStatus).catch(() => setLearningStatus(null));
    void getDashboardSummary().then(setSummary).catch(() => setSummary(null));
    void fetch(`${API_URL}/scan-all`)
      .then((r) => (r.ok ? (r.json() as Promise<ScanAllResponse>) : null))
      .then(setScan)
      .catch(() => setScan(null));
  }, []);

  // Refresh the KPI strip on its own cadence: day P&L and drawdown move with
  // the market, while the routing/health data above changes rarely.
  useEffect(() => {
    const t = setInterval(() => {
      void getDashboardSummary().then(setSummary).catch(() => undefined);
    }, 30_000);
    return () => clearInterval(t);
  }, []);

  const armed = rules?.filter((r) => r.armed).length ?? null;
  const totalRules = rules?.length ?? null;
  const pnl = positions?.reduce((a, p) => a + p.unrealized_pnl, 0) ?? null;
  const breadthPct = scan?.scored ? Math.round((scan.breadth_up / scan.scored) * 100) : null;
  const totPosVal = positions?.reduce((a, p) => a + p.quantity * p.last_price, 0) ?? null;
  const killed = risk?.kill_switch === true;

  const perf = summary?.performance;
  const posSum = summary?.positions;
  const breadthTrend = summary?.breadth;

  const dayPnl = posSum?.day_pnl ?? null;
  const dayPnlPct = posSum?.day_pnl_pct ?? null;
  // Absent field means the backend is older than this UI, so fall back to
  // trusting the number rather than blanking a value we actually have.
  const dayPnlComplete = posSum?.day_pnl_complete ?? true;
  const winRate = perf?.win_rate ?? null;
  const drawdown = perf?.current_drawdown_pct ?? null;
  const openPositions = posSum?.count ?? positions?.length ?? null;
  const hasPositions = (openPositions ?? 0) > 0;

  const momentumSymbols = scan?.rows?.slice(0, 5).map((r) => r.symbol) ?? [];
  const { getTick, connected, bridgeActive } = useLiveTicks(momentumSymbols);

  return (
    <div className="space-y-5">
      {/* ── KPI strip ──────────────────────────────────────────────────── */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi
          label="Day P&L"
          tone={dayPnl === null || !hasPositions ? "muted" : dayPnl >= 0 ? "pos" : "neg"}
          value={
            !hasPositions ? (
              "Flat"
            ) : dayPnl === null ? (
              "—"
            ) : dayPnlComplete === false ? (
              // IIFL position rows carry no prior close, so day P&L is
              // genuinely unknown. Showing "0" here would read as "unchanged
              // today" for a book that may be moving hard.
              <span className="text-muted-foreground">n/a</span>
            ) : (
              <span className="flex items-center gap-1">
                {dayPnl >= 0 ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />}
                {dayPnl >= 0 ? "+" : "−"}
                <AnimatedNumber value={Math.abs(dayPnl)} format={inrFmt} />
              </span>
            )
          }
          sub={
            !hasPositions
              ? "no open book"
              : dayPnlComplete === false
                ? "broker reports no prior close"
                : dayPnlPct === null
                  ? "no prior close"
                  : `${dayPnlPct >= 0 ? "+" : ""}${dayPnlPct.toFixed(2)}% on invested`
          }
          series={perf?.sparkline}
          sparkTone="auto"
          footnote={
            hasPositions ? (
              <span className="text-muted-foreground/70">{openPositions} open</span>
            ) : (
              <>flat <RelativeTime value={posSum?.last_flat_at ?? null} /></>
            )
          }
        />

        <Kpi
          label="Win rate · 30 trades"
          tone={winRate === null ? "muted" : winRate >= 50 ? "pos" : "neg"}
          value={winRate === null ? "—" : `${winRate.toFixed(1)}%`}
          sub={
            perf?.sample
              ? `${perf.wins}W / ${perf.losses}L of last ${perf.sample}`
              : "no closed trades yet"
          }
          series={perf?.equity_curve}
          sparkTone="neutral"
          footnote={
            perf?.realized_pnl ? (
              <span className={perf.realized_pnl >= 0 ? "text-emerald-500" : "text-destructive"}>
                realised {perf.realized_pnl >= 0 ? "+" : "−"}₹
                {Math.abs(perf.realized_pnl).toLocaleString("en-IN", { maximumFractionDigits: 0 })}
              </span>
            ) : (
              <span className="text-muted-foreground/70">needs 30 closed trades to be meaningful</span>
            )
          }
        />

        <Kpi
          label="Current drawdown"
          tone={drawdown === null ? "muted" : drawdown <= -10 ? "neg" : drawdown < 0 ? "warn" : "pos"}
          value={drawdown === null ? "—" : `${drawdown.toFixed(2)}%`}
          sub={
            perf?.max_drawdown_pct !== undefined
              ? `peak-to-trough ${perf.max_drawdown_pct.toFixed(2)}%`
              : "no equity history"
          }
          series={perf?.equity_curve}
          sparkTone="down"
          footnote={
            drawdown !== null && drawdown < 0 ? (
              <span className="text-amber-600 dark:text-amber-400">below peak</span>
            ) : drawdown === 0 ? (
              <span className="text-emerald-500">at peak</span>
            ) : null
          }
        />

        <Kpi
          label="Active strategies"
          value={learningStatus ? String(learningStatus.total_cycles_trained) : "—"}
          sub={
            learningStatus
              ? `${learningStatus.market_regime.regime.replace("_", " ")} regime`
              : "initializing"
          }
          tone="muted"
          footnote={
            learningStatus ? (
              <>breadth {learningStatus.market_regime.breadth_pct}% · {summary?.exchange ?? "NSEEQ"}</>
            ) : null
          }
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-3">

      {/* ── Col 1: System status ───────────────────────────────────────── */}
      <Section icon={<Activity className="h-4 w-4" />} title="System status">
        <StatRow
          label="Broker session"
          value={
            <span className="flex items-center gap-1.5">
              <StatusDot ok={health?.session_active ?? null} />
              {health === null ? "Loading…" : health.session_active ? "Live" : "Not logged in"}
            </span>
          }
          tone={health?.session_active ? "pos" : health ? "neg" : undefined}
        />
        <StatRow
          label="Live feed"
          value={
            <span className="flex items-center gap-1.5">
              <StatusDot ok={connected && bridgeActive} />
              {connected && bridgeActive ? "Streaming" : connected ? "Standby" : "Offline"}
            </span>
          }
          tone={connected && bridgeActive ? "pos" : connected ? "warn" : "neg"}
        />
        <StatRow
          label="Risk engine"
          value={killed ? "Kill switch ON" : "Running"}
          tone={killed ? "neg" : "pos"}
          sub={killed ? "No new orders allowed" : undefined}
        />
        {/* `Environment` used to live here as a muted row. It now has a
            full-width banner at the top of the page — a fact this important
            should not be something you have to choose to read. */}
        <StatRow
          label="AI Quant Learning"
          value={
            learningStatus ? (
              <span className="flex items-center gap-1.5 text-primary">
                <BrainCircuit className="h-3.5 w-3.5" />
                {learningStatus.market_regime.regime.replace("_", " ")}
              </span>
            ) : "Initializing"
          }
          tone={learningStatus?.market_regime.regime === "BULL_TREND" ? "pos" : learningStatus?.market_regime.regime === "BEAR_TREND" ? "warn" : undefined}
          sub={learningStatus ? `${learningStatus.market_regime.breadth_pct}% breadth · ${learningStatus.total_cycles_trained} trained cycles` : undefined}
        />
        <StatRow
          label="Morning briefing"
          value={briefCfg?.last_sent ? "Sent" : "Not sent yet"}
          tone={briefCfg?.last_sent ? "pos" : undefined}
          sub={
            briefCfg?.last_sent ? (
              <RelativeTime value={briefCfg.last_sent.sent_at} lead="relative" />
            ) : undefined
          }
        />
      </Section>

      {/* ── Col 2: Market + Portfolio ──────────────────────────────────── */}
      <Section
        icon={<BarChart2 className="h-4 w-4" />}
        title="Market & portfolio"
        action="Open portfolio"
        onAction={() => onNavigate("portfolio")}
      >
        <StatRow
          label="Market breadth"
          value={
            breadthPct !== null ? (
              <span className="inline-flex items-center gap-1.5">
                <NumberTicker value={scan!.breadth_up} locale />
                <span className="font-normal text-muted-foreground"> / {scan!.scored} up</span>
                {breadthTrend?.expanding !== null && breadthTrend?.expanding !== undefined && (
                  <TrendArrow rising={breadthTrend.expanding} />
                )}
              </span>
            ) : breadthTrend?.current !== null && breadthTrend?.current !== undefined ? (
              // The 5-session series comes from cached daily history and is
              // available even when the scanner has not run. Showing "run the
              // scanner" beside a real sparkline would read as a contradiction.
              <span className="inline-flex items-center gap-1.5">
                {breadthTrend.current.toFixed(1)}%
                <span className="font-normal text-muted-foreground"> advancing</span>
                {breadthTrend.expanding !== null && breadthTrend.expanding !== undefined && (
                  <TrendArrow rising={breadthTrend.expanding} />
                )}
              </span>
            ) : "Run scanner first"
          }
          tone={
            breadthPct !== null ? (breadthPct >= 50 ? "pos" : "neg")
              : breadthTrend?.current != null ? (breadthTrend.current >= 50 ? "pos" : "neg")
                : undefined
          }
          sub={
            breadthPct !== null
              ? `${breadthPct}% of names in uptrend${
                  breadthTrend?.delta_5d !== null && breadthTrend?.delta_5d !== undefined
                    ? ` · ${breadthTrend.delta_5d >= 0 ? "+" : ""}${breadthTrend.delta_5d.toFixed(1)}pp over 5 sessions`
                    : ""
                }`
              : breadthTrend?.delta_5d != null
                ? `${breadthTrend.delta_5d >= 0 ? "+" : ""}${breadthTrend.delta_5d.toFixed(1)}pp over 5 sessions${
                    breadthTrend.dates?.length ? ` · to ${breadthTrend.dates[breadthTrend.dates.length - 1]}` : ""
                  }`
                : undefined
          }
          trailing={
            breadthTrend?.series && breadthTrend.series.length > 1 ? (
              <Sparkline
                data={breadthTrend.series}
                tone="auto"
                width={54}
                height={20}
                ariaLabel="breadth over the last 5 sessions"
              />
            ) : undefined
          }
        />
        <StatRow
          label="Unrealised P&L"
          value={
            pnl !== null ? (
              <span className="flex items-center gap-1">
                {pnl >= 0
                  ? <TrendingUp className="h-3.5 w-3.5" />
                  : <TrendingDown className="h-3.5 w-3.5" />}
                {pnl >= 0 ? "+" : "−"}
                <AnimatedNumber value={Math.abs(pnl)} format={inrFmt} />
              </span>
            ) : "Flat"
          }
          tone={pnl === null ? undefined : pnl >= 0 ? "pos" : "neg"}
          sub={
            hasPositions
              ? `${openPositions} open position${openPositions !== 1 ? "s" : ""}`
              : "no open positions"
          }
        />
        <StatRow
          label="Portfolio value"
          value={
            hasPositions && totPosVal !== null ? (
              <AnimatedNumber
                value={totPosVal}
                format={(n) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
              />
            ) : (
              <span className="font-normal text-muted-foreground">
                no open positions — last flat{" "}
                <RelativeTime value={posSum?.last_flat_at ?? null} />
              </span>
            )
          }
          sub={hasPositions ? "at last live price" : undefined}
        />

        {/* 0 armed is not a statistic, it is a call to action — "0 / 3 armed"
            tells you a number and leaves you to work out what to do about it. */}
        {armed === 0 ? (
          <div className="py-3">
            <button
              type="button"
              onClick={() => onNavigate("signals")}
              className="group flex w-full items-center justify-between gap-3 rounded-xl border border-amber-500/40 bg-amber-500/[0.07] px-3.5 py-2.5 text-left transition-colors hover:bg-amber-500/[0.12]"
            >
              <span className="flex items-center gap-2.5">
                <BellOff className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <span className="text-[13px] font-semibold text-amber-700 dark:text-amber-300">
                  No alerts armed — Create one
                </span>
              </span>
              <ArrowRight className="h-3.5 w-3.5 shrink-0 text-amber-600 transition-transform group-hover:translate-x-0.5 dark:text-amber-400" />
            </button>
          </div>
        ) : (
          <StatRow
            label="Alert rules"
            value={totalRules === null ? "—" : `${armed ?? 0} / ${totalRules} armed`}
            tone={armed === null ? undefined : "pos"}
            sub={armed !== null && armed > 0 ? "watching price & RSI" : undefined}
          />
        )}

        <StatRow
          label="Recent alert fires"
          value={events === null ? "—" : events.length === 0 ? "None yet" : `${events.length} in log`}
          tone={events !== null && events.length > 0 ? "warn" : undefined}
          sub={
            events && events.length > 0 && events[0].ts ? (
              <>
                last <RelativeTime value={events[0].ts} />
              </>
            ) : undefined
          }
        />

        {/* A flat book is a real state, not a missing value — say so plainly
            and say when it started, rather than showing a dash. */}
        {posSum !== undefined && !hasPositions ? (
          <div className="py-3">
            <FlatState
              icon={<Minus className="h-4 w-4" />}
              title="No open positions"
              since={posSum.last_flat_at ?? null}
              detail="Day P&L and drawdown read from the trade book until a position opens."
            />
          </div>
        ) : null}
      </Section>

      {/* ── Col 3: Live tickers / positions / signals ──────────────────── */}
      <div className="space-y-5">

        {/* Top movers */}
        <Section
          icon={<Zap className="h-4 w-4" />}
          title="Top movers"
          action="Open scanner"
          onAction={() => onNavigate("markets")}
        >
          {scan && scan.rows.length > 0 ? (
            scan.rows.slice(0, 5).map((r) => {
              const tick = getTick(r.symbol);
              const ltp = tick?.ltp ?? r.last;
              const chg = r.day_chg_pct;
              const band = rsiBand(r.rsi);
              return (
                <StatRow
                  key={r.symbol}
                  label={r.symbol.replace("-EQ", "")}
                  value={`₹${ltp.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}`}
                  tone={
                    tick?.flash === "up" ? "pos" :
                    tick?.flash === "down" ? "neg" :
                    chg > 0 ? "pos" : chg < 0 ? "neg" : undefined
                  }
                  sub={
                    <span className="inline-flex items-center gap-1.5">
                      <span className={chg >= 0 ? "text-emerald-500" : "text-destructive"}>
                        {chg >= 0 ? "+" : ""}
                        {chg.toFixed(2)}%
                      </span>
                      <span className="text-muted-foreground/50">·</span>
                      {/* Band before number: "Overbought" is the actionable
                          fact, "RSI 82" is the evidence for it. */}
                      <span className={cn("font-medium", band.cls)}>{band.label}</span>
                      <span className="text-muted-foreground/80">RSI {r.rsi.toFixed(0)}</span>
                    </span>
                  }
                />
              );
            })
          ) : (
            <div className="py-4 text-sm text-muted-foreground">Run the scanner to see ranked names.</div>
          )}
        </Section>

        {/* Recent signals */}
        <Section
          icon={<Bell className="h-4 w-4" />}
          title="Recent alert fires"
          action="Open signals & alerts"
          onAction={() => onNavigate("signals")}
        >
          {events && events.length > 0 ? (
            events.slice(0, 3).map((e) => (
              <div key={e.id} className="flex items-start gap-2.5 py-2.5 border-b border-border/40 last:border-0">
                <span className={cn("mt-0.5 shrink-0", e.ok ? "text-emerald-500" : "text-destructive")}>
                  {e.ok
                    ? <CheckCircle2 className="h-3.5 w-3.5" />
                    : <XCircle className="h-3.5 w-3.5" />}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-foreground">{e.rule}</p>
                  <p className="truncate text-xs text-muted-foreground">{e.message.slice(0, 55)}</p>
                  {e.ts ? (
                    <p className="mt-0.5 text-[10.5px]">
                      <RelativeTime value={e.ts} />
                    </p>
                  ) : null}
                </div>
              </div>
            ))
          ) : (
            <div className="py-4 text-sm text-muted-foreground">No alert fires yet.</div>
          )}
        </Section>

        {/* Pending trade signals */}
        <Section
          icon={<Crosshair className="h-4 w-4" />}
          title="Trade Queue (Semi-Auto)"
          action="Open trade queue"
          onAction={() => onNavigate("trading")}
        >
          {tradeQueue && tradeQueue.signals.filter((s) => s.status === "PENDING").length > 0 ? (
            tradeQueue.signals
              .filter((s) => s.status === "PENDING")
              .slice(0, 3)
              .map((s) => (
                <div key={s.id} className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0">
                  <div>
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-semibold text-foreground">{s.symbol.replace("-EQ", "")}</span>
                      <span
                        className={cn(
                          "rounded px-1.5 py-0.2 text-[10px] font-bold",
                          s.action === "BUY" ? "bg-emerald-500/10 text-emerald-600" : "bg-destructive/10 text-destructive",
                        )}
                      >
                        {s.action}
                      </span>
                    </div>
                    <div className="text-xs text-muted-foreground">{s.setup}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-xs font-semibold tabular-nums text-foreground">₹{s.entry_price.toFixed(2)}</div>
                    <div className="text-[11px] text-muted-foreground">{s.quantity} qty · R:R {s.rr_ratio.toFixed(1)}</div>
                  </div>
                </div>
              ))
          ) : (
            <div className="py-3">
              <FlatState
                icon={<Crosshair className="h-4 w-4" />}
                title={
                  tradeQueue?.active
                    ? `${tradeQueue.active} trade(s) in flight`
                    : "No pending trades awaiting approval"
                }
                detail={
                  health?.execution_mode === "paper"
                    ? "Paper mode — signals queue here but nothing is transmitted."
                    : undefined
                }
              />
            </div>
          )}
        </Section>
      </div>
      </div>
    </div>
  );
}
