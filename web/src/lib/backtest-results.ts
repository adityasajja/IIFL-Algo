/**
 * Pure derivations behind the backtest results page.
 *
 * These live apart from the component because they are the parts that can be
 * *wrong* rather than merely ugly, and they are the parts worth a test. The
 * monthly-matrix reconciliation in particular is a correctness check the page
 * performs on the backend's own numbers: if the twelve months of a year do not
 * compound to the stated annual figure, something is wrong and the page has to
 * say so rather than print both numbers and let the reader pick.
 *
 * No React, no fetch, no DOM — so it is testable without a browser.
 */

import type { BacktestStatus, CurvePoint, MonthlyRow } from "../api";

export const TERMINAL_STATUSES: BacktestStatus[] = [
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

export function isTerminal(status: BacktestStatus | null | undefined): boolean {
  return !!status && TERMINAL_STATUSES.includes(status);
}

/**
 * Compound a matrix of monthly percentage returns into one figure.
 *
 * Returns `null` when no month carries a number, which is different from
 * returning 0: a run with no bars is not a run that broke even.
 */
export function compoundMonthly(matrix: MonthlyRow[]): number | null {
  let acc = 1;
  let seen = false;
  for (const row of matrix) {
    for (const month of row.months) {
      if (month === null || month === undefined) continue;
      acc *= 1 + month / 100;
      seen = true;
    }
  }
  return seen ? (acc - 1) * 100 : null;
}

export interface Reconciliation {
  compounded: number | null;
  stated: number | null;
  /** Absolute difference in percentage points, or null if either side is missing. */
  drift: number | null;
  /** True when the two agree to within `tolerance` percentage points. */
  reconciled: boolean;
}

/**
 * Compare what the months compound to against what the run states.
 *
 * The tolerance is in percentage points and defaults to 0.05, which is tight
 * enough to catch a real disagreement (a missing month, a double-counted one)
 * and loose enough to absorb the rounding in the stored per-month figures —
 * they are persisted to full precision, but a hand-entered or re-imported row
 * might not be.
 */
export function reconcile(
  matrix: MonthlyRow[],
  statedTotal: number | string | boolean | null | undefined,
  tolerance = 0.05,
): Reconciliation {
  const compounded = compoundMonthly(matrix);
  const stated = typeof statedTotal === "number" && Number.isFinite(statedTotal)
    ? statedTotal
    : null;
  const drift =
    compounded !== null && stated !== null ? Math.abs(compounded - stated) : null;
  return {
    compounded,
    stated,
    drift,
    reconciled: drift !== null && drift < tolerance,
  };
}

/**
 * The step a run is in, in words.
 *
 * The engine reports progress in coarse named stages, not a per-bar
 * percentage, so this maps the stage rather than inventing a smooth curve. A
 * smooth bar would be reporting the passage of time, not the state of the work.
 */
export function progressLabel(progress: number | null | undefined): string {
  const p = progress ?? 0;
  if (p <= 0) return "Queued — waiting for a worker";
  if (p < 0.9) return "Loading data and running the engine";
  return "Writing results";
}

/** Percentage of the progress bar to fill, floored so a fresh run is visible. */
export function progressWidth(progress: number | null | undefined): number {
  const p = Math.max(0, Math.min(1, progress ?? 0));
  return Math.max(p * 100, 4);
}

/**
 * Whether a realised loss went past the stop that should have capped it.
 *
 * A stop is a *trigger*, not a guaranteed price: the engine fills on the next
 * open, so a gap can close a trade well past its level. Labelling that a rule
 * failure would be wrong; labelling it "gapped through" is what actually
 * happened. The 0.5pp slack absorbs the difference between the entry price the
 * stop is measured from and the round-trip average the return is computed on.
 */
export function gappedThroughStop(
  exitReason: string | null,
  stopPct: number | null | undefined,
  returnPct: number | null | undefined,
): boolean {
  if (exitReason !== "stop_loss") return false;
  if (stopPct === null || stopPct === undefined) return false;
  if (returnPct === null || returnPct === undefined) return false;
  return returnPct < -stopPct - 0.5;
}

/** A curve is only worth drawing with at least two points. */
export function plottable(points: CurvePoint[] | null | undefined): boolean {
  return !!points && points.length > 1;
}

/** Trade page ranges, 1-based and inclusive, for the "showing x–y of n" label. */
export function pageRange(offset: number, limit: number, total: number) {
  if (total === 0) return { from: 0, to: 0, total };
  return { from: offset + 1, to: Math.min(offset + limit, total), total };
}
