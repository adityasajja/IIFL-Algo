/**
 * View models for the Analytics / Trade Attribution panel.
 *
 * All the interpretation lives here, in pure functions, for the same reason the
 * other `*-view.ts` modules do: a bucketing rule that only exists inside JSX
 * cannot be unit-tested, and "which bucket did this trade land in" is exactly the
 * kind of claim that should have a test.
 *
 * The one rule this module exists to keep
 * ---------------------------------------
 *
 * **A rate with no denominator is not zero.** Every ratio below returns `null`
 * when its denominator is missing or zero, and the panel renders `—`. A win rate
 * over zero trades is unknown, not 0%, and printing 0.00 would show a losing
 * system where there is simply no system yet.
 *
 * The second rule, which is subtler
 * ---------------------------------
 *
 * **A bucket's `suppressed` flag is about the statistic, not the bucket.** A
 * bucket of three trades still gets rendered — dropping it would make the
 * table's totals disagree with the book it came from — but its mean is marked as
 * not worth quoting. `bucketViews` carries that flag through untouched.
 */

import type {
  AnalyticsBucket,
  AttributedTradeRow,
  AttributionCoverage,
  EvidenceCounts,
  MaeMfePoint,
  TradeAttributionDetail,
} from "../api";

// ─── formatting ───────────────────────────────────────────────────────────────

export const EM_DASH = "\u2014";

/** A number, or an em dash. Never a fabricated zero. */
export function num(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return value.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

/** Signed percent, or an em dash. */
export function pct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** Rupees, or an em dash. Uses the Indian grouping the rest of the app uses. */
export function money(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return `\u20b9${value.toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
}

/** Basis points with an explicit sign, because adverse-positive matters. */
export function bps(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}bps`;
}

/** A duration in seconds as a compact human string, or an em dash. */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return EM_DASH;
  const s = Math.abs(seconds);
  if (s < 60) return `${seconds.toFixed(0)}s`;
  if (s < 3600) return `${(seconds / 60).toFixed(1)}m`;
  if (s < 86_400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86_400).toFixed(1)}d`;
}

/** A ratio as an R multiple, e.g. `1.83R`. */
export function rMultiple(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}R`;
}

/**
 * A profit factor, which is undefined rather than infinite when there are no
 * losses. Rendering "∞" would rank a three-winner book above every real
 * strategy, so the absence is shown as an absence.
 */
export function profitFactor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return EM_DASH;
  return value.toFixed(2);
}

// ─── evidence vocabulary ──────────────────────────────────────────────────────

export type Tone = "good" | "bad" | "warn" | "info" | "flat";

/**
 * The grade label a reader should see.
 *
 * `forward` is the only grade on which a claim can rest, because it means the
 * trade was recorded before its outcome was known. `in_sample` means the trade
 * was measured on the history the rule was chosen on.
 */
export function gradeView(
  grade: string | null | undefined,
  klass: string | null | undefined
): { label: string; tone: Tone; hint: string } {
  const detail = klass ? ` (${klass.replace(/_/g, " ").toLowerCase()})` : "";
  if (grade === "forward") {
    return {
      label: "Forward",
      tone: "good",
      hint: `Recorded before the outcome was known${detail}. This is the only basis for a claim.`,
    };
  }
  return {
    label: "In-sample",
    tone: "warn",
    hint: `Measured on the history the rule was selected on${detail}. Not independent evidence.`,
  };
}

/**
 * The counts line every aggregate must carry.
 *
 * The wording is deliberately explicit about the split, because "112 trades" and
 * "112 trades, 0 of them forward" are the same sentence to a reader in a hurry
 * and completely different claims.
 */
export function evidenceCountsView(counts: EvidenceCounts | null | undefined): {
  total: number;
  forward: number;
  inSample: number;
  allForward: boolean;
  label: string;
  tone: Tone;
} {
  if (!counts) {
    return {
      total: 0,
      forward: 0,
      inSample: 0,
      allForward: false,
      label: "no evidence counts",
      tone: "flat",
    };
  }
  const { n = 0, forward_n = 0, in_sample_n = 0 } = counts;
  const allForward = Boolean(counts.all_forward);
  return {
    total: n,
    forward: forward_n,
    inSample: in_sample_n,
    allForward,
    label:
      forward_n === 0
        ? `${n} trades, 0 of them forward`
        : allForward
          ? `${n} trades, all forward`
          : `${n} trades, ${forward_n} forward / ${in_sample_n} in-sample`,
    tone: forward_n === 0 ? "warn" : allForward ? "good" : "info",
  };
}

/**
 * How much of the closed book the panel is actually describing.
 *
 * `complete === null` means the count could not be taken, which is not the same
 * as "the book is not fully attributed" — those call for different responses
 * from the reader, so they render differently.
 */
export function coverageView(coverage: AttributionCoverage | null | undefined): {
  text: string;
  tone: Tone;
  warning: string | null;
} {
  if (!coverage) {
    return { text: "attribution coverage unknown", tone: "flat", warning: null };
  }
  const { attributed, closed_trades: closed, complete } = coverage;
  if (closed == null || complete == null) {
    return {
      text: `${num(attributed, 0)} attributed; the closed-book count is unavailable`,
      tone: "flat",
      warning:
        "The total book size could not be read, so this summary cannot state what share of the record it covers.",
    };
  }
  const missing = Math.max(0, closed - attributed);
  if (complete) {
    return {
      text: `all ${num(closed, 0)} closed trades attributed`,
      tone: "good",
      warning: null,
    };
  }
  return {
    text: `${num(attributed, 0)} of ${num(closed, 0)} closed trades attributed`,
    tone: missing > 0 ? "warn" : "info",
    warning: `${num(missing, 0)} closed trade${missing === 1 ? "" : "s"} have no attribution yet, so every figure below describes the attributed subset only.`,
  };
}

// ─── buckets ──────────────────────────────────────────────────────────────────

export interface BucketView {
  key: string;
  label: string;
  n: number;
  suppressed: boolean;
  netPnl: number | null;
  expect: number | null;
  winRate: number | null;
  profitFactor: number | null;
  holdingSec: number | null;
  costs: number | null;
  counts: EvidenceCounts | null;
  evidence: string;
  tone: Tone;
  /** True when the mean exists but the sample is under the floor. */
  thin: boolean;
}

const LABELS: Record<string, string> = {
  signal_present: "Signal linked",
  no_signal_linkage: "No signal linkage",
  context_supportive: "Context supportive",
  context_unsupportive: "Context unsupportive",
  context_neutral: "Context neutral",
  high_slippage: "High slippage",
  low_slippage: "Low slippage",
  favourable_or_zero: "Favourable / zero",
  slippage_le_5bps: "\u2264 5bps",
  slippage_5_15bps: "5\u201315bps",
  slippage_15_40bps: "15\u201340bps",
  slippage_gt_40bps: "> 40bps",
  never_favourable: "Never favourable",
  under_1R: "< 1R",
  "1R_to_2R": "1\u20132R",
  "2R_to_3R": "2\u20133R",
  above_3R: "> 3R",
  mild_under_quarter_R: "< 0.25R",
  quarter_to_half_R: "0.25\u20130.5R",
  half_to_1R: "0.5\u20131R",
  worse_than_1R: "> 1R",
  target_driven: "Target",
  stop_driven: "Stop",
  time_exit: "Time",
  TARGET_DRIVEN: "Target",
  STOP_DRIVEN: "Stop",
  TIME_EXIT: "Time",
  oversized: "Oversized",
  undersized: "Undersized",
  within_band: "Within band",
  capped: "Capped",
  uncapped: "Uncapped",
  intraday_lt_1h: "< 1h",
  intraday_1_6h: "1\u20136h",
  up_to_1d: "up to 1d",
  days_1_5: "1\u20135d",
  beyond_5d: "> 5d",
};

/** A readable label for a bucket key, falling back to the key itself. */
export function bucketLabel(key: string): string {
  if (LABELS[key]) return LABELS[key];
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function bucketViews(buckets: AnalyticsBucket[] | null | undefined): BucketView[] {
  if (!buckets) return [];
  return buckets.map((b) => {
    const counts = b.counts ?? null;
    const ev = evidenceCountsView(counts);
    const suppressed = Boolean(b.suppressed) || b.n === 0;
    return {
      key: b.key,
      label: bucketLabel(b.key),
      n: b.n,
      suppressed,
      netPnl: b.net_pnl,
      expect: b.expectancy,
      winRate: b.win_rate,
      profitFactor: b.profit_factor,
      holdingSec: b.mean_holding_sec,
      costs: b.total_costs,
      counts,
      evidence: ev.label,
      tone: ev.tone,
      thin: suppressed && (b.net_pnl != null || b.win_rate != null),
    };
  });
}

/**
 * Independent buckets whose counts do not sum to the book, stated rather than
 * silently dropped.
 *
 * A branch bucket list excludes rows the classifier could not place. Those rows
 * are still in every headline figure, so a table whose counts fall short of the
 * total needs a line explaining why — otherwise the reader assumes the branch
 * covers everything.
 */
export function bucketedAccounting(
  views: BucketView[],
  total: number
): { bucketed: number; excluded: number; note: string | null } {
  const bucketed = views.reduce((sum, v) => sum + v.n, 0);
  const excluded = Math.max(0, total - bucketed);
  if (excluded === 0) return { bucketed, excluded, note: null };
  return {
    bucketed,
    excluded,
    note: `${num(excluded, 0)} of ${num(total, 0)} trades could not be classified on this dimension and are excluded from the table above. They remain in every headline figure.`,
  };
}

// ─── MAE / MFE ────────────────────────────────────────────────────────────────

export interface ScatterPoint {
  tradeId: string | null;
  symbol: string | null;
  x: number;
  y: number;
  /** True when the adverse excursion reached the stop, i.e. the exit was forced. */
  forced: boolean;
  grade: string | null;
}

/**
 * The MFE-vs-outcome scatter, in a coordinate space the chart can plot directly.
 *
 * Points missing either axis are dropped rather than plotted at zero. A trade
 * whose excursion could not be measured is not a trade that did not move, and a
 * point at the origin would read as the strongest possible "no relationship".
 */
export function scatterPoints(
  points: MaeMfePoint[] | null | undefined,
  axis: "mfe" | "mae" = "mfe"
): ScatterPoint[] {
  if (!points) return [];
  const out: ScatterPoint[] = [];
  for (const p of points) {
    const outcome = p.net_pnl ?? p.return_pct;
    const excursion = axis === "mfe" ? p.mfe_over_risk : p.mae_over_risk;
    if (outcome == null || excursion == null || !Number.isFinite(outcome)) continue;
    out.push({
      tradeId: p.trade_id,
      symbol: p.symbol,
      x: excursion,
      y: outcome,
      forced: Boolean(p.capped_by_stop),
      grade: p.evidence_grade,
    });
  }
  return out;
}

/**
 * A plain statement of what a distribution measured, including what it could not.
 *
 * `measured` against `n` is the number that matters: a mean MFE over 12 of 90
 * trades is a statement about 12 trades, and the panel has to say so.
 */
export function distributionView(
  dist: { mean: number | null; median: number | null; measured: number } | null | undefined,
  total: number
): { mean: number | null; median: number | null; measured: number; unmeasured: number; note: string } {
  if (!dist) {
    return { mean: null, median: null, measured: 0, unmeasured: total, note: "not measured" };
  }
  const unmeasured = Math.max(0, total - dist.measured);
  return {
    mean: dist.mean,
    median: dist.median,
    measured: dist.measured,
    unmeasured,
    note:
      unmeasured > 0
        ? `measured on ${num(dist.measured, 0)} of ${num(total, 0)} trades; ${num(unmeasured, 0)} had no usable bars`
        : `measured on all ${num(dist.measured, 0)} trades`,
  };
}

// ─── reason codes ─────────────────────────────────────────────────────────────

export type CodeFamily = "decision" | "execution" | "exit" | "other";

const DECISION_CODES = new Set([
  "SIGNAL_POSITIVE",
  "CONTEXT_POSITIVE",
  "CONTEXT_NEGATIVE",
  "SIZING_OVERSIZED",
  "SIZING_UNDERSIZED",
  "RISK_HIGH",
  "RISK_LOW",
  "OVERSIZED",
  "UNDERSIZED",
  "HIGH_RISK",
  "LOW_RISK",
]);

const EXECUTION_CODES = new Set([
  "GOOD_ENTRY",
  "POOR_ENTRY",
  "HIGH_SLIPPAGE",
  "LOW_SLIPPAGE",
]);

const EXIT_CODES = new Set(["GOOD_EXIT", "EARLY_EXIT", "LATE_EXIT", "STOP_DRIVEN", "TARGET_DRIVEN", "TIME_EXIT"]);

/**
 * Which family a code belongs to.
 *
 * This is the distinction the specification asked to be made visible: a code
 * about what the *strategy decided* is not the same as a code about *how the fill
 * went*. Grouping them would let a bad fill be read as a bad signal.
 */
export function codeFamily(code: string): CodeFamily {
  if (DECISION_CODES.has(code)) return "decision";
  if (EXECUTION_CODES.has(code)) return "execution";
  if (EXIT_CODES.has(code)) return "exit";
  return "other";
}

export const FAMILY_LABEL: Record<CodeFamily, string> = {
  decision: "Strategy decision",
  execution: "Execution outcome",
  exit: "Exit classification",
  other: "Other",
};

export const FAMILY_TONE: Record<CodeFamily, Tone> = {
  decision: "info",
  execution: "warn",
  exit: "flat",
  other: "flat",
};

/**
 * Reason codes grouped by family, in a stable order.
 *
 * Deterministic ordering matters here: the codes are stored as a sorted list so
 * that two runs over the same inputs produce byte-identical rows, and a panel
 * that re-ordered them would make a stable record look like it was changing.
 */
export function codeGroups(
  codes: string[] | null | undefined
): { family: CodeFamily; label: string; tone: Tone; codes: string[] }[] {
  if (!codes || codes.length === 0) return [];
  const byFamily: Record<CodeFamily, string[]> = {
    decision: [],
    execution: [],
    exit: [],
    other: [],
  };
  for (const raw of codes) {
    const code = String(raw);
    byFamily[codeFamily(code)].push(code);
  }
  const order: CodeFamily[] = ["decision", "execution", "exit", "other"];
  return order
    .filter((f) => byFamily[f].length > 0)
    .map((f) => ({
      family: f,
      label: FAMILY_LABEL[f],
      tone: FAMILY_TONE[f],
      codes: byFamily[f].map((c) => c.replace(/_/g, " ")),
    }));
}

/** The reason detail for one code, if the engine recorded one. */
export function codeBasis(
  detail: Record<string, unknown> | null | undefined,
  code: string
): string | null {
  if (!detail) return null;
  const raw = detail[code];
  if (raw == null) return null;
  if (typeof raw === "string") return raw;
  if (typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    const parts: string[] = [];
    if (typeof obj.basis === "string") parts.push(obj.basis);
    if (obj.threshold != null) parts.push(`threshold ${obj.threshold}`);
    if (obj.entry_slippage_bps != null) parts.push(`entry ${Number(obj.entry_slippage_bps).toFixed(1)}bps`);
    if (obj.total_slippage_bps != null) parts.push(`total ${Number(obj.total_slippage_bps).toFixed(1)}bps`);
    if (obj.mfe_over_risk != null) parts.push(`MFE ${Number(obj.mfe_over_risk).toFixed(2)}R`);
    if (obj.mae_r != null) parts.push(`MAE ${Number(obj.mae_r).toFixed(2)}R`);
    return parts.length ? parts.join(" \u00b7 ") : null;
  }
  return String(raw);
}

// ─── attribution detail ───────────────────────────────────────────────────────

export interface BranchView {
  key: string;
  label: string;
  /** The measurement of the branch, e.g. the score or the slippage. */
  headline: string;
  /** The plain-language reading of what the headline means. */
  reading: string;
  tone: Tone;
}

function pick(obj: Record<string, unknown> | undefined, key: string): unknown {
  return obj == null ? undefined : obj[key];
}

function str(value: unknown): string {
  if (value == null) return EM_DASH;
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function numeric(value: unknown): number | null {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * The nine branches as rows a reader can scan.
 *
 * Each row carries a *reading* rather than a verdict. "Context score 62" is a
 * measurement; "this was a good signal" would be a prediction, and this layer
 * does not make predictions.
 */
export function branchViews(detail: TradeAttributionDetail | null): BranchView[] {
  if (!detail) return [];
  const t = detail.attribution ?? {};
  const signal = (t.signal ?? {}) as Record<string, unknown>;
  const context = (t.context ?? {}) as Record<string, unknown>;
  const entry = (t.entry ?? {}) as Record<string, unknown>;
  const sizing = (t.sizing ?? {}) as Record<string, unknown>;
  const risk = (t.risk ?? {}) as Record<string, unknown>;
  const execution = (t.execution ?? {}) as Record<string, unknown>;
  const exit = (t.exit ?? {}) as Record<string, unknown>;
  const outcome = (t.outcome ?? {}) as Record<string, unknown>;

  const out: BranchView[] = [];

  // Signal
  const strategyId = pick(signal, "strategy_id");
  const setup = pick(signal, "setup");
  out.push({
    key: "signal",
    label: "Signal",
    headline: strategyId ? `${str(strategyId)}${setup ? ` \u00b7 ${str(setup)}` : ""}` : str(setup),
    reading: signal.signal_id
      ? "The signal this trade was opened on, as recorded at entry."
      : "No signal linkage was recorded, so the entry cannot be traced to a rule.",
    tone: signal.signal_id ? "flat" : "warn",
  });

  // Context
  const score = numeric(pick(context, "score"));
  const ctxClass = pick(context, "context_class");
  out.push({
    key: "context",
    label: "Market context",
    headline: score != null ? `score ${str(score)}${ctxClass ? ` \u00b7 ${str(ctxClass)}` : ""}` : str(ctxClass),
    reading:
      score != null
        ? "The score the context engine recorded at signal time. Not a probability of profit."
        : "No context was recorded for this signal.",
    tone: score != null ? "info" : "flat",
  });

  // Entry
  const entrySlip = numeric(pick(entry, "slippage_bps"));
  out.push({
    key: "entry",
    label: "Entry",
    headline: entrySlip != null ? bps(entrySlip) : str(pick(entry, "fill_ratio")),
    reading:
      entrySlip != null
        ? entrySlip > 0
          ? "Filled worse than the reference price; the difference is a real cost."
          : "Filled at or better than the reference price."
        : "The entry was not measurable against a reference price.",
    tone: entrySlip == null ? "flat" : entrySlip > 0 ? "warn" : "good",
  });

  // Sizing
  const method = pick(sizing, "method");
  const capReason = pick(sizing, "cap_reason");
  out.push({
    key: "sizing",
    label: "Position sizing",
    headline: method ? str(method) : EM_DASH,
    reading: capReason
      ? `Size was limited: ${String(capReason).replace(/_/g, " ")}.`
      : "No sizing cap bound this position.",
    tone: capReason ? "warn" : "flat",
  });

  // Risk
  const realizedRisk = numeric(pick(risk, "realized_risk_pct"));
  const plannedRisk = numeric(pick(risk, "planned_risk_amount"));
  out.push({
    key: "risk",
    label: "Risk",
    headline: realizedRisk != null ? pct(realizedRisk) : plannedRisk != null ? money(plannedRisk) : EM_DASH,
    reading:
      realizedRisk != null
        ? "The risk the position actually carried, as a share of entry value."
        : "Planned risk was recorded but the realized figure is not available.",
    tone: realizedRisk == null ? "flat" : realizedRisk > 2 ? "warn" : "flat",
  });

  // Execution
  const totalSlip = numeric(pick(execution, "total_slippage_bps"));
  const costPct = numeric(pick(execution, "execution_cost_pct"));
  out.push({
    key: "execution",
    label: "Execution",
    headline: totalSlip != null ? bps(totalSlip) : EM_DASH,
    reading:
      totalSlip != null
        ? `Total slippage across measurable legs. Costs were ${costPct != null ? pct(costPct) : "not recorded"} of position value.`
        : "Slippage was not measurable — no reference price was recorded for the legs.",
    tone: totalSlip == null ? "flat" : totalSlip > 15 ? "warn" : totalSlip < 0 ? "good" : "flat",
  });

  // Exit
  const exitReason = pick(exit, "reason");
  const capture = numeric(pick(exit, "capture_efficiency_pct"));
  out.push({
    key: "exit",
    label: "Exit",
    headline: exitReason ? str(exitReason).replace(/_/g, " ") : EM_DASH,
    reading:
      capture != null
        ? `Captured ${capture.toFixed(0)}% of the move that was available while the position was open.`
        : "The share of the available move that was captured could not be measured.",
    tone: capture == null ? "flat" : capture < 40 ? "warn" : "flat",
  });

  // Outcome
  const net = numeric(pick(outcome, "net_pnl"));
  const realizedR = numeric(pick(outcome, "realized_over_risk"));
  out.push({
    key: "outcome",
    label: "Outcome",
    headline: net != null ? money(net) : EM_DASH,
    reading:
      realizedR != null
        ? `That is ${rMultiple(realizedR)} against the risk the trade was sized to take.`
        : "No R multiple is available — the planned risk was not recorded.",
    tone: net == null ? "flat" : net > 0 ? "good" : net < 0 ? "bad" : "flat",
  });

  return out;
}

/**
 * What could not be resolved for this trade, quoted from the engine.
 *
 * The engine writes a reason per missing field, distinguishing "this source
 * cannot have it" from "it has not been computed yet". Both are shown verbatim
 * because collapsing them would tell a reader to re-run something that will
 * never produce the field.
 */
export function missingFieldsView(
  fields: Record<string, string> | null | undefined,
  fallback: Record<string, string> | null | undefined
): { field: string; reason: string }[] {
  const source = fields && Object.keys(fields).length ? fields : fallback;
  if (!source) return [];
  return Object.entries(source)
    .map(([field, reason]) => ({ field: field.replace(/_/g, " "), reason: String(reason) }))
    .sort((a, b) => a.field.localeCompare(b.field));
}

/** The methodology the API states, as ordered key/value rows. */
export function methodologyView(
  methodology: Record<string, string> | null | undefined
): { key: string; label: string; value: string }[] {
  if (!methodology) return [];
  const PREFERRED = [
    "window",
    "source",
    "sign",
    "rupees",
    "risk_multiple",
    "look_ahead",
    "missing",
  ];
  const labelOf = (k: string) => k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const keys = [
    ...PREFERRED.filter((k) => k in methodology),
    ...Object.keys(methodology).filter((k) => !PREFERRED.includes(k)),
  ];
  return keys.map((k) => ({ key: k, label: labelOf(k), value: methodology[k] }));
}

// ─── trade table ──────────────────────────────────────────────────────────────

export interface TradeRowView {
  tradeId: string;
  symbol: string;
  side: string;
  netPnl: number | null;
  netReturnPct: number | null;
  mfePct: number | null;
  maePct: number | null;
  capture: number | null;
  slippageBps: number | null;
  holdingSec: number | null;
  exitReason: string | null;
  grade: string | null;
  klass: string | null;
  simulated: boolean;
  pnlTone: Tone;
}

export function tradeRowViews(rows: AttributedTradeRow[] | null | undefined): TradeRowView[] {
  if (!rows) return [];
  return rows.map((r) => {
    const net = r.net_pnl;
    return {
      tradeId: String(r.trade_id),
      symbol: r.symbol ?? EM_DASH,
      side: (r.side ?? EM_DASH).toUpperCase(),
      netPnl: net,
      netReturnPct: r.net_return_pct,
      mfePct: r.mfe_pct,
      maePct: r.mae_pct,
      capture: r.capture_efficiency_pct,
      slippageBps: r.total_slippage_bps,
      holdingSec: r.holding_sec,
      exitReason: r.exit_reason,
      grade: r.evidence_grade,
      klass: r.evidence_class,
      simulated: Boolean(r.simulated),
      pnlTone: net == null ? "flat" : net > 0 ? "good" : net < 0 ? "bad" : "flat",
    };
  });
}

/**
 * The filter chips currently in force, so a reader can see the scope they are
 * looking at without opening the filter controls.
 */
export function filterChips(filters: Record<string, unknown> | null | undefined): string[] {
  if (!filters) return [];
  return Object.entries(filters)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${String(v)}`);
}

/**
 * Whether a book is large enough for a figure to be worth reading at all.
 *
 * Used to decide whether to show an empty state ("nothing to describe yet")
 * rather than a grid of em dashes, which reads as "everything is zero".
 */
export function isEmptyBook(counts: EvidenceCounts | null | undefined): boolean {
  return !counts || (counts.n ?? 0) === 0;
}

/**
 * The one-sentence honesty footer for the panel.
 *
 * Stated once and generated from the data, so it cannot drift out of step with
 * the numbers above it.
 */
export function scopeNote(
  counts: EvidenceCounts | null | undefined,
  coverage: AttributionCoverage | null | undefined
): string {
  const ev = evidenceCountsView(counts);
  const cov = coverageView(coverage);
  if (isEmptyBook(counts)) {
    return "No attributed trades yet. Nothing on this screen is a finding — including the blanks.";
  }
  if (ev.forward === 0) {
    return `${ev.label}. Every trade here was measured on the history its rule was chosen on, so nothing below is independent evidence.`;
  }
  if (!ev.allForward) {
    return `${ev.label}. Only the ${num(ev.forward, 0)} forward trades can support a claim; the in-sample rows are context. ${cov.text}.`;
  }
  return `${ev.label}. ${cov.text}.`;
}
