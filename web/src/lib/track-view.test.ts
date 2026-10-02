import { describe, expect, it } from "vitest";

import {
  domain,
  drawdownSeries,
  freshnessView,
  isThin,
  nearestIndex,
  niceTicks,
  returnSeries,
  shortDay,
  signedPct,
  versus,
  type TrackPoint,
} from "./track-view";

const pts = (eq: number[], bench?: (number | null)[]): TrackPoint[] =>
  eq.map((e, i) => ({ d: `2026-07-${String(i + 1).padStart(2, "0")}`, equity: e, benchmark: bench ? bench[i] : null }));

describe("returns", () => {
  it("are measured from the base, for both lines", () => {
    const r = returnSeries(pts([100, 110, 99], [100, 102, 101]), 100);
    expect(r.map((x) => x.ret)).toEqual([0, 10, -1].map((v) => expect.closeTo(v, 9)));
    expect(r.map((x) => x.bench)).toEqual([0, 2, 1].map((v) => expect.closeTo(v, 9)));
  });
  it("keep a missing benchmark missing rather than zero", () => {
    expect(returnSeries(pts([100, 101]), 100).every((x) => x.bench === null)).toBe(true);
  });
  it("refuse a non-positive base", () => {
    expect(returnSeries(pts([100]), 0)).toEqual([]);
  });
});

describe("drawdown", () => {
  it("is depth below the running peak, zero at a new high", () => {
    const dd = drawdownSeries(pts([100, 120, 90, 120, 130]), 100);
    expect(dd[0]).toBe(0);
    expect(dd[1]).toBe(0);
    expect(dd[2]).toBeCloseTo(-25, 9);
    expect(dd[3]).toBe(0);
  });
  it("starts from the base, so a fall straight away is a drawdown", () => {
    expect(drawdownSeries(pts([90]), 100)[0]).toBeCloseTo(-10, 9);
  });
});

describe("axis", () => {
  it("ticks are round and include zero when in range", () => {
    expect(niceTicks(-1.2, 4.7)).toEqual([0, 2, 4]);
    expect(niceTicks(0, 10, 5)).toEqual([0, 2, 4, 6, 8, 10]);
  });
  it("a flat series still has a tick", () => {
    expect(niceTicks(3, 3)).toEqual([3]);
  });
  it("the domain always holds zero and is padded", () => {
    const [lo, hi] = domain([2, 5]);
    expect(lo).toBeLessThan(0);
    expect(hi).toBeGreaterThan(5);
  });
  it("an all-zero series gets a usable range", () => {
    const [lo, hi] = domain([0, 0]);
    expect(hi).toBeGreaterThan(lo);
  });
});

describe("pointer", () => {
  it("snaps to the nearest point and clamps at the ends", () => {
    expect(nearestIndex(0, 10, 100, 11)).toBe(0);
    expect(nearestIndex(60, 10, 100, 11)).toBe(5);
    expect(nearestIndex(999, 10, 100, 11)).toBe(10);
    expect(nearestIndex(5, 10, 100, 1)).toBe(0);
  });
});

describe("wording", () => {
  it("shows the sign, and a dash for unknown", () => {
    expect(signedPct(2.345)).toBe("+2.35%");
    expect(signedPct(-0.4)).toBe("−0.40%");
    expect(signedPct(0)).toBe("0.00%");
    expect(signedPct(-0.001)).toBe("0.00%");
    expect(signedPct(null)).toBe("—");
    expect(signedPct(NaN)).toBe("—");
  });
  it("dates drop the year only in the current year", () => {
    const now = new Date("2026-10-02T00:00:00");
    expect(shortDay("2026-07-03", now)).toBe("3 Jul");
    expect(shortDay("2025-12-31", now)).toBe("31 Dec 2025");
    expect(shortDay(null, now)).toBe("—");
    expect(shortDay("garbage", now)).toBe("—");
  });
  it("freshness is never green unless it is current", () => {
    expect(freshnessView("fresh", 0)).toEqual({ word: "Current", tone: "good" });
    expect(freshnessView("late", 1).tone).toBe("warn");
    expect(freshnessView("stale", 4)).toEqual({ word: "4 sessions behind", tone: "bad" });
    expect(freshnessView("missing", null).tone).toBe("bad");
  });
  it("compares with the yardstick", () => {
    expect(versus(3, 1)).toMatchObject({ diff: 2, ahead: true });
    expect(versus(1, 3)).toMatchObject({ ahead: false });
    expect(versus(1, null)).toBeNull();
  });
  it("calls a short record thin", () => {
    expect(isThin(5)).toBe(true);
    expect(isThin(60)).toBe(false);
  });
});
