/**
 * Pure derivations for the paper deployment screen.
 *
 * Kept out of the panel so they can be tested without a DOM, and — more to the
 * point — so the *decisions* the screen makes are readable in one place. The
 * three that carry real consequences:
 *
 * 1. **What the four controls may do.** The backend refuses most transitions
 *    (`start` accepts only `PENDING`/`PAUSED`; a stopped deployment is terminal;
 *    pause needs a running one). A screen that offers a button the server will
 *    reject teaches the user that the button is broken. So the gating lives here,
 *    mirrors the repository's `WHERE` clauses, and is tested.
 * 2. **How a null P&L is rendered.** `today_pnl` is `null` when no fill exists
 *    before the session boundary. That is "not yet measured", not "flat", and the
 *    two must not print the same.
 * 3. **The pipeline rail.** The five stages are shown in causal order always —
 *    a stage that has not happened yet is greyed, not omitted, because a chain
 *    drawn only from what has occurred cannot show the user where it *stopped*.
 *
 * No React, no fetch, no imports beyond types.
 */

import type {
  ComparisonVerdict,
  DeploymentStatus,
  MonitorOverview,
  PaperPosition,
  TimelineEvent,
  TimelineStage,
} from "../api";

/** The chain, in causal order. Mirrors `TIMELINE_STAGES` in the backend. */
export const STAGES: readonly TimelineStage[] = [
  "signal",
  "risk",
  "order",
  "fill",
  "position",
] as const;

export const STAGE_LABEL: Record<TimelineStage, string> = {
  signal: "Signal",
  risk: "Risk",
  order: "Order",
  fill: "Fill",
  position: "Position",
};

export const STAGE_BLURB: Record<TimelineStage, string> = {
  signal: "a rule fired on a live tick",
  risk: "the gate approved or rejected it",
  order: "the OMS placed it at the paper venue",
  fill: "the venue matched it at the live price",
  position: "the ledger folded the fill into the book",
};

/** Which controls are usable on a deployment in this state.
 *
 *  `canStart` is true for `PENDING` and `PAUSED` and **false for `STOPPED`**.
 *  That asymmetry is the single most surprising thing about this screen, so it
 *  is also surfaced as `resetInstead` rather than left for the user to discover
 *  by clicking a disabled button. */
export interface ControlGates {
  canStart: boolean;
  canPause: boolean;
  canStop: boolean;
  canReset: boolean;
  /** Why start is unavailable, in words, when it is. `null` otherwise. */
  startBlocked: string | null;
  /** True when the only way forward is a reset — a fresh deployment. */
  resetInstead: boolean;
}

export function controlGates(status: DeploymentStatus | string | null | undefined): ControlGates {
  const state = String(status ?? "");
  const pending = state === "PENDING";
  const running = state === "RUNNING";
  const paused = state === "PAUSED";
  const stopped = state === "STOPPED";

  const canStart = pending || paused;
  const startBlocked = canStart
    ? null
    : stopped
      ? "a stopped deployment is terminal — reset to run this strategy again"
      : running
        ? "already running"
        : "unknown state";

  return {
    canStart,
    canPause: running,
    canStop: running || paused,
    // Always available: reset is the only action that works from every state,
    // including the terminal one, which is exactly why it exists.
    canReset: true,
    startBlocked,
    resetInstead: stopped,
  };
}

/** Is this combination of state and outcome worth the viewer's attention?
 *
 *  Used to tint a timeline row. A `rejected` risk decision and a completed fill
 *  are both "normal", but only one of them is something to look at. */
export function outcomeTone(outcome: string): "good" | "bad" | "warn" | "flat" {
  if (outcome === "rejected") return "bad";
  if (outcome === "approved" || outcome === "filled" || outcome === "recorded") return "good";
  if (outcome === "placed") return "warn";
  return "flat";
}

/** One timeline row, already de-duplicated into a single chain link.
 *
 *  `count` exists because a busy deployment emits an `order` entry for both
 *  `SUBMITTED` and `ACKNOWLEDGED` — two facts, one link in the chain. Collapsing
 *  them keeps the rail readable while `summaries` retains both lines so nothing
 *  is hidden. */
export interface ChainLink {
  stage: TimelineStage;
  ts: string;
  count: number;
  summaries: string[];
  reason: string | null;
  order_id: string | null;
}

/** Group a timeline into the five stages, newest order first within each stage.
 *
 *  Returns one link per stage **that has occurred**. The rail the UI draws is
 *  `STAGES`; this is what has actually filled in. */
export function toChain(events: TimelineEvent[]): ChainLink[] {
  const byStage = new Map<TimelineStage, ChainLink>();
  // Newest first: an operator reads the timeline to answer "what just happened",
  // so the most recent occurrence of each stage is the one worth showing.
  const ordered = [...events].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));

  for (const event of ordered) {
    if (!STAGES.includes(event.stage)) continue;
    const existing = byStage.get(event.stage);
    if (existing) {
      existing.count += 1;
      existing.summaries.push(event.summary);
      existing.reason = existing.reason ?? event.reason;
      continue;
    }
    byStage.set(event.stage, {
      stage: event.stage,
      ts: event.ts,
      count: 1,
      summaries: [event.summary],
      reason: event.reason,
      order_id: event.order_id,
    });
  }

  // Emit in causal order, not chronological: the point of the rail is the chain.
  return STAGES.map((s) => byStage.get(s)).filter((l): l is ChainLink => l !== undefined);
}

/** Positions the ledger could not value. Named rather than dropped — see
 *  `PaperSnapshot.complete`. */
export function unpricedOf(overview: MonitorOverview | null): string[] {
  if (!overview) return [];
  return overview.pnl.unpriced_symbols ?? [];
}

/** Open positions only. The ledger returns flat rows too, and a flat row is not
 *  a holding — showing it would inflate the position count. */
export function openPositions(overview: MonitorOverview | null): PaperPosition[] {
  if (!overview) return [];
  return overview.positions.filter((p) => p.quantity !== 0);
}

/** The one-line explanation of why a deployment is idle, or `null` when it is
 *  trading.
 *
 *  Ordered by what the operator can act on: a blocked rule set is a
 *  configuration problem they can fix, a closed session is not. `status`
 *  already computes this server-side; this only decides the wording and falls
 *  back through the individual flags when `not_trading_because` is absent. */
export function idleReason(status: MonitorOverview["status"] | null): string | null {
  if (!status) return null;
  if (status.trading) return null;
  if (status.not_trading_because) return status.not_trading_because;
  if (status.blocked_reason) return status.blocked_reason;
  if (status.status !== "RUNNING") return `deployment is ${status.status}`;
  if (!status.runner_running) return "the paper runner is not running";
  if (!status.loop_attached) return "the runner has not attached a loop yet";
  if (!status.in_market_hours) return "the cash session is closed";
  return "not trading";
}

/** Format a nullable money figure, distinguishing "not measured" from zero.
 *
 *  `null` renders as an em dash with the caller's note; `0` renders as ₹0. The
 *  distinction is the whole reason `today_pnl` is nullable in the API. */
export function fmtMoneyOrDash(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  return `${sign}₹${Math.abs(value).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

/** Percentage with an explicit sign, or a dash. Never `+0.00%` for unknown. */
export function fmtPctOrDash(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** IST wall-clock for a timestamp, so an operator reads exchange time.
 *
 *  The deployment trades an Indian session; a timeline rendered in the browser's
 *  local zone would put a 09:15 fill at 03:45 for anyone reading from Europe and
 *  make the session boundary impossible to see. */
export function istClock(ts: string | null | undefined): string {
  if (!ts) return "—";
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function istDate(ts: string | null | undefined): string {
  if (!ts) return "—";
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleDateString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

/** How long ago, in words, from an ISO timestamp. Coarse on purpose: the exact
 *  second is in the tooltip, and "3m ago" is what an operator actually reads. */
export function ago(ts: string | null | undefined, now: number = Date.now()): string {
  if (!ts) return "—";
  const parsed = new Date(ts).getTime();
  if (Number.isNaN(parsed)) return "—";
  const seconds = Math.max(0, Math.round((now - parsed) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Split the comma-separated symbol input the deploy form uses.
 *
 *  Upper-cased and de-duplicated because the runner upper-cases symbols before
 *  matching them against the cache; letting `reliance` through would create a
 *  deployment whose symbols never resolve and which is therefore silently inert. */
export function parseSymbols(raw: string): string[] {
  const seen = new Set<string>();
  for (const piece of raw.split(/[,\s]+/)) {
    const symbol = piece.trim().toUpperCase();
    if (symbol) seen.add(symbol);
  }
  return [...seen];
}

/** Whether the deploy form can be submitted, and what is missing if not. */
export function deployReadiness(input: {
  strategyId: string;
  version: string;
  capital: string;
  symbols: string;
  universe: string;
}): { ready: boolean; problem: string | null } {
  if (!input.strategyId) return { ready: false, problem: "pick a saved strategy" };
  if (!input.version) return { ready: false, problem: "pick the strategy version to pin" };
  if (!(Number(input.capital) > 0)) return { ready: false, problem: "capital must be positive" };
  // The runner resolves its universe from `config.symbols` only; a universe name
  // it does not read would produce a deployment with no symbols, which the
  // runner then skips — running, and doing nothing, with no error shown.
  if (!input.symbols.trim() && !input.universe.trim()) {
    return { ready: false, problem: "pick a universe or type at least one symbol" };
  }
  return { ready: true, problem: null };
}

/** The version number to preselect for a chosen strategy, or `""` for none.
 *
 *  **"Newest" is the highest version number, not the last row.** The service
 *  serves the list `ORDER BY version DESC`, so it arrives newest-first — and
 *  reading `versions[versions.length - 1]` on a newest-first list returns the
 *  *oldest*. That is what this panel used to do, under a comment that said
 *  "preselect the newest".
 *
 *  It failed silently, and expensively. A version is what a deployment is
 *  *pinned* to: the rules it will keep running for months. Preselecting the
 *  oldest meant the default form submission deployed the least-current rules,
 *  with nothing on screen to suggest it — the dropdown showed `v1`, which is a
 *  valid choice, and only a user who remembered authoring `v2` would notice.
 *
 *  Comparing the numbers is correct whichever way the list is ordered, which is
 *  the property worth having: the ordering of an API response is not something
 *  a form should be silently coupled to.
 */
export function newestVersion(
  versions: readonly { version: number }[] | null | undefined,
): string {
  if (!versions || versions.length === 0) return "";
  const latest = versions.reduce((a, b) => (b.version > a.version ? b : a));
  return String(latest.version);
}

// ---------------------------------------------------------------------------
// champion vs challenger
// ---------------------------------------------------------------------------
//
// The comparison screen reports readiness, never a winner. Two rules keep it
// honest, and each has a test:
//
// 1. **The verdict is about sample, not superiority.** Tones follow the
//    readiness ladder: muted below the floor, amber while early, and only
//    then a neutral "ready to read" — never green-for-the-leader, because
//    there is no leader.
// 2. **A delta without both sizes is a rumour.** Delta cells always render
//    beside the two arm counts; a missing side renders as an em dash, never
//    as zero.

export type VerdictTone = "good" | "warn" | "muted";

export function comparisonVerdictTone(verdict: ComparisonVerdict): VerdictTone {
  switch (verdict) {
    case "COMPARISON READY":
      return "good";
    case "EARLY EVIDENCE":
      return "warn";
    default:
      return "muted";
  }
}

export function comparisonVerdictBlurb(verdict: ComparisonVerdict): string {
  switch (verdict) {
    case "COMPARISON READY":
      return "Both arms clear the comparison floor. Deltas are descriptive — this licenses reading, not promoting.";
    case "EARLY EVIDENCE":
      return "Both arms trade, but the thinner one is below the comparison floor. Treat every delta as provisional.";
    default:
      return "At least one arm is below the 10-trade minimum. No side-by-side reading is supported yet.";
  }
}

const EM_DASH = "—";

/** "+1.25%" for return_pct, "+12.50" for net_pnl, em dash when unmeasured. */
export function fmtDelta(value: number | null | undefined, metric: string): string {
  if (value == null) return EM_DASH;
  const signed = `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
  return metric === "net_pnl" ? signed : `${signed}%`;
}

/** "12 vs 9" — the two sample sizes every delta must be read beside. */
export function deltaSample(nChampion: number, nChallenger: number): string {
  return `${nChampion} vs ${nChallenger} trades`;
}

/** "1.5 → 1.9" for scalars; objects stringify compactly rather than "[object]". */
export function fmtDiffValue(value: unknown): string {
  if (value == null) return EM_DASH;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
