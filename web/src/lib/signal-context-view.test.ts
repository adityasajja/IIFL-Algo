import { describe, expect, it } from "vitest";

import type {
  EffectivenessAxis,
  EffectivenessBucket,
  EffectivenessScoreVerdict,
  SignalContextBucket,
  SignalContextEffectiveness,
  SignalContextRecord,
  SignalScoreCriterion,
} from "../api";
import {
  analyticsAccounting,
  bucketLabel,
  bucketViews,
  contextClassTone,
  criterionViews,
  effectivenessAccounting,
  effectivenessBucketViews,
  effectivenessSignificanceLabel,
  effectivenessSignificanceTone,
  evidenceLabel,
  evidenceTone,
  forwardVsInSample,
  isSupportedFinding,
  metCount,
  missingFieldsView,
  scoreVerdictView,
  scoreView,
  signalRowView,
  suggestiveFindings,
  supportedFindings,
  unmeasuredCount,
  unsupportedAxes,
  DIMENSION_OPTIONS,
} from "./signal-context-view";

const EM_DASH = "\u2014";

function criterion(over: Partial<SignalScoreCriterion> = {}): SignalScoreCriterion {
  return {
    key: "stock_rs",
    label: "Stock outperforming NIFTY",
    weight: 20,
    met: true,
    points_awarded: 20,
    value: "+3.2%",
    reason: "stock RS +3.2% > 0%",
    ...over,
  };
}

function bucket(over: Partial<SignalContextBucket> = {}): SignalContextBucket {
  return {
    key: "STRONG_CONTEXT",
    n: 10,
    n_forward: 6,
    n_in_sample: 4,
    mean_return: 1.5,
    median_return: 1.2,
    win_rate: 0.6,
    ci_low: 0.31,
    ci_high: 0.83,
    profit_factor: 1.8,
    suppressed: false,
    evidence_note: "forward",
    ...over,
  };
}

function record(over: Partial<SignalContextRecord> = {}): SignalContextRecord {
  return {
    id: 1,
    user_id: "u1",
    signal_id: "RELIANCE:2026-01-02",
    strategy_id: "MOMENTUM_BREAKOUT",
    strategy_version: 1,
    symbol: "RELIANCE",
    action: "BUY",
    signal_source: "PAPER",
    signal_ts: "2026-01-02T09:20:00",
    context_model_version: "v1.0.0",
    context_class: "NEUTRAL_CONTEXT",
    context_score: 65,
    max_possible_score: 100,
    has_insufficient_data: false,
    run_id: null,
    trade_id: null,
    order_id: null,
    market_context: {
      regime: "BULLISH_TREND",
      nifty_trend_pct: 1.8,
      breadth_above_ema50_pct: 61.0,
      breadth_above_ema20_pct: 64.0,
      volatility_atr_pct: 0.9,
      volatility_ratio: 1.1,
      advance_decline_ratio: 1.4,
      benchmark_symbol: "NIFTYBEES-EQ",
      benchmark_is_proxy: true,
      regime_model_version: "v1.0.0",
      as_of: "2026-01-02T09:20:00",
    },
    sector_context: null,
    stock_context: {
      relative_strength_nifty_20d: 3.2,
      relative_volume: 1.9,
      atr_pct: 1.4,
      trend_pct: 2.1,
      above_sma50: true,
      from_52w_high_pct: -4.0,
      from_52w_low_pct: 40.0,
      gap_pct: 0.2,
      close: 2900,
      as_of: "2026-01-02T09:20:00",
    },
    score_breakdown: [criterion()],
    missing_fields: [],
    benchmark_provenance: {},
    created_at: "2026-01-02T09:20:05",
    ...over,
  };
}

describe("scoreView", () => {
  it("states that a score is conditions met, not a probability", () => {
    const v = scoreView(record());
    expect(v.text).toBe("65 / 100");
    expect(v.pct).toBe(65);
    expect(v.band).toBe("neutral");
    expect(v.tone).toBe("muted");
    expect(v.insufficient).toBe(false);
    expect(v.caption).toContain("not a probability of profit");
    expect(v.caption).toContain("65%");
  });

  it("bands a strong score without calling it good news about the trade", () => {
    const v = scoreView({ context_score: 80, max_possible_score: 100, has_insufficient_data: false });
    expect(v.band).toBe("strong");
    expect(v.tone).toBe("good");
    expect(v.caption).toContain("not a probability");
  });

  it("reports an unmeasurable score as a lower bound, never a figure", () => {
    const v = scoreView({ context_score: 30, max_possible_score: 100, has_insufficient_data: true });
    expect(v.insufficient).toBe(true);
    expect(v.pct).toBeNull();
    expect(v.text).toBe("\u226530 / 100");
    expect(v.tone).toBe("muted");
    expect(v.caption).toContain("lower bound");
  });

  it("refuses to invent a percentage when the maximum is unknown", () => {
    const v = scoreView({ context_score: 0, max_possible_score: 0, has_insufficient_data: false });
    expect(v.pct).toBeNull();
    expect(v.text).toBe("0 / 0");
    expect(v.caption).toContain("cannot be read as a share");
  });
});

describe("criterionViews", () => {
  it("marks a met criterion as met with its awarded points", () => {
    const [v] = criterionViews([criterion()]);
    expect(v.status).toBe("met");
    expect(v.points).toBe("+20");
    expect(v.tone).toBe("good");
  });

  it("distinguishes a measured-but-unmet condition from a missing one", () => {
    const [notMet, notMeasured, hasReason] = criterionViews([
      criterion({ met: false, points_awarded: 0, value: "-1.0%" }),
      criterion({ met: false, points_awarded: 0, value: null, reason: null }),
      criterion({
        met: false,
        points_awarded: 0,
        value: null,
        reason: "sector relative strength unavailable",
      }),
    ]);
    expect(notMet.status).toBe("not_met");
    expect(notMet.tone).toBe("warn");
    expect(notMet.value).toBe("-1.0%");

    expect(notMeasured.status).toBe("not_measured");
    expect(notMeasured.tone).toBe("muted");
    expect(notMeasured.value).toBe(EM_DASH);
    expect(notMeasured.reason).toContain("not measurable");

    expect(hasReason.status).toBe("not_measured");
    expect(hasReason.reason).toContain("unavailable");
  });

  it("counts met and unmeasured criteria separately", () => {
    const breakdown = [
      criterion({ met: true }),
      criterion({ met: false, value: "-1%" }),
      criterion({ met: false, value: null }),
    ];
    expect(metCount(breakdown)).toBe(1);
    expect(unmeasuredCount(breakdown)).toBe(1);
  });
});

describe("missingFieldsView", () => {
  it("treats an empty missing list as a real, stated result", () => {
    const v = missingFieldsView([]);
    expect(v.none).toBe(true);
    expect(v.text).toContain("Every condition was measurable");
  });

  it("names what could not be measured", () => {
    const v = missingFieldsView(["sector_rs_1m", "breadth_above_ema50_pct"]);
    expect(v.none).toBe(false);
    expect(v.text).toContain("sector_rs_1m");
    expect(v.text).toContain("breadth_above_ema50_pct");
  });
});

describe("bucketViews", () => {
  it("shows forward statistics and says they are forward", () => {
    const [v] = bucketViews([bucket()]);
    expect(v.suppressed).toBe(false);
    expect(v.statsShown).toBe(true);
    expect(v.evidenceLabel).toBe("Forward");
    expect(v.evidenceTone).toBe("good");
    expect(v.mean).toBe("+1.50%");
    expect(v.winRate).toBe("60.0%");
    expect(v.ci).toBe("[31.0% \u2026 83.0%]");
    expect(v.unresolved).toBe(0);
  });

  it("shows in-sample statistics but labels them in-sample, not suppressed", () => {
    const [v] = bucketViews([
      bucket({ n_forward: 0, n_in_sample: 10, evidence_note: "in_sample_only" }),
    ]);
    expect(v.suppressed).toBe(false);
    expect(v.statsShown).toBe(true);
    expect(v.evidenceLabel).toBe("In-sample");
    expect(v.evidenceTone).toBe("muted");
    expect(v.mean).toBe("+1.50%");
  });

  it("suppresses a thin forward bucket but keeps its counts", () => {
    const [v] = bucketViews([
      bucket({
        n: 12,
        n_forward: 2,
        n_in_sample: 0,
        mean_return: 3.0,
        median_return: 3.0,
        win_rate: 1.0,
        ci_low: 0.3,
        ci_high: 1.0,
        profit_factor: 4.0,
        suppressed: true,
        evidence_note: "insufficient_forward_observations",
      }),
    ]);
    expect(v.suppressed).toBe(true);
    expect(v.statsShown).toBe(false);
    expect(v.n).toBe(12);
    expect(v.nForward).toBe(2);
    expect(v.unresolved).toBe(10);
    expect(v.mean).toBe(EM_DASH);
    expect(v.winRate).toBe(EM_DASH);
    expect(v.profitFactor).toBe(EM_DASH);
    expect(v.ci).toBe("");
    expect(v.evidenceTone).toBe("warn");
    expect(v.evidenceLabel).toBe("Thin forward");
  });

  it("never renders an absent mean as a measured zero", () => {
    const [v] = bucketViews([
      bucket({
        n_forward: 0,
        n_in_sample: 0,
        mean_return: null,
        median_return: null,
        win_rate: null,
        ci_low: null,
        ci_high: null,
        profit_factor: null,
        suppressed: true,
        evidence_note: "no_resolvable_outcomes",
      }),
    ]);
    expect(v.mean).toBe(EM_DASH);
    expect(v.mean).not.toBe("0.00");
    expect(v.median).toBe(EM_DASH);
    expect(v.evidenceLabel).toBe("No outcomes");
    expect(v.evidenceTone).toBe("muted");
  });
});

describe("analyticsAccounting", () => {
  it("says how many outcomes are forward versus unresolved", () => {
    const v = analyticsAccounting([
      bucket({ n: 10, n_forward: 6, n_in_sample: 4 }),
      bucket({ key: "WEAK_CONTEXT", n: 5, n_forward: 0, n_in_sample: 0 }),
    ]);
    expect(v.total).toBe(15);
    expect(v.forward).toBe(6);
    expect(v.inSample).toBe(4);
    expect(v.unresolved).toBe(5);
    expect(v.note).toContain("6 forward");
    expect(v.note).toContain("5 unresolved");
  });

  it("says nothing is recorded when there are no buckets", () => {
    const v = analyticsAccounting([]);
    expect(v.total).toBe(0);
    expect(v.note).toContain("nothing to bucket");
  });
});

describe("categorical labels", () => {
  it("does not tone a refusal to judge as bad news", () => {
    expect(contextClassTone("INSUFFICIENT_DATA")).toBe("muted");
    expect(contextClassTone("NEUTRAL_CONTEXT")).toBe("muted");
    expect(contextClassTone("WEAK_CONTEXT")).toBe("warn");
    expect(contextClassTone("STRONG_CONTEXT")).toBe("good");
  });

  it("humanises known bucket keys and degrades unknown ones without emptying them", () => {
    expect(bucketLabel("INSUFFICIENT_DATA")).toBe("Insufficient data");
    expect(bucketLabel("70-100")).toBe("Score 70\u2013100");
    expect(bucketLabel("SOME_NEW_KEY")).toBe("SOME NEW KEY");
  });

  it("treats an unrecognised evidence note as unknown rather than forward", () => {
    expect(evidenceTone("forward")).toBe("good");
    expect(evidenceTone("in_sample_only")).toBe("muted");
    expect(evidenceLabel("something_new" as never)).toBe("Unknown");
  });
});

describe("signalRowView", () => {
  it("carries the score view and regime through to the row", () => {
    const row = signalRowView(record());
    expect(row.symbol).toBe("RELIANCE");
    expect(row.regime).toBe("BULLISH_TREND");
    expect(row.score.text).toBe("65 / 100");
    expect(row.contextClassLabel).toBe("Neutral context");
    expect(row.modelVersion).toBe("v1.0.0");
  });
});

describe("DIMENSION_OPTIONS", () => {
  it("offers every backend dimension exactly once", () => {
    const keys = DIMENSION_OPTIONS.map((o) => o.key).sort();
    expect(keys).toEqual(
      [
        "breadth",
        "context_class",
        "regime",
        "score_band",
        "sector_rs",
        "stock_rs",
        "volatility",
      ].sort(),
    );
  });
});

describe("effectiveness significance", () => {
  it("tones strong and moderate as findings, weak as a warning, the rest as muted", () => {
    expect(effectivenessSignificanceTone("strong")).toBe("good");
    expect(effectivenessSignificanceTone("moderate")).toBe("good");
    expect(effectivenessSignificanceTone("weak")).toBe("warn");
    expect(effectivenessSignificanceTone("not_significant")).toBe("muted");
    expect(effectivenessSignificanceTone("insufficient_sample")).toBe("muted");
    expect(effectivenessSignificanceTone("in_sample_not_a_claim")).toBe("muted");
    expect(effectivenessSignificanceLabel("in_sample_not_a_claim")).toBe("In-sample");
    expect(effectivenessSignificanceLabel("not_significant")).toBe("No distinction");
  });

  it("never lets an in-sample bucket read as supported, however large", () => {
    expect(
      isSupportedFinding({
        suppressed: false,
        significance: "in_sample_not_a_claim",
        lift: 5.0,
      }),
    ).toBe(false);
    expect(
      isSupportedFinding({ suppressed: false, significance: "strong", lift: 5.0 }),
    ).toBe(true);
    expect(
      isSupportedFinding({ suppressed: false, significance: "moderate", lift: 0.5 }),
    ).toBe(true);
    expect(
      isSupportedFinding({ suppressed: false, significance: "weak", lift: 0.5 }),
    ).toBe(false);
    expect(
      isSupportedFinding({ suppressed: true, significance: "strong", lift: 5.0 }),
    ).toBe(false);
    expect(
      isSupportedFinding({ suppressed: false, significance: "strong", lift: null }),
    ).toBe(false);
  });
});

function effStats(over: Partial<EffectivenessBucket["stats"]> = {}) {
  return {
    n: 12,
    wins: 9,
    win_rate: 0.75,
    win_rate_ci: [0.46, 0.91] as [number, number],
    mean: 3.0,
    mean_ci: [1.0, 5.0] as [number, number],
    median: 2.8,
    trimmed_mean: 2.9,
    stdev: 2.0,
    without_best: 2.5,
    profit_factor: 2.2,
    payoff_ratio: 1.5,
    total: 36.0,
    best: 6.0,
    worst: -1.0,
    small_sample: true,
    ...over,
  };
}

function effBucket(over: Partial<EffectivenessBucket> = {}): EffectivenessBucket {
  return {
    label: "80-100",
    n: 12,
    sample_size: 12,
    stats: effStats(),
    win_rate: 0.75,
    win_rate_ci: [0.46, 0.91],
    mean: 3.0,
    mean_ci: [1.0, 5.0],
    median: 2.8,
    profit_factor: 2.2,
    is_meaningful: true,
    sample_adequacy: "adequate",
    lift: 4.0,
    p_value: 0.002,
    p_adjusted: 0.02,
    significance: "strong",
    comparisons: 30,
    suppressed: false,
    note: null,
    max_drawdown: 1.5,
    n_forward: 12,
    evidence_class_forward: "PAPER_FORWARD",
    ...over,
  };
}

function effAxis(over: Partial<EffectivenessAxis> = {}): EffectivenessAxis {
  return {
    axis: "context_score",
    label: "Context score at signal",
    max_values: 4,
    coverage: { with_value: 24, scanned: 24 },
    excluded: { axis_missing: 0, outcome_missing: 0 },
    buckets: [effBucket()],
    ...over,
  };
}

function effResult(over: Partial<SignalContextEffectiveness> = {}): SignalContextEffectiveness {
  return {
    metric: "return_pct",
    min_sample: 10,
    bonferroni_comparisons: 30,
    model_note: "the context score is the sum of met criteria, not a probability",
    forward_n: 24,
    forward_with_metric: 24,
    in_sample_n: 0,
    score_verdict: {
      bands: ["80-100", "0-39"],
      means: { "80-100": 3.0, "0-39": -1.0 },
      monotonic_high_is_better: true,
      high_band: effBucket(),
      low_band: effBucket({
        label: "0-39",
        mean: -1.0,
        lift: -4.0,
        significance: "strong",
      }),
      may_claim: true,
      statement: "the 80-100 band averaged 3.0 over 12 forward trades",
    } as EffectivenessScoreVerdict,
    axes: [effAxis()],
    in_sample_axes: [],
    caveats: ["the scoring model is unchanged"],
    generated_at: "2026-09-17T00:00:00",
    model_version: "v1.0.0",
    ...over,
  };
}

describe("effectivenessBucketViews", () => {
  it("renders a supported bucket with lift, adjusted p and intervals", () => {
    const [v] = effectivenessBucketViews(effAxis(), "return_pct");
    expect(v.label).toBe("Score 80\u2013100");
    expect(v.n).toBe(12);
    expect(v.statsShown).toBe(true);
    expect(v.supported).toBe(true);
    expect(v.mean).toBe("+3.00%");
    expect(v.meanCI).toBe("[+1.00% \u2026 +5.00%]");
    expect(v.lift).toBe("+4.00%");
    expect(v.pAdjusted).toBe("0.0200");
    expect(v.significanceLabel).toBe("Strong");
    expect(v.significanceTone).toBe("good");
  });

  it("renders a suppressed bucket as counts and em dashes, never 0.00", () => {
    const [v] = effectivenessBucketViews(
      effAxis({ buckets: [effBucket({ suppressed: true, significance: "insufficient_sample" })] }),
      "return_pct",
    );
    expect(v.n).toBe(12);
    expect(v.statsShown).toBe(false);
    expect(v.supported).toBe(false);
    expect(v.mean).toBe(EM_DASH);
    expect(v.mean).not.toBe("0.00");
    expect(v.lift).toBe(EM_DASH);
    expect(v.pAdjusted).toBe(EM_DASH);
    expect(v.significanceLabel).toBe("Too few");
  });

  it("formats net_pnl without a percent sign", () => {
    const [v] = effectivenessBucketViews(effAxis(), "net_pnl");
    expect(v.mean).toBe("+3.00");
    expect(v.lift).toBe("+4.00");
  });
});

describe("scoreVerdictView", () => {
  it("carries the claim and the per-band means through", () => {
    const v = scoreVerdictView(effResult().score_verdict, "return_pct");
    expect(v.mayClaim).toBe(true);
    expect(v.claimLabel).toBe("Supported");
    expect(v.claimTone).toBe("good");
    expect(v.monotonic).toBe(true);
    expect(v.bands.map((b) => b.meanText)).toEqual(["+3.00%", "-1.00%"]);
  });

  it("reads as not proven when the analysis withholds the claim", () => {
    const v = scoreVerdictView(
      { ...effResult().score_verdict, may_claim: false },
      "return_pct",
    );
    expect(v.mayClaim).toBe(false);
    expect(v.claimLabel).toBe("Not proven");
    expect(v.claimTone).toBe("muted");
  });
});

describe("supportedFindings / suggestiveFindings / unsupportedAxes", () => {
  it("lists strong buckets, separates weak ones, and names what is unproven", () => {
    const result = effResult({
      axes: [
        effAxis(),
        effAxis({
          axis: "market_breadth",
          label: "Market breadth at entry",
          buckets: [effBucket({ label: "healthy_55_70", significance: "weak", lift: 1.0 })],
        }),
        effAxis({
          axis: "volatility_regime",
          label: "Market volatility regime",
          buckets: [
            effBucket({ label: "normal", suppressed: true, significance: "insufficient_sample", n: 3 }),
          ],
        }),
      ],
    });
    const supported = supportedFindings(result);
    expect(supported.map((f) => f.bucketLabel)).toEqual(["Score 80\u2013100"]);
    expect(supported[0].axisLabel).toBe("Context score at signal");

    const suggestive = suggestiveFindings(result);
    expect(suggestive.map((f) => f.bucketLabel)).toEqual(["healthy 55 70"]);

    const unsupported = unsupportedAxes(result);
    expect(unsupported.map((u) => u.axis).sort()).toEqual([
      "market_breadth",
      "volatility_regime",
    ]);
  });
});

describe("forwardVsInSample", () => {
  it("places forward and in-sample buckets side by side without merging them", () => {
    const result = effResult({
      in_sample_axes: [
        effAxis({
          buckets: [effBucket({ significance: "in_sample_not_a_claim", mean: 1.0 })],
        }),
      ],
    });
    const rows = forwardVsInSample("context_score", result);
    expect(rows).toHaveLength(1);
    expect(rows[0].forwardMean).toBe("+3.00%");
    expect(rows[0].inSampleMean).toBe("+1.00%");
    expect(rows[0].forwardN).toBe(12);
    expect(rows[0].inSampleN).toBe(12);
  });
});

describe("effectivenessAccounting", () => {
  it("states the sample, the floor and the correction in one note", () => {
    const v = effectivenessAccounting(effResult());
    expect(v.forward).toBe(24);
    expect(v.withMetric).toBe(24);
    expect(v.note).toContain("24 forward contexts");
    expect(v.note).toContain("floor 10 trades");
    expect(v.note).toContain("30 comparisons");
  });

  it("says plainly when there is no forward evidence yet", () => {
    const v = effectivenessAccounting(effResult({ forward_n: 0, forward_with_metric: 0 }));
    expect(v.note).toContain("No forward");
  });
});
