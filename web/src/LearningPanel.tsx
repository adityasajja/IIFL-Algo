/**
 * The learning dashboard — what the trade history says.
 *
 * This is the screen for Phases 1 to 3 of the learning engine: the normalised
 * dataset, the per-axis performance analysis, and the backtest-vs-live drift
 * comparison. It is read-only. There is no button here that changes a strategy,
 * because there is no route behind one.
 *
 * Three things about this panel are deliberate.
 *
 * **The empty state is the primary render path, not an edge case.** The live
 * database currently holds zero trades. A screen that shows `0` for every
 * figure is worse than one that shows nothing, because `0` is a measurement and
 * "no trades exist" is not. So when the book is empty this renders the reason,
 * the missing-feature list, and nothing else.
 *
 * **Absent and zero are rendered differently everywhere.** The backend is
 * careful to send `null` for an unmeasured metric, and `—` is what appears on
 * screen. A drift metric that could not be scored shows its values but no
 * delta, with the reason beside it.
 *
 * **Nothing here proposes a parameter value.** The observations are advisory
 * statements with evidence and sample sizes. Phase 4 will add proposals behind
 * an approval gate; drawing them here first would put an unvalidated number in
 * front of someone who might act on it.
 */

import { AlertTriangle, FlaskConical, Info, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import {
  learningOverview,
  learningReadiness,
  triggerDailyLearningCycle,
  type LearningAnalysis,
  type LearningDatasetSummary,
  type LearningDrift,
  type LearningOverview,
  type LearningReadiness,
  type LearningReport,
  type DriftPair,
  type BacktestVsForwardComparison,
  type ReadinessStrategy,
  type StoredLearningObservation,
  type DailyLearningCycleReport,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { AnimatedNumber } from "./components/ui/animated-number";
import { NumberTicker } from "./components/ui/number-ticker";
import { Select } from "./components/ui/select";
import { Badge, Callout } from "./components/ui/stat";
import {
  blockedMetrics,
  bucketViews,
  coverageLabel,
  emptyState,
  evidenceStatusLabel,
  evidenceView,
  findingViews,
  headlineTone,
  isRefusal,
  metricView,
  nothingMeasured,
  orderFindings,
  readinessRowView,
  readinessTotals,
  reportHeadline,
  sampleVerdict,
  starvedAxes,
  unrecordedMetrics,
  type Tone,
} from "./lib/learning-view";

const TONE_CLASS: Record<Tone, string> = {
  good: "text-emerald-500",
  bad: "text-destructive",
  warn: "text-amber-500",
  muted: "text-muted-foreground",
};

export function LearningPanel() {
  const [overview, setOverview] = useState<LearningOverview | null>(null);
  const [readiness, setReadiness] = useState<LearningReadiness | null>(null);
  const [readinessError, setReadinessError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [windowDays, setWindowDays] = useState(90);
  const [cycleRunning, setCycleRunning] = useState(false);
  const [cycleMsg, setCycleMsg] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setOverview(await learningOverview(windowDays));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the learning report");
    } finally {
      setBusy(false);
    }
    // Readiness is a separate, cheaper read. A failure here must not take
    // down the whole screen — the section says why it is missing instead.
    try {
      setReadiness(await learningReadiness());
      setReadinessError("");
    } catch (err) {
      setReadiness(null);
      setReadinessError(err instanceof Error ? err.message : "could not load readiness");
    }
  }, [windowDays]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <div className="space-y-4">
        <ErrorBox>{error}</ErrorBox>
      </div>
    );
  }

  if (!overview) {
    return <Hint>Reading the trade history…</Hint>;
  }

  const empty = emptyState(
    overview.dataset.trades,
    overview.missing_features,
    overview.limitations,
  );

  const handleRunCycle = async () => {
    setCycleRunning(true);
    setCycleMsg("");
    try {
      const res = await triggerDailyLearningCycle();
      setCycleMsg(`Cycle ${res.cycle_id} executed: ${res.status}`);
      await load();
    } catch (err) {
      setCycleMsg(err instanceof Error ? err.message : "Learning cycle execution failed");
    } finally {
      setCycleRunning(false);
    }
  };

  return (
    <div className="space-y-4">
      <Header
        overview={overview}
        windowDays={windowDays}
        onWindow={setWindowDays}
        onRefresh={load}
        busy={busy}
      />

      <DailyLearningCycleSection
        cycle={overview.latest_cycle}
        running={cycleRunning}
        onRunCycle={handleRunCycle}
        message={cycleMsg}
      />

      <ReadinessSection readiness={readiness} error={readinessError} />

      {empty.empty ? <EmptyBook state={empty} /> : null}

      {!empty.empty && nothingMeasured(overview) ? (
        <Callout>
          There are trades, but nothing cleared the sample floor yet. Every figure
          below is withheld rather than guessed — see the sample sizes.
        </Callout>
      ) : null}

      <MissingFeatures missing={overview.missing_features} />

      {!empty.empty ? (
        <>
          <BacktestVsForwardSection comparison={overview.backtest_vs_forward} />
          <AnalysisSection analysis={overview.analysis} />
          <ObservationHistorySection observations={overview.observations} />
          <DriftSection drift={overview.drift} />
          <ReportSection report={overview.report} />
        </>
      ) : null}

      <Limitations limitations={overview.limitations} />
    </div>
  );
}


// ---------------------------------------------------------------------------
// learning readiness — is trustworthy evidence accumulating?
// ---------------------------------------------------------------------------

const STATE_BADGE: Record<Tone, string> = {
  good: "text-emerald-500 border-emerald-500/20 bg-emerald-500/10",
  bad: "text-destructive border-destructive/20 bg-destructive/10",
  warn: "text-amber-500 border-amber-500/20 bg-amber-500/10",
  muted: "text-muted-foreground border-border/60 bg-muted/20",
};

function bandTotals(strategies: ReadinessStrategy[]): { band: string; trades: number }[] {
  const totals: Record<string, number> = {};
  for (const s of strategies || []) {
    for (const b of s.summary?.score_bands || []) {
      totals[b.band] = (totals[b.band] || 0) + (b.forward_trades ?? 0);
    }
  }
  const order = ["0-39", "40-59", "60-79", "80-100"];
  return order
    .filter((band) => band in totals)
    .map((band) => ({ band, trades: totals[band] }));
}

function ReadinessSection({
  readiness,
  error,
}: {
  readiness: LearningReadiness | null;
  error: string;
}) {
  if (error) {
    return (
      <Card className="p-5">
        <h3 className="text-sm font-semibold tracking-tight">Learning Readiness</h3>
        <p className="mt-1 text-[12px] text-muted-foreground">
          Readiness is unavailable: {error}. The rest of this screen is unaffected.
        </p>
      </Card>
    );
  }
  if (!readiness) {
    return <Hint>Reading readiness…</Hint>;
  }

  const totals = readinessTotals(readiness.strategies, readiness.quality_issues);
  const bands = bandTotals(readiness.strategies);
  const states: { label: string; count: number; tone: Tone }[] = [
    { label: "Not ready", count: totals.byState["NOT READY"] || 0, tone: "muted" },
    { label: "Minimum sample", count: totals.byState["MINIMUM SAMPLE"] || 0, tone: "warn" },
    { label: "Analysis ready", count: totals.byState["ANALYSIS READY"] || 0, tone: "good" },
    {
      label: "Optimization eligible",
      count: totals.byState["OPTIMIZATION ELIGIBLE"] || 0,
      tone: "good",
    },
  ];

  return (
    <Card className="p-5">
      <div className="flex items-center gap-2">
        <FlaskConical className="size-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold tracking-tight">Learning Readiness</h3>
        <Badge>research only — changes nothing</Badge>
      </div>
      <p className="mt-1 text-[12px] text-muted-foreground">
        Whether each strategy is accumulating enough genuine forward evidence for
        analysis. States are labels, not actions: nothing here pauses trading,
        edits a strategy, or touches risk.
      </p>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
          <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Strategies tracked</div>
          <div className="mt-1 text-lg font-semibold tabular-nums">
            <NumberTicker value={totals.strategies} />
          </div>
          <div className="text-[10px] text-muted-foreground">{totals.forward} forward trades</div>
        </div>
        {states.map((s) => (
          <div key={s.label} className="rounded-xl border border-border/60 bg-muted/20 p-3">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{s.label}</div>
            <div className={`mt-1 text-lg font-semibold tabular-nums ${TONE_CLASS[s.tone]}`}>
              <NumberTicker value={s.count} />
            </div>
            <div className="text-[10px] text-muted-foreground">strategies</div>
          </div>
        ))}
      </div>

      {readiness.strategies.length === 0 ? (
        <div className="mt-4 rounded-lg border border-dashed border-border/60 p-4 text-center text-[12px] text-muted-foreground">
          No forward evidence yet. The first closed paper trade starts the count;
          10 clears the minimum, 50 opens analysis.
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border/60 text-left text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="px-2 py-1.5 font-medium">Strategy</th>
                <th className="px-2 py-1.5 font-medium">Forward trades</th>
                <th className="px-2 py-1.5 font-medium">Next gate</th>
                <th className="px-2 py-1.5 font-medium">Status</th>
                <th className="px-2 py-1.5 font-medium">Context</th>
                <th className="px-2 py-1.5 font-medium">Last trade</th>
              </tr>
            </thead>
            <tbody>
              {readiness.strategies.map((s) => {
                const row = readinessRowView(s);
                return (
                  <tr key={row.id} className="border-b border-border/40 last:border-0">
                    <td className="px-2 py-1.5">
                      <div className="font-medium text-foreground">{row.name}</div>
                      <div className="text-[10px] text-muted-foreground">
                        {row.version}
                        {s.deployment ? ` · ${s.deployment.mode} ${s.deployment.status}` : ""}
                        {s.open_forward_trades > 0 ? ` · ${s.open_forward_trades} open` : ""}
                      </div>
                    </td>
                    <td className="px-2 py-1.5 tabular-nums">{row.forward}</td>
                    <td className="px-2 py-1.5">
                      <span className={TONE_CLASS[row.gateTone]}>{row.gate}</span>
                    </td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${STATE_BADGE[row.stateTone]}`}>
                        {row.stateLabel}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-muted-foreground">{row.contextCoverage}</td>
                    <td className="px-2 py-1.5 text-muted-foreground">{row.lastTrade}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {bands.length > 0 ? (
        <div className="mt-4">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Context score evidence
          </div>
          <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {bands.map((b) => (
              <div key={b.band} className="rounded-xl border border-border/60 bg-muted/20 p-3">
                <div className="text-[11px] text-muted-foreground">Score {b.band.replace("-", "–")}</div>
                <div className="mt-1 text-lg font-semibold tabular-nums">
                  <NumberTicker value={b.trades} />
                </div>
                <div className="text-[10px] text-muted-foreground">forward trades</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {readiness.strategies.map((s) => (
        <details key={s.strategy_id} className="mt-3 rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
          <summary className="cursor-pointer text-[12px] font-medium text-foreground">
            {s.strategy_name} — evidence detail
          </summary>
          <div className="mt-2 space-y-2 text-[12px] text-muted-foreground">
            {s.state_reasons.map((reason, i) => (
              <p key={i}>• {reason}</p>
            ))}
            <p>
              Score bands:{" "}
              {(s.summary?.score_bands || [])
                .map(
                  (b) =>
                    `${b.band} ${b.forward_trades} (${evidenceStatusLabel(b.status).toLowerCase()})`
                )
                .join(" · ")}
            </p>
            <p>
              Regimes:{" "}
              {Object.entries(s.summary?.regime_distribution || {})
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ") || "—"}
            </p>
            <p>
              Recent ({s.summary?.recent_vs_historical.window_days}d) mean{" "}
              {s.summary?.recent_vs_historical.recent_mean ?? "—"} over{" "}
              {s.summary?.recent_vs_historical.recent_n ?? 0} vs historical{" "}
              {s.summary?.recent_vs_historical.historical_mean ?? "—"} over{" "}
              {s.summary?.recent_vs_historical.historical_n ?? 0}
            </p>
            <p>
              Accumulation:{" "}
              {(s.progression || [])
                .slice(-6)
                .map((w) => `${w.week.slice(5)}: ${w.trades}`)
                .join(" · ") || "—"}
            </p>
            <p>
              Observations recorded: {s.forward_observations_recorded} forward ·{" "}
              {s.context_observations_recorded} context · optimization{" "}
              {s.optimization_ready ? "eligible" : "not eligible"}
            </p>
            <p>
              Missing features:{" "}
              {s.missing_features.length > 0 ? s.missing_features.join(", ") : "none listed"}
            </p>
            <p>
              Last cycle:{" "}
              {s.last_cycle
                ? `${s.last_cycle.execution_date} (${s.last_cycle.status}${
                    s.last_cycle.strategy_included ? ", included" : ", not included"
                  })`
                : "no cycle recorded yet"}
            </p>
            {(s.quality_issues || []).length > 0 ? (
              <p>
                Quality flags:{" "}
                {s.quality_issues
                  .map((q) => `${q.code} ×${q.count} (${q.severity})`)
                  .join(" · ")}
              </p>
            ) : null}
          </div>
        </details>
      ))}

      {(readiness.quality_issues || []).length > 0 ? (
        <div className="mt-4 space-y-2">
          {readiness.quality_issues.map((q) => (
            <div
              key={q.code}
              className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-500"
            >
              <AlertTriangle className="size-4 shrink-0" />
              <span>
                <strong>{q.code}</strong> ×{q.count} ({q.severity}) — {q.explanation}{" "}
                <span className="font-mono text-[11px]">{q.sample_refs.join(", ")}</span>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {(readiness.limitations || []).length > 0 ? (
        <div className="mt-3 text-[11px] text-muted-foreground">
          {readiness.limitations.map((note, i) => (
            <p key={i}>• {note}</p>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// header
// ---------------------------------------------------------------------------

function Header({
  overview,
  windowDays,
  onWindow,
  onRefresh,
  busy,
}: {
  overview: LearningOverview;
  windowDays: number;
  onWindow: (days: number) => void;
  onRefresh: () => void;
  busy: boolean;
}) {
  const report = reportHeadline(overview.report);
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <FlaskConical className="size-4 text-muted-foreground" />
            <h2 className="text-base font-semibold tracking-tight">Learning</h2>
          </div>
          <p className="mt-1.5 text-[13px] text-muted-foreground">
            What your past trades say about your strategies. Read only.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select
            size="sm"
            value={String(windowDays)}
            onChange={(v) => onWindow(Number(v))}
            options={[30, 90, 180, 365].map((d) => ({ value: String(d), label: `${d} days` }))}
          />
          <button
            type="button"
            onClick={onRefresh}
            disabled={busy}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border px-2.5 text-xs hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw className={busy ? "size-3.5 animate-spin" : "size-3.5"} />
            Rebuild
          </button>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure
          label="Trades"
          value={<NumberTicker value={overview.dataset.trades} locale />}
          sub={sourceLine(overview)}
        />
        <Figure
          label="Closed"
          value={<NumberTicker value={overview.dataset.closed_trades} locale />}
          sub={overview.dataset.open_trades ? `${overview.dataset.open_trades} still open` : ""}
        />
        <Figure
          label="Net P&L"
          value={
            overview.dataset.net_pnl_total == null ? (
              "—"
            ) : (
              <AnimatedNumber
                value={overview.dataset.net_pnl_total}
                format={(n) =>
                  `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`
                }
              />
            )
          }
          sub={overview.dataset.net_pnl_total == null ? "not measured" : ""}
        />
        <Figure
          label="Strategies"
          value={<NumberTicker value={overview.dataset.strategies.length} />}
        />
      </div>

      <EvidenceStrip summary={overview.dataset} />

      <div className="mt-4 rounded-xl border border-border/60 bg-muted/30 px-3.5 py-2.5">
        <p className={`text-[13px] ${TONE_CLASS[report.tone]}`}>{report.text}</p>
      </div>
    </Card>
  );
}

/**
 * How much of this book is evidence, which is the first thing to read.
 *
 * It sits directly under the headline figures on purpose. Every other number on
 * this panel — the trade count, the P&L, the buckets — reads the same whether
 * the rows behind it were recorded before their outcomes or measured on the
 * history the rules were chosen from. This strip is the one place that
 * difference is visible, so it is rendered from the payload rather than derived
 * in the component, and it states the counts instead of a verdict.
 */
function EvidenceStrip({ summary }: { summary: LearningDatasetSummary }) {
  const evidence = evidenceView(summary);

  return (
    <div className="mt-4 rounded-xl border border-border/60 bg-muted/30 px-3.5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
            How much is proven
          </span>
          <Badge>{evidence.gradeLabel}</Badge>
        </div>
      </div>
      <p className={`mt-1.5 text-[13px] ${TONE_CLASS[evidence.tone]}`}>{evidence.note}</p>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure label="Total" value={<NumberTicker value={evidence.total} locale />} />
        <Figure
          label="Forward paper"
          value={<NumberTicker value={evidence.forward} locale />}
          sub="tested on new days"
        />
        <Figure
          label="Backtested"
          value={<NumberTicker value={evidence.inSample} locale />}
          sub="on past data"
        />
        <Figure
          label="Latest"
          value={evidence.latestForward ?? "—"}
          sub={evidence.latestForward ? "most recent fill" : "none recorded"}
        />
      </div>
    </div>
  );
}

function sourceLine(overview: LearningOverview): string {
  const parts = Object.entries(overview.dataset.sources)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${k.toLowerCase()} ${n}`);
  return parts.length ? parts.join(" · ") : "no source has trades";
}

function Figure({ label, value, sub }: { label: string; value: ReactNode; sub?: string }) {
  return (
    <div className="rounded-xl border border-border/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg tabular-nums">{value}</div>
      {sub ? <div className="text-[11px] text-muted-foreground">{sub}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// empty and missing
// ---------------------------------------------------------------------------

function EmptyBook({ state }: { state: ReturnType<typeof emptyState> }) {
  return (
    <Card className="p-5">
      <div className="flex items-start gap-3">
        <Info className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div>
          <h3 className="text-sm font-medium">{state.title}</h3>
          <p className="mt-1 text-[13px] text-muted-foreground">{state.detail}</p>
          <p className="mt-2 text-[13px] text-muted-foreground">
            Nothing on this screen is a zero. A figure that could not be measured
            is left blank and named, because a printed zero reads as a result.
          </p>
        </div>
      </div>
    </Card>
  );
}

function MissingFeatures({ missing }: { missing: Record<string, string> }) {
  // Build-internal notes (which columns don't exist yet); not useful to read here.
  void missing;
  const entries: [string, string][] = [];
  if (!entries.length) return null;
  return (
    <Card className="p-5">
      <CardHeader
        title="Features that do not exist in this build"
        sub="Recorded per trade rather than assumed, so a blank cell has a reason"
      />
      <ul className="mt-3 space-y-2">
        {entries.map(([feature, reason]) => (
          <li key={feature} className="flex items-start gap-2 text-[13px]">
            <Badge>{feature}</Badge>
            <span className="text-muted-foreground">{reason}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// drift
// ---------------------------------------------------------------------------

function DriftSection({ drift }: { drift: LearningDrift }) {
  const tone = headlineTone(drift.headline);
  return (
    <Card className="p-5">
      <CardHeader
        title="Backtest vs live drift"
        sub="Whether the strategy is doing live what it did in testing"
        action={<Badge>{drift.strategy}</Badge>}
      />
      <p className={`mt-3 text-[13px] ${TONE_CLASS[tone]}`}>{drift.headline}</p>

      {isRefusal(drift.headline) ? (
        <ul className="mt-3 space-y-1 text-[13px] text-muted-foreground">
          {drift.limitations.map((l) => (
            <li key={l}>— {l}</li>
          ))}
        </ul>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        {Object.entries(drift.available_sources).map(([source, n]) => (
          <Badge key={source}>
            {source} {n}
          </Badge>
        ))}
      </div>

      {drift.pairs.map((pair) => (
        <PairTable key={`${pair.reference}-${pair.comparison}`} pair={pair} />
      ))}

      {drift.findings.length ? (
        <div className="mt-4">
          <h4 className="text-[13px] font-medium">Findings</h4>
          <ul className="mt-2 space-y-2">
            {findingViews(orderFindings(drift.findings)).map((f, i) => (
              <li
                key={`${f.kind}-${i}`}
                className="rounded-xl border border-border/60 px-3.5 py-2.5"
              >
                <p className={`text-[13px] ${TONE_CLASS[f.tone]}`}>{f.statement}</p>
                {f.evidence ? (
                  <p className="mt-0.5 text-[12px] text-muted-foreground">{f.evidence}</p>
                ) : null}
                {f.confidence ? (
                  <p className="text-[11px] text-muted-foreground">
                    confidence {f.confidence}
                    {f.sample ? ` · n=${f.sample}` : ""}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

function PairTable({ pair }: { pair: DriftPair }) {
  const blocked = blockedMetrics(pair);
  const unrecorded = unrecordedMetrics(pair);
  return (
    <div className="mt-4">
      <div className="flex items-center justify-between">
        <h4 className="text-[13px] font-medium">
          {pair.reference} ({pair.reference_n}) vs {pair.comparison} ({pair.comparison_n})
        </h4>
        <Badge>{pair.status}</Badge>
      </div>

      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border/60">
              <th className="py-1.5 pr-3 font-normal">metric</th>
              <th className="py-1.5 pr-3 text-right font-normal">baseline</th>
              <th className="py-1.5 pr-3 text-right font-normal">live</th>
              <th className="py-1.5 pr-3 text-right font-normal">delta</th>
              <th className="py-1.5 pr-3 text-right font-normal">n</th>
              <th className="py-1.5 font-normal">verdict</th>
            </tr>
          </thead>
          <tbody>
            {pair.metrics.map((m) => {
              const view = metricView(m);
              return (
                <tr key={m.metric} className="border-b border-border/30 last:border-0">
                  <td className="py-1.5 pr-3">{view.label}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{view.baseline}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{view.live}</td>
                  {/* A withheld delta renders as a dash, never as 0.00. */}
                  <td className="py-1.5 pr-3 text-right tabular-nums">{view.delta}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                    {view.sample}
                  </td>
                  <td className={`py-1.5 ${TONE_CLASS[view.tone]}`}>
                    {view.measured ? (
                      <span className="inline-flex items-center gap-1">
                        {view.direction === "deteriorated" ? (
                          <TrendingDown className="size-3" />
                        ) : view.direction === "improved" ? (
                          <TrendingUp className="size-3" />
                        ) : null}
                        {view.direction} / {view.magnitude}
                      </span>
                    ) : (
                      <span title={view.reason}>{m.status}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {blocked.length ? (
        <div className="mt-2 rounded-xl border border-border/60 bg-muted/30 px-3.5 py-2.5">
          <p className="text-[12px] text-muted-foreground">
            {blocked.length} metric(s) were recorded on both sides but the samples
            are too small to score. These are <strong>not measured</strong>, which
            is not the same as unchanged:
          </p>
          <ul className="mt-1 space-y-0.5 text-[12px] text-muted-foreground">
            {blocked.map((m) => (
              <li key={m.metric}>— {m.label}: {m.reason}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {unrecorded.length ? (
        <p className="mt-2 text-[12px] text-muted-foreground">
          {unrecorded.length} metric(s) were never recorded by one of the sources:{" "}
          {unrecorded.map((m) => m.label).join(", ")}. A missing column is a gap in
          the record, not a null result.
        </p>
      ) : null}

      {typeof pair.regime?.summary === "string" ? (
        <p className="mt-2 text-[12px] text-muted-foreground">
          Regime mix: {String(pair.regime.summary)}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// analysis
// ---------------------------------------------------------------------------

function AnalysisSection({ analysis }: { analysis: LearningAnalysis }) {
  const verdict = sampleVerdict(analysis.n);
  const starved = starvedAxes(analysis);
  return (
    <Card className="p-5">
      <CardHeader
        title="What the conditions say"
        sub={`Sliced on ${analysis.breakdowns.length} axes, ranked on ${analysis.metric}`}
        action={<Badge>{verdict.text}</Badge>}
      />

      {analysis.caveats.length ? (
        <ul className="mt-3 space-y-1">
          {analysis.caveats.map((c) => (
            <li key={c} className="flex items-start gap-1.5 text-[12px] text-amber-500">
              <AlertTriangle className="mt-0.5 size-3 shrink-0" />
              {c}
            </li>
          ))}
        </ul>
      ) : null}

      {analysis.notable.length === 0 ? (
        <p className="mt-3 text-[13px] text-muted-foreground">
          No bucket separated from its baseline after the multiple-comparisons
          correction. That is the expected result on a small sample, and it is
          reported rather than filled with the best-looking bucket.
        </p>
      ) : null}

      {analysis.breakdowns.map((bd) => (
        <div key={bd.axis} className="mt-4">
          <div className="flex items-center justify-between">
            <h4 className="text-[13px] font-medium">{bd.label}</h4>
            <span className="text-[11px] text-muted-foreground">
              coverage {coverageLabel(bd)}
            </span>
          </div>
          {bd.buckets.length === 0 ? (
            <p className="mt-1 text-[12px] text-muted-foreground">
              No trade in the sample carries a value for this axis.
            </p>
          ) : (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-[12px]">
                <thead className="text-left text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="py-1.5 pr-3 font-normal">bucket</th>
                    <th className="py-1.5 pr-3 text-right font-normal">n</th>
                    <th className="py-1.5 pr-3 text-right font-normal">win rate</th>
                    <th className="py-1.5 pr-3 text-right font-normal">mean</th>
                    <th className="py-1.5 pr-3 text-right font-normal">median</th>
                    <th className="py-1.5 pr-3 text-right font-normal">profit factor</th>
                    <th className="py-1.5 pr-3 text-right font-normal">lift</th>
                    <th className="py-1.5 pr-3 font-normal">mean 95% CI</th>
                    <th className="py-1.5 pr-3 font-normal">sample adequacy</th>
                    <th className="py-1.5 font-normal">verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {bucketViews(bd.buckets, analysis.metric).map((b) => (
                    <tr key={b.label} className="border-b border-border/30 last:border-0">
                      <td className="py-1.5 pr-3 font-medium">{b.label}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{b.n}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {b.winRate}
                        {b.winRateCi ? (
                          <span className="ml-1 text-[10px] text-muted-foreground">
                            {b.winRateCi}
                          </span>
                        ) : null}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{b.mean}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{b.median}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{b.profitFactor}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{b.lift}</td>
                      <td className="py-1.5 pr-3 tabular-nums text-muted-foreground">
                        {b.interval || "—"}
                      </td>
                      <td className="py-1.5 pr-3">
                        {b.sampleAdequacy === "adequate" ? (
                          <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-500">
                            Adequate
                          </span>
                        ) : b.sampleAdequacy === "small_sample" ? (
                          <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-500">
                            Small Sample
                          </span>
                        ) : (
                          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            Suppressed
                          </span>
                        )}
                      </td>
                      <td className="py-1.5">
                        {b.suppressed ? (
                          <span className="text-muted-foreground" title={b.note}>
                            suppressed
                          </span>
                        ) : (
                          <span className={b.enoughToJudge ? "" : "text-muted-foreground"}>
                            {b.significance}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}

      {starved.length ? (
        <p className="mt-3 text-[12px] text-muted-foreground">
          {starved.length} axis/axes produced no judgeable bucket:{" "}
          {starved.map((s) => s.axis).join(", ")}.
        </p>
      ) : null}
    </Card>
  );
}

function BacktestVsForwardSection({
  comparison,
}: {
  comparison?: BacktestVsForwardComparison;
}) {
  if (!comparison) return null;
  const pair = comparison.comparison;
  return (
    <Card className="p-5">
      <CardHeader
        title="Backtest vs Paper Forward Comparison"
        sub={`Strategy: ${comparison.strategy} · Objective drift detection without judging strategy as good or bad`}
        action={
          <div className="flex gap-2">
            <Badge>Backtest n={comparison.backtest_n}</Badge>
            <Badge>Forward n={comparison.forward_n}</Badge>
          </div>
        }
      />
      {comparison.limitations.length ? (
        <ul className="mt-3 space-y-1 text-[13px] text-muted-foreground">
          {comparison.limitations.map((l) => (
            <li key={l}>— {l}</li>
          ))}
        </ul>
      ) : null}

      <div className="mt-4">
        <PairTable pair={pair} />
      </div>

      {comparison.findings.length ? (
        <div className="mt-4">
          <h4 className="text-[13px] font-medium">Forward Drift Findings</h4>
          <ul className="mt-2 space-y-2">
            {findingViews(orderFindings(comparison.findings)).map((f, i) => (
              <li
                key={`${f.kind}-${i}`}
                className="rounded-xl border border-border/60 px-3.5 py-2.5"
              >
                <p className={`text-[13px] ${TONE_CLASS[f.tone]}`}>{f.statement}</p>
                {f.evidence ? (
                  <p className="mt-0.5 text-[12px] text-muted-foreground">{f.evidence}</p>
                ) : null}
                {f.confidence ? (
                  <p className="text-[11px] text-muted-foreground">
                    confidence {f.confidence}
                    {f.sample ? ` · n=${f.sample}` : ""}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

function ObservationHistorySection({
  observations,
}: {
  observations?: StoredLearningObservation[];
}) {
  if (!observations || observations.length === 0) return null;

  return (
    <Card className="p-5">
      <CardHeader
        title="Learning Observation History"
        sub="Persistent statistical observations extracted from genuine forward trade evidence"
        action={<Badge>{observations.length} findings</Badge>}
      />
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border/60">
              <th className="py-1.5 pr-3 font-normal">date</th>
              <th className="py-1.5 pr-3 font-normal">strategy</th>
              <th className="py-1.5 pr-3 font-normal">condition / bucket</th>
              <th className="py-1.5 pr-3 text-right font-normal">n</th>
              <th className="py-1.5 pr-3 font-normal">evidence class</th>
              <th className="py-1.5 pr-3 text-right font-normal">confidence</th>
              <th className="py-1.5 font-normal">significance</th>
            </tr>
          </thead>
          <tbody>
            {observations.map((obs) => {
              const res = obs.statistical_result || {};
              const confPct =
                obs.confidence != null ? `${(obs.confidence * 100).toFixed(0)}%` : "—";
              return (
                <tr key={obs.observation_id} className="border-b border-border/30 last:border-0">
                  <td className="py-1.5 pr-3 tabular-nums">{obs.date}</td>
                  <td className="py-1.5 pr-3 font-mono text-[11px]">{obs.strategy_id}</td>
                  <td className="py-1.5 pr-3 font-medium">{obs.condition_bucket}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{obs.sample_size}</td>
                  <td className="py-1.5 pr-3">
                    <span className="rounded border border-border/60 bg-muted/40 px-1.5 py-0.5 text-[10px] font-mono">
                      {obs.evidence_class}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums">{confPct}</td>
                  <td className="py-1.5 text-muted-foreground">
                    {res.significance || "insufficient_sample"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// report
// ---------------------------------------------------------------------------

function ReportSection({ report }: { report: LearningReport }) {
  const view = reportHeadline(report);
  return (
    <Card className="p-5">
      <CardHeader
        title="Daily report"
        sub={`${report.as_of} · ${report.window_days}d baseline`}
        action={<Badge>{report.trades_today} closed today</Badge>}
      />
      <p className={`mt-3 text-[13px] ${TONE_CLASS[view.tone]}`}>{view.text}</p>

      {report.observations.length ? (
        <div className="mt-4">
          <h4 className="text-[13px] font-medium">
            Observations
            <span className="ml-2 text-[11px] font-normal text-muted-foreground">
              advisory — no parameter value is proposed
            </span>
          </h4>
          <ul className="mt-2 space-y-2">
            {report.observations.map((o, i) => (
              <li key={`${o.kind}-${i}`} className="rounded-xl border border-border/60 px-3.5 py-2.5">
                <p className="text-[13px]">{o.statement}</p>
                <p className="mt-0.5 text-[12px] text-muted-foreground">{o.evidence}</p>
                <p className="text-[11px] text-muted-foreground">
                  confidence {o.confidence} · n={o.sample_size}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.unusual.length ? (
        <div className="mt-4">
          <h4 className="text-[13px] font-medium">Unusual</h4>
          <ul className="mt-1 space-y-1 text-[12px] text-muted-foreground">
            {report.unusual.map((u) => (
              <li key={u}>— {u}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

function Limitations({ limitations }: { limitations: string[] }) {
  if (!limitations.length) return null;
  return (
    <Card className="p-5">
      <CardHeader
        title="What this screen could not measure"
        sub="Every withheld figure has a reason, listed here rather than hidden"
      />
      <ul className="mt-3 space-y-1.5">
        {limitations.map((l) => (
          <li key={l} className="text-[12px] text-muted-foreground">
            — {l}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function DailyLearningCycleSection({
  cycle,
  running,
  onRunCycle,
  message,
}: {
  cycle?: DailyLearningCycleReport | null;
  running: boolean;
  onRunCycle: () => void;
  message?: string;
}) {
  const statusColor =
    cycle?.status === "SUCCESS"
      ? "text-emerald-500 border-emerald-500/20 bg-emerald-500/10"
      : cycle?.status === "PARTIAL" || cycle?.status === "INSUFFICIENT_SAMPLE"
      ? "text-amber-500 border-amber-500/20 bg-amber-500/10"
      : cycle?.status === "ERROR"
      ? "text-destructive border-destructive/20 bg-destructive/10"
      : "text-muted-foreground border-border/60 bg-muted/20";

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold tracking-tight">Latest Learning Cycle</h3>
            {cycle ? (
              <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${statusColor}`}>
                {cycle.status}
              </span>
            ) : (
              <Badge>No run recorded</Badge>
            )}
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Automated post-session learning cycle: collects genuine forward paper trades, generates hypotheses, and updates research queue.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right text-[11px] text-muted-foreground">
            <div>Next scheduled: <span className="font-medium text-foreground">Post-session (15:45 IST)</span></div>
            {cycle?.completed_at ? (
              <div>Last run: <span className="font-medium text-foreground">{new Date(cycle.completed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ({cycle.runtime_seconds}s)</span></div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onRunCycle}
            disabled={running}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-background px-3 text-xs font-medium hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw className={running ? "size-3 animate-spin" : "size-3"} />
            {running ? "Running Cycle…" : "Run Daily Cycle"}
          </button>
        </div>
      </div>

      {message ? (
        <div className="mt-3 rounded-lg border border-border/60 bg-muted/40 px-3 py-1.5 text-[12px] font-mono">
          {message}
        </div>
      ) : null}

      {cycle ? (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
            <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
              <div className="text-[11px] text-muted-foreground uppercase tracking-wide">Trades Processed</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.trades_processed}</div>
              <div className="text-[10px] text-muted-foreground">Forward paper</div>
            </div>
            <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
              <div className="text-[11px] text-muted-foreground uppercase tracking-wide">New Observations</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.observations_generated}</div>
              <div className="text-[10px] text-muted-foreground">n ≥ 10 & significant</div>
            </div>
            <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
              <div className="text-[11px] text-muted-foreground uppercase tracking-wide">Hypotheses</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.hypotheses_generated}</div>
              <div className="text-[10px] text-muted-foreground">Research candidates</div>
            </div>
            <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
              <div className="text-[11px] text-muted-foreground uppercase tracking-wide">Optimization Candidates</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.candidates_generated}</div>
              <div className="text-[10px] text-muted-foreground">Adaptive parameters</div>
            </div>
            <div className="rounded-xl border border-border/60 bg-muted/20 p-3">
              <div className="text-[11px] text-muted-foreground uppercase tracking-wide">Recommendations</div>
              <div className="mt-1 text-lg font-semibold tabular-nums text-primary">{cycle.recommendations_generated}</div>
              <div className="text-[10px] text-muted-foreground">PROPOSED / In queue</div>
            </div>
          </div>

          {/* Strategy diagnostic callouts */}
          <div className="mt-4 space-y-2">
            {cycle.insufficient_data_strategies && cycle.insufficient_data_strategies.length > 0 ? (
              <div className="flex items-center gap-2 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-500">
                <AlertTriangle className="size-4 shrink-0" />
                <span>
                  <strong>Insufficient Forward Data (n &lt; 10):</strong>{" "}
                  {cycle.insufficient_data_strategies.join(", ")} — withheld from candidate generation to prevent false learning.
                </span>
              </div>
            ) : null}

            {cycle.drift_detected && Object.entries(cycle.drift_detected).some(([_, detected]) => detected) ? (
              <div className="flex items-center gap-2 rounded-lg border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-500">
                <AlertTriangle className="size-4 shrink-0" />
                <span>
                  <strong>Statistical Drift Detected:</strong>{" "}
                  {Object.entries(cycle.drift_detected)
                    .filter(([_, d]) => d)
                    .map(([s]) => s)
                    .join(", ")} — forward distributions deviate from historical baseline.
                </span>
              </div>
            ) : null}

            {cycle.notes && cycle.notes.length > 0 ? (
              <div className="rounded-lg border border-border/60 bg-muted/20 p-3 text-[12px]">
                <div className="font-medium text-foreground mb-1">Cycle Notes & Explanations:</div>
                <ul className="space-y-1 text-muted-foreground">
                  {cycle.notes.map((note, idx) => (
                    <li key={idx} className="flex items-start gap-1.5">
                      <span className="text-muted-foreground select-none">•</span>
                      <span>{note}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {cycle.errors && cycle.errors.length > 0 ? (
              <div className="rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-[12px] text-destructive">
                <div className="font-medium mb-1">Cycle Errors:</div>
                <ul className="space-y-1">
                  {cycle.errors.map((err, idx) => (
                    <li key={idx} className="flex items-start gap-1.5">
                      <span>✕</span>
                      <span>{err}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </>
      ) : (
        <div className="mt-3 rounded-lg border border-dashed border-border/60 p-4 text-center text-[12px] text-muted-foreground">
          No automated cycle has executed yet. Click &quot;Run Daily Cycle&quot; to process available forward paper trades.
        </div>
      )}
    </Card>
  );
}

