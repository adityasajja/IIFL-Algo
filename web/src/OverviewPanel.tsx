import {
  Activity,
  ArrowRight,
  BarChart2,
  Bell,
  BrainCircuit,
  CheckCircle2,
  Crosshair,
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
  getHealth,
  getPositions,
  getRiskStatus,
  getSelfLearningStatus,
  getTradeSignals,
  type AlertEvent,
  type AlertRule,
  type Health,
  type Position,
  type RiskStatus,
  type SelfLearningStatus,
  type TradeSignalsResponse,
} from "./api";
import { AnimatedNumber } from "./components/ui/animated-number";
import { NumberTicker } from "./components/ui/number-ticker";
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
}: {
  label: string;
  value: ReactNode;
  tone?: "pos" | "neg" | "warn";
  sub?: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0">
      <span className="text-sm text-muted-foreground">{label}</span>
      <div className="text-right">
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

  useEffect(() => {
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void getPositions().then(setPositions).catch(() => setPositions(null));
    void getAlertRules().then(setRules).catch(() => setRules(null));
    void getAlertEvents().then(setEvents).catch(() => setEvents(null));
    void getBriefingConfig().then(setBriefCfg).catch(() => setBriefCfg(null));
    void getRiskStatus().then(setRisk).catch(() => setRisk(null));
    void getTradeSignals().then(setTradeQueue).catch(() => setTradeQueue(null));
    void getSelfLearningStatus().then(setLearningStatus).catch(() => setLearningStatus(null));
    void fetch(`${API_URL}/scan-all`)
      .then((r) => (r.ok ? (r.json() as Promise<ScanAllResponse>) : null))
      .then(setScan)
      .catch(() => setScan(null));
  }, []);

  const armed = rules?.filter((r) => r.armed).length ?? null;
  const totalRules = rules?.length ?? null;
  const pnl = positions?.reduce((a, p) => a + p.unrealized_pnl, 0) ?? null;
  const breadthPct = scan?.scored ? Math.round((scan.breadth_up / scan.scored) * 100) : null;
  const totPosVal = positions?.reduce((a, p) => a + p.quantity * p.last_price, 0) ?? null;
  const killed = risk?.kill_switch === true;

  const momentumSymbols = scan?.rows?.slice(0, 5).map((r) => r.symbol) ?? [];
  const { getTick, connected, bridgeActive } = useLiveTicks(momentumSymbols);

  return (
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
        <StatRow
          label="Environment"
          value={health?.env ?? "—"}
        />
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
          sub={briefCfg?.last_sent ? briefCfg.last_sent.sent_at.slice(0, 16).replace("T", " ") : undefined}
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
              <span>
                <NumberTicker value={scan!.breadth_up} locale />
                <span className="font-normal text-muted-foreground"> / {scan!.scored} up</span>
              </span>
            ) : "Run scanner first"
          }
          tone={breadthPct === null ? undefined : breadthPct >= 50 ? "pos" : "neg"}
          sub={breadthPct !== null ? `${breadthPct}% of names in uptrend` : undefined}
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
            ) : "No positions"
          }
          tone={pnl === null ? undefined : pnl >= 0 ? "pos" : "neg"}
          sub={pnl !== null ? `${positions!.length} open position${positions!.length !== 1 ? "s" : ""}` : undefined}
        />
        <StatRow
          label="Portfolio value"
          value={
            totPosVal !== null ? (
              <AnimatedNumber
                value={totPosVal}
                format={(n) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
              />
            ) : "—"
          }
          sub="at last live price"
        />
        <StatRow
          label="Alert rules"
          value={totalRules === null ? "—" : `${armed ?? 0} / ${totalRules} armed`}
          sub={armed !== null && armed > 0 ? "watching price & RSI" : undefined}
        />
        <StatRow
          label="Recent alert fires"
          value={events === null ? "—" : events.length === 0 ? "None yet" : `${events.length} in log`}
          tone={events !== null && events.length > 0 ? "warn" : undefined}
        />
      </Section>

      {/* ── Col 3: Live tickers / positions / signals ──────────────────── */}
      <div className="space-y-5">

        {/* Top momentum */}
        <Section
          icon={<Zap className="h-4 w-4" />}
          title="Top momentum"
          action="Open scanner"
          onAction={() => onNavigate("scanner")}
        >
          {scan && scan.rows.length > 0 ? (
            scan.rows.slice(0, 5).map((r) => {
              const tick = getTick(r.symbol);
              const ltp = tick?.ltp ?? r.last;
              const chg = r.day_chg_pct;
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
                  sub={`RSI ${r.rsi.toFixed(0)} · ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}
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
          onAction={() => onNavigate("alerts")}
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
          onAction={() => onNavigate("trade-signals")}
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
            <div className="py-3 text-sm text-muted-foreground">
              {tradeQueue?.active ? `${tradeQueue.active} active trade(s) in flight.` : "No pending trades awaiting approval."}
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}
