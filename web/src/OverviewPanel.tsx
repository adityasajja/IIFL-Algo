import { ArrowRight } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import {
  API_URL,
  getAlertEvents,
  getAlertRules,
  getBriefingConfig,
  getHealth,
  getPositions,
  getRiskStatus,
  type AlertEvent,
  type AlertRule,
  type Health,
  type Position,
} from "./api";
import { AnimatedNumber } from "./components/ui/animated-number";
import { Card, ErrorBox } from "./components/ui/card";
import { NumberTicker } from "./components/ui/number-ticker";
import { TiltCard } from "./components/ui/tilt-card";
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

function Kpi({
  label,
  children,
  sub,
  subTone,
}: {
  label: string;
  children: ReactNode;
  sub: ReactNode;
  subTone?: "pos" | "neg";
}) {
  return (
    <TiltCard max={6} className="border border-border bg-card">
      <div className="p-5">
        <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
          {label}
        </div>
        <div className="mt-1.5 text-[26px] font-bold leading-none tracking-tight tabular-nums">
          {children}
        </div>
        <div
          className={cn(
            "mt-2 text-xs text-muted-foreground",
            subTone === "pos" && "text-emerald-600 dark:text-emerald-400",
            subTone === "neg" && "text-destructive",
          )}
        >
          {sub}
        </div>
      </div>
    </TiltCard>
  );
}

function Feat({
  title,
  tag,
  lines,
  empty,
  action,
  onOpen,
}: {
  title: string;
  tag: string;
  lines: ReactNode[];
  empty: string;
  action: string;
  onOpen: () => void;
}) {
  return (
    <Card className="flex flex-col p-5 transition-colors hover:border-primary/40">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold">{title}</span>
        <span className="text-xs text-muted-foreground">{tag}</span>
      </div>
      <div className="mt-2.5 flex-1 space-y-1.5 text-[13px] text-muted-foreground">
        {lines.length > 0 ? lines : <div>{empty}</div>}
      </div>
      <button
        type="button"
        onClick={onOpen}
        className="group mt-3 inline-flex items-center gap-1 text-[13px] font-semibold text-primary"
      >
        {action}
        <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
      </button>
    </Card>
  );
}

const inr0 = (n: number) =>
  `₹${(n >= 0 ? "+" : "−") + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export default function OverviewPanel({ onNavigate }: Props) {
  const [health, setHealth] = useState<Health | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [events, setEvents] = useState<AlertEvent[] | null>(null);
  const [briefCfg, setBriefCfg] = useState<{ last_sent: { sent_at: string; channel: string } | null } | null>(null);
  const [risk, setRisk] = useState<Record<string, unknown> | null>(null);
  const [scan, setScan] = useState<ScanAllResponse | null>(null);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    const loaders: Promise<unknown>[] = [
      getHealth().then(setHealth).catch(() => setHealth(null)),
      getPositions().then(setPositions).catch(() => setPositions(null)),
      getAlertRules().then(setRules).catch(() => setRules(null)),
      getAlertEvents().then(setEvents).catch(() => setEvents(null)),
      getBriefingConfig().then(setBriefCfg).catch(() => setBriefCfg(null)),
      getRiskStatus().then(setRisk).catch(() => setRisk(null)),
      fetch(`${API_URL}/scan-all`)
        .then((r) => (r.ok ? (r.json() as Promise<ScanAllResponse>) : null))
        .then(setScan)
        .catch(() => setScan(null)),
    ];
    void Promise.allSettled(loaders).then((res) => {
      const failed = res.filter((r) => r.status === "rejected");
      if (failed.length) setErrors([`${failed.length} sources unavailable (may need a live IIFL session)`]);
    });
  }, []);

  const armed = rules?.filter((r) => r.armed).length ?? 0;
  const pnl = positions?.reduce((a, p) => a + p.unrealized_pnl, 0) ?? null;
  const breadthPct = scan && scan.scored ? (scan.breadth_up / scan.scored) * 100 : null;
  const totPosVal = positions?.reduce((a, p) => a + p.quantity * p.last_price, 0) ?? null;
  const killed = risk?.["kill_switch"] === true;

  return (
    <div>
      <div className="grid gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi
          label="Market breadth"
          sub={
            breadthPct === null
              ? "universe not loaded"
              : `${breadthPct.toFixed(0)}% of names in uptrend · as of ${scan!.as_of}`
          }
        >
          {scan ? (
            <>
              <NumberTicker value={scan.breadth_up} locale />{" "}
              <span className="text-muted-foreground">/ <NumberTicker value={scan.scored} locale /></span>
            </>
          ) : (
            "—"
          )}
        </Kpi>

        <Kpi
          label="Unrealised P&L"
          sub={pnl === null ? "positions unavailable" : `${positions!.length} open positions`}
          subTone={pnl === null ? undefined : pnl >= 0 ? "pos" : "neg"}
        >
          {pnl === null ? "—" : <AnimatedNumber value={pnl} format={inr0} />}
        </Kpi>

        <Kpi label="Portfolio value" sub="marked at last live price">
          {totPosVal === null ? (
            "—"
          ) : (
            <AnimatedNumber
              value={totPosVal}
              format={(n) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
            />
          )}
        </Kpi>

        <Kpi
          label="Kill switch"
          sub={killed ? "risk engine halted" : "risk engine running"}
          subTone={killed ? "neg" : "pos"}
        >
          {killed ? "ENGAGED" : "Safe"}
        </Kpi>
      </div>

      <div className="mt-3.5 grid gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi label="Armed alerts" sub="Telegram price & RSI watch">
          {rules === null ? (
            "—"
          ) : (
            <>
              <NumberTicker value={armed} />{" "}
              <span className="text-muted-foreground">/ <NumberTicker value={rules.length} /></span>
            </>
          )}
        </Kpi>

        <Kpi
          label="Briefing"
          sub={
            briefCfg?.last_sent
              ? `${briefCfg.last_sent.sent_at} via ${briefCfg.last_sent.channel}`
              : "no briefing delivered yet"
          }
        >
          {briefCfg?.last_sent ? "Sent" : "Not sent"}
        </Kpi>

        <Kpi
          label="Engine"
          sub={health ? `${health.env} · session ${health.session_active ? "active" : "none"}` : "backend unreachable"}
          subTone={health ? (health.session_active ? "pos" : "neg") : undefined}
        >
          {health ? health.status : "offline"}
        </Kpi>

        <Kpi label="Recent fires" sub="in the last 50 event window">
          {events === null ? "—" : <NumberTicker value={events.length} />}
        </Kpi>
      </div>

      {errors.map((e, i) => (
        <div className="mt-3.5" key={i}>
          <ErrorBox>{e}</ErrorBox>
        </div>
      ))}

      <div className="mt-[18px] grid gap-3.5 lg:grid-cols-3">
        <Feat
          title="Top momentum"
          tag="scanner"
          lines={
            scan
              ? scan.rows.slice(0, 4).map((r) => (
                  <div key={r.symbol}>
                    <strong className="font-semibold text-foreground">{r.symbol.replace("-EQ", "")}</strong>
                    {" "}· ₹{r.last.toLocaleString("en-IN")} · RSI {r.rsi.toFixed(0)}
                  </div>
                ))
              : []
          }
          empty="Run the scanner to see ranked names."
          action="Open scanner"
          onOpen={() => onNavigate("scanner")}
        />
        <Feat
          title="Newest alerts"
          tag="telegram"
          lines={
            events && events.length > 0
              ? events.slice(0, 3).map((e) => (
                  <div key={e.id}>
                    <strong className="font-semibold text-foreground">{e.rule}</strong>
                    {" "}· {e.message.slice(0, 70)}
                  </div>
                ))
              : []
          }
          empty="No alert fires yet. Set a rule to watch a level."
          action="Open alerts"
          onOpen={() => onNavigate("alerts")}
        />
        <Feat
          title="Positions"
          tag="portfolio"
          lines={
            positions && positions.length > 0
              ? positions.slice(0, 4).map((p) => (
                  <div key={`${p.exchange}:${p.symbol}`}>
                    <strong className="font-semibold text-foreground">{p.symbol}</strong>
                    {" "}· {p.quantity} @ ₹{p.avg_price.toLocaleString("en-IN")}
                  </div>
                ))
              : []
          }
          empty="No live positions visible yet."
          action="Open portfolio"
          onOpen={() => onNavigate("portfolio")}
        />
      </div>
    </div>
  );
}
