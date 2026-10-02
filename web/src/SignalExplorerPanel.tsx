/**
 * SignalExplorerPanel — the Context-Aware Signal Engine, made inspectable.
 *
 * SIGNAL → CONTEXT → SCORE → OUTCOME
 *
 * Four sub-panels:
 * 1. Signals — the enriched record, newest first, each row opening the full
 * market/sector/stock context and per-criterion score breakdown.
 * 2. Analytics — outcomes bucketed by a context dimension, with honest counts
 * and suppression. In-sample buckets are shown but labelled.
 * 3. Effectiveness — does a higher score accompany a different forward
 * outcome? Score bands and features measured against their
 * complements, with the multiple-comparisons correction shown
 * and in-sample results kept apart. A measurement, never a
 * scoring change.
 * 4. Model — the versioned scoring model this whole screen is interpreted
 * against: criteria, weights and thresholds.
 *
 * The score is the share of predefined conditions met at signal time. It is not
 * a probability of profit, and nothing on this panel proposes a trade.
 *
 * Indian cash equities only. No F&O.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";

import {
  getSignalContext,
  getSignalContextAnalytics,
  getSignalContextEffectiveness,
  getSignalContextModel,
  getSignalContexts,
  ContextDimension,
  EffectivenessMetric,
  SignalContextAnalytics,
  SignalContextClass,
  SignalContextEffectiveness,
  SignalContextModel,
  SignalContextRecord,
  SignalSource,
} from "./api";
import {
  analyticsAccounting,
  bucketViews,
  contextClassLabel,
  contextClassTone,
  criterionViews,
  DIMENSION_OPTIONS,
  effectivenessAccounting,
  effectivenessBucketViews,
  forwardVsInSample,
  missingFieldsView,
  scoreVerdictView,
  signalRowView,
  suggestiveFindings,
  supportedFindings,
  unsupportedAxes,
} from "./lib/signal-context-view";
import { Select } from "./components/ui/select";
import { PageLoader } from "./components/ui/loading";
import { Card, ErrorBox } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { fieldLabel } from "./components/ui/form-styles";
import { Badge } from "./components/ui/stat";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { cn } from "./lib/utils";
import { toneOf, toneText, type Tone as UiTone } from "./lib/tone";

// ─── Colour / theme helpers ───────────────────────────────────────────────────

/** This panel's view-models say "muted" where the shared tone says "flat". */
const asTone = (t: string | undefined): UiTone => (t === "muted" || t == null ? "flat" : (t as UiTone));


const fmt = (v: number | null | undefined, digits = 2, suffix = "%"): string => {
  if (v == null) return "\u2014";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}${suffix}`;
};

const fmtAbs = (v: number | null | undefined, digits = 2, suffix = "%"): string => {
  if (v == null) return "\u2014";
  return `${v.toFixed(digits)}${suffix}`;
};

const shortTs = (ts: string | null | undefined): string =>
  ts ? String(ts).replace("T", " ").slice(0, 16) : "\u2014";

// ─── Shared sub-components ───────────────────────────────────────────────────

function ErrorBanner({ msg }: { msg: string }) {
  return <ErrorBox>{msg}</ErrorBox>;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-3 text-xs font-medium text-muted-foreground">{children}</h3>;
}

function TonePill({ label, tone }: { label: string; tone: string }) {
  return <Badge tone={asTone(tone)}>{label}</Badge>;
}

const TH = ({ children }: { children: React.ReactNode }) => (
  <th
  className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground whitespace-nowrap"
  >
    {children}
  </th>
);

const TD = ({
  children,
  tone,
  bold,
}: {
  children: React.ReactNode;
  tone?: UiTone | "strong";
  bold?: boolean;
}) => (
  <td className={cn("px-3 py-1.5", tone === "strong" ? "text-foreground" : toneText[tone ?? "flat"], bold && "font-semibold")}>
    {children}
  </td>
);

// ─── Sub-panel 1: Signals ─────────────────────────────────────────────────────

function DetailRow({ label, value, tone }: { label: string; value: string; tone?: UiTone }) {
  return (
    <div className="flex justify-between gap-2 py-0.5">
      <span className="text-caption text-muted-foreground">{label}</span>
      <span className={cn("text-caption font-semibold", tone ? toneText[tone] : "text-foreground")}>{value}</span>
    </div>
  );
}

function SignalDetail({ record }: { record: SignalContextRecord }) {
  const score = signalRowView(record).score;
  const criteria = criterionViews(record.score_breakdown);
  const missing = missingFieldsView(record.missing_fields);
  const m = record.market_context;
  const s = record.stock_context;
  const sec = record.sector_context;

  return (
    <Card padding="md">
      <div className="flex justify-between flex-wrap gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg font-semibold text-foreground">{record.symbol}</span>
            <span className="text-xs text-primary-soft font-semibold">{record.action}</span>
            <TonePill label={contextClassLabel(record.context_class)} tone={contextClassTone(record.context_class)} />
          </div>
          <div className="text-caption text-muted-foreground mt-1">
            {record.signal_id} · {record.signal_source} · {shortTs(record.signal_ts)}
          </div>
        </div>
        <div className="text-right">
          <div className={cn("text-2xl font-semibold", toneText[asTone(score.tone)])}>
            {score.text}
          </div>
          <div className="text-micro text-muted-foreground max-w-xs">{score.caption}</div>
        </div>
      </div>

      {!missing.none && (
        <div className="mt-2.5 text-caption text-warning">⚠ {missing.text}</div>
      )}

      <div className="mt-3 border-t border-t-primary/10 pt-2">
        <SectionTitle>Score breakdown</SectionTitle>
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-b-primary/15">
              <TH>Criterion</TH>
              <TH>Weight</TH>
              <TH>Measurement</TH>
              <TH>Status</TH>
              <TH>Reason</TH>
            </tr>
          </thead>
          <tbody>
            {criteria.map((c) => (
              <tr key={c.key} className="border-b border-b-primary/7">
                <TD tone="strong" bold>
                  {c.label}
                </TD>
                <TD>{c.weight}</TD>
                <TD tone={toneOf(c.status === "met" ? 1 : c.status === "not_met" ? -1 : null)}>{c.value}</TD>
                <TD>
                  <TonePill label={c.status.replace(/_/g, " ")} tone={c.tone} />
                </TD>
                <TD>{c.reason || "\u2014"}</TD>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div
      className="mt-3 grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-3"
      >
        <div>
          <SectionTitle>Market (as of {shortTs(m?.as_of)})</SectionTitle>
          <DetailRow label="Regime" value={m?.regime ?? "\u2014"} />
          <DetailRow label="NIFTY trend vs SMA50" value={fmt(m?.nifty_trend_pct)} tone={toneOf(m?.nifty_trend_pct)} />
          <DetailRow label="Breadth > EMA50" value={fmtAbs(m?.breadth_above_ema50_pct)} />
          <DetailRow label="Volatility ratio" value={fmtAbs(m?.volatility_ratio, 2, "x")} />
          <DetailRow label="Advance / decline" value={fmtAbs(m?.advance_decline_ratio, 2, "")} />
          <DetailRow
            label="Benchmark"
            value={`${m?.benchmark_symbol ?? "\u2014"}${m?.benchmark_is_proxy ? " (proxy)" : ""}`}
          />
        </div>
        <div>
          <SectionTitle>Sector</SectionTitle>
          <DetailRow label="Sector" value={sec?.sector ?? "\u2014"} />
          <DetailRow label="RS vs NIFTY 1M" value={fmt(sec?.relative_strength_1m)} tone={toneOf(sec?.relative_strength_1m)} />
          <DetailRow label="Return 1M" value={fmt(sec?.return_1m_pct)} tone={toneOf(sec?.return_1m_pct)} />
          <DetailRow label="Trend" value={sec?.trend ?? "\u2014"} />
          <DetailRow label="Volume multiple" value={fmtAbs(sec?.volume_multiple, 2, "x")} />
        </div>
        <div>
          <SectionTitle>Stock (as of {shortTs(s?.as_of)})</SectionTitle>
          <DetailRow label="RS vs NIFTY 20D" value={fmt(s?.relative_strength_nifty_20d)} tone={toneOf(s?.relative_strength_nifty_20d)} />
          <DetailRow label="Relative volume" value={fmtAbs(s?.relative_volume, 2, "x")} />
          <DetailRow label="ATR %" value={fmtAbs(s?.atr_pct)} />
          <DetailRow label="Trend vs SMA50" value={fmt(s?.trend_pct)} tone={toneOf(s?.trend_pct)} />
          <DetailRow label="From 52W high" value={fmt(s?.from_52w_high_pct)} />
          <DetailRow label="Close" value={s?.close != null ? s.close.toLocaleString("en-IN") : "\u2014"} />
        </div>
      </div>

      <div className="mt-2.5 text-micro text-muted-foreground">
        Model {record.context_model_version} · recorded {shortTs(record.created_at)}
      </div>
    </Card>
  );
}

const SOURCE_OPTIONS: (SignalSource | "")[] = ["", "PAPER", "BACKTEST", "LIVE"];
const CLASS_OPTIONS: (SignalContextClass | "")[] = [
  "",
  "STRONG_CONTEXT",
  "NEUTRAL_CONTEXT",
  "WEAK_CONTEXT",
  "INSUFFICIENT_DATA",
];

function SignalsPanel() {
  const [source, setSource] = useState<SignalSource | "">("");
  const [klass, setKlass] = useState<SignalContextClass | "">("");
  const [items, setItems] = useState<SignalContextRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<SignalContextRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Only the newest request may write state: changing a filter quickly must not
  // let a slower, older response land last and show rows for the wrong filter.
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    getSignalContexts({
      limit: 100,
      source: source || undefined,
      contextClass: klass || undefined,
    })
      .then((r) => {
        if (mine !== seq.current) return;
        setItems(r.data.items);
        setTotal(r.data.total);
      })
      .catch((e) => mine === seq.current && setError(String(e)))
      .finally(() => mine === seq.current && setLoading(false));
  }, [source, klass]);

  useEffect(() => {
    load();
  }, [load]);

  const open = useCallback((signalId: string) => {
    getSignalContext(signalId)
      .then((r) => setDetail(r.data))
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-2 flex-wrap items-center">
        <span className="text-caption text-muted-foreground font-semibold">Source:</span>
        <Select
          size="sm"
          value={source}
          onChange={(v) => setSource(v as SignalSource | "")}
          options={SOURCE_OPTIONS.map((s) => ({
            value: s,
            label: s || "All sources",
          }))}
          className="w-32"
        />
        <span className="text-caption text-muted-foreground font-semibold">Context:</span>
        <Select
          size="sm"
          value={klass}
          onChange={(v) => setKlass(v as SignalContextClass | "")}
          options={CLASS_OPTIONS.map((k) => ({
            value: k,
            label: k ? contextClassLabel(k) : "All classes",
          }))}
          className="w-44"
        />
        <Button id="signal-refresh" size="xs" variant="outline" onClick={load}>
          ↻ Refresh
        </Button>
        <span className="text-caption text-muted-foreground ml-auto">
          {items.length} of {total}
        </span>
      </div>

      {error && <ErrorBanner msg={error} />}
      {loading ? (
        <PageLoader label="Loading signals" />
      ) : items.length === 0 ? (
        <Card padding="md" className="text-xs text-muted-foreground">
          No signals match
        </Card>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-b-primary/15">
                <TH>Symbol</TH>
                <TH>Action</TH>
                <TH>Source</TH>
                <TH>Signal time</TH>
                <TH>Score</TH>
                <TH>Context</TH>
                <TH>Regime</TH>
                <TH>Model</TH>
              </tr>
            </thead>
            <tbody>
              {items.map((r, i) => {
                const row = signalRowView(r);
                return (
                  <tr
                    key={r.signal_id}
                    id={`signal-row-${r.signal_id}`}
                    onClick={() => open(r.signal_id)}
                    className={cn("cursor-pointer border-b border-border/50", i % 2 === 1 && "bg-muted/20")}
                  >
                    <TD tone="strong" bold>
                      {row.symbol}
                    </TD>
                    <TD tone={row.action === "BUY" ? "good" : "bad"}>{row.action}</TD>
                    <TD>{row.source}</TD>
                    <TD>{shortTs(row.signalTs)}</TD>
                    <TD tone={asTone(row.score.tone)} bold>
                      {row.score.text}
                    </TD>
                    <TD>
                      <TonePill label={row.contextClassLabel} tone={row.contextClassTone} />
                    </TD>
                    <TD>{row.regime}</TD>
                    <TD>{row.modelVersion}</TD>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {detail && (
        <div className="flex flex-col gap-2">
          <div className="flex justify-between items-center">
            <SectionTitle>Signal detail</SectionTitle>
            <Button
              id="signal-detail-close"
              size="xs"
              variant="quiet"
              onClick={() => setDetail(null)}
            >
              Close
            </Button>
          </div>
          <SignalDetail record={detail} />
        </div>
      )}
    </div>
  );
}

// ─── Sub-panel 2: Analytics ───────────────────────────────────────────────────

function AnalyticsPanel() {
  const [dimension, setDimension] = useState<ContextDimension>("context_class");
  const [source, setSource] = useState<SignalSource | "">("");
  const [result, setResult] = useState<SignalContextAnalytics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seq = useRef(0);
  const run = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    getSignalContextAnalytics({ dimension, source: source || undefined })
      .then((r) => mine === seq.current && setResult(r.data))
      .catch((e) => mine === seq.current && setError(String(e)))
      .finally(() => mine === seq.current && setLoading(false));
  }, [dimension, source]);

  useEffect(() => {
    run();
  }, [run]);

  const views = result ? bucketViews(result.buckets) : [];
  const accounting = result ? analyticsAccounting(result.buckets) : null;
  const desc = DIMENSION_OPTIONS.find((o) => o.key === dimension)?.description ?? "";

  return (
    <div className="flex flex-col gap-3">
      <Card padding="md">
        <div className="flex gap-4 flex-wrap items-end">
          <div className="flex flex-col gap-1">
            <label className={fieldLabel}>BUCKET BY</label>
            <Select
              size="sm"
              value={dimension}
              onChange={(v) => setDimension(v as ContextDimension)}
              options={DIMENSION_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
              className="w-[200px]"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className={fieldLabel}>SOURCE</label>
            <Select
              size="sm"
              value={source}
              onChange={(v) => setSource(v as SignalSource | "")}
              options={SOURCE_OPTIONS.map((s) => ({
                value: s,
                label: s || "All sources",
              }))}
              className="w-[160px]"
            />
          </div>
          <Button id="analytics-run" size="xs" title={desc} onClick={run} disabled={loading}>
            {loading ? "Analyzing…" : "Analyze"}
          </Button>
        </div>
      </Card>

      {error && <ErrorBanner msg={error} />}

      {result && (
        <>
          <Card padding="md">
            <div className="flex justify-between flex-wrap gap-2">
              <span className="text-xs text-foreground">{accounting?.note}</span>
              <span className="text-caption text-muted-foreground">
                Model {result.model_version} · stats require ≥{result.min_forward_n} forward outcomes
              </span>
            </div>
          </Card>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-b-primary/15">
                  <TH>{DIMENSION_OPTIONS.find((o) => o.key === dimension)?.label ?? "Bucket"}</TH>
                  <TH>Evidence</TH>
                  <TH>N</TH>
                  <TH>Forward</TH>
                  <TH>In-sample</TH>
                  <TH>Unresolved</TH>
                  <TH>Mean return</TH>
                  <TH>Median</TH>
                  <TH>Win rate</TH>
                  <TH>Profit factor</TH>
                  <TH>Win-rate 95% CI</TH>
                </tr>
              </thead>
              <tbody>
                {views.map((b, i) => (
                  <tr
                    key={b.key}
                    className={cn("border-b border-border/50", b.suppressed ? "bg-muted/30 opacity-60" : i % 2 === 1 && "bg-muted/20")}
                  >
                    <TD tone="strong" bold>
                      {b.label}
                      {b.suppressed && (
                        <span className="ml-1.5 text-micro text-muted-foreground">(too few)</span>
                      )}
                    </TD>
                    <TD>
                      <TonePill label={b.evidenceLabel} tone={b.evidenceTone} />
                    </TD>
                    <TD>{b.n}</TD>
                    <TD>{b.nForward}</TD>
                    <TD>{b.nInSample}</TD>
                    <TD>{b.unresolved}</TD>
                    <TD tone={toneOf(b.mean.startsWith("+") ? 1 : b.mean === "\u2014" ? null : -1)} bold>
                      {b.mean}
                    </TD>
                    <TD>{b.median}</TD>
                    <TD tone={b.winRate === "\u2014" ? "flat" : "strong"}>{b.winRate}</TD>
                    <TD>{b.profitFactor}</TD>
                    <TD>{b.ci || "\u2014"}</TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex gap-4 flex-wrap text-micro text-muted-foreground">
            <span>
              <span className="text-gain">● Forward</span> — out-of-sample evidence
            </span>
            <span>
              <span className="text-warning">● Thin forward</span> — recorded, below the floor
            </span>
            <span>
            <span className="text-muted-foreground">● In-sample</span> — backtest; shown but not proof
            </span>
          </div>
        </>
      )}
    </div>
  );
}

// ─── Sub-panel 3: Effectiveness ───────────────────────────────────────────────

const METRIC_OPTIONS: { id: EffectivenessMetric; label: string }[] = [
  { id: "return_pct", label: "Return %" },
  { id: "net_pnl", label: "Net P&L" },
];

function EffectivenessPanel() {
  const [source, setSource] = useState<SignalSource | "">("");
  const [metric, setMetric] = useState<EffectivenessMetric>("return_pct");
  const [axisName, setAxisName] = useState("context_score");
  const [result, setResult] = useState<SignalContextEffectiveness | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seq = useRef(0);
  const run = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    setError(null);
    getSignalContextEffectiveness({ source: source || undefined, metric })
      .then((r) => {
        if (mine !== seq.current) return;
        setResult(r.data);
        if (!(r.data.axes || []).some((a) => a.axis === axisName)) {
          setAxisName(r.data.axes[0]?.axis ?? "context_score");
        }
      })
      .catch((e) => mine === seq.current && setError(String(e)))
      .finally(() => mine === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, metric]);

  useEffect(() => {
    run();
  }, [run]);

  const accounting = result ? effectivenessAccounting(result) : null;
  const verdict = result ? scoreVerdictView(result.score_verdict, result.metric) : null;
  const supported = result ? supportedFindings(result) : [];
  const suggestive = result ? suggestiveFindings(result) : [];
  const unsupported = result ? unsupportedAxes(result) : [];
  const axis = result?.axes.find((a) => a.axis === axisName) ?? result?.axes[0];
  const bucketRows = axis && result ? effectivenessBucketViews(axis, result.metric) : [];
  const comparison =
    axis && result ? forwardVsInSample(axis.axis, result) : [];

  return (
    <div className="flex flex-col gap-3">
      <Card padding="md">
        <div className="flex gap-4 flex-wrap items-end">
          <div className="flex flex-col gap-1">
            <label className={fieldLabel}>SOURCE</label>
            <Select
              size="sm"
              value={source}
              onChange={(v) => setSource(v as SignalSource | "")}
              options={SOURCE_OPTIONS.map((s) => ({
                value: s,
                label: s || "All sources",
              }))}
              className="w-[160px]"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className={fieldLabel}>METRIC</label>
            <Select
              size="sm"
              value={metric}
              onChange={(v) => setMetric(v as EffectivenessMetric)}
              options={METRIC_OPTIONS.map((m) => ({ value: m.id, label: m.label }))}
              className="w-[160px]"
            />
          </div>
          <Button id="effectiveness-run" size="xs" onClick={run} disabled={loading}>
            {loading ? "Measuring…" : "Measure"}
          </Button>
        </div>
      </Card>

      {error && <ErrorBanner msg={error} />}

      {result && (
        <>
          <Card padding="md">
            <div className="flex justify-between flex-wrap gap-2">
              <span className="text-xs text-foreground">{accounting?.note}</span>
              <span className="text-caption text-muted-foreground">
                Model {result.model_version} · floor {result.min_sample} trades ·{" "}
                {result.bonferroni_comparisons} comparisons
              </span>
            </div>
          </Card>

          <Card padding="md">
            <div className="flex items-center gap-2 mb-2">
              <SectionTitle>High vs low score</SectionTitle>
              {verdict && <TonePill label={verdict.claimLabel} tone={verdict.claimTone} />}
            </div>
            <div className="text-body text-foreground leading-relaxed">{verdict?.statement}</div>
            <div className="flex gap-2 flex-wrap mt-2.5">
              {(verdict?.bands || []).map((b) => (
                <span
                  key={b.label}
                  className="text-caption text-foreground bg-primary/10 rounded-md py-1 px-2.5"
                >
                  {b.label}: <strong>{b.meanText}</strong>
                </span>
              ))}
            </div>
            {verdict && !verdict.monotonic && verdict.bands.length > 1 && (
              <div className="mt-1.5 text-caption text-warning">Bands not in order</div>
            )}
          </Card>

          <Card padding="md">
            <SectionTitle>
              Supported findings ({supported.length}) — forward only, corrected
            </SectionTitle>
            {supported.length === 0 ? (
              <div className="text-xs text-muted-foreground">
                Nothing significant yet
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-b-primary/15">
                      <TH>Dimension</TH>
                      <TH>Bucket</TH>
                      <TH>N</TH>
                      <TH>Mean</TH>
                      <TH>Lift vs complement</TH>
                      <TH>Adj. p</TH>
                      <TH>Strength</TH>
                    </tr>
                  </thead>
                  <tbody>
                    {supported.map((f, i) => (
                      <tr
                        key={`${f.axisLabel}-${f.bucketLabel}`}
                        className={cn("border-b border-border/50", i % 2 === 1 && "bg-muted/20")}
                      >
                        <TD tone="strong">{f.axisLabel}</TD>
                        <TD tone="strong" bold>
                          {f.bucketLabel}
                        </TD>
                        <TD>{f.n}</TD>
                        <TD>{f.mean}</TD>
                        <TD tone={toneOf(f.liftValue)} bold>
                          {f.lift}
                        </TD>
                        <TD>{f.pAdjusted}</TD>
                        <TD>
                          <TonePill label={f.significanceLabel} tone="good" />
                        </TD>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {suggestive.length > 0 && (
            <Card padding="md">
              <SectionTitle><span title="Weak after correction: worth watching as the sample grows, not worth acting on.">Suggestive, not supported ({suggestive.length})</span></SectionTitle>
              {suggestive.map((f) => (
                <div key={`${f.axisLabel}-${f.bucketLabel}`} className="text-xs text-foreground py-0.5 px-0">
                  {f.axisLabel} · <strong>{f.bucketLabel}</strong> — n={f.n}, lift {f.lift},{" "}
                  adj. p {f.pAdjusted}{" "}
                  <TonePill label={f.significanceLabel} tone="warn" />
                </div>
              ))}
            </Card>
          )}

          <Card padding="md">
            <SectionTitle>Still unproven ({unsupported.length})</SectionTitle>
            {unsupported.map((u) => (
              <div key={u.axis} className="text-xs text-muted-foreground py-0.5 px-0">
                <strong className="text-foreground">{u.label}</strong> — {u.reason}
              </div>
            ))}
          </Card>

          <Card padding="md">
            <div className="flex gap-4 flex-wrap items-end">
              <div className="flex flex-col gap-1">
                <label className={fieldLabel}>FEATURE AXIS</label>
                <Select
                  size="sm"
                  value={axis?.axis ?? "context_score"}
                  onChange={setAxisName}
                  options={(result.axes || []).map((a) => ({ value: a.axis, label: a.label }))}
                  className="w-[260px]"
                />
              </div>
              <span className="text-caption text-muted-foreground">
                {axis
                  ? `${axis.coverage.with_value} of ${axis.coverage.scanned} forward rows carry a value · ` +
                    `${axis.excluded?.axis_missing ?? 0} excluded (no value), ` +
                    `${axis.excluded?.outcome_missing ?? 0} without outcome`
                  : ""}
              </span>
            </div>
          </Card>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-b-primary/15">
                  <TH>{axis?.label ?? "Bucket"}</TH>
                  <TH>N</TH>
                  <TH>Mean</TH>
                  <TH>Mean 95% CI</TH>
                  <TH>Median</TH>
                  <TH>Win rate</TH>
                  <TH>Profit factor</TH>
                  <TH>Lift</TH>
                  <TH>Adj. p</TH>
                  <TH>Verdict</TH>
                  <TH>Drawdown</TH>
                </tr>
              </thead>
              <tbody>
                {bucketRows.map((b, i) => (
                  <tr
                    key={b.label}
                    className={cn("border-b border-border/50", b.suppressed ? "bg-muted/30 opacity-60" : i % 2 === 1 && "bg-muted/20")}
                  >
                    <TD tone="strong" bold>
                      {b.label}
                      {b.suppressed && (
                        <span className="ml-1.5 text-micro text-muted-foreground">(too few)</span>
                      )}
                    </TD>
                    <TD>{b.n}</TD>
                    <TD>{b.mean}</TD>
                    <TD>{b.meanCI || "\u2014"}</TD>
                    <TD>{b.median}</TD>
                    <TD>{b.winRate}{b.winRateCI ? ` ${b.winRateCI}` : ""}</TD>
                    <TD>{b.profitFactor}</TD>
                    <TD tone={b.lift.startsWith("+") ? "good" : b.lift === "\u2014" ? "flat" : "bad"} bold>
                      {b.lift}
                    </TD>
                    <TD>{b.pAdjusted}</TD>
                    <TD>
                      <TonePill label={b.significanceLabel} tone={b.significanceTone} />
                    </TD>
                    <TD>{b.drawdown}</TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <Card padding="md">
            <SectionTitle><span title="Shown side by side, never merged: an in-sample mean is a measurement of the past the rule was chosen on, not evidence about the future.">Forward vs in-sample — {axis?.label}</span></SectionTitle>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">
                <thead>
                  <tr className="border-b border-b-primary/15">
                    <TH>Bucket</TH>
                    <TH>Forward n</TH>
                    <TH>Forward mean</TH>
                    <TH>In-sample n</TH>
                    <TH>In-sample mean</TH>
                  </tr>
                </thead>
                <tbody>
                  {comparison.map((r) => (
                    <tr key={r.label} className="border-b border-b-primary/7">
                      <TD tone="strong" bold>
                        {r.label}
                      </TD>
                      <TD>{r.forwardN}</TD>
                      <TD>{r.forwardMean}</TD>
                      <TD>{r.inSampleN}</TD>
                      <TD>{r.inSampleMean}</TD>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card padding="md">
            <SectionTitle>Caveats</SectionTitle>
            <ul className="m-0 list-disc pl-4 text-xs leading-relaxed text-muted-foreground">
              <li>{result.model_note}</li>
              {result.caveats.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
            <div className="mt-1.5 text-micro text-muted-foreground">
              Generated {shortTs(result.generated_at)}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

// ─── Sub-panel 4: Model ───────────────────────────────────────────────────────

function ModelPanel() {
  const [model, setModel] = useState<SignalContextModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSignalContextModel()
      .then((r) => setModel(r.data))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <PageLoader label="Loading model" />;
  if (error) return <ErrorBanner msg={error} />;
  if (!model) return null;

  const total = model.criteria.reduce((sum, c) => sum + c.weight, 0);

  return (
    <div className="flex flex-col gap-4">
      <Card padding="md">
        <div className="text-caption text-muted-foreground">ACTIVE CONTEXT MODEL</div>
        <div className="text-lg font-semibold text-primary-soft" title={model.description}>{model.version}</div>
      </Card>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-b-primary/15">
              <TH>Criterion</TH>
              <TH>Weight</TH>
              <TH>Description</TH>
            </tr>
          </thead>
          <tbody>
            {model.criteria.map((c) => (
              <tr key={c.key} className="border-b border-b-primary/7">
                <TD tone="strong" bold>
                  {c.label}
                </TD>
                <TD tone="info" bold>
                  +{c.weight}
                </TD>
                <TD>{c.description}</TD>
              </tr>
            ))}
            <tr className="border-t border-t-primary/20">
            <TD bold>
                Total
              </TD>
              <TD bold>
                {total}
              </TD>
              <TD>Share met, not a probability</TD>
            </tr>
          </tbody>
        </table>
      </div>

      <Card padding="md">
        <SectionTitle>Thresholds</SectionTitle>
        <div
        className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-2"
        >
          <DetailRow label="Strong context ≥" value={String(model.strong_min_score)} />
          <DetailRow label="Neutral context ≥" value={String(model.neutral_min_score)} />
          <DetailRow label="Bullish trend ≥" value={fmtAbs(model.bullish_trend_min_pct)} />
          <DetailRow label="Bearish trend ≤" value={fmtAbs(model.bearish_trend_max_pct)} />
          <DetailRow label="Strong breadth ≥" value={fmtAbs(model.breadth_strong_min_pct)} />
          <DetailRow label="Elevated vol ratio ≥" value={fmtAbs(model.volatility_elevated_ratio, 2, "x")} />
          <DetailRow label="Strong sector RS >" value={fmtAbs(model.sector_strong_rs_pct)} />
          <DetailRow label="Stock RS >" value={fmtAbs(model.stock_strong_rs_pct)} />
          <DetailRow label="RVOL confirmation ≥" value={fmtAbs(model.rvol_confirmation_min, 2, "x")} />
        </div>
      </Card>
    </div>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────────

type ExplorerSub = "signals" | "analytics" | "effectiveness" | "model";

const SUB_TABS: { id: ExplorerSub; label: string }[] = [
  { id: "signals", label: "Signals" },
  { id: "analytics", label: "Analytics" },
  { id: "effectiveness", label: "Effectiveness" },
  { id: "model", label: "Model" },
];

export default function SignalExplorerPanel() {
  const [sub, setSub] = useState<ExplorerSub>("signals");

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Tabs value={sub} onValueChange={(v) => setSub(v as ExplorerSub)} variant="segment">
          <TabsList>
        {SUB_TABS.map((t) => (
              <TabsTrigger key={t.id} value={t.id}>
            {t.label}
              </TabsTrigger>
        ))}
          </TabsList>
        </Tabs>
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      {sub === "signals" && <SignalsPanel />}
      {sub === "analytics" && <AnalyticsPanel />}
      {sub === "effectiveness" && <EffectivenessPanel />}
      {sub === "model" && <ModelPanel />}
    </div>
  );
}
