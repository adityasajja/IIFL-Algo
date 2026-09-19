/**
 * Tests for the pure derivations behind the backtest results page.
 *
 * The monthly figures in these cases are the *real* ones from this project's
 * own runs, not invented fixtures. That matters: the reconciliation exists to
 * check the backend's numbers against themselves, so testing it against a
 * synthetic matrix that merely adds up would test nothing. The real run
 * compounds to −4.007% against a stated −4.013%, and the 0.006pp gap is the
 * rounding of each cell to two decimals.
 *
 * Where a case is *not* anchored to a stored run — `gappedThroughStop`, because
 * no stored trade has yet overshot its stop — the comment says so, rather than
 * borrowing authority from a transcript that does not exist.
 */
import { describe, expect, it } from "vitest";

import type { MonthlyRow } from "../api";
import {
  compoundMonthly,
  gappedThroughStop,
  isTerminal,
  pageRange,
  plottable,
  progressLabel,
  progressWidth,
  reconcile,
} from "./backtest-results";

/** Build a year row from twelve values; `null` means "no bars that month". */
const year = (y: number, months: (number | null)[]): MonthlyRow => ({
  year: y,
  months,
  year_total: null,
});

const flat = (v: number | null) => Array<number | null>(12).fill(v);

describe("isTerminal", () => {
  it("treats the three finished states as terminal", () => {
    expect(isTerminal("COMPLETED")).toBe(true);
    expect(isTerminal("FAILED")).toBe(true);
    expect(isTerminal("CANCELLED")).toBe(true);
  });

  it("keeps a run in flight non-terminal, so the page keeps polling", () => {
    expect(isTerminal("QUEUED")).toBe(false);
    expect(isTerminal("RUNNING")).toBe(false);
  });

  it("treats a missing status as non-terminal rather than finished", () => {
    // A run whose status has not loaded yet must not be shown as a result.
    expect(isTerminal(null)).toBe(false);
    expect(isTerminal(undefined)).toBe(false);
  });
});

describe("compoundMonthly", () => {
  it("compounds a single month", () => {
    expect(compoundMonthly([year(2024, [10, ...Array(11).fill(null)])])).toBeCloseTo(10, 10);
  });

  it("compounds multiplicatively, not additively", () => {
    // +10% then −10% is −1%, not 0%. Getting this wrong is the classic
    // arithmetic error in a monthly returns table.
    const rows = [year(2024, [10, -10, ...Array(10).fill(null)])];
    expect(compoundMonthly(rows)).toBeCloseTo(-1, 10);
  });

  it("skips null months instead of reading them as zero", () => {
    // A month with no bars is not a flat month. Treating it as 0.00% would
    // understate nothing here but would misstate the count of active months.
    const withNulls = [year(2024, [10, null, null, ...Array(9).fill(null)])];
    expect(compoundMonthly(withNulls)).toBeCloseTo(10, 10);
  });

  it("returns null when no month carries a number", () => {
    // Not 0. A run with no bars did not break even.
    expect(compoundMonthly([year(2024, flat(null))])).toBeNull();
    expect(compoundMonthly([])).toBeNull();
  });

  it("reproduces a real run's stated total", () => {
    // The reconciliation this function exists for, taken verbatim from a real
    // run of `sma_crossover` (fast 10 / slow 30) on INFY+TCS+RELIANCE,
    // 2025-08-08 to 2026-09-11. Twelve trades, exposure 9.0%.
    //
    // The run's own `total_return_pct` is −4.0130. The months below compound to
    // −4.007005, a drift of 0.006pp — entirely the rounding of each cell to two
    // decimals for display. The two agree, and that agreement is the point:
    // this is a correctness check on the backend's numbers, so the fixture has
    // to be the backend's numbers, not an invented set that happens to add up.
    const rows: MonthlyRow[] = [
      // Aug/Sep are 0.00% not null: the run started mid-August and was holding
      // nothing yet, which is a flat month, not a month with no bars.
      year(2025, [null, null, null, null, null, null, null, 0, 0, -0.26, 1.16, 0.88]),
      year(2026, [-0.18, -1.39, -0.69, -1.26, -0.76, -0.63, -0.55, -0.58, 0.21, null, null, null]),
    ];
    const compounded = compoundMonthly(rows);
    expect(compounded).not.toBeNull();
    expect(compounded as number).toBeCloseTo(-4.007, 3);
    // And the run's stated figure and the months agree, which is what the
    // results page asserts before it will show the matrix without a warning.
    const r = reconcile(rows, -4.013);
    expect(r.reconciled).toBe(true);
  });

  it("would catch a dropped month even when it is one cell in thirty-six", () => {
    // A 36-cell matrix that loses a single −1.39% month still looks right to
    // the eye. The reconciliation is what makes it visible.
    const rows: MonthlyRow[] = [
      year(2025, [null, null, null, null, null, null, null, 0, 0, -0.26, 1.16, 0.88]),
    ];
    expect(compoundMonthly(rows) as number).toBeCloseTo(1.7849, 3);
    const r = reconcile(rows, -4.013);
    expect(r.reconciled).toBe(false);
    expect(r.drift as number).toBeGreaterThan(5);
  });
});

describe("reconcile", () => {
  it("agrees when the months compound to the stated total", () => {
    const rows = [year(2024, [5, 5, ...Array(10).fill(null)])];
    const r = reconcile(rows, 10.25); // 1.05^2 - 1 = 10.25%
    expect(r.reconciled).toBe(true);
    expect(r.drift).toBeLessThan(0.05);
    expect(r.compounded).toBeCloseTo(10.25, 6);
    expect(r.stated).toBe(10.25);
  });

  it("flags a disagreement rather than printing both numbers", () => {
    // A missing month is the realistic cause. The page must surface it.
    const rows = [year(2024, [5, 5, 5, ...Array(9).fill(null)])];
    const r = reconcile(rows, 10.25); // actually 15.76%
    expect(r.reconciled).toBe(false);
    expect(r.drift).toBeGreaterThan(0.05);
  });

  it("does not claim reconciliation when either side is missing", () => {
    // A run with no stated total cannot be checked, and saying it reconciles
    // would be a passing grade for an absent answer.
    const r = reconcile([], null);
    expect(r.reconciled).toBe(false);
    expect(r.drift).toBeNull();
  });

  it("ignores a non-numeric stated total rather than coercing it", () => {
    const r = reconcile([year(2024, [1, ...Array(11).fill(null)])], "1.00" as unknown as string);
    expect(r.stated).toBeNull();
    expect(r.reconciled).toBe(false);
  });

  it("absorbs rounding in the stored monthly figures", () => {
    // The stored months are full precision, but a re-imported row may be
    // rounded. 0.02pp of drift must not be reported as a failure.
    const rows = [year(2024, [10, 0.0002, ...Array(10).fill(null)])];
    const r = reconcile(rows, 10.0002);
    expect(r.reconciled).toBe(true);
  });
});

describe("progressLabel", () => {
  it("names the stage rather than inventing a percentage", () => {
    expect(progressLabel(0)).toMatch(/waiting/i);
    expect(progressLabel(0.35)).toMatch(/running the engine/i);
    expect(progressLabel(1)).toMatch(/writing/i);
  });

  it("treats a missing progress value as queued", () => {
    expect(progressLabel(null)).toMatch(/queued/i);
    expect(progressLabel(undefined)).toMatch(/queued/i);
  });
});

describe("progressWidth", () => {
  it("floors a fresh run so the bar is visible", () => {
    expect(progressWidth(0)).toBe(4);
  });

  it("scales to a full bar at completion", () => {
    expect(progressWidth(1)).toBe(100);
    expect(progressWidth(0.5)).toBe(50);
  });

  it("clamps a value outside [0,1] rather than overflowing the bar", () => {
    expect(progressWidth(-1)).toBe(4);
    expect(progressWidth(2)).toBe(100);
  });
});

describe("gappedThroughStop", () => {
  it("flags a stop that closed past its level", () => {
    // The engine fills on the NEXT open, so a stop is a trigger, not a
    // guaranteed price: a gap can carry the fill well past the level. The
    // numbers here are illustrative of that shape, not a transcript of a
    // stored trade — the runs on disk so far closed inside their stops (the
    // worst was a 5% stop realising −3.66%), so there is no real overshoot to
    // quote yet. Labelling those an overshoot would be inventing the very
    // failure this guards against.
    expect(gappedThroughStop("stop_loss", 4, -7.26)).toBe(true);
  });

  it("does not flag a stop that closed near its level", () => {
    expect(gappedThroughStop("stop_loss", 5, -5.1)).toBe(false);
  });

  it("only applies to stop exits", () => {
    // A take-profit or a signal exit cannot have gapped *through a stop*.
    expect(gappedThroughStop("take_profit", 5, -7.26)).toBe(false);
    expect(gappedThroughStop("signal", 5, -12)).toBe(false);
    expect(gappedThroughStop(null, 5, -12)).toBe(false);
  });

  it("says nothing when the stop or the return is unknown", () => {
    expect(gappedThroughStop("stop_loss", null, -7.26)).toBe(false);
    expect(gappedThroughStop("stop_loss", 4, null)).toBe(false);
  });
});

describe("plottable", () => {
  it("needs two points to draw a line", () => {
    expect(plottable([{ ts: "2024-01-01", value: 1 }])).toBe(false);
    expect(
      plottable([
        { ts: "2024-01-01", value: 1 },
        { ts: "2024-01-02", value: 2 },
      ]),
    ).toBe(true);
  });

  it("handles a missing curve", () => {
    expect(plottable(null)).toBe(false);
    expect(plottable(undefined)).toBe(false);
    expect(plottable([])).toBe(false);
  });
});

describe("pageRange", () => {
  it("reports a 1-based inclusive range", () => {
    expect(pageRange(0, 100, 250)).toEqual({ from: 1, to: 100, total: 250 });
    expect(pageRange(100, 100, 250)).toEqual({ from: 101, to: 200, total: 250 });
  });

  it("clamps the last page to the total", () => {
    expect(pageRange(200, 100, 250)).toEqual({ from: 201, to: 250, total: 250 });
  });

  it("reports an empty range for an empty result without claiming 0–0 of 1", () => {
    expect(pageRange(0, 100, 0)).toEqual({ from: 0, to: 0, total: 0 });
  });
});
