/**
 * Pure derivations for the learning dashboard.
 *
 * The whole reason this is a separate module rather than inline JSX: a learning
 * screen is easy to make *reassuring* by accident, and reassuring-by-accident is
 * the one failure mode that matters here. Three rules are enforced here, and
 * each has a test:
 *
 * 1. **Absent is not zero.** `delta: null` with `status: "insufficient"` renders
 *    as "not measured", never as `0.00`. A zero delta means "no change"; a null
 *    delta means "we could not tell", and those are opposite conclusions.
 * 2. **Caveats are not optional.** A bucket below the sample floor, a strategy
 *    whose edge is one trade, a backtest-only sample — each must produce a
 *    visible marker. A tidy table with a hidden caveat is a lie of omission.
 * 3. **Nothing here proposes a change.** No function returns a parameter value.
 *    The engine may propose; this screen may not.
 */

import type {
  DriftFinding,
  DriftMetric,
  DriftPair,
  LearningAnalysis,
  LearningBreakdown,
  LearningBucket,
  LearningDatasetSummary,
  LearningReport,
} from "../api";

// ---------------------------------------------------------------------------
// verdict tone
// ---------------------------------------------------------------------------

export type Tone = "good" | "bad" | "warn" | "muted";

/**
 * The tone for a source's headline.
 *
 * Deliberately matched on the *refusals* first, and searched for the negated
 * phrases before the positive ones. `"no drift detected"` contains
 * `"drift detected"` as a substring, so checking the positive form first tones
 * a clean bill of health as bad news — the exact inversion this function exists
 * to prevent. Longest-match-first is not a nicety here; it is the correctness
 * condition.
 */
export function headlineTone(headline: string): Tone {
  const text = (headline || "").toLowerCase();

  // Refusals first: "we could not check" must not look like "it is fine", and
  // must not look like a finding either.
  if (isRefusal(headline)) return "muted";

  // Negated positives before positives.
  if (text.includes("no drift detected")) return "good";
  if (text.includes("no material drift")) return "warn";
  if (text.includes("drift detected")) return "bad";

  return "muted";
}

/** Whether a headline is a *refusal* rather than a finding. */
export function isRefusal(headline: string): boolean {
  const text = (headline || "").toLowerCase();
  return (
    text.includes("could not be measured") ||
    text.includes("cannot be measured") ||
    text.includes("not yet measurable") ||
    text.includes("needs two to compare") ||
    text.includes("nothing to compare") ||
    text.includes("no strategy appears in more than one")
  );
}

// ---------------------------------------------------------------------------
// metric rendering
// ---------------------------------------------------------------------------

/** How a metric's numbers should appear. Absent metrics get no numbers at all. */
export interface MetricView {
  metric: string;
  label: string;
  /** True when the two sides were measured and are comparable. */
  measured: boolean;
  /** Human reason when not measured — always non-empty in that case. */
  reason: string;
  baseline: string;
  live: string;
  delta: string;
  /** '' when not measured. Never '+0.00'. */
  relative: string;
  direction: string;
  magnitude: string;
  tone: Tone;
  sample: string;
}

const NOT_MEASURED = "\u2014"; // em dash: a real glyph, not an empty cell

/**
 * Turn one drift metric into display strings.
 *
 * The unit matters and is not interchangeable: expectancy is rupees, return is
 * percent, slippage is basis points. Printing them with one formatter is how a
 * 0.4% return gets read as ₹0.40.
 */
export function metricView(m: DriftMetric): MetricView {
  const measured = m.status === "ok";

  const unit = (value: number | null): string => {
    if (value == null) return NOT_MEASURED;
    if (m.metric === "win_rate") return `${(value * 100).toFixed(1)}%`;
    if (m.metric === "expectancy_per_trade") return `\u20b9${value.toFixed(2)}`;
    if (m.metric === "mean_return_pct") return `${value.toFixed(2)}%`;
    if (m.metric === "slippage_bps") return `${value.toFixed(1)} bps`;
    if (m.metric === "duration_days") return `${value.toFixed(2)} d`;
    return value.toFixed(2);
  };

  const sample =
    m.reference_n && m.comparison_n
      ? `${m.reference_n} vs ${m.comparison_n}`
      : `${m.reference_n || 0} vs ${m.comparison_n || 0}`;

  if (!measured) {
    return {
      metric: m.metric,
      label: m.label,
      measured: false,
      reason: m.reason || "not measured",
      baseline: m.reference_value == null ? NOT_MEASURED : unit(m.reference_value),
      live: m.comparison_value == null ? NOT_MEASURED : unit(m.comparison_value),
      delta: NOT_MEASURED,
      relative: "",
      direction: "unknown",
      magnitude: "unknown",
      tone: "muted",
      sample,
    };
  }

  const deltaSign = m.delta != null && m.delta > 0 ? "+" : "";
  const tone: Tone =
    m.direction === "deteriorated"
      ? m.magnitude === "within noise"
        ? "warn"
        : "bad"
      : m.direction === "improved"
        ? "good"
        : "muted";

  return {
    metric: m.metric,
    label: m.label,
    measured: true,
    reason: m.reason || "",
    baseline: m.reference_value == null ? NOT_MEASURED : unit(m.reference_value),
    live: m.comparison_value == null ? NOT_MEASURED : unit(m.comparison_value),
    delta: m.delta == null ? NOT_MEASURED : `${deltaSign}${unit(m.delta)}`,
    relative:
      m.relative == null ? "" : `${m.relative > 0 ? "+" : ""}${(m.relative * 100).toFixed(1)}%`,
    direction: m.direction,
    magnitude: m.magnitude,
    tone,
    sample,
  };
}

/** Metrics that were measured on both sides but could not be scored. */
export function blockedMetrics(pair: DriftPair): DriftMetric[] {
  return pair.metrics.filter(
    (m) =>
      m.status === "insufficient" && m.reference_value != null && m.comparison_value != null,
  );
}

/** Metrics neither source recorded — a data gap, not a null result. */
export function unrecordedMetrics(pair: DriftPair): DriftMetric[] {
  return pair.metrics.filter((m) => m.status === "absent");
}

// ---------------------------------------------------------------------------
// findings
// ---------------------------------------------------------------------------

export interface FindingView {
  kind: string;
  severity: string;
  tone: Tone;
  statement: string;
  evidence: string;
  confidence: string;
  sample: string;
}

export function findingViews(findings: DriftFinding[]): FindingView[] {
  return (findings || []).map((f) => ({
    kind: f.kind,
    severity: f.severity,
    tone: f.severity === "warning" ? "bad" : f.severity === "info" ? "warn" : "muted",
    statement: f.statement,
    evidence: f.evidence || "",
    confidence: f.confidence || "",
    sample: f.sample_size != null ? String(f.sample_size) : "",
  }));
}

/**
 * Order findings so the ones worth acting on come first.
 *
 * A regime mismatch outranks a metric drift: if the conditions differed, the
 * metric differences below it are partly explained by that, and an operator who
 * reads the metric first has already drawn the wrong conclusion.
 */
export function orderFindings(findings: DriftFinding[]): DriftFinding[] {
  const rank = (f: DriftFinding): number => {
    if (f.kind === "regime_mismatch") return 0;
    if (f.kind === "multiple_strategies") return 1;
    if (f.severity === "warning") return 2;
    if (f.severity === "info") return 3;
    return 4;
  };
  return [...(findings || [])].sort((a, b) => rank(a) - rank(b));
}

// ---------------------------------------------------------------------------
// buckets
// ---------------------------------------------------------------------------

export interface BucketView {
  label: string;
  n: number;
  mean: string;
  median: string;
  winRate: string;
  winRateCi: string;
  profitFactor: string;
  lift: string;
  interval: string;
  significance: string;
  suppressed: boolean;
  sampleAdequacy: "adequate" | "small_sample" | "insufficient";
  isMeaningful: boolean;
  note: string;
  /** A bucket with n=1 is never shown as evidence, however good the number. */
  enoughToJudge: boolean;
}

/**
 * Render one figure in the metric's own unit.
 *
 * `net_pnl` is money and gets a rupee sign; `return_pct` is a percentage and
 * gets a percent sign. A shared formatter would put `1.19` in front of a reader
 * who has no way to tell whether that is rupees or percent — and the paper
 * ledger, which is where the current evidence lives, is returns-only, so the
 * percentage branch is the one that actually renders today.
 */
function amount(value: number | null | undefined, metric: string): string {
  if (value == null || Number.isNaN(value)) return NOT_MEASURED;
  if (metric === "return_pct") return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
  return `\u20b9${value.toFixed(2)}`;
}

export function bucketViews(buckets: LearningBucket[], metric = "net_pnl"): BucketView[] {
  return (buckets || []).map((b) => {
    const stats = (b.stats || {}) as Record<string, any>;
    const interval = stats.mean_ci;
    const wr = stats.win_rate;
    const wrCi = stats.win_rate_ci;
    const pf = stats.profit_factor;
    const isSmall = Boolean(stats.small_sample);
    const suppressed = Boolean(b.suppressed);
    const sampleAdequacy: "adequate" | "small_sample" | "insufficient" =
      !suppressed && !isSmall ? "adequate" : !suppressed ? "small_sample" : "insufficient";

    return {
      label: b.label,
      n: b.n,
      mean: amount(stats.mean, metric),
      median: amount(stats.median, metric),
      winRate: wr != null ? `${(wr * 100).toFixed(1)}%` : NOT_MEASURED,
      winRateCi:
        Array.isArray(wrCi) && wrCi.length === 2
          ? `[${(wrCi[0] * 100).toFixed(1)}% \u2026 ${(wrCi[1] * 100).toFixed(1)}%]`
          : "",
      profitFactor: pf != null ? Number(pf).toFixed(2) : "—",
      lift: b.lift == null ? NOT_MEASURED : amount(b.lift, metric),
      interval:
        Array.isArray(interval) && interval.length === 2
          ? `[${amount(interval[0], metric)} \u2026 ${amount(interval[1], metric)}]`
          : "",
      significance: b.significance || "not_tested",
      suppressed,
      sampleAdequacy,
      isMeaningful: !suppressed && !isSmall && ["strong", "moderate", "weak"].includes(b.significance || ""),
      note: b.note || "",
      enoughToJudge: !suppressed && b.n >= 10,
    };
  });
}

/** Buckets worth surfacing: not suppressed, with a real sample behind them. */
export function notableBuckets(analysis: LearningAnalysis): LearningBucket[] {
  return (analysis?.notable || []).filter((b) => !b.suppressed && b.n >= 10);
}

/** Axes whose buckets were all suppressed, so the UI can say why rather than look empty. */
export function starvedAxes(analysis: LearningAnalysis): LearningBreakdown[] {
  return (analysis?.breakdowns || []).filter(
    (bd) => bd.rows_with_value === 0 || bd.buckets.every((b) => b.suppressed),
  );
}

export function coverageLabel(bd: LearningBreakdown): string {
  if (!bd.rows_scanned) return "0/0";
  return `${bd.rows_with_value}/${bd.rows_scanned}`;
}

// ---------------------------------------------------------------------------
// the empty and limited states
// ---------------------------------------------------------------------------

export interface EmptyState {
  empty: boolean;
  title: string;
  detail: string;
  missing: { feature: string; reason: string }[];
}

/**
 * What to show when there is nothing to show.
 *
 * This is the live state of the project — the database holds zero trades — so it
 * is the *primary* render path, not an edge case. It must state what is missing
 * and why, because a blank panel reads as a bug and a zero reads as a result.
 */
export function emptyState(
  trades: number,
  missing: Record<string, string>,
  limitations: string[],
): EmptyState {
  const missingList = Object.entries(missing || {}).map(([feature, reason]) => ({
    feature,
    reason,
  }));
  if (trades > 0) {
    return { empty: false, title: "", detail: "", missing: missingList };
  }
  return {
    empty: true,
    title: "No closed trades yet",
    detail:
      limitations?.[0] ||
      "No backtest trade and no journal entry has been recorded, so every figure " +
        "below is correctly absent rather than zero.",
    missing: missingList,
  };
}

/** Sample-size verdict, phrased so it cannot be mistaken for a quality signal. */
export function sampleVerdict(n: number, floor = 10): { text: string; tone: Tone } {
  if (n === 0) return { text: "no trades", tone: "muted" };
  if (n < floor) return { text: `${n} trades \u2014 below the ${floor}-trade floor`, tone: "muted" };
  if (n < 30) return { text: `${n} trades \u2014 small sample`, tone: "warn" };
  return { text: `${n} trades`, tone: "good" };
}

/** The report's headline plus whether to believe it. */
export function reportHeadline(report: LearningReport): {
  text: string;
  tone: Tone;
  advisory: boolean;
} {
  return {
    text: report?.headline || "",
    tone: headlineTone(report?.headline || ""),
    // Read from the payload, not assumed.
    advisory: report?.advisory === true && report?.applies_changes === false,
  };
}

/**
 * Whether every section is unmeasured, so the screen can collapse to one message.
 *
 * Distinct from `empty`: a book can hold trades and still have nothing to say,
 * e.g. every trade is still open.
 */
export function nothingMeasured(overview: {
  analysis?: LearningAnalysis;
  drift?: { pairs?: DriftPair[]; headline?: string };
}): boolean {
  const noBuckets = notableBuckets(overview.analysis as LearningAnalysis).length === 0;
  const noDrift = isRefusal(overview.drift?.headline || "");
  return noBuckets && noDrift;
}

// ---------------------------------------------------------------------------
// evidence — how much of this book is actually evidence
// ---------------------------------------------------------------------------

export interface EvidenceView {
  total: number;
  forward: number;
  inSample: number;
  /** The most recent forward trade date, as a date. `null` when none exists. */
  latestForward: string | null;
  /** `"net_pnl 0, return_pct 140"` — which outcome columns are populated. */
  coverage: string;
  /** Which grade the book leads with. `none` is a book nobody graded. */
  grade: "forward" | "in_sample" | "none";
  gradeLabel: string;
  tone: Tone;
  /** One sentence saying what the counts mean. Never a quality judgement. */
  note: string;
}

/**
 * The evidence figures, derived so the screen cannot restate them wrongly.
 *
 * This is the most important strip on the learning screen, because it is the one
 * that stops a reader treating a measurement as a test. The failure it guards
 * against is specific and quiet: a book of four hundred backfilled rows and no
 * forward one looks, on every other panel, exactly like a well-tested strategy.
 *
 * The count that decides the verdict is `forward_observations`, never `trades`.
 * A book of in-sample rows is reported as in-sample however large it is, and a
 * book nobody graded is reported as ungraded rather than defaulted into either
 * category — the backend grades such a row in-sample for safety, but saying "all
 * of this is in-sample" about a book with no verdicts would assert something the
 * data does not contain.
 */
export function evidenceView(summary: LearningDatasetSummary): EvidenceView {
  const total = summary?.observations ?? summary?.trades ?? 0;
  const grades = summary?.evidence_grades ?? {};
  const forward = grades.forward ?? summary?.forward_observations ?? 0;
  const inSample = grades.in_sample ?? summary?.in_sample_observations ?? 0;
  const coverage = Object.entries(summary?.metric_coverage ?? {})
    .map(([name, n]) => `${name} ${n}`)
    .join(", ");
  const latestForward = summary?.latest_forward_ts
    ? String(summary.latest_forward_ts).slice(0, 10)
    : null;

  if (total === 0) {
    return {
      total,
      forward,
      inSample,
      latestForward,
      coverage,
      grade: "none",
      gradeLabel: "nothing recorded",
      tone: "muted",
      note: "No trade has been recorded yet, so there is nothing to grade.",
    };
  }

  if (forward === 0) {
    return {
      total,
      forward,
      inSample,
      latestForward,
      coverage,
      grade: "in_sample",
      gradeLabel: "in-sample only",
      tone: "warn",
      note:
        `${inSample} of ${total} observations are graded in-sample: they were ` +
        "measured on the history the rules were selected from, so they describe " +
        "the selection and not whether anything still works. No forward " +
        "observation has been recorded yet.",
    };
  }

  const latest = latestForward ? ` The most recent closed ${latestForward}.` : "";
  return {
    total,
    forward,
    inSample,
    latestForward,
    coverage,
    grade: "forward",
    gradeLabel: forward >= 10 ? "forward evidence" : "forward — below the floor",
    tone: forward >= 10 ? "good" : "warn",
    note:
      `${forward} of ${total} observations are forward evidence — recorded ` +
      `before their outcome was known.${latest}` +
      (inSample > 0
        ? ` The other ${inSample} are in-sample and are not evidence.`
        : ""),
  };
}

// ---------------------------------------------------------------------------
// forward-learning readiness
// ---------------------------------------------------------------------------
//
// The readiness payload answers "is trustworthy evidence accumulating?" per
// strategy. Three rules are enforced here, matching the backend module:
//
// 1. **States are labels, never actions.** Tones distinguish the four states,
//    but every state renders beside its reasons — a green pill without the
//    "why" would read as approval.
// 2. **Absent is not zero.** An unmeasured mean, a missing version, a strategy
//    with no cycle yet: all render as em dashes or stated absences.
// 3. **A gate is have/required/status, always all three.** "6 trades" without
//    the 10 it is measured against invites the reader to supply their own bar.

import type {
  EvidenceStatus,
  ReadinessQualityIssue,
  ReadinessState,
  ReadinessStrategy,
} from "../api";

export function readinessStateTone(state: ReadinessState): Tone {
  switch (state) {
    case "OPTIMIZATION ELIGIBLE":
      return "good";
    case "ANALYSIS READY":
      return "good";
    case "MINIMUM SAMPLE":
      return "warn";
    default:
      return "muted";
  }
}

export function readinessStateLabel(state: ReadinessState): string {
  switch (state) {
    case "NOT READY":
      return "Not ready";
    case "MINIMUM SAMPLE":
      return "Minimum sample";
    case "ANALYSIS READY":
      return "Analysis ready";
    case "OPTIMIZATION ELIGIBLE":
      return "Optimization eligible";
    default:
      return String(state || "unknown");
  }
}

export function evidenceStatusTone(status: EvidenceStatus): Tone {
  switch (status) {
    case "ANALYSIS_READY":
      return "good";
    case "SMALL_SAMPLE":
      return "warn";
    default:
      return "muted";
  }
}

export function evidenceStatusLabel(status: EvidenceStatus): string {
  switch (status) {
    case "ANALYSIS_READY":
      return "Analysis ready";
    case "SMALL_SAMPLE":
      return "Small sample";
    default:
      return "Insufficient";
  }
}

/** "6 / 10" — or "52 · all gates cleared" once there is no next gate. */
export function gateProgress(have: number, required: number | null): string {
  if (required == null) return `${have} · all gates cleared`;
  return `${have} / ${required}`;
}

export function qualitySeverityTone(severity: ReadinessQualityIssue["severity"]): Tone {
  return severity === "blocking" ? "bad" : "warn";
}

export interface ReadinessRowView {
  id: string;
  name: string;
  forward: number;
  gate: string;
  gateTone: Tone;
  state: ReadinessState;
  stateLabel: string;
  stateTone: Tone;
  contextCoverage: string;
  lastTrade: string;
  version: string;
}

export function readinessRowView(strategy: ReadinessStrategy): ReadinessRowView {
  const gate = strategy.next_gate;
  const coverage = strategy.summary?.score_coverage;
  return {
    id: strategy.strategy_id,
    name: strategy.strategy_name || strategy.strategy_id,
    forward: strategy.forward_trades ?? 0,
    gate: gateProgress(strategy.forward_trades ?? 0, gate?.required ?? null),
    gateTone: evidenceStatusTone(gate?.status ?? "INSUFFICIENT"),
    state: strategy.state,
    stateLabel: readinessStateLabel(strategy.state),
    stateTone: readinessStateTone(strategy.state),
    contextCoverage:
      coverage == null
        ? NOT_MEASURED
        : `${coverage.with_score ?? 0} of ${coverage.scanned ?? 0} with a recorded score`,
    lastTrade: strategy.last_trade ? String(strategy.last_trade).slice(0, 10) : NOT_MEASURED,
    version:
      strategy.current_version != null
        ? `v${strategy.current_version}`
        : NOT_MEASURED,
  };
}

export interface ReadinessTotals {
  strategies: number;
  forward: number;
  byState: Record<string, number>;
  blocking: number;
}

/** The section header figures: how many strategies sit in each state. */
export function readinessTotals(
  strategies: ReadinessStrategy[],
  issues: ReadinessQualityIssue[],
): ReadinessTotals {
  const byState: Record<string, number> = {};
  let forward = 0;
  for (const s of strategies || []) {
    byState[s.state] = (byState[s.state] || 0) + 1;
    forward += s.forward_trades ?? 0;
  }
  return {
    strategies: (strategies || []).length,
    forward,
    byState,
    blocking: (issues || []).filter((i) => i.severity === "blocking").length,
  };
}
