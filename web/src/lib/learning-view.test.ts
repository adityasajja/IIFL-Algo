import { describe, expect, it } from "vitest";

import type {
  DriftMetric,
  DriftPair,
  LearningAnalysis,
  LearningBucket,
  LearningDatasetSummary,
  ReadinessQualityIssue,
  ReadinessStrategy,
} from "../api";
import {
  blockedMetrics,
  bucketViews,
  emptyState,
  evidenceStatusLabel,
  evidenceStatusTone,
  evidenceView,
  findingViews,
  gateProgress,
  headlineTone,
  isRefusal,
  metricView,
  notableBuckets,
  nothingMeasured,
  orderFindings,
  qualitySeverityTone,
  readinessRowView,
  readinessStateLabel,
  readinessStateTone,
  readinessTotals,
  reportHeadline,
  sampleVerdict,
  starvedAxes,
  unrecordedMetrics,
} from "./learning-view";

/**
 * A metric that could not be scored. The point of these tests is that the
 * absence has to survive all the way to the screen — the backend is careful to
 * send `null`, and a view layer that formats it as `0.00` throws that care away.
 */
function metric(over: Partial<DriftMetric> = {}): DriftMetric {
  return {
    metric: "expectancy_per_trade",
    label: "expectancy per trade",
    higher_is_better: true,
    reference: "BACKTEST",
    comparison: "LIVE",
    status: "ok",
    reference_value: 100,
    comparison_value: 40,
    reference_n: 60,
    comparison_n: 40,
    delta: -60,
    relative: -0.6,
    direction: "deteriorated",
    magnitude: "material",
    reason: null,
    p_value: 0.001,
    significant: true,
    ...over,
  };
}

function pair(over: Partial<DriftPair> = {}): DriftPair {
  return {
    reference: "BACKTEST",
    comparison: "LIVE",
    reference_n: 60,
    comparison_n: 40,
    status: "ok",
    metrics: [],
    distribution: {},
    regime: {},
    findings: [],
    limitations: [],
    deteriorated: [],
    improved: [],
    ...over,
  };
}

describe("headlineTone", () => {
  it("tones a refusal as muted, not as good news", () => {
    // The single most important assertion in this file. "We could not check"
    // must never look like "we checked and it is fine".
    expect(headlineTone("Drift could not be measured: LIVE has no closed trades.")).toBe("muted");
    expect(headlineTone("No closed trades, so drift cannot be measured.")).toBe("muted");
  });

  it("tones a not-yet-measurable sample as muted", () => {
    expect(
      headlineTone(
        "Drift not yet measurable — 3 metric(s) were recorded on both sides but " +
          "the samples are too small to score.",
      ),
    ).toBe("muted");
  });

  it("tones real deterioration as bad", () => {
    expect(headlineTone("Drift detected — 2 metric(s) below baseline.")).toBe("bad");
  });

  it("tones an explicit no-material-drift as warn, not good", () => {
    expect(headlineTone("No material drift in the 2 metric(s) that moved.")).toBe("warn");
  });

  it("tones a clean measurement as good", () => {
    expect(
      headlineTone(
        "No drift detected across the metrics that both sources could support.",
      ),
    ).toBe("good");
  });

  it("does not tone 'no drift detected' as if it were 'drift detected'", () => {
    // The negated phrase contains the positive one as a substring, so a naive
    // ordering inverts the single most important verdict on the screen.
    expect(headlineTone("No drift detected.")).toBe("good");
    expect(headlineTone("Drift detected — 1 metric(s) below baseline.")).toBe("bad");
    expect("no drift detected").toContain("drift detected");
  });

  it("defaults an unrecognised headline to muted rather than good", () => {
    // Unknown text must not be toned favourably by accident.
    expect(headlineTone("something new the backend learned to say")).toBe("muted");
    expect(headlineTone("")).toBe("muted");
  });
});

describe("isRefusal", () => {
  it("recognises every phrasing of 'we could not check'", () => {
    for (const text of [
      "Drift could not be measured: nothing",
      "No closed trades, so drift cannot be measured.",
      "Drift not yet measurable — 3 metric(s)",
      "Only one source has trades (BACKTEST 60); drift needs two to compare.",
      "2 sources hold trades but no strategy appears in more than one",
    ]) {
      expect(isRefusal(text)).toBe(true);
    }
  });

  it("does not treat a real finding as a refusal", () => {
    expect(isRefusal("Drift detected — 2 metric(s) below baseline.")).toBe(false);
    expect(isRefusal("No drift detected across the metrics")).toBe(false);
  });
});

describe("metricView", () => {
  it("never renders an unscored metric as zero", () => {
    const view = metricView(
      metric({
        status: "insufficient",
        delta: null,
        relative: null,
        reference_value: 100,
        comparison_value: 1,
        reason: "LIVE has 8 closed trade(s); a difference needs at least 10",
      }),
    );
    expect(view.measured).toBe(false);
    expect(view.delta).not.toBe("+0.00");
    expect(view.delta).not.toBe("0.00");
    expect(view.relative).toBe("");
    expect(view.reason).toContain("8 closed trade(s)");
    expect(view.tone).toBe("muted");
  });

  it("still shows the values it did measure when withholding a verdict", () => {
    // Withholding the comparison is not the same as hiding the numbers.
    const view = metricView(
      metric({ status: "insufficient", delta: null, relative: null }),
    );
    expect(view.baseline).toBe("₹100.00");
    expect(view.live).toBe("₹40.00");
    expect(view.sample).toBe("60 vs 40");
  });

  it("formats each unit differently, because they are not interchangeable", () => {
    expect(metricView(metric({ metric: "expectancy_per_trade" })).baseline).toBe("₹100.00");
    expect(metricView(metric({ metric: "mean_return_pct", reference_value: 1 })).baseline).toBe(
      "1.00%",
    );
    expect(metricView(metric({ metric: "win_rate", reference_value: 0.62 })).baseline).toBe("62.0%");
    expect(metricView(metric({ metric: "slippage_bps", reference_value: 8.5 })).baseline).toBe(
      "8.5 bps",
    );
    expect(metricView(metric({ metric: "duration_days", reference_value: 5 })).baseline).toBe("5.00 d");
  });

  it("marks a deterioration as bad, and a within-noise one as warn", () => {
    expect(metricView(metric({ magnitude: "material" })).tone).toBe("bad");
    expect(metricView(metric({ magnitude: "within noise" })).tone).toBe("warn");
  });

  it("marks an improvement as good", () => {
    expect(
      metricView(metric({ direction: "improved", delta: 20, relative: 0.2 })).tone,
    ).toBe("good");
  });

  it("keeps holding-period changes neutral", () => {
    // A longer hold is a change, not a defect — the backend refuses to judge
    // it and the UI must not invent a colour for it.
    const view = metricView(
      metric({ metric: "duration_days", direction: "changed", delta: 2, relative: 0.4 }),
    );
    expect(view.tone).toBe("muted");
  });

  it("signs a positive delta and leaves a negative one alone", () => {
    expect(metricView(metric({ delta: 5, direction: "improved" })).delta).toBe("+₹5.00");
    expect(metricView(metric({ delta: -5 })).delta).toBe("₹-5.00");
  });
});

describe("blockedMetrics and unrecordedMetrics", () => {
  it("separates a small sample from a missing column", () => {
    // "we have some data but not enough" and "this was never recorded" call for
    // different actions, so they must not collapse into one list.
    const p = pair({
      metrics: [
        metric({ metric: "expectancy_per_trade", status: "insufficient" }),
        metric({ metric: "slippage_bps", status: "absent", reason: "LIVE recorded no slippage" }),
        metric({ metric: "win_rate", status: "ok" }),
      ],
    });
    expect(blockedMetrics(p).map((m) => m.metric)).toEqual(["expectancy_per_trade"]);
    expect(unrecordedMetrics(p).map((m) => m.metric)).toEqual(["slippage_bps"]);
  });

  it("does not count an absent side as a blocked one", () => {
    // absent means there is no value to have been blocked.
    const p = pair({
      metrics: [metric({ status: "absent", reference_value: null, comparison_value: 5 })],
    });
    expect(blockedMetrics(p)).toEqual([]);
  });
});

describe("orderFindings", () => {
  it("puts a regime mismatch above the metric drifts it explains", () => {
    // Reading the metric first means drawing the conclusion before learning
    // that the conditions differed.
    const ordered = orderFindings([
      { kind: "metric_drift", severity: "warning", statement: "expectancy fell" },
      { kind: "regime_mismatch", severity: "warning", statement: "regime differed" },
      { kind: "trade_frequency", severity: "info", statement: "trading less" },
    ]);
    expect(ordered[0].kind).toBe("regime_mismatch");
    expect(ordered[1].kind).toBe("metric_drift");
    expect(ordered[2].kind).toBe("trade_frequency");
  });

  it("is stable for equal ranks", () => {
    const input = [
      { kind: "metric_drift", severity: "warning", statement: "a" },
      { kind: "metric_drift", severity: "warning", statement: "b" },
    ];
    expect(orderFindings(input).map((f) => f.statement)).toEqual(["a", "b"]);
  });

  it("tolerates a missing list", () => {
    expect(orderFindings(undefined as never)).toEqual([]);
  });
});

describe("findingViews", () => {
  it("tones a warning as bad and an info as warn", () => {
    const views = findingViews([
      { kind: "metric_drift", severity: "warning", statement: "x" },
      { kind: "trade_frequency", severity: "info", statement: "y" },
    ]);
    expect(views[0].tone).toBe("bad");
    expect(views[1].tone).toBe("warn");
  });

  it("carries the evidence through rather than dropping it", () => {
    const views = findingViews([
      {
        kind: "metric_drift",
        severity: "warning",
        statement: "x",
        evidence: "40 LIVE trades against 60 BACKTEST trades",
        confidence: "high",
        sample_size: 40,
      },
    ]);
    expect(views[0].evidence).toContain("40 LIVE trades");
    expect(views[0].confidence).toBe("high");
    expect(views[0].sample).toBe("40");
  });
});

describe("bucketViews", () => {
  function bucket(over: Partial<LearningBucket> = {}): LearningBucket {
    return {
      label: "2.0-3.0x",
      n: 184,
      stats: {
        n: 184,
        mean: 431.2,
        median: 400,
        mean_ci: [120, 700],
      },
      lift: 0.32,
      significance: "moderate",
      suppressed: false,
      note: null,
      ...over,
    };
  }

  it("refuses to present a suppressed bucket as evidence", () => {
    const [view] = bucketViews([bucket({ n: 3, suppressed: true, note: "below the floor" })]);
    expect(view.enoughToJudge).toBe(false);
    expect(view.note).toBe("below the floor");
  });

  it("marks a sample at the floor as judgeable", () => {
    expect(bucketViews([bucket({ n: 10 })])[0].enoughToJudge).toBe(true);
    expect(bucketViews([bucket({ n: 9 })])[0].enoughToJudge).toBe(false);
  });

  it("shows the interval so a wide one is visible", () => {
    expect(bucketViews([bucket()])[0].interval).toBe("[₹120.00 … ₹700.00]");
  });

  it("renders a null mean as a dash rather than zero", () => {
    const [view] = bucketViews([bucket({ stats: { mean: null } })]);
    expect(view.mean).toBe("—");
    expect(bucketViews([bucket({ lift: null })])[0].lift).toBe("—");
  });

  it("reads the statistics from where the payload nests them", () => {
    // The bucket carries no `mean` of its own; reading it from the bucket
    // renders every row as "not measured" for a book that has data.
    const [view] = bucketViews([bucket()]);
    expect(view.mean).toBe("₹431.20");
  });

  it("renders a return metric in percent, not rupees", () => {
    // The paper ledger records returns and no position size, so this is the
    // branch that actually renders today.
    const [view] = bucketViews([bucket({ stats: { mean: 1.19 }, lift: 1.12 })], "return_pct");
    expect(view.mean).toBe("+1.19%");
    expect(view.lift).toBe("+1.12%");
    expect(view.interval).toBe("");
  });

  it("does not render a lift in the metric's units as a percentage of money", () => {
    // `lift` is a difference in the metric's units, so a 2.8-rupee gap is
    // ₹2.80 — not "280%".
    const [view] = bucketViews([bucket({ lift: -2.77 })], "net_pnl");
    expect(view.lift).toBe("₹-2.77");
  });
});

describe("notableBuckets and starvedAxes", () => {
  const analysis = (over: Partial<LearningAnalysis> = {}): LearningAnalysis => ({
    strategy: "ALL",
    n: 300,
    metric: "net_pnl",
    overall: {},
    breakdowns: [],
    notable: [],
    caveats: [],
    ...over,
  });

  it("keeps only unsuppressed buckets with a real sample", () => {
    const kept = notableBuckets(
      analysis({
        notable: [
          { label: "a", n: 184, suppressed: false } as LearningBucket,
          { label: "b", n: 3, suppressed: false } as LearningBucket,
          { label: "c", n: 50, suppressed: true } as LearningBucket,
        ],
      }),
    );
    expect(kept.map((b) => b.label)).toEqual(["a"]);
  });

  it("identifies an axis with no coverage so the UI can explain it", () => {
    const starved = starvedAxes(
      analysis({
        breakdowns: [
          {
            axis: "sector_strength",
            label: "Sector strength",
            metric: "net_pnl",
            rows_with_value: 0,
            rows_scanned: 300,
            coverage: 0,
            baseline: {},
            buckets: [],
          },
        ],
      }),
    );
    expect(starved.map((b) => b.axis)).toEqual(["sector_strength"]);
  });

  it("does not call an axis starved when one bucket is real", () => {
    const starved = starvedAxes(
      analysis({
        breakdowns: [
          {
            axis: "rvol_bucket",
            label: "Relative volume",
            metric: "net_pnl",
            rows_with_value: 200,
            rows_scanned: 300,
            coverage: 0.67,
            baseline: {},
            buckets: [
              { label: "a", n: 150, suppressed: false } as LearningBucket,
              { label: "b", n: 2, suppressed: true } as LearningBucket,
            ],
          },
        ],
      }),
    );
    expect(starved).toEqual([]);
  });

  it("tolerates a missing analysis", () => {
    expect(notableBuckets(undefined as never)).toEqual([]);
    expect(starvedAxes(undefined as never)).toEqual([]);
  });
});

describe("emptyState", () => {
  it("describes the empty book with the reason the backend gave", () => {
    // This is the live state of the project, not an edge case.
    const state = emptyState(0, { india_vix: "no cached series" }, [
      "the dataset is empty: no backtest trade and no journal entry",
    ]);
    expect(state.empty).toBe(true);
    expect(state.title).toBe("No closed trades yet");
    expect(state.detail).toContain("no backtest trade");
    expect(state.missing).toEqual([{ feature: "india_vix", reason: "no cached series" }]);
  });

  it("supplies its own detail when the backend sent no limitation", () => {
    const state = emptyState(0, {}, []);
    expect(state.detail).toContain("correctly absent rather than zero");
  });

  it("is not the empty state once there are trades", () => {
    expect(emptyState(5, {}, []).empty).toBe(false);
  });
});

describe("sampleVerdict", () => {
  it("says 'no trades' rather than '0 trades'", () => {
    expect(sampleVerdict(0).text).toBe("no trades");
  });

  it("names the floor when below it", () => {
    const verdict = sampleVerdict(4);
    expect(verdict.text).toContain("below the 10-trade floor");
    expect(verdict.tone).toBe("muted");
  });

  it("calls a small sample small rather than adequate", () => {
    expect(sampleVerdict(20).tone).toBe("warn");
    expect(sampleVerdict(200).tone).toBe("good");
  });
});

describe("reportHeadline", () => {
  it("reports advisory status from the payload, not from assumption", () => {
    const report = {
      headline: "No closed trades on 2026-09-15.",
      advisory: true,
      applies_changes: false,
    } as never;
    const view = reportHeadline(report);
    expect(view.advisory).toBe(true);
    expect(view.text).toContain("No closed trades");
  });

  it("refuses to call a report advisory if the payload does not say so", () => {
    const view = reportHeadline({ headline: "x", advisory: true, applies_changes: true } as never);
    expect(view.advisory).toBe(false);
  });

  it("survives a missing report", () => {
    expect(reportHeadline(undefined as never).text).toBe("");
  });
});

describe("nothingMeasured", () => {
  it("is true when no bucket is notable and drift refused", () => {
    expect(
      nothingMeasured({
        analysis: { notable: [], breakdowns: [] } as never,
        drift: { pairs: [], headline: "Drift could not be measured: nothing" },
      }),
    ).toBe(true);
  });

  it("is false when drift found something even if no bucket did", () => {
    expect(
      nothingMeasured({
        analysis: { notable: [], breakdowns: [] } as never,
        drift: { pairs: [], headline: "Drift detected — 2 metric(s) below baseline." },
      }),
    ).toBe(false);
  });

  it("is false when a bucket is notable even if drift refused", () => {
    expect(
      nothingMeasured({
        analysis: {
          notable: [{ label: "a", n: 100, suppressed: false } as LearningBucket],
          breakdowns: [],
        } as never,
        drift: { pairs: [], headline: "Drift could not be measured: nothing" },
      }),
    ).toBe(false);
  });
});

/**
 * The evidence strip.
 *
 * The property under test is narrow and load-bearing: a book of in-sample rows
 * must never be presented as though it were evidence. Every other number on the
 * learning screen reads the same either way, so this derivation is the only
 * thing standing between a reader and a backfilled book that looks tested.
 */
describe("evidenceView", () => {
  function summary(over: Partial<LearningDatasetSummary> = {}): LearningDatasetSummary {
    return {
      generated_at: "2026-09-16T10:00:00Z",
      observations: 0,
      trades: 0,
      closed_trades: 0,
      open_trades: 0,
      forward_observations: 0,
      in_sample_observations: 0,
      latest_forward_ts: null,
      sources: {},
      evidence_grades: { forward: 0, in_sample: 0 },
      evidence_classes: {},
      metric_coverage: {},
      paper_ledger: {},
      strategies: [],
      symbols: 0,
      date_range: { first: null, last: null },
      net_pnl_total: null,
      missing_features: {},
      warnings: [],
      ...over,
    };
  }

  it("reports nothing recorded for an empty book, not an in-sample book", () => {
    const view = evidenceView(summary());
    expect(view.grade).toBe("none");
    expect(view.note).toContain("nothing to grade");
  });

  it("calls a book of backfilled rows in-sample however large it is", () => {
    const view = evidenceView(
      summary({
        observations: 140,
        trades: 140,
        in_sample_observations: 140,
        evidence_grades: { forward: 0, in_sample: 140 },
      }),
    );
    expect(view.grade).toBe("in_sample");
    expect(view.forward).toBe(0);
    expect(view.gradeLabel).toContain("in-sample");
    expect(view.note).toContain("not whether anything still works");
    expect(view.note).not.toContain("forward evidence");
  });

  it("counts only the forward rows as evidence in a mixed book", () => {
    const view = evidenceView(
      summary({
        observations: 12,
        trades: 12,
        forward_observations: 3,
        in_sample_observations: 9,
        latest_forward_ts: "2026-06-12T15:30:00",
        evidence_grades: { forward: 3, in_sample: 9 },
      }),
    );
    expect(view.grade).toBe("forward");
    expect(view.forward).toBe(3);
    expect(view.inSample).toBe(9);
    expect(view.latestForward).toBe("2026-06-12");
    // Three forward observations do not clear the floor, and the label says so
    // rather than implying the book is proven.
    expect(view.gradeLabel).toContain("below the floor");
    expect(view.tone).toBe("warn");
    expect(view.note).toContain("The other 9 are in-sample");
  });

  it("marks a forward book that clears the floor as evidence", () => {
    const view = evidenceView(
      summary({
        observations: 20,
        trades: 20,
        forward_observations: 20,
        latest_forward_ts: "2026-06-12T15:30:00",
        evidence_grades: { forward: 20, in_sample: 0 },
        metric_coverage: { net_pnl: 20, return_pct: 20 },
      }),
    );
    expect(view.gradeLabel).toBe("forward evidence");
    expect(view.tone).toBe("good");
    expect(view.coverage).toBe("net_pnl 20, return_pct 20");
  });

describe("evidenceView", () => {
  it("reads the grade counts off the payload rather than recomputing them", () => {
    // ``forward_observations`` and ``evidence_grades`` disagree here on purpose.
    // The grades map is authoritative, and a view that derived the split from
    // the top-level counts instead would show a different book than the one the
    // backend measured.
    const view = evidenceView(
      summary({
        observations: 5,
        trades: 5,
        forward_observations: 0,
        evidence_grades: { forward: 5, in_sample: 0 },
      }),
    );
    expect(view.forward).toBe(5);
    expect(view.grade).toBe("forward");
  });
});

});

describe("readiness views", () => {
  function strategy(over: Partial<ReadinessStrategy> = {}): ReadinessStrategy {
    return {
      strategy_id: "strat-1",
      strategy_name: "Breakout V1",
      versions_seen: [1],
      current_version: 1,
      deployment: { mode: "PAPER", status: "RUNNING" },
      forward_trades: 6,
      open_forward_trades: 0,
      state: "NOT READY",
      state_reasons: ["6 forward trades are below the 10-trade minimum"],
      next_gate: { required: 10, have: 6, status: "INSUFFICIENT" },
      summary: {
        n: 6,
        n_with_metric: 6,
        metric: "return_pct",
        stats: {},
        max_drawdown: 1.0,
        score_bands: [],
        score_coverage: { with_score: 4, scanned: 6 },
        regime_distribution: {},
        sector_distribution: {},
        recent_vs_historical: {
          window_days: 30,
          recent_n: 2,
          recent_mean: 1.5,
          historical_n: 4,
          historical_mean: 0.5,
        },
      },
      metric_note: null,
      progression: [],
      context_observations_recorded: 2,
      forward_observations_recorded: 0,
      optimization_ready: false,
      missing_features: [],
      last_trade: "2026-09-10T10:00:00",
      last_cycle: null,
      quality_issues: [],
      ...over,
    };
  }

  it("tones the four research states without ever reading one as approval", () => {
    expect(readinessStateTone("NOT READY")).toBe("muted");
    expect(readinessStateTone("MINIMUM SAMPLE")).toBe("warn");
    expect(readinessStateTone("ANALYSIS READY")).toBe("good");
    expect(readinessStateTone("OPTIMIZATION ELIGIBLE")).toBe("good");
    expect(readinessStateLabel("MINIMUM SAMPLE")).toBe("Minimum sample");
    expect(evidenceStatusTone("INSUFFICIENT")).toBe("muted");
    expect(evidenceStatusTone("SMALL_SAMPLE")).toBe("warn");
    expect(evidenceStatusTone("ANALYSIS_READY")).toBe("good");
    expect(evidenceStatusLabel("SMALL_SAMPLE")).toBe("Small sample");
    expect(qualitySeverityTone("blocking")).toBe("bad");
    expect(qualitySeverityTone("watch")).toBe("warn");
  });

  it("always shows have beside required on a gate", () => {
    expect(gateProgress(6, 10)).toBe("6 / 10");
    expect(gateProgress(52, null)).toBe("52 · all gates cleared");
  });

  it("renders a strategy row with counts, gate, coverage and version", () => {
    const row = readinessRowView(strategy());
    expect(row.name).toBe("Breakout V1");
    expect(row.forward).toBe(6);
    expect(row.gate).toBe("6 / 10");
    expect(row.stateLabel).toBe("Not ready");
    expect(row.contextCoverage).toBe("4 of 6 with a recorded score");
    expect(row.lastTrade).toBe("2026-09-10");
    expect(row.version).toBe("v1");
  });

  it("renders absence as absence, never as zero", () => {
    const row = readinessRowView(
      strategy({ current_version: null, last_trade: null }),
    );
    expect(row.version).toBe("—");
    expect(row.lastTrade).toBe("—");
  });

  it("totals strategies by state and counts blocking issues", () => {
    const issues: ReadinessQualityIssue[] = [
      {
        code: "inconsistent_provenance",
        severity: "blocking",
        count: 1,
        sample_refs: ["t-1"],
        explanation: "broken",
      },
      {
        code: "missing_sector",
        severity: "watch",
        count: 2,
        sample_refs: ["t-2"],
        explanation: "no sector",
      },
    ];
    const totals = readinessTotals(
      [strategy(), strategy({ strategy_id: "s2", state: "ANALYSIS READY" })],
      issues,
    );
    expect(totals.strategies).toBe(2);
    expect(totals.forward).toBe(12);
    expect(totals.byState).toEqual({ "NOT READY": 1, "ANALYSIS READY": 1 });
    expect(totals.blocking).toBe(1);
  });
});
