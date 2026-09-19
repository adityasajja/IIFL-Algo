import { describe, expect, it } from "vitest";

import type { AnalyticsBucket, EvidenceCounts, MaeMfePoint } from "../api";
import {
  EM_DASH,
  bps,
  bucketLabel,
  bucketViews,
  bucketedAccounting,
  codeFamily,
  codeGroups,
  coverageView,
  distributionView,
  duration,
  evidenceCountsView,
  filterChips,
  gradeView,
  isEmptyBook,
  money,
  num,
  pct,
  profitFactor,
  rMultiple,
  scatterPoints,
  scopeNote,
} from "./analytics-view";

// ─── the rule that everything else depends on ─────────────────────────────────

describe("a missing number is never rendered as zero", () => {
  it("formats null and undefined as an em dash", () => {
    expect(num(null)).toBe(EM_DASH);
    expect(num(undefined)).toBe(EM_DASH);
    expect(pct(null)).toBe(EM_DASH);
    expect(money(null)).toBe(EM_DASH);
    expect(bps(null)).toBe(EM_DASH);
    expect(rMultiple(null)).toBe(EM_DASH);
    expect(duration(null)).toBe(EM_DASH);
  });

  it("formats a real zero as zero, because zero is a measurement", () => {
    expect(num(0)).toBe("0");
    expect(pct(0)).toBe("+0.00%");
    expect(money(0)).toContain("0");
    expect(bps(0)).toBe("+0.0bps");
  });

  it("refuses to render a non-finite number", () => {
    // A division that produced Infinity should read as "unknown", not as a
    // number a reader might act on.
    expect(num(Number.POSITIVE_INFINITY)).toBe(EM_DASH);
    expect(num(Number.NaN)).toBe(EM_DASH);
    expect(profitFactor(Number.POSITIVE_INFINITY)).toBe(EM_DASH);
  });
});

describe("profit factor is undefined rather than infinite", () => {
  it("shows an em dash when the API could not define it", () => {
    expect(profitFactor(null)).toBe(EM_DASH);
  });

  it("shows the number when it is defined", () => {
    expect(profitFactor(2.5)).toBe("2.50");
    expect(profitFactor(0.8)).toBe("0.80");
  });
});

describe("slippage is adverse-positive", () => {
  it("shows a leading plus for a cost and a minus for a favourable fill", () => {
    expect(bps(12.5)).toBe("+12.5bps");
    expect(bps(-3.2)).toBe("-3.2bps");
  });
});

// ─── evidence vocabulary ──────────────────────────────────────────────────────

describe("evidence counts state the forward share", () => {
  it("says 'zero of them forward' rather than only the total", () => {
    const counts: EvidenceCounts = {
      n: 112,
      forward_n: 0,
      in_sample_n: 112,
      by_grade: { in_sample: 112 },
      by_class: { IN_SAMPLE: 112 },
      all_forward: false,
    };
    const view = evidenceCountsView(counts);
    expect(view.label).toBe("112 trades, 0 of them forward");
    expect(view.tone).toBe("warn");
    expect(view.allForward).toBe(false);
  });

  it("splits a mixed book explicitly", () => {
    const view = evidenceCountsView({
      n: 10,
      forward_n: 3,
      in_sample_n: 7,
      by_grade: { forward: 3, in_sample: 7 },
      by_class: { LIVE_FORWARD: 3, IN_SAMPLE: 7 },
      all_forward: false,
    });
    expect(view.label).toContain("3 forward");
    expect(view.label).toContain("7 in-sample");
  });

  it("marks an all-forward book as such", () => {
    const view = evidenceCountsView({
      n: 4,
      forward_n: 4,
      in_sample_n: 0,
      by_grade: { forward: 4 },
      by_class: { LIVE_FORWARD: 4 },
      all_forward: true,
    });
    expect(view.label).toBe("4 trades, all forward");
    expect(view.tone).toBe("good");
    expect(view.allForward).toBe(true);
  });

  it("handles absent counts without inventing a book", () => {
    const view = evidenceCountsView(null);
    expect(view.total).toBe(0);
    expect(view.tone).toBe("flat");
  });
});

describe("grade labelling", () => {
  it("calls forward the only claimable grade", () => {
    const view = gradeView("forward", "LIVE_FORWARD");
    expect(view.label).toBe("Forward");
    expect(view.tone).toBe("good");
    expect(view.hint).toContain("only basis for a claim");
  });

  it("warns on in-sample and explains why", () => {
    const view = gradeView("in_sample", "BACKTEST");
    expect(view.label).toBe("In-sample");
    expect(view.tone).toBe("warn");
    expect(view.hint).toContain("Not independent evidence");
  });

  it("treats an absent grade as in-sample, the conservative default", () => {
    expect(gradeView(null, null).label).toBe("In-sample");
    expect(gradeView(undefined, undefined).tone).toBe("warn");
  });
});

describe("coverage distinguishes 'unknown' from 'incomplete'", () => {
  it("reports a complete book as good", () => {
    const view = coverageView({ attributed: 40, closed_trades: 40, complete: true });
    expect(view.tone).toBe("good");
    expect(view.warning).toBeNull();
  });

  it("warns and quantifies the gap on a partial book", () => {
    const view = coverageView({ attributed: 11, closed_trades: 400, complete: false });
    expect(view.tone).toBe("warn");
    expect(view.text).toContain("11 of 400");
    expect(view.warning).toContain("389");
  });

  it("does NOT claim incompleteness when the count could not be taken", () => {
    // complete === null means "we could not count", which is a different
    // statement from "the book is not fully attributed".
    const view = coverageView({ attributed: 7, closed_trades: null, complete: null });
    expect(view.warning).toContain("could not be read");
    expect(view.tone).toBe("flat");
  });
});

// ─── buckets ──────────────────────────────────────────────────────────────────

function bucket(over: Partial<AnalyticsBucket> = {}): AnalyticsBucket {
  return {
    key: "signal_present",
    n: 20,
    net_pnl: 500,
    net_return_pct_mean: 0.4,
    win_rate: 0.55,
    expectancy: 25,
    profit_factor: 1.6,
    mean_holding_sec: 7200,
    total_costs: 100,
    suppressed: false,
    counts: {
      n: 20,
      forward_n: 0,
      in_sample_n: 20,
      by_grade: { in_sample: 20 },
      by_class: { IN_SAMPLE: 20 },
      all_forward: false,
    },
    ...over,
  };
}

describe("bucket views", () => {
  it("labels known keys in plain language", () => {
    expect(bucketLabel("signal_present")).toBe("Signal linked");
    expect(bucketLabel("high_slippage")).toBe("High slippage");
    expect(bucketLabel("stop_driven")).toBe("Stop");
  });

  it("falls back to an underscored key rather than dropping it", () => {
    expect(bucketLabel("some_new_bucket")).toBe("Some New Bucket");
  });

  it("carries the suppressed flag through untouched", () => {
    const views = bucketViews([bucket({ n: 3, suppressed: true })]);
    expect(views[0].suppressed).toBe(true);
    expect(views[0].thin).toBe(true);
  });

  it("treats a zero-count bucket as suppressed", () => {
    const views = bucketViews([bucket({ n: 0, suppressed: false, net_pnl: null })]);
    expect(views[0].suppressed).toBe(true);
  });

  it("is empty for absent buckets rather than throwing", () => {
    expect(bucketViews(null)).toEqual([]);
    expect(bucketViews(undefined)).toEqual([]);
  });
});

describe("bucketed accounting states what was excluded", () => {
  it("says nothing when every row was placed", () => {
    const views = bucketViews([bucket({ n: 10 }), bucket({ key: "context_neutral", n: 5 })]);
    const acc = bucketedAccounting(views, 15);
    expect(acc.excluded).toBe(0);
    expect(acc.note).toBeNull();
  });

  it("explains the shortfall when rows could not be classified", () => {
    // The count matters: those rows are still in every headline figure, so the
    // table's totals disagreeing with the book needs a sentence.
    const views = bucketViews([bucket({ n: 10 })]);
    const acc = bucketedAccounting(views, 25);
    expect(acc.excluded).toBe(15);
    expect(acc.note).toContain("15 of 25");
    expect(acc.note).toContain("remain in every headline figure");
  });
});

// ─── MAE / MFE ────────────────────────────────────────────────────────────────

describe("scatter points drop what cannot be plotted", () => {
  const points: MaeMfePoint[] = [
    {
      trade_id: "T-1",
      symbol: "RELIANCE",
      net_pnl: 400,
      return_pct: 4,
      mfe_pct: 6,
      mae_pct: -2,
      mfe_over_risk: 2,
      mae_over_risk: 0.7,
      realized_over_risk: 1.3,
      capped_by_stop: false,
      evidence_grade: "forward",
    },
    {
      trade_id: "T-2",
      symbol: "TCS",
      net_pnl: -300,
      return_pct: -3,
      mfe_pct: null,
      mae_pct: null,
      mfe_over_risk: null,
      mae_over_risk: null,
      realized_over_risk: -1,
      capped_by_stop: true,
      evidence_grade: "in_sample",
    },
  ];

  it("plots only trades with both an excursion and an outcome", () => {
    const out = scatterPoints(points, "mfe");
    expect(out.length).toBe(1);
    expect(out[0].tradeId).toBe("T-1");
  });

  it("does not plot an unmeasured excursion at the origin", () => {
    // A point at (0, y) would read as the strongest possible "no relationship",
    // which is a claim the data does not support.
    const out = scatterPoints(points, "mae");
    expect(out.map((p) => p.tradeId)).not.toContain("T-2");
  });

  it("marks a stop-driven trade so it can be drawn differently", () => {
    const withStop = scatterPoints(
      [{ ...points[0], capped_by_stop: true }],
      "mfe"
    );
    expect(withStop[0].forced).toBe(true);
  });

  it("falls back to the return when there is no rupee figure", () => {
    // The paper ledger records a return and no position size.
    const out = scatterPoints(
      [{ ...points[0], net_pnl: null, return_pct: 2.5 }],
      "mfe"
    );
    expect(out[0].y).toBe(2.5);
  });

  it("is empty rather than throwing on absent points", () => {
    expect(scatterPoints(null)).toEqual([]);
  });
});

describe("distribution views state what they measured", () => {
  it("names the unmeasured count when some trades had no bars", () => {
    const view = distributionView({ mean: 1.4, median: 1.2, measured: 12 }, 90);
    expect(view.measured).toBe(12);
    expect(view.unmeasured).toBe(78);
    expect(view.note).toContain("12 of 90");
  });

  it("says so when everything was measured", () => {
    const view = distributionView({ mean: 1.4, median: 1.2, measured: 30 }, 30);
    expect(view.note).toContain("all 30");
  });

  it("reports an absent distribution without inventing zeros", () => {
    const view = distributionView(null, 10);
    expect(view.mean).toBeNull();
    expect(view.measured).toBe(0);
    expect(view.unmeasured).toBe(10);
  });
});

// ─── reason codes ─────────────────────────────────────────────────────────────

describe("code families keep decisions apart from execution", () => {
  it("classifies a decision code as a decision", () => {
    expect(codeFamily("CONTEXT_NEGATIVE")).toBe("decision");
    expect(codeFamily("OVERSIZED")).toBe("decision");
  });

  it("classifies a fill-quality code as execution", () => {
    expect(codeFamily("POOR_ENTRY")).toBe("execution");
    expect(codeFamily("HIGH_SLIPPAGE")).toBe("execution");
  });

  it("classifies an exit code as an exit", () => {
    expect(codeFamily("STOP_DRIVEN")).toBe("exit");
    expect(codeFamily("TARGET_DRIVEN")).toBe("exit");
  });

  it("puts an unknown code in 'other' rather than guessing a family", () => {
    expect(codeFamily("SOME_FUTURE_CODE")).toBe("other");
  });

  it("groups in a stable order so a stored record does not appear to change", () => {
    const groups = codeGroups(["POOR_ENTRY", "CONTEXT_NEGATIVE", "STOP_DRIVEN"]);
    expect(groups.map((g) => g.family)).toEqual(["decision", "execution", "exit"]);
  });

  it("never mixes a decision code into the execution group", () => {
    const groups = codeGroups(["CONTEXT_POSITIVE", "HIGH_SLIPPAGE"]);
    const decision = groups.find((g) => g.family === "decision");
    const execution = groups.find((g) => g.family === "execution");
    expect(decision?.codes).toEqual(["CONTEXT POSITIVE"]);
    expect(execution?.codes).toEqual(["HIGH SLIPPAGE"]);
  });

  it("is empty for no codes", () => {
    expect(codeGroups(null)).toEqual([]);
    expect(codeGroups([])).toEqual([]);
  });
});

// ─── scope and empty states ───────────────────────────────────────────────────

describe("scope note", () => {
  it("says nothing on this screen is a finding when the book is empty", () => {
    const note = scopeNote({ n: 0, forward_n: 0, in_sample_n: 0, by_grade: {}, by_class: {}, all_forward: false }, null);
    expect(note).toContain("Nothing on this screen is a finding");
  });

  it("states plainly when there are no forward trades at all", () => {
    const note = scopeNote(
      {
        n: 140,
        forward_n: 0,
        in_sample_n: 140,
        by_grade: { in_sample: 140 },
        by_class: { IN_SAMPLE: 140 },
        all_forward: false,
      },
      { attributed: 140, closed_trades: 140, complete: true }
    );
    expect(note).toContain("0 of them forward");
    expect(note).toContain("nothing below is independent evidence");
  });

  it("separates the two grades on a mixed book", () => {
    const note = scopeNote(
      {
        n: 10,
        forward_n: 2,
        in_sample_n: 8,
        by_grade: { forward: 2, in_sample: 8 },
        by_class: { LIVE_FORWARD: 2, IN_SAMPLE: 8 },
        all_forward: false,
      },
      { attributed: 10, closed_trades: 10, complete: true }
    );
    expect(note).toContain("Only the 2 forward trades can support a claim");
  });

  it("states plainly when the whole book is forward", () => {
    const note = scopeNote(
      {
        n: 5,
        forward_n: 5,
        in_sample_n: 0,
        by_grade: { forward: 5 },
        by_class: { LIVE_FORWARD: 5 },
        all_forward: true,
      },
      { attributed: 5, closed_trades: 5, complete: true }
    );
    expect(note).toContain("all forward");
    expect(note).toContain("all 5 closed trades attributed");
  });
});

describe("empty book detection", () => {
  it("treats absent counts as empty", () => {
    expect(isEmptyBook(null)).toBe(true);
    expect(isEmptyBook(undefined)).toBe(true);
  });

  it("treats zero rows as empty", () => {
    expect(
      isEmptyBook({ n: 0, forward_n: 0, in_sample_n: 0, by_grade: {}, by_class: {}, all_forward: false })
    ).toBe(true);
  });

  it("treats a non-empty book as non-empty", () => {
    expect(
      isEmptyBook({ n: 1, forward_n: 0, in_sample_n: 1, by_grade: {}, by_class: {}, all_forward: false })
    ).toBe(false);
  });
});

describe("filter chips", () => {
  it("lists only the filters actually in force", () => {
    expect(filterChips({ strategy_id: "momentum", symbol: null, source: "" })).toEqual([
      "strategy id: momentum",
    ]);
  });

  it("is empty when nothing is filtered", () => {
    expect(filterChips({})).toEqual([]);
    expect(filterChips(null)).toEqual([]);
  });
});

describe("R multiple formatting", () => {
  it("signs the figure so a loss is unmistakable", () => {
    expect(rMultiple(1.83)).toBe("+1.83R");
    expect(rMultiple(-1.02)).toBe("-1.02R");
    expect(rMultiple(0)).toBe("+0.00R");
  });
});

describe("duration formatting", () => {
  it("scales to the unit a trader would say out loud", () => {
    expect(duration(45)).toBe("45s");
    expect(duration(1800)).toBe("30.0m");
    expect(duration(7200)).toBe("2.0h");
    expect(duration(3 * 86_400)).toBe("3.0d");
  });
});
