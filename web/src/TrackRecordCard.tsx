import { Database, FlaskConical, LineChart, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { getTrackRecord, type TrackRecord } from "./api";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { Tooltip } from "./components/motion/tooltip";
import { Button } from "./components/ui/button";
import { Card, ErrorBox } from "./components/ui/card";
import { Ring } from "./components/ui/ring";
import { TrackChart } from "./components/ui/track-chart";
import { formatInr } from "./lib/format";
import { Callout } from "./components/ui/stat";
import { drawdownSeries, isThin, MIN_MEANINGFUL_DAYS, returnSeries, shortDay, signedPct, versus } from "./lib/track-view";
import { toneChip, toneOf, toneOfShare, toneText, type Tone } from "./lib/tone";
import { cn } from "./lib/utils";

const RANGES = [30, 60, 90, 180] as const;

/**
 * The page the product is judged on: what the strategies actually did on paper, against the
 * market, with the depth of every fall, and where the numbers came from. It leads with a single
 * figure and a picture, and keeps the words to labels.
 */
export function TrackRecordCard({
  running,
  starting,
  startError,
  onStartStarter,
  onBuild,
  onSeePast,
}: {
  /** Paper runs that are going right now: tells "nothing yet" from "running, no trade yet". */
  running: number;
  starting: boolean;
  startError: string | null;
  onStartStarter: () => void;
  onBuild: () => void;
  onSeePast: () => void;
}) {
  const [days, setDays] = useState<number>(90);
  const [data, setData] = useState<TrackRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"chart" | "table">("chart");

  const load = useCallback(async (d: number) => {
    setLoading(true);
    setError(null);
    try {
      setData(await getTrackRecord(d));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the track record");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load(days);
  }, [days, load]);

  return (
    <Card padding="md" className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <LineChart className="size-4 text-primary" aria-hidden="true" />
          Track record
          <Chipish tone="warn" icon={<FlaskConical className="size-3" />} tip="Paper trading: fills are simulated, no real order was sent.">
            Simulated
          </Chipish>
          {data?.provenance?.demo ? (
            <Chipish tone="warn" icon={<TriangleAlert className="size-3" />} tip="Synthetic data from the demo seeder. Nothing here is a real result.">
              Demo data
            </Chipish>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Tabs value={String(days)} onValueChange={(v) => setDays(Number(v))} variant="segment">
            <TabsList>
              {RANGES.map((r) => (
                <TrackTab key={r} value={String(r)} label={`${r}d`} />
              ))}
            </TabsList>
          </Tabs>
          {data?.has_data ? (
            <Button size="xs" variant="quiet" onClick={() => setView(view === "chart" ? "table" : "chart")}>
              {view === "chart" ? "Table" : "Chart"}
            </Button>
          ) : null}
        </div>
      </div>

      {error ? (
        <ErrorBox>
          {error}{" "}
          <Button size="inline" variant="link" onClick={() => void load(days)}>
            Retry
          </Button>
        </ErrorBox>
      ) : !data ? (
        <div className="h-72 animate-pulse rounded-lg bg-muted/40" aria-label="Loading" />
      ) : !data.has_data ? (
        <EmptyRecord
          running={running}
          starting={starting}
          error={startError}
          onStartStarter={onStartStarter}
          onBuild={onBuild}
          onSeePast={onSeePast}
        />
      ) : (
        <Filled data={data} view={view} refreshing={loading} />
      )}
    </Card>
  );
}

/** "NIFTY 50" -> "Nifty 50": the backend keys it in capitals. */
const niceName = (n: string) => n.replace(/\b([A-Z])([A-Z]+)\b/g, (_, a, b) => a + b.toLowerCase());

function TrackTab({ value, label }: { value: string; label: string }) {
  return <TabsTrigger value={value}>{label}</TabsTrigger>;
}

function Chipish({ tone, icon, tip, children }: { tone: Tone; icon: ReactNode; tip?: string; children: ReactNode }) {
  const chip = (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-caption font-semibold", toneChip[tone])}>
      {icon}
      {children}
    </span>
  );
  return tip ? (
    <Tooltip content={tip} side="bottom" delay={300}>
      {chip}
    </Tooltip>
  ) : (
    chip
  );
}

/**
 * Nothing has traded yet. Either nothing is running (so: one click to start a ready-made
 * strategy), or something is running and is waiting for its rule to fire (so: say that, and
 * point at what it did on past prices). A ghost of the chart shows the shape of what is coming.
 */
function EmptyRecord({
  running,
  starting,
  error,
  onStartStarter,
  onBuild,
  onSeePast,
}: {
  running: number;
  starting: boolean;
  error: string | null;
  onStartStarter: () => void;
  onBuild: () => void;
  onSeePast: () => void;
}) {
  const waiting = running > 0;
  return (
    <div className="relative grid min-h-72 place-items-center overflow-hidden rounded-lg border border-dashed border-border">
      <svg viewBox="0 0 600 220" className="absolute inset-0 h-full w-full text-muted-foreground/30" preserveAspectRatio="none" aria-hidden="true">
        {[40, 90, 140, 190].map((y) => (
          <line key={y} x1="0" x2="600" y1={y} y2={y} stroke="currentColor" strokeOpacity="0.4" />
        ))}
        <path d="M0,150 C80,150 120,110 200,120 S320,70 400,85 S520,40 600,50" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="4 6" />
        <path d="M0,150 C100,145 200,135 300,128 S480,112 600,100" fill="none" stroke="currentColor" strokeOpacity="0.6" strokeWidth="1.5" />
      </svg>
      <div className="relative flex max-w-md flex-col items-center gap-3 rounded-xl bg-card/90 px-8 py-8 text-center backdrop-blur-sm">
        {waiting ? (
          <>
            <span className="relative grid size-3 place-items-center" aria-hidden="true">
              <span className="absolute inline-flex size-3 animate-ping rounded-full bg-gain/40" />
              <span className="relative size-2 rounded-full bg-gain" />
            </span>
            <div className="text-2xl font-semibold tracking-tight text-foreground">
              {running} {running === 1 ? "strategy" : "strategies"} running
            </div>
            <div className="text-sm text-muted-foreground">No trade yet</div>
            <Button variant="outline" onClick={onSeePast}>
              See it on past prices
            </Button>
          </>
        ) : (
          <>
            <div className="text-2xl font-semibold tracking-tight text-foreground">No paper trades yet</div>
            <div className="flex items-center gap-4 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-0.5 w-4 rounded-full bg-primary" aria-hidden="true" /> Your paper trades
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="h-0.5 w-4 rounded-full bg-muted-foreground/70" aria-hidden="true" /> Nifty 50
              </span>
            </div>
            <Button onClick={onStartStarter} disabled={starting}>
              {starting ? "Starting\u2026" : "Try a ready-made strategy"}
            </Button>
            <Button size="inline" variant="link" className="text-xs" onClick={onBuild}>
              Or build your own
            </Button>
          </>
        )}
        {error ? <div className="text-xs text-loss">{error}</div> : null}
      </div>
    </div>
  );
}

function Filled({ data, view, refreshing }: { data: TrackRecord; view: "chart" | "table"; refreshing: boolean }) {
  const s = data.summary!;
  const points = data.points!;
  const base = data.base!;
  const days = data.trading_days!;
  const vs = versus(s.return_pct, s.benchmark_return_pct);
  const prov = data.provenance;
  const thin = isThin(days);
  const rows = useMemo(() => {
    const r = returnSeries(points, base);
    const dd = drawdownSeries(points, base);
    return r.map((p, i) => ({ ...p, equity: points[i].equity, below: dd[i] }));
  }, [points, base]);

  return (
    <div className={cn("space-y-5 transition-opacity", refreshing && "opacity-60")}>
      {/* the one number, then who it beat */}
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-3">
        <div>
          <div className="text-xs text-muted-foreground">
            Return, {shortDay(data.start)} to {shortDay(data.end)}
          </div>
          <div className={cn("text-5xl font-semibold leading-none tracking-tight", toneText[toneOf(s.return_pct)])}>
            {signedPct(s.return_pct)}
          </div>
        </div>
        {vs ? (
          <div className={cn("flex items-center gap-2 rounded-lg border px-3 py-2", toneChip[vs.level ? "flat" : vs.ahead ? "good" : "bad"])}>
            <span aria-hidden="true" className="text-lg leading-none">
              {vs.level ? "=" : vs.ahead ? "▲" : "▼"}
            </span>
            <div className="leading-tight">
              <div className="text-sm font-semibold tabular-nums">
                {vs.level ? "Level with" : `${Math.abs(vs.diff).toFixed(2)} pts ${vs.ahead ? "ahead of" : "behind"}`} Nifty 50
              </div>
              <div className="text-caption opacity-80 tabular-nums">Nifty {signedPct(s.benchmark_return_pct)}</div>
            </div>
          </div>
        ) : (
          <Chipish tone="flat" icon={<LineChart className="size-3" />}>
            No Nifty series to compare
          </Chipish>
        )}
      </div>

      {view === "chart" ? (
        <TrackChart points={points} base={base} />
      ) : (
        <div className="max-h-80 overflow-auto rounded-lg border border-border">
          <table className="w-full border-collapse text-body">
            <thead className="sticky top-0 bg-card">
              <tr className="border-b border-border">
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Date</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Value</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Return</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Nifty 50</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Below peak</th>
              </tr>
            </thead>
            <tbody>
              {[...rows].reverse().map((r) => (
                <tr key={r.d} className="border-b border-border/50">
                  <td className="px-3 py-1.5 text-body">{shortDay(r.d)}</td>
                  <td className="px-3 py-1.5 text-right text-body tabular-nums">{formatInr(r.equity)}</td>
                  <td className={cn("px-3 py-1.5 text-right text-body tabular-nums", toneText[toneOf(r.ret)])}>{signedPct(r.ret)}</td>
                  <td className="px-3 py-1.5 text-right text-body tabular-nums text-muted-foreground">{signedPct(r.bench)}</td>
                  <td className="px-3 py-1.5 text-right text-body tabular-nums text-muted-foreground">{signedPct(r.below)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* four facts, each a picture before it is a number */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Deepest fall">
          <div className={cn("text-xl font-semibold tabular-nums", s.max_drawdown_pct < -0.0001 ? toneText.bad : "text-foreground")}>
            {signedPct(s.max_drawdown_pct, 1)}
          </div>
          <Meter value={Math.min(100, Math.abs(s.max_drawdown_pct) * 4)} tone="bad" />
        </Tile>
        <Tile label="Trades that made money">
          {s.win_rate_pct == null ? (
            <div className="text-xl font-semibold text-muted-foreground">{"—"}</div>
          ) : (
            <div className="flex items-center gap-3">
              <Ring value={s.win_rate_pct} size={44} stroke={11} toneClass={cn(toneOfShare(s.win_rate_pct, 50, 40) === "good" ? "stroke-gain" : toneOfShare(s.win_rate_pct, 50, 40) === "warn" ? "stroke-warning" : "stroke-loss")} />
              <div>
                <div className="text-xl font-semibold tabular-nums">{s.win_rate_pct.toFixed(0)}%</div>
                <div className="text-caption text-muted-foreground">of {s.closed_trades}</div>
              </div>
            </div>
          )}
          <Payoff win={s.avg_win ?? null} loss={s.avg_loss ?? null} factor={s.profit_factor ?? null} />
        </Tile>
        <Tile label="Days of evidence">
          <div className="text-xl font-semibold tabular-nums">
            {days}
            <span className="text-sm font-normal text-muted-foreground"> / {MIN_MEANINGFUL_DAYS * 3}</span>
          </div>
          <Meter value={(days / (MIN_MEANINGFUL_DAYS * 3)) * 100} tone={thin ? "warn" : "good"} />
        </Tile>
        <Tile label="Net after costs">
          <div className={cn("text-xl font-semibold tabular-nums", toneText[toneOf(s.net_pnl)])}>
            {s.net_pnl > 0 ? "+" : ""}
            {formatInr(s.net_pnl)}
          </div>
          <div className="text-caption text-muted-foreground tabular-nums">costs {formatInr(s.commission_paid)}</div>
        </Tile>
      </div>

      {/* where it came from: three small chips, details on hover */}
      <div className="flex flex-wrap items-center gap-2">
        <Chipish tone="warn" icon={<FlaskConical className="size-3" />} tip={prov.fills}>
          Simulated fills
        </Chipish>
        <Chipish tone={prov.prices_as_of ? "flat" : "bad"} icon={<Database className="size-3" />} tip={prov.prices}>
          Prices to {shortDay(prov.prices_as_of)}
        </Chipish>
        <Chipish
          tone={prov.benchmark.as_of ? "flat" : "bad"}
          icon={<LineChart className="size-3" />}
          tip={prov.benchmark.source ?? "Daily index closes"}
        >
          {niceName(prov.benchmark.name)} to {shortDay(prov.benchmark.as_of)}
        </Chipish>
        {!prov.complete ? (
          <Chipish tone="warn" icon={<TriangleAlert className="size-3" />} tip="On these days a held stock had no price, so it was valued at what it cost.">
            {prov.unpriced_days} {prov.unpriced_days === 1 ? "day" : "days"} valued at cost
          </Chipish>
        ) : null}
      </div>

      {thin ? (
        <Callout tone="info">
          <span title={`A few weeks is luck as much as skill; it starts to mean something after ${MIN_MEANINGFUL_DAYS} days.`}>
            Too early to judge: {days} of {MIN_MEANINGFUL_DAYS} days
          </span>
        </Callout>
      ) : null}
    </div>
  );
}

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col justify-between gap-2 rounded-lg border border-border p-3">
      <div className="text-micro font-semibold uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

/**
 * What a win is worth against what a loss costs, as two bars of proportional length. A win rate alone
 * hides this: most trades can win and the book still lose money.
 */
function Payoff({ win, loss, factor }: { win: number | null; loss: number | null; factor: number | null }) {
  if (win == null && loss == null) return null;
  const w = win ?? 0;
  const l = Math.abs(loss ?? 0);
  const top = Math.max(w, l) || 1;
  return (
    <div className="space-y-1" aria-label={`Average win ${formatInr(w)}, average loss ${formatInr(-l)}`}>
      <div className="flex items-center gap-2">
        <div className="h-1.5 rounded-full bg-gain" style={{ width: `${(w / top) * 100}%`, minWidth: w ? 4 : 0 }} />
        <span className="text-micro tabular-nums text-muted-foreground">{win == null ? "\u2014" : `+${formatInr(w)}`}</span>
      </div>
      <div className="flex items-center gap-2">
        <div className="h-1.5 rounded-full bg-loss" style={{ width: `${(l / top) * 100}%`, minWidth: l ? 4 : 0 }} />
        <span className="text-micro tabular-nums text-muted-foreground">{loss == null ? "\u2014" : `\u2212${formatInr(l)}`}</span>
      </div>
      {factor != null ? <div className="text-micro text-muted-foreground tabular-nums">profit factor {factor.toFixed(2)}</div> : null}
    </div>
  );
}

/** A thin meter: same-hue track, the fill carries the state. */
function Meter({ value, tone }: { value: number; tone: Tone }) {
  const fill = tone === "bad" ? "bg-loss" : tone === "warn" ? "bg-warning" : tone === "good" ? "bg-gain" : "bg-primary";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="presentation">
      <div className={cn("h-full rounded-full", fill)} style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
    </div>
  );
}
