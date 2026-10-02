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
import { Button } from "./components/ui/button";

const TONE_CLASS: Record<Tone, string> = {
  good: "text-gain",
  bad: "text-destructive",
  warn: "text-warning",
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
      const [overviewRes, readinessRes] = await Promise.allSettled([
        learningOverview(windowDays),
        learningReadiness(),
      ]);

      if (overviewRes.status === "fulfilled") {
        setOverview(overviewRes.value);
      setError("");
      } else {
        setError(
          overviewRes.reason instanceof Error
            ? overviewRes.reason.message
            : "could not load the learning report"
        );
      }

      if (readinessRes.status === "fulfilled") {
        setReadiness(readinessRes.value);
        setReadinessError("");
      } else {
        setReadiness(null);
        setReadinessError(
          readinessRes.reason instanceof Error
            ? readinessRes.reason.message
            : "could not load readiness"
        );
      }
    } finally {
      setBusy(false);
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
          <span title="Every figure below is withheld rather than guessed.">Small sample — figures withheld</span>
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
  good: "text-gain border-gain/20 bg-gain/[0.08]",
  bad: "text-destructive border-destructive/20 bg-destructive/[0.08]",
  warn: "text-warning border-warning/20 bg-warning/[0.08]",
  muted: "text-muted-foreground border-border/60 bg-muted/40",
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
        <p className="mt-1 text-xs text-muted-foreground">
          Readiness unavailable: {error}
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
        <h3
          className="text-sm font-semibold tracking-tight"
          title={(readiness.limitations || []).join(" • ") || "Labels only: nothing here changes trading, strategies or risk."}
        >
          Learning Readiness
        </h3>
        <Badge>Research only</Badge>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
        <div className="text-caption uppercase tracking-wide text-muted-foreground">Strategies tracked</div>
          <div className="mt-1 text-lg font-semibold tabular-nums">
            <NumberTicker value={totals.strategies} />
          </div>
          <div className="text-micro text-muted-foreground">{totals.forward} forward trades</div>
        </div>
        {states.map((s) => (
          <div key={s.label} className="rounded-lg border border-border/60 bg-muted/20 p-3">
          <div className="text-caption uppercase tracking-wide text-muted-foreground">{s.label}</div>
            <div className={`mt-1 text-lg font-semibold tabular-nums ${TONE_CLASS[s.tone]}`}>
              <NumberTicker value={s.count} />
            </div>
            <div className="text-micro text-muted-foreground">strategies</div>
          </div>
        ))}
      </div>

      {readiness.strategies.length === 0 ? (
        <div className="mt-4 rounded-md border border-dashed border-border/60 p-4 text-center text-xs text-muted-foreground">
          <span title="The first closed paper trade starts the count; 10 clears the minimum, 50 opens analysis.">No forward trades yet</span>
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border/60 text-left text-micro uppercase tracking-wider text-muted-foreground">
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Strategy</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Forward trades</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Next gate</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Context</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Last trade</th>
              </tr>
            </thead>
            <tbody>
              {readiness.strategies.map((s) => {
                const row = readinessRowView(s);
                return (
                  <tr key={row.id} className="border-b border-border/40 last:border-0">
                    <td className="px-3 py-1.5">
                      <div className="font-medium text-foreground">{row.name}</div>
                      <div className="text-micro text-muted-foreground">
                        {row.version}
                        {s.deployment ? ` · ${s.deployment.mode} ${s.deployment.status}` : ""}
                        {s.open_forward_trades > 0 ? ` · ${s.open_forward_trades} open` : ""}
                      </div>
                    </td>
                    <td className="px-3 py-1.5 tabular-nums">{row.forward}</td>
                    <td className="px-3 py-1.5">
                      <span className={TONE_CLASS[row.gateTone]}>{row.gate}</span>
                    </td>
                    <td className="px-3 py-1.5">
                    <span className={`rounded-full border px-2 py-0.5 text-micro font-medium uppercase tracking-wider ${STATE_BADGE[row.stateTone]}`}>
                        {row.stateLabel}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">{row.contextCoverage}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{row.lastTrade}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {bands.length > 0 ? (
        <div className="mt-4">
          <div className="text-caption font-medium uppercase tracking-wide text-muted-foreground">
            Context score evidence
          </div>
          <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {bands.map((b) => (
              <div key={b.band} className="rounded-lg border border-border/60 bg-muted/20 p-3">
              <div className="text-caption text-muted-foreground">Score {b.band.replace("-", "–")}</div>
                <div className="mt-1 text-lg font-semibold tabular-nums">
                  <NumberTicker value={b.trades} />
                </div>
                <div className="text-micro text-muted-foreground">forward trades</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {readiness.strategies.map((s) => (
        <details key={s.strategy_id} className="mt-3 rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-foreground">
            {s.strategy_name} — evidence detail
          </summary>
          <div className="mt-2 space-y-2 text-xs text-muted-foreground">
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
              className="flex items-start gap-2 rounded-md border border-warning/20 bg-warning/[0.08] px-3 py-2 text-xs text-warning"
            >
              <AlertTriangle className="size-4 shrink-0" />
              <span title={`${q.explanation} ${q.sample_refs.join(", ")}`}>
                <strong>{q.code}</strong> ×{q.count} ({q.severity})
              </span>
            </div>
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
        </div>
        <div className="flex items-center gap-2">
          <Select
            size="sm"
            value={String(windowDays)}
            onChange={(v) => onWindow(Number(v))}
            options={[30, 90, 180, 365].map((d) => ({ value: String(d), label: `${d} days` }))}
          />
          <Button size="xs" variant="quiet" onClick={onRefresh} disabled={busy}>
            <RefreshCw className={busy ? "size-3.5 animate-spin" : "size-3.5"} />
            Rebuild
          </Button>
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

      <div className="mt-4 rounded-lg border border-border/60 bg-muted/30 px-3.5 py-2.5">
        <p className={`text-body ${TONE_CLASS[report.tone]}`}>{report.text}</p>
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
    <div className="mt-4 rounded-lg border border-border/60 bg-muted/30 px-3.5 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="text-caption uppercase tracking-wide text-muted-foreground">
            How much is proven
          </span>
        </div>
      </div>
      <p className={`mt-1.5 text-body ${TONE_CLASS[evidence.tone]}`} title={evidence.note}>
        {evidence.gradeLabel}
      </p>
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
    <div className="rounded-lg border border-border/60 px-3 py-2">
      <div className="text-caption uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg tabular-nums">{value}</div>
      {sub ? <div className="text-caption text-muted-foreground">{sub}</div> : null}
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
          <p className="mt-1 text-body text-muted-foreground" title={state.detail}>Not enough trades yet</p>
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
          <li key={feature} className="flex items-start gap-2 text-body">
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
        action={<Badge>{drift.strategy}</Badge>}
      />
      <p
        className={`mt-3 text-body ${TONE_CLASS[tone]}`}
        title={isRefusal(drift.headline) ? drift.limitations.join(" — ") : undefined}
      >
        {drift.headline}
      </p>

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
          <h4 className="text-body font-medium">Findings</h4>
          <ul className="mt-2 space-y-2">
            {findingViews(orderFindings(drift.findings)).map((f, i) => (
              <li
                key={`${f.kind}-${i}`}
                className="rounded-lg border border-border/60 px-3.5 py-2.5"
              >
                <p className={`text-body ${TONE_CLASS[f.tone]}`} title={f.evidence || undefined}>{f.statement}</p>
                {f.confidence ? (
                  <p className="text-caption text-muted-foreground">
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
        <h4 className="text-body font-medium">
          {pair.reference} ({pair.reference_n}) vs {pair.comparison} ({pair.comparison_n})
        </h4>
        <Badge>{pair.status}</Badge>
      </div>

      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border/60">
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">metric</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">baseline</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">live</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">delta</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">n</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">verdict</th>
            </tr>
          </thead>
          <tbody>
            {pair.metrics.map((m) => {
              const view = metricView(m);
              return (
                <tr key={m.metric} className="border-b border-border/30 last:border-0">
                  <td className="px-3 py-1.5">{view.label}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{view.baseline}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{view.live}</td>
                  {/* A withheld delta renders as a dash, never as 0.00. */}
                  <td className="px-3 py-1.5 text-right tabular-nums">{view.delta}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
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
        <p
          className="mt-2 text-xs text-muted-foreground"
          title={blocked.map((m) => `${m.label}: ${m.reason}`).join("\n")}
        >
          {blocked.length} too small to score
        </p>
      ) : null}

      {unrecorded.length ? (
        <p
          className="mt-2 text-xs text-muted-foreground"
          title={unrecorded.map((m) => m.label).join(", ")}
        >
          {unrecorded.length} not recorded
        </p>
      ) : null}

      {typeof pair.regime?.summary === "string" ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {String(pair.regime.summary)}
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
        sub={analysis.metric}
        action={<Badge>{verdict.text}</Badge>}
      />

      {analysis.caveats.length ? (
        <p
          className="mt-3 flex items-center gap-1.5 text-xs text-warning"
          title={analysis.caveats.join("\n")}
        >
          <AlertTriangle className="size-3 shrink-0" />
          {analysis.caveats.length} caveat{analysis.caveats.length === 1 ? "" : "s"}
        </p>
      ) : null}

      {analysis.notable.length === 0 ? (
        <p
          className="mt-3 text-body text-muted-foreground"
          title="No bucket separated from its baseline after multiple-comparisons correction."
        >
          No significant bucket
        </p>
      ) : null}

      {analysis.breakdowns.map((bd) => {
        // One summary line per condition. The table opens only when some bucket is big enough to
        // judge; with a small sample nearly every bucket is "suppressed" and the tables are noise.
        const views = bucketViews(bd.buckets, analysis.metric);
        const judgeable = views.filter((b) => !b.suppressed && b.enoughToJudge).length;
        return (
        <details key={bd.axis} className="mt-3 rounded-lg border border-border/60" open={judgeable > 0 && views.length > 1}>
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2">
            <h4 className="text-body font-medium">{bd.label}</h4>
            <span className="text-caption text-muted-foreground">
              {bd.buckets.length === 0
                ? "no data"
                : views.length === 1
                  ? "1 group"
                  : judgeable > 0
                    ? `${judgeable} of ${views.length} judgeable`
                    : "too few trades"}
              {" · "}
              {coverageLabel(bd)}
            </span>
          </summary>
          {bd.buckets.length === 0 ? (
            <p className="px-3 pb-2 text-xs text-muted-foreground">
              No data
            </p>
          ) : (
            <div className="overflow-x-auto border-t border-border/60">
              <table className="w-full text-xs">
                <thead className="text-left text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">bucket</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">n</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">win rate</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">mean</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">median</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">profit factor</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">lift</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">mean 95% CI</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">sample adequacy</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {views.map((b) => (
                    <tr key={b.label} className="border-b border-border/30 last:border-0">
                      <td className="px-3 py-1.5 font-medium">{b.label}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{b.n}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {b.winRate}
                        {b.winRateCi ? (
                          <span className="ml-1 text-micro text-muted-foreground">
                            {b.winRateCi}
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{b.mean}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{b.median}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{b.profitFactor}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{b.lift}</td>
                      <td className="px-3 py-1.5 tabular-nums text-muted-foreground">
                        {b.interval || "—"}
                      </td>
                      <td className="px-3 py-1.5">
                        {b.sampleAdequacy === "adequate" ? (
                          <span className="rounded-md border border-gain/20 bg-gain/[0.08] px-1.5 py-0.5 text-micro font-medium text-gain">
                            Adequate
                          </span>
                        ) : b.sampleAdequacy === "small_sample" ? (
                          <span className="rounded-md border border-warning/20 bg-warning/[0.08] px-1.5 py-0.5 text-micro font-medium text-warning">
                            Small Sample
                          </span>
                        ) : (
                          <span className="rounded-md bg-muted px-1.5 py-0.5 text-micro text-muted-foreground">
                            Suppressed
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
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
        </details>
        );
      })}

      {starved.length ? (
        <p
          className="mt-3 text-xs text-muted-foreground"
          title={starved.map((s) => s.axis).join(", ")}
        >
          {starved.length} not enough data
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
        sub={comparison.strategy}
        action={
          <div className="flex gap-2">
            <Badge>Backtest n={comparison.backtest_n}</Badge>
            <Badge>Forward n={comparison.forward_n}</Badge>
          </div>
        }
      />
      {comparison.limitations.length ? (
        <p
          className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground"
          title={comparison.limitations.join("\n")}
        >
          <Info className="size-3 shrink-0" />
          {comparison.limitations.length} note{comparison.limitations.length === 1 ? "" : "s"}
        </p>
      ) : null}

      <div className="mt-4">
        <PairTable pair={pair} />
      </div>

      {comparison.findings.length ? (
        <div className="mt-4">
          <h4 className="text-body font-medium">Forward Drift Findings</h4>
          <ul className="mt-2 space-y-2">
            {findingViews(orderFindings(comparison.findings)).map((f, i) => (
              <li
                key={`${f.kind}-${i}`}
                className="rounded-lg border border-border/60 px-3.5 py-2.5"
              >
                <p className={`text-body ${TONE_CLASS[f.tone]}`} title={f.evidence || undefined}>{f.statement}</p>
                {f.confidence ? (
                  <p className="text-caption text-muted-foreground">
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
        action={<Badge>{observations.length} findings</Badge>}
      />
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border/60">
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">date</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">strategy</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">condition / bucket</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">n</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">evidence class</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">confidence</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">significance</th>
            </tr>
          </thead>
          <tbody>
            {observations.map((obs) => {
              const res = obs.statistical_result || {};
              const confPct =
                obs.confidence != null ? `${(obs.confidence * 100).toFixed(0)}%` : "—";
              return (
                <tr key={obs.observation_id} className="border-b border-border/30 last:border-0">
                  <td className="px-3 py-1.5 tabular-nums">{obs.date}</td>
                  <td className="px-3 py-1.5 font-mono text-caption">{obs.strategy_id}</td>
                  <td className="px-3 py-1.5 font-medium">{obs.condition_bucket}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{obs.sample_size}</td>
                  <td className="px-3 py-1.5">
                    <span className="rounded-lg border border-border/60 bg-muted/40 px-1.5 py-0.5 text-micro font-mono">
                      {obs.evidence_class}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{confPct}</td>
                  <td className="px-3 py-1.5 text-muted-foreground">
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
      <p className={`mt-3 text-body ${TONE_CLASS[view.tone]}`}>{view.text}</p>

      {report.observations.length ? (
        <div className="mt-4">
          <h4 className="text-body font-medium">
            Observations
          </h4>
          <ul className="mt-2 space-y-2">
            {report.observations.map((o, i) => (
              <li key={`${o.kind}-${i}`} className="rounded-lg border border-border/60 px-3.5 py-2.5">
                <p className="text-body" title={o.evidence}>{o.statement}</p>
                <p className="text-caption text-muted-foreground">
                  confidence {o.confidence} · n={o.sample_size}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.unusual.length ? (
        <div className="mt-4">
          <h4 className="text-body font-medium" title={report.unusual.join("\n")}>
            Unusual ({report.unusual.length})
          </h4>
        </div>
      ) : null}
    </Card>
  );
}

function Limitations({ limitations }: { limitations: string[] }) {
  if (!limitations.length) return null;
  return (
    <p
      className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground"
      title={limitations.join("\n")}
    >
      <Info className="size-3 shrink-0" />
      {limitations.length} not measured
    </p>
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
      ? "text-gain border border-gain/20 bg-gain/[0.08]"
      : cycle?.status === "PARTIAL" || cycle?.status === "INSUFFICIENT_SAMPLE"
        ? "text-warning border border-warning/20 bg-warning/[0.08]"
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
              <span className={`rounded-full border px-2 py-0.5 text-micro font-medium uppercase tracking-wider ${statusColor}`}>
                {cycle.status}
              </span>
            ) : (
              <Badge>No run recorded</Badge>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right text-caption text-muted-foreground">
            <div>Next scheduled: <span className="font-medium text-foreground">Post-session (15:45 IST)</span></div>
            {cycle?.completed_at ? (
              <div>Last run: <span className="font-medium text-foreground">{new Date(cycle.completed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ({cycle.runtime_seconds}s)</span></div>
            ) : null}
          </div>
          <Button size="xs" variant="outline" onClick={onRunCycle} disabled={running}>
            <RefreshCw className={running ? "size-3 animate-spin" : "size-3"} />
            {running ? "Running cycle…" : "Run daily cycle"}
          </Button>
        </div>
      </div>

      {message ? (
        <div className="mt-3 rounded-lg border border-border/60 bg-muted/40 px-3 py-1.5 text-xs font-mono">
          {message}
        </div>
      ) : null}

      {cycle ? (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
            <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
            <div className="text-caption text-muted-foreground uppercase tracking-wide">Trades Processed</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.trades_processed}</div>
            </div>
            <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
            <div className="text-caption text-muted-foreground uppercase tracking-wide">New Observations</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.observations_generated}</div>
            </div>
            <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
            <div className="text-caption text-muted-foreground uppercase tracking-wide">Hypotheses</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.hypotheses_generated}</div>
            </div>
            <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
            <div className="text-caption text-muted-foreground uppercase tracking-wide">Optimization Candidates</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">{cycle.candidates_generated}</div>
            </div>
            <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
            <div className="text-caption text-muted-foreground uppercase tracking-wide">Recommendations</div>
              <div className="mt-1 text-lg font-semibold tabular-nums text-primary">{cycle.recommendations_generated}</div>
            </div>
          </div>

          {/* Strategy diagnostic callouts */}
          <div className="mt-4 space-y-2">
            {cycle.insufficient_data_strategies && cycle.insufficient_data_strategies.length > 0 ? (
              <div className="flex items-center gap-2 rounded-md border border-warning/20 bg-warning/[0.08] px-3 py-2 text-xs text-warning">
                <AlertTriangle className="size-4 shrink-0" />
                <span>
                  <strong>n &lt; 10:</strong>{" "}
                  {cycle.insufficient_data_strategies.join(", ")}
                </span>
              </div>
            ) : null}

            {cycle.drift_detected && Object.entries(cycle.drift_detected).some(([_, detected]) => detected) ? (
              <div className="flex items-center gap-2 rounded-md border border-destructive/20 bg-destructive/[0.08] px-3 py-2 text-xs text-destructive">
                <AlertTriangle className="size-4 shrink-0" />
                <span title="Forward distributions deviate from historical baseline.">
                  <strong>Drift:</strong>{" "}
                  {Object.entries(cycle.drift_detected)
                    .filter(([_, d]) => d)
                    .map(([s]) => s)
                    .join(", ")}
                </span>
              </div>
            ) : null}

            {cycle.notes && cycle.notes.length > 0 ? (
              <div
                className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2 text-xs text-muted-foreground"
                title={cycle.notes.join("\n")}
              >
                {cycle.notes.length} note{cycle.notes.length === 1 ? "" : "s"}
              </div>
            ) : null}

            {cycle.errors && cycle.errors.length > 0 ? (
              <div className="rounded-md border border-destructive/20 bg-destructive/10 p-3 text-xs text-destructive">
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
        <div className="mt-3 rounded-md border border-dashed border-border/60 p-4 text-center text-xs text-muted-foreground">
          No cycle run yet
        </div>
      )}
    </Card>
  );
}

