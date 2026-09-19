/**
 * Pure derivations for the Signal Explorer.
 *
 * A context score is the one number on this screen that is easy to misread, so
 * this module is deliberately as careful as the backend it renders. Three rules
 * are enforced here, and each has a test:
 *
 * 1. **A score is conditions met, never a probability.** `scoreView` always
 *    prints that caption. A reader who takes `70` for a 70% chance of profit has
 *    been misled by the UI, not by the model.
 * 2. **Absent is not zero, and unmeasured is not unmet.** A criterion whose
 *    required measurement was `null` renders as *not measured* (muted), never as
 *    a failed condition; a suppressed bucket shows its counts and em dashes,
 *    never `0.00`.
 * 3. **In-sample is shown, but it is not evidence.** A backtest bucket is not
 *    hidden — hiding real numbers is its own lie — but it is labelled
 *    `in-sample`, and only forward buckets may read as out-of-sample.
 *
 * Nothing here proposes a change to a strategy, a threshold, or an order.
 */

import type {
  ContextDimension,
  ContextEvidenceNote,
  EffectivenessAxis,
  EffectivenessBucket,
  EffectivenessScoreVerdict,
  SignalContextBucket,
  SignalContextEffectiveness,
  SignalContextRecord,
  SignalScoreCriterion,
} from "../api";

export type Tone = "good" | "bad" | "warn" | "muted";

const NOT_MEASURED = "\u2014"; // em dash: a real glyph, not an empty cell

// ---------------------------------------------------------------------------
// categorical labels
// ---------------------------------------------------------------------------

export function contextClassTone(klass: string): Tone {
  if (klass === "STRONG_CONTEXT") return "good";
  if (klass === "WEAK_CONTEXT") return "warn";
  // NEUTRAL_CONTEXT is genuinely unremarkable, and INSUFFICIENT_DATA is a
  // refusal to judge rather than a bad reading. Neither is bad news.
  return "muted";
}

export function contextClassLabel(klass: string): string {
  switch (klass) {
    case "STRONG_CONTEXT":
      return "Strong context";
    case "NEUTRAL_CONTEXT":
      return "Neutral context";
    case "WEAK_CONTEXT":
      return "Weak context";
    case "INSUFFICIENT_DATA":
      return "Insufficient data";
    default:
      return klass || "unknown";
  }
}

const BUCKET_LABELS: Record<string, string> = {
  STRONG_CONTEXT: "Strong context",
  NEUTRAL_CONTEXT: "Neutral context",
  WEAK_CONTEXT: "Weak context",
  INSUFFICIENT_DATA: "Insufficient data",
  "0-39": "Score 0\u201339",
  "40-59": "Score 40\u201359",
  "40-69": "Score 40\u201369",
  "60-79": "Score 60\u201379",
  "70-100": "Score 70\u2013100",
  "80-100": "Score 80\u2013100",
  BULLISH_TREND: "Bullish trend",
  BEARISH_TREND: "Bearish trend",
  SIDEWAYS: "Sideways",
  HIGH_VOLATILITY: "High volatility",
  LOW_VOLATILITY: "Low volatility",
  UNKNOWN: "Unknown",
  positive: "Positive",
  non_positive: "Non-positive",
  strong: "Strong",
  weak: "Weak",
  elevated: "Elevated",
  normal: "Normal",
};

/** Human label for a bucket key. Unknown keys degrade to a spaced key, not "". */
export function bucketLabel(key: string): string {
  return BUCKET_LABELS[key] ?? key.replace(/_/g, " ");
}

/** The score is only interpretable against its model version and weight total. */
export function isInsufficient(record: Pick<SignalContextRecord, "has_insufficient_data">): boolean {
  return record.has_insufficient_data === true;
}

// ---------------------------------------------------------------------------
// the score
// ---------------------------------------------------------------------------

export interface ScoreView {
  /** Always non-empty: "65 / 100", or "≥65 / 100" when conditions were unmeasurable. */
  text: string;
  /** Null when the reading is incomplete and must not be treated as a figure. */
  pct: number | null;
  band: "strong" | "neutral" | "weak" | "unknown";
  tone: Tone;
  insufficient: boolean;
  /** Always non-empty. States that the score is not a probability. */
  caption: string;
}

/**
 * Render a context score without ever calling it a probability.
 *
 * When any criterion could not be measured the score is a **lower bound**: the
 * unmeasured conditions were not credited, so the true share of met conditions
 * is at least this. Printing a bare number there would assert precision the
 * data does not support, so the text carries a `≥`.
 */
export function scoreView(record: {
  context_score: number;
  max_possible_score: number;
  has_insufficient_data: boolean;
}): ScoreView {
  const score = record.context_score ?? 0;
  const max = record.max_possible_score ?? 0;
  const pct = max > 0 ? Math.round((score / max) * 100) : null;

  if (record.has_insufficient_data) {
    return {
      text: `\u2265${score} / ${max}`,
      pct: null,
      band: "unknown",
      tone: "muted",
      insufficient: true,
      caption:
        "At least this share of the model's conditions were met. Some conditions " +
        "could not be measured, so this is a lower bound \u2014 not a profit probability.",
    };
  }

  const band: ScoreView["band"] =
    pct == null ? "unknown" : pct >= 70 ? "strong" : pct >= 40 ? "neutral" : "weak";

  return {
    text: `${score} / ${max}`,
    pct,
    band,
    tone: band === "strong" ? "good" : band === "weak" ? "warn" : "muted",
    insufficient: false,
    caption:
      pct == null
        ? "The model's maximum weight is unknown, so this score cannot be read as a share."
        : `${pct}% of the model's contextual conditions were met \u2014 not a probability of profit.`,
  };
}

// ---------------------------------------------------------------------------
// the criterion breakdown
// ---------------------------------------------------------------------------

export interface CriterionView {
  key: string;
  label: string;
  weight: number;
  status: "met" | "not_met" | "not_measured";
  met: boolean;
  points: string;
  value: string;
  reason: string;
  tone: Tone;
}

/**
 * One row per scoring criterion.
 *
 * The distinction that matters: `met: false` with a real `value` is a condition
 * that was measured and did not hold; `met: false` with `value: null` is one we
 * could not measure at all. Collapsing the two would report missing data as a
 * failed strategy signal.
 */
export function criterionViews(breakdown: SignalScoreCriterion[]): CriterionView[] {
  return (breakdown || []).map((c) => {
    const measured = c.value != null;
    const status: CriterionView["status"] = c.met
      ? "met"
      : measured
        ? "not_met"
        : "not_measured";
    return {
      key: c.key,
      label: c.label,
      weight: c.weight,
      status,
      met: c.met,
      points: c.met ? `+${c.points_awarded}` : "0",
      value: measured ? String(c.value) : NOT_MEASURED,
      reason: c.reason || (measured ? "" : "not measurable at signal time"),
      tone: c.met ? "good" : measured ? "warn" : "muted",
    };
  });
}

export function metCount(breakdown: SignalScoreCriterion[]): number {
  return (breakdown || []).filter((c) => c.met).length;
}

export function unmeasuredCount(breakdown: SignalScoreCriterion[]): number {
  return (breakdown || []).filter((c) => !c.met && c.value == null).length;
}

export interface MissingView {
  none: boolean;
  fields: string[];
  text: string;
}

/** What could not be measured. Empty is a real result and must be stated. */
export function missingFieldsView(missing: string[]): MissingView {
  const fields = (missing || []).filter(Boolean);
  if (fields.length === 0) {
    return { none: true, fields: [], text: "Every condition was measurable at signal time." };
  }
  return {
    none: false,
    fields,
    text: `Not measurable at signal time: ${fields.join(", ")}.`,
  };
}

// ---------------------------------------------------------------------------
// analytics buckets
// ---------------------------------------------------------------------------

export interface BucketView {
  key: string;
  label: string;
  n: number;
  nForward: number;
  nInSample: number;
  /** Contexts in this bucket with no resolved outcome at all. */
  unresolved: number;
  suppressed: boolean;
  statsShown: boolean;
  evidence: ContextEvidenceNote;
  evidenceLabel: string;
  evidenceTone: Tone;
  mean: string;
  median: string;
  winRate: string;
  profitFactor: string;
  /** 95% Wilson interval on the win rate, e.g. "[42.0% … 78.0%]". */
  ci: string;
}

export function evidenceLabel(note: ContextEvidenceNote): string {
  switch (note) {
    case "forward":
      return "Forward";
    case "insufficient_forward_observations":
      return "Thin forward";
    case "in_sample_only":
      return "In-sample";
    case "no_resolvable_outcomes":
      return "No outcomes";
    default:
      return "Unknown";
  }
}

export function evidenceTone(note: ContextEvidenceNote): Tone {
  switch (note) {
    case "forward":
      return "good";
    case "insufficient_forward_observations":
      return "warn";
    // In-sample and no-outcome buckets are not evidence; neither is an error.
    default:
      return "muted";
  }
}

const pct = (v: number | null | undefined): string =>
  v == null ? NOT_MEASURED : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

export function bucketViews(buckets: SignalContextBucket[]): BucketView[] {
  return (buckets || []).map((b) => {
    const suppressed = b.suppressed === true;
    const scale = (v: number | null): string => (v == null ? NOT_MEASURED : `${(v * 100).toFixed(1)}%`);
    return {
      key: b.key,
      label: bucketLabel(b.key),
      n: b.n ?? 0,
      nForward: b.n_forward ?? 0,
      nInSample: b.n_in_sample ?? 0,
      unresolved: Math.max(0, (b.n ?? 0) - (b.n_forward ?? 0) - (b.n_in_sample ?? 0)),
      suppressed,
      // A suppressed bucket shows its counts and nothing else: `0.00` would read
      // as a measured zero.
      statsShown: !suppressed,
      evidence: b.evidence_note,
      evidenceLabel: evidenceLabel(b.evidence_note),
      evidenceTone: evidenceTone(b.evidence_note),
      mean: suppressed ? NOT_MEASURED : pct(b.mean_return),
      median: suppressed ? NOT_MEASURED : pct(b.median_return),
      winRate: suppressed ? NOT_MEASURED : scale(b.win_rate),
      profitFactor:
        suppressed || b.profit_factor == null ? NOT_MEASURED : b.profit_factor.toFixed(2),
      ci:
        suppressed || b.ci_low == null || b.ci_high == null
          ? ""
          : `[${(b.ci_low * 100).toFixed(1)}% \u2026 ${(b.ci_high * 100).toFixed(1)}%]`,
    };
  });
}

export interface AccountingView {
  total: number;
  forward: number;
  inSample: number;
  unresolved: number;
  note: string;
}

/**
 * The arithmetic behind a bucket table, said out loud.
 *
 * `unresolved` is the number of contexts whose entry never produced a closed
 * trade. It is the reason a large `n` can sit above a suppressed stat, so it is
 * surfaced rather than left for the reader to infer.
 */
export function analyticsAccounting(buckets: SignalContextBucket[]): AccountingView {
  const total = (buckets || []).reduce((sum, b) => sum + (b.n ?? 0), 0);
  const forward = (buckets || []).reduce((sum, b) => sum + (b.n_forward ?? 0), 0);
  const inSample = (buckets || []).reduce((sum, b) => sum + (b.n_in_sample ?? 0), 0);
  const unresolved = Math.max(0, total - forward - inSample);

  if (total === 0) {
    return {
      total,
      forward,
      inSample,
      unresolved,
      note: "No enriched signal has been recorded yet, so there is nothing to bucket.",
    };
  }

  const forwardText =
    forward === 0
      ? "None of them has a forward (out-of-sample) outcome yet."
      : `${forward} have a forward outcome.`;

  return {
    total,
    forward,
    inSample,
    unresolved,
    note:
      `${total} signal${total === 1 ? "" : "s"} bucketed \u00b7 ` +
      `${forward} forward, ${inSample} in-sample, ${unresolved} unresolved. ${forwardText}`,
  };
}

// ---------------------------------------------------------------------------
// the list rows
// ---------------------------------------------------------------------------

export interface SignalRowView {
  signalId: string;
  symbol: string;
  action: string;
  source: string;
  regime: string;
  score: ScoreView;
  contextClass: string;
  contextClassLabel: string;
  contextClassTone: Tone;
  signalTs: string;
  modelVersion: string;
}

export function signalRowView(record: SignalContextRecord): SignalRowView {
  return {
    signalId: record.signal_id,
    symbol: record.symbol,
    action: record.action,
    source: record.signal_source,
    regime: record.market_context?.regime ?? "unknown",
    score: scoreView(record),
    contextClass: record.context_class,
    contextClassLabel: contextClassLabel(record.context_class),
    contextClassTone: contextClassTone(record.context_class),
    signalTs: record.signal_ts,
    modelVersion: record.context_model_version,
  };
}

// ---------------------------------------------------------------------------
// context effectiveness
// ---------------------------------------------------------------------------
//
// The effectiveness payload answers one question — "does a higher score
// accompany a different forward outcome?" — and this section renders it under
// three rules, each with a test:
//
// 1. **A suppressed bucket shows counts and em dashes, never 0.00.** A bare
//    zero would read as a measured zero.
// 2. **Only strong/moderate forward buckets may read as supported.** `weak` is
//    suggestive at best, and an in-sample bucket is descriptive by
//    construction: it can never be a finding, however large it is.
// 3. **Every figure is shown against its complement, never against zero.**
//    `lift` is bucket-minus-complement, and the p-value shown is the
//    Bonferroni-adjusted one, so the correction is visible, not assumed.

export function effectivenessSignificanceLabel(sig: string): string {
  switch (sig) {
    case "strong":
      return "Strong";
    case "moderate":
      return "Moderate";
    case "weak":
      return "Weak";
    case "not_significant":
      return "No distinction";
    case "not_tested":
      return "Not tested";
    case "insufficient_sample":
      return "Too few";
    case "in_sample_not_a_claim":
      return "In-sample";
    default:
      return String(sig || "unknown").replace(/_/g, " ");
  }
}

export function effectivenessSignificanceTone(sig: string): Tone {
  if (sig === "strong" || sig === "moderate") return "good";
  if (sig === "weak") return "warn";
  // Thin, untested and in-sample buckets are not errors — just not findings.
  return "muted";
}

/**
 * May this bucket be presented as a supported finding?
 *
 * Strong or moderate, forward, unsuppressed, and measured against a real
 * complement. An in-sample bucket is excluded by construction, not by size.
 */
export function isSupportedFinding(
  bucket: Pick<EffectivenessBucket, "suppressed" | "significance" | "lift">
): boolean {
  if (bucket.suppressed) return false;
  if (bucket.significance === "in_sample_not_a_claim") return false;
  if (bucket.lift == null) return false;
  return bucket.significance === "strong" || bucket.significance === "moderate";
}

const fmtMetric = (v: number | null | undefined, metric: string): string => {
  if (v == null) return NOT_MEASURED;
  const signed = `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
  return metric === "net_pnl" ? signed : `${signed}%`;
};

const fmtInterval = (
  pair: [number, number] | null | undefined,
  metric: string
): string => {
  if (!pair) return "";
  return `[${fmtMetric(pair[0], metric)} \u2026 ${fmtMetric(pair[1], metric)}]`;
};

export interface EffectivenessBucketView {
  label: string;
  n: number;
  suppressed: boolean;
  /** False for suppressed rows: the table prints em dashes instead. */
  statsShown: boolean;
  significance: string;
  significanceLabel: string;
  significanceTone: Tone;
  mean: string;
  meanCI: string;
  median: string;
  winRate: string;
  winRateCI: string;
  profitFactor: string;
  /** Bucket mean minus complement mean. Never a comparison against zero. */
  lift: string;
  /** The Bonferroni-adjusted p-value — the correction is shown, not assumed. */
  pAdjusted: string;
  drawdown: string;
  note: string;
  supported: boolean;
  inSample: boolean;
}

export function effectivenessBucketViews(
  axis: EffectivenessAxis,
  metric: string
): EffectivenessBucketView[] {
  return (axis?.buckets || []).map((b) => {
    const suppressed = b.suppressed === true;
    const inSample = b.significance === "in_sample_not_a_claim";
    const scale = (v: number | null): string =>
      v == null ? NOT_MEASURED : `${(v * 100).toFixed(1)}%`;
    return {
      label: bucketLabel(b.label),
      n: b.n ?? 0,
      suppressed,
      statsShown: !suppressed,
      significance: b.significance,
      significanceLabel: effectivenessSignificanceLabel(b.significance),
      significanceTone: effectivenessSignificanceTone(b.significance),
      mean: suppressed ? NOT_MEASURED : fmtMetric(b.mean, metric),
      meanCI: suppressed ? "" : fmtInterval(b.mean_ci, metric),
      median: suppressed ? NOT_MEASURED : fmtMetric(b.median, metric),
      winRate: suppressed ? NOT_MEASURED : scale(b.win_rate),
      winRateCI:
        suppressed || !b.win_rate_ci
          ? ""
          : `[${(b.win_rate_ci[0] * 100).toFixed(1)}% \u2026 ${(b.win_rate_ci[1] * 100).toFixed(1)}%]`,
      profitFactor:
        suppressed || b.profit_factor == null ? NOT_MEASURED : b.profit_factor.toFixed(2),
      lift: suppressed ? NOT_MEASURED : fmtMetric(b.lift, metric),
      pAdjusted: suppressed || b.p_adjusted == null ? NOT_MEASURED : b.p_adjusted.toFixed(4),
      drawdown:
        suppressed || b.max_drawdown == null
          ? NOT_MEASURED
          : fmtMetric(b.max_drawdown, metric),
      note: b.note || "",
      supported: isSupportedFinding(b),
      inSample,
    };
  });
}

export interface ScoreBandView {
  label: string;
  mean: number | null;
  meanText: string;
}

export interface ScoreVerdictView {
  statement: string;
  mayClaim: boolean;
  claimLabel: string;
  claimTone: Tone;
  monotonic: boolean;
  bands: ScoreBandView[];
}

export function scoreVerdictView(
  verdict: EffectivenessScoreVerdict,
  metric: string
): ScoreVerdictView {
  const bands = (verdict?.bands || []).map((label) => {
    const mean = verdict.means ? verdict.means[label] ?? null : null;
    return { label: bucketLabel(label), mean, meanText: fmtMetric(mean, metric) };
  });
  const mayClaim = verdict?.may_claim === true;
  return {
    statement: verdict?.statement || "No verdict available.",
    mayClaim,
    claimLabel: mayClaim ? "Supported" : "Not proven",
    claimTone: mayClaim ? "good" : "muted",
    monotonic: verdict?.monotonic_high_is_better === true,
    bands,
  };
}

export interface SupportedFinding {
  axisLabel: string;
  bucketLabel: string;
  n: number;
  lift: string;
  liftValue: number | null;
  significanceLabel: string;
  mean: string;
  pAdjusted: string;
}

export interface SuggestiveFinding extends SupportedFinding {}

/** Forward buckets the sample supports, strongest lift first. */
export function supportedFindings(result: SignalContextEffectiveness): SupportedFinding[] {
  const out: SupportedFinding[] = [];
  for (const axis of result?.axes || []) {
    for (const b of axis.buckets || []) {
      if (!isSupportedFinding(b)) continue;
      out.push({
        axisLabel: axis.label,
        bucketLabel: bucketLabel(b.label),
        n: b.n,
        lift: fmtMetric(b.lift, result.metric),
        liftValue: b.lift,
        significanceLabel: effectivenessSignificanceLabel(b.significance),
        mean: fmtMetric(b.mean, result.metric),
        pAdjusted: b.p_adjusted == null ? NOT_MEASURED : b.p_adjusted.toFixed(4),
      });
    }
  }
  return out.sort((a, b) => Math.abs(b.liftValue ?? 0) - Math.abs(a.liftValue ?? 0));
}

/** Buckets worth watching but not strong enough to act on. */
export function suggestiveFindings(result: SignalContextEffectiveness): SuggestiveFinding[] {
  const out: SuggestiveFinding[] = [];
  for (const axis of result?.axes || []) {
    for (const b of axis.buckets || []) {
      if (b.suppressed || b.significance !== "weak" || b.lift == null) continue;
      out.push({
        axisLabel: axis.label,
        bucketLabel: bucketLabel(b.label),
        n: b.n,
        lift: fmtMetric(b.lift, result.metric),
        liftValue: b.lift,
        significanceLabel: effectivenessSignificanceLabel(b.significance),
        mean: fmtMetric(b.mean, result.metric),
        pAdjusted: b.p_adjusted == null ? NOT_MEASURED : b.p_adjusted.toFixed(4),
      });
    }
  }
  return out.sort((a, b) => Math.abs(b.liftValue ?? 0) - Math.abs(a.liftValue ?? 0));
}

export interface UnsupportedAxis {
  axis: string;
  label: string;
  scanned: number;
  reason: string;
}

/** Axes where no bucket clears the bar — the "remains unproven" list. */
export function unsupportedAxes(result: SignalContextEffectiveness): UnsupportedAxis[] {
  const out: UnsupportedAxis[] = [];
  for (const axis of result?.axes || []) {
    const buckets = axis.buckets || [];
    const supported = buckets.filter((b) => isSupportedFinding(b)).length;
    if (supported > 0) continue;
    const scanned = axis.coverage?.scanned ?? 0;
    const withValue =
      axis.coverage?.with_value ?? buckets.reduce((s, b) => s + (b.n ?? 0), 0);
    const reason =
      scanned === 0
        ? "No forward observations scanned on this axis yet."
        : withValue < (result?.min_sample ?? 10)
          ? `Largest bucket is below the ${result?.min_sample ?? 10}-trade floor.`
          : "Buckets clear the floor but no difference survives the correction.";
    out.push({ axis: axis.axis, label: axis.label, scanned, reason });
  }
  return out;
}

export interface ForwardInSampleRow {
  label: string;
  forwardN: number;
  forwardMean: string;
  inSampleN: number;
  inSampleMean: string;
}

/** One axis, forward buckets beside their in-sample counterparts by label. */
export function forwardVsInSample(
  axisName: string,
  result: SignalContextEffectiveness
): ForwardInSampleRow[] {
  const forward = (result?.axes || []).find((a) => a.axis === axisName);
  const inSample = (result?.in_sample_axes || []).find((a) => a.axis === axisName);
  const inByLabel = new Map((inSample?.buckets || []).map((b) => [b.label, b]));
  const rows: ForwardInSampleRow[] = [];
  for (const b of forward?.buckets || []) {
    const peer = inByLabel.get(b.label);
    rows.push({
      label: bucketLabel(b.label),
      forwardN: b.suppressed ? b.n : (b.n_forward ?? b.n),
      forwardMean: b.suppressed ? NOT_MEASURED : fmtMetric(b.mean, result.metric),
      inSampleN: peer?.n ?? 0,
      inSampleMean: fmtMetric(peer?.mean ?? null, result.metric),
    });
  }
  return rows;
}

export interface EffectivenessAccounting {
  forward: number;
  withMetric: number;
  inSample: number;
  minSample: number;
  comparisons: number;
  note: string;
}

export function effectivenessAccounting(
  result: SignalContextEffectiveness
): EffectivenessAccounting {
  const forward = result?.forward_n ?? 0;
  const withMetric = result?.forward_with_metric ?? 0;
  const inSample = result?.in_sample_n ?? 0;
  const minSample = result?.min_sample ?? 10;
  const comparisons = result?.bonferroni_comparisons ?? 0;
  const note =
    forward === 0
      ? "No forward (paper/live) context with an outcome has been recorded yet. " +
        "Backtest rows are in-sample by construction and support no claim."
      : `${forward} forward context${forward === 1 ? "" : "s"}, ` +
        `${withMetric} with a resolved ${result?.metric ?? "return_pct"} \u00b7 ` +
        `${inSample} in-sample (kept separate) \u00b7 floor ${minSample} trades \u00b7 ` +
        `p-values corrected for ${comparisons} comparisons.`;
  return { forward, withMetric, inSample, minSample, comparisons, note };
}

// ---------------------------------------------------------------------------
// the dimensions the UI can ask for
// ---------------------------------------------------------------------------

export const DIMENSION_OPTIONS: {
  key: ContextDimension;
  label: string;
  description: string;
}[] = [
  {
    key: "context_class",
    label: "Context class",
    description: "STRONG / NEUTRAL / WEAK / INSUFFICIENT_DATA",
  },
  { key: "score_band", label: "Score band", description: "0\u201339 / 40\u201369 / 70\u2013100" },
  {
    key: "regime",
    label: "Market regime",
    description: "BULLISH_TREND / BEARISH_TREND / SIDEWAYS / HIGH_VOLATILITY / LOW_VOLATILITY",
  },
  { key: "sector_rs", label: "Sector RS", description: "sector 1M RS vs NIFTY: positive / non-positive" },
  { key: "stock_rs", label: "Stock RS", description: "stock 20D RS vs NIFTY: positive / non-positive" },
  { key: "breadth", label: "Breadth", description: "% above EMA50: strong (\u226555) / weak" },
  { key: "volatility", label: "Volatility", description: "ATR ratio: elevated (\u22651.3) / normal" },
];
