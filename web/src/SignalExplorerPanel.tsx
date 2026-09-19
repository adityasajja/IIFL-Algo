/**
 * SignalExplorerPanel — the Context-Aware Signal Engine, made inspectable.
 *
 * SIGNAL → CONTEXT → SCORE → OUTCOME
 *
 * Four sub-panels:
 *   1. Signals   — the enriched record, newest first, each row opening the full
 *                  market/sector/stock context and per-criterion score breakdown.
 *   2. Analytics — outcomes bucketed by a context dimension, with honest counts
 *                  and suppression. In-sample buckets are shown but labelled.
 *   3. Effectiveness — does a higher score accompany a different forward
 *                  outcome? Score bands and features measured against their
 *                  complements, with the multiple-comparisons correction shown
 *                  and in-sample results kept apart. A measurement, never a
 *                  scoring change.
 *   4. Model     — the versioned scoring model this whole screen is interpreted
 *                  against: criteria, weights and thresholds.
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

// ─── Colour / theme helpers ───────────────────────────────────────────────────

const TONE_COLOURS: Record<string, string> = {
  good: "#10b981",
  bad: "#ef4444",
  warn: "#eab308",
  muted: "#94a3b8",
};

const trend = (v: number | null | undefined): string => {
  if (v == null) return "#94a3b8";
  return v > 0 ? "#10b981" : v < 0 ? "#ef4444" : "#94a3b8";
};

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
  return (
    <div
      style={{
        background: "rgba(239,68,68,0.1)",
        border: "1px solid rgba(239,68,68,0.3)",
        borderRadius: 8,
        padding: "0.75rem 1rem",
        color: "#ef4444",
        fontSize: 13,
      }}
    >
      {msg}
    </div>
  );
}

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div
      style={{
        background: "rgba(30,41,59,0.7)",
        border: "1px solid rgba(99,102,241,0.15)",
        borderRadius: 12,
        padding: "1rem 1.25rem",
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3
      style={{
        fontSize: 11,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "#94a3b8",
        marginBottom: "0.75rem",
        margin: 0,
      }}
    >
      {children}
    </h3>
  );
}

function TonePill({ label, tone }: { label: string; tone: string }) {
  const colour = TONE_COLOURS[tone] ?? TONE_COLOURS.muted;
  return (
    <span
      style={{
        background: `${colour}20`,
        color: colour,
        borderRadius: 4,
        padding: "0.1rem 0.4rem",
        fontSize: 10,
        fontWeight: 700,
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}

const TH = ({ children }: { children: React.ReactNode }) => (
  <th
    style={{
      padding: "0.4rem 0.6rem",
      textAlign: "left",
      fontWeight: 700,
      color: "#64748b",
      fontSize: 10,
      textTransform: "uppercase",
      letterSpacing: "0.05em",
      whiteSpace: "nowrap",
    }}
  >
    {children}
  </th>
);

const TD = ({
  children,
  colour,
  bold,
}: {
  children: React.ReactNode;
  colour?: string;
  bold?: boolean;
}) => (
  <td style={{ padding: "0.4rem 0.6rem", color: colour ?? "#94a3b8", fontWeight: bold ? 700 : 400 }}>
    {children}
  </td>
);

// ─── Sub-panel 1: Signals ─────────────────────────────────────────────────────

function DetailRow({ label, value, colour }: { label: string; value: string; colour?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 8, padding: "0.15rem 0" }}>
      <span style={{ fontSize: 11, color: "#64748b" }}>{label}</span>
      <span style={{ fontSize: 11, color: colour ?? "#cbd5e1", fontWeight: 600 }}>{value}</span>
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
    <Card>
      <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: "1rem" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 18, fontWeight: 800, color: "#e2e8f0" }}>{record.symbol}</span>
            <span style={{ fontSize: 12, color: "#818cf8", fontWeight: 700 }}>{record.action}</span>
            <TonePill label={contextClassLabel(record.context_class)} tone={contextClassTone(record.context_class)} />
          </div>
          <div style={{ fontSize: 11, color: "#475569", marginTop: 4 }}>
            {record.signal_id} · {record.signal_source} · {shortTs(record.signal_ts)}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, fontWeight: 800, color: TONE_COLOURS[score.tone] }}>
            {score.text}
          </div>
          <div style={{ fontSize: 10, color: "#64748b", maxWidth: 320 }}>{score.caption}</div>
        </div>
      </div>

      {!missing.none && (
        <div style={{ marginTop: "0.6rem", fontSize: 11, color: "#eab308" }}>⚠ {missing.text}</div>
      )}

      <div style={{ marginTop: "0.75rem", borderTop: "1px solid rgba(99,102,241,0.1)", paddingTop: "0.5rem" }}>
        <SectionTitle>Score breakdown</SectionTitle>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
              <TH>Criterion</TH>
              <TH>Weight</TH>
              <TH>Measurement</TH>
              <TH>Status</TH>
              <TH>Reason</TH>
            </tr>
          </thead>
          <tbody>
            {criteria.map((c) => (
              <tr key={c.key} style={{ borderBottom: "1px solid rgba(99,102,241,0.07)" }}>
                <TD colour="#e2e8f0" bold>
                  {c.label}
                </TD>
                <TD>{c.weight}</TD>
                <TD colour={trend(c.status === "met" ? 1 : c.status === "not_met" ? -1 : null)}>{c.value}</TD>
                <TD>
                  <TonePill label={c.status.replace(/_/g, " ")} tone={c.tone} />
                </TD>
                <TD colour="#64748b">{c.reason || "\u2014"}</TD>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div
        style={{
          marginTop: "0.75rem",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "0.75rem",
        }}
      >
        <div>
          <SectionTitle>Market (as of {shortTs(m?.as_of)})</SectionTitle>
          <DetailRow label="Regime" value={m?.regime ?? "\u2014"} />
          <DetailRow label="NIFTY trend vs SMA50" value={fmt(m?.nifty_trend_pct)} colour={trend(m?.nifty_trend_pct)} />
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
          <DetailRow label="RS vs NIFTY 1M" value={fmt(sec?.relative_strength_1m)} colour={trend(sec?.relative_strength_1m)} />
          <DetailRow label="Return 1M" value={fmt(sec?.return_1m_pct)} colour={trend(sec?.return_1m_pct)} />
          <DetailRow label="Trend" value={sec?.trend ?? "\u2014"} />
          <DetailRow label="Volume multiple" value={fmtAbs(sec?.volume_multiple, 2, "x")} />
        </div>
        <div>
          <SectionTitle>Stock (as of {shortTs(s?.as_of)})</SectionTitle>
          <DetailRow label="RS vs NIFTY 20D" value={fmt(s?.relative_strength_nifty_20d)} colour={trend(s?.relative_strength_nifty_20d)} />
          <DetailRow label="Relative volume" value={fmtAbs(s?.relative_volume, 2, "x")} />
          <DetailRow label="ATR %" value={fmtAbs(s?.atr_pct)} />
          <DetailRow label="Trend vs SMA50" value={fmt(s?.trend_pct)} colour={trend(s?.trend_pct)} />
          <DetailRow label="From 52W high" value={fmt(s?.from_52w_high_pct)} />
          <DetailRow label="Close" value={s?.close != null ? s.close.toLocaleString("en-IN") : "\u2014"} />
        </div>
      </div>

      <div style={{ marginTop: "0.6rem", fontSize: 10, color: "#475569" }}>
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
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
        <span style={{ fontSize: 11, color: "#64748b", fontWeight: 600 }}>Source:</span>
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
        <span style={{ fontSize: 11, color: "#64748b", fontWeight: 600 }}>Context:</span>
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
        <button id="signal-refresh" onClick={load} style={buttonStyle}>
          ↻ Refresh
        </button>
        <span style={{ fontSize: 11, color: "#475569", marginLeft: "auto" }}>
          {items.length} of {total}
        </span>
      </div>

      {error && <ErrorBanner msg={error} />}
      {loading ? (
        <PageLoader label="Loading signals" />
      ) : items.length === 0 ? (
        <Card style={{ fontSize: 12, color: "#94a3b8" }}>
          No enriched signal matches these filters yet. Contexts are written when a
          paper/live bar fires or when a backtest completes — an empty panel means
          none has happened, not that scoring failed.
        </Card>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
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
                    style={{
                      borderBottom: "1px solid rgba(99,102,241,0.07)",
                      background: i % 2 === 0 ? "transparent" : "rgba(30,41,59,0.2)",
                      cursor: "pointer",
                    }}
                  >
                    <TD colour="#e2e8f0" bold>
                      {row.symbol}
                    </TD>
                    <TD colour={row.action === "BUY" ? "#10b981" : "#ef4444"}>{row.action}</TD>
                    <TD>{row.source}</TD>
                    <TD>{shortTs(row.signalTs)}</TD>
                    <TD colour={TONE_COLOURS[row.score.tone]} bold>
                      {row.score.text}
                    </TD>
                    <TD>
                      <TonePill label={row.contextClassLabel} tone={row.contextClassTone} />
                    </TD>
                    <TD>{row.regime}</TD>
                    <TD colour="#475569">{row.modelVersion}</TD>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {detail && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <SectionTitle>Signal detail</SectionTitle>
            <button id="signal-detail-close" onClick={() => setDetail(null)} style={buttonStyle}>
              Close
            </button>
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
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <Card>
        <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={labelStyle}>BUCKET BY</label>
            <Select
              size="sm"
              value={dimension}
              onChange={(v) => setDimension(v as ContextDimension)}
              options={DIMENSION_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
              className="w-[200px]"
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={labelStyle}>SOURCE</label>
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
          <button id="analytics-run" onClick={run} disabled={loading} style={buttonStyle}>
            {loading ? "Analyzing…" : "Analyze"}
          </button>
        </div>
        <div style={{ marginTop: 6, fontSize: 11, color: "#475569" }}>{desc}</div>
      </Card>

      {error && <ErrorBanner msg={error} />}

      {result && (
        <>
          <Card>
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <span style={{ fontSize: 12, color: "#cbd5e1" }}>{accounting?.note}</span>
              <span style={{ fontSize: 11, color: "#475569" }}>
                Model {result.model_version} · stats require ≥{result.min_forward_n} forward outcomes
              </span>
            </div>
          </Card>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
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
                    style={{
                      borderBottom: "1px solid rgba(99,102,241,0.07)",
                      background: b.suppressed
                        ? "rgba(15,23,42,0.3)"
                        : i % 2 === 0
                          ? "transparent"
                          : "rgba(30,41,59,0.2)",
                      opacity: b.suppressed ? 0.65 : 1,
                    }}
                  >
                    <TD colour="#e2e8f0" bold>
                      {b.label}
                      {b.suppressed && (
                        <span style={{ marginLeft: 6, fontSize: 10, color: "#475569" }}>(too few)</span>
                      )}
                    </TD>
                    <TD>
                      <TonePill label={b.evidenceLabel} tone={b.evidenceTone} />
                    </TD>
                    <TD>{b.n}</TD>
                    <TD>{b.nForward}</TD>
                    <TD>{b.nInSample}</TD>
                    <TD>{b.unresolved}</TD>
                    <TD colour={trend(b.mean.startsWith("+") ? 1 : b.mean === "\u2014" ? null : -1)} bold>
                      {b.mean}
                    </TD>
                    <TD>{b.median}</TD>
                    <TD colour={b.winRate === "\u2014" ? "#94a3b8" : "#cbd5e1"}>{b.winRate}</TD>
                    <TD>{b.profitFactor}</TD>
                    <TD colour="#64748b">{b.ci || "\u2014"}</TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", fontSize: 10, color: "#475569" }}>
            <span>
              <span style={{ color: "#10b981" }}>● Forward</span> — out-of-sample evidence
            </span>
            <span>
              <span style={{ color: "#eab308" }}>● Thin forward</span> — recorded, below the floor
            </span>
            <span>
              <span style={{ color: "#64748b" }}>● In-sample</span> — backtest; shown but not proof
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
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <Card>
        <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={labelStyle}>SOURCE</label>
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
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={labelStyle}>METRIC</label>
            <Select
              size="sm"
              value={metric}
              onChange={(v) => setMetric(v as EffectivenessMetric)}
              options={METRIC_OPTIONS.map((m) => ({ value: m.id, label: m.label }))}
              className="w-[160px]"
            />
          </div>
          <button id="effectiveness-run" onClick={run} disabled={loading} style={buttonStyle}>
            {loading ? "Measuring…" : "Measure"}
          </button>
        </div>
        <div style={{ marginTop: 6, fontSize: 11, color: "#475569" }}>
          Bands are compared to their complement within the same evidence class — never to
          zero. The score is the share of conditions met, not a probability of profit, and
          nothing here changes it.
        </div>
      </Card>

      {error && <ErrorBanner msg={error} />}

      {result && (
        <>
          <Card>
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <span style={{ fontSize: 12, color: "#cbd5e1" }}>{accounting?.note}</span>
              <span style={{ fontSize: 11, color: "#475569" }}>
                Model {result.model_version} · floor {result.min_sample} trades ·{" "}
                {result.bonferroni_comparisons} comparisons
              </span>
            </div>
          </Card>

          <Card>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: "0.5rem" }}>
              <SectionTitle>High vs low score</SectionTitle>
              {verdict && <TonePill label={verdict.claimLabel} tone={verdict.claimTone} />}
            </div>
            <div style={{ fontSize: 13, color: "#e2e8f0", lineHeight: 1.6 }}>{verdict?.statement}</div>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginTop: "0.6rem" }}>
              {(verdict?.bands || []).map((b) => (
                <span
                  key={b.label}
                  style={{
                    fontSize: 11,
                    color: "#cbd5e1",
                    background: "rgba(99,102,241,0.1)",
                    borderRadius: 6,
                    padding: "0.25rem 0.6rem",
                  }}
                >
                  {b.label}: <strong>{b.meanText}</strong>
                </span>
              ))}
            </div>
            {verdict && !verdict.monotonic && verdict.bands.length > 1 && (
              <div style={{ marginTop: "0.4rem", fontSize: 11, color: "#eab308" }}>
                Band means do not fall monotonically from high to low score.
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle>
              Supported findings ({supported.length}) — forward only, corrected
            </SectionTitle>
            {supported.length === 0 ? (
              <div style={{ fontSize: 12, color: "#94a3b8" }}>
                Nothing clears the bar yet: no forward bucket is both above the{" "}
                {result.min_sample}-trade floor and significant after the correction. The
                scoring model is unchanged.
              </div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
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
                        style={{
                          borderBottom: "1px solid rgba(99,102,241,0.07)",
                          background: i % 2 === 0 ? "transparent" : "rgba(30,41,59,0.2)",
                        }}
                      >
                        <TD colour="#e2e8f0">{f.axisLabel}</TD>
                        <TD colour="#e2e8f0" bold>
                          {f.bucketLabel}
                        </TD>
                        <TD>{f.n}</TD>
                        <TD>{f.mean}</TD>
                        <TD colour={trend(f.liftValue)} bold>
                          {f.lift}
                        </TD>
                        <TD colour="#64748b">{f.pAdjusted}</TD>
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
            <Card>
              <SectionTitle>Suggestive, not supported ({suggestive.length})</SectionTitle>
              <div style={{ fontSize: 11, color: "#64748b", marginBottom: "0.5rem" }}>
                Weak after correction: worth watching as the sample grows, not worth acting on.
              </div>
              {suggestive.map((f) => (
                <div key={`${f.axisLabel}-${f.bucketLabel}`} style={{ fontSize: 12, color: "#cbd5e1", padding: "0.15rem 0" }}>
                  {f.axisLabel} · <strong>{f.bucketLabel}</strong> — n={f.n}, lift {f.lift},{" "}
                  adj. p {f.pAdjusted}{" "}
                  <TonePill label={f.significanceLabel} tone="warn" />
                </div>
              ))}
            </Card>
          )}

          <Card>
            <SectionTitle>Still unproven ({unsupported.length})</SectionTitle>
            {unsupported.map((u) => (
              <div key={u.axis} style={{ fontSize: 12, color: "#94a3b8", padding: "0.15rem 0" }}>
                <strong style={{ color: "#cbd5e1" }}>{u.label}</strong> — {u.reason}
              </div>
            ))}
          </Card>

          <Card>
            <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "flex-end" }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={labelStyle}>FEATURE AXIS</label>
                <Select
                  size="sm"
                  value={axis?.axis ?? "context_score"}
                  onChange={setAxisName}
                  options={(result.axes || []).map((a) => ({ value: a.axis, label: a.label }))}
                  className="w-[260px]"
                />
              </div>
              <span style={{ fontSize: 11, color: "#475569" }}>
                {axis
                  ? `${axis.coverage.with_value} of ${axis.coverage.scanned} forward rows carry a value · ` +
                    `${axis.excluded?.axis_missing ?? 0} excluded (no value), ` +
                    `${axis.excluded?.outcome_missing ?? 0} without outcome`
                  : ""}
              </span>
            </div>
          </Card>

          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
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
                    style={{
                      borderBottom: "1px solid rgba(99,102,241,0.07)",
                      background: b.suppressed
                        ? "rgba(15,23,42,0.3)"
                        : i % 2 === 0
                          ? "transparent"
                          : "rgba(30,41,59,0.2)",
                      opacity: b.suppressed ? 0.65 : 1,
                    }}
                  >
                    <TD colour="#e2e8f0" bold>
                      {b.label}
                      {b.suppressed && (
                        <span style={{ marginLeft: 6, fontSize: 10, color: "#475569" }}>(too few)</span>
                      )}
                    </TD>
                    <TD>{b.n}</TD>
                    <TD>{b.mean}</TD>
                    <TD colour="#64748b">{b.meanCI || "\u2014"}</TD>
                    <TD>{b.median}</TD>
                    <TD>{b.winRate}{b.winRateCI ? ` ${b.winRateCI}` : ""}</TD>
                    <TD>{b.profitFactor}</TD>
                    <TD colour={b.lift.startsWith("+") ? "#10b981" : b.lift === "\u2014" ? "#94a3b8" : "#ef4444"} bold>
                      {b.lift}
                    </TD>
                    <TD colour="#64748b">{b.pAdjusted}</TD>
                    <TD>
                      <TonePill label={b.significanceLabel} tone={b.significanceTone} />
                    </TD>
                    <TD>{b.drawdown}</TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <Card>
            <SectionTitle>Forward vs in-sample — {axis?.label}</SectionTitle>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: "0.5rem" }}>
              The same buckets measured on backtests sit beside the forward ones. They are
              shown, never merged: an in-sample mean is a measurement of the past the rule
              was chosen on, not evidence about the future.
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
                    <TH>Bucket</TH>
                    <TH>Forward n</TH>
                    <TH>Forward mean</TH>
                    <TH>In-sample n</TH>
                    <TH>In-sample mean</TH>
                  </tr>
                </thead>
                <tbody>
                  {comparison.map((r) => (
                    <tr key={r.label} style={{ borderBottom: "1px solid rgba(99,102,241,0.07)" }}>
                      <TD colour="#e2e8f0" bold>
                        {r.label}
                      </TD>
                      <TD>{r.forwardN}</TD>
                      <TD>{r.forwardMean}</TD>
                      <TD>{r.inSampleN}</TD>
                      <TD colour="#64748b">{r.inSampleMean}</TD>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card>
            <SectionTitle>Caveats</SectionTitle>
            <ul style={{ margin: 0, paddingLeft: "1.1rem", fontSize: 12, color: "#94a3b8", lineHeight: 1.7 }}>
              <li>{result.model_note}</li>
              {result.caveats.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
            <div style={{ marginTop: "0.4rem", fontSize: 10, color: "#475569" }}>
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
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <div style={{ fontSize: 11, color: "#64748b" }}>ACTIVE CONTEXT MODEL</div>
        <div style={{ fontSize: 18, fontWeight: 800, color: "#818cf8" }}>{model.version}</div>
        <p style={{ margin: "0.5rem 0 0", fontSize: 12, color: "#94a3b8", lineHeight: 1.6 }}>
          {model.description}
        </p>
      </Card>

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
              <TH>Criterion</TH>
              <TH>Weight</TH>
              <TH>Description</TH>
            </tr>
          </thead>
          <tbody>
            {model.criteria.map((c) => (
              <tr key={c.key} style={{ borderBottom: "1px solid rgba(99,102,241,0.07)" }}>
                <TD colour="#e2e8f0" bold>
                  {c.label}
                </TD>
                <TD colour="#818cf8" bold>
                  +{c.weight}
                </TD>
                <TD colour="#64748b">{c.description}</TD>
              </tr>
            ))}
            <tr style={{ borderTop: "1px solid rgba(99,102,241,0.2)" }}>
              <TD colour="#94a3b8" bold>
                Total
              </TD>
              <TD colour="#94a3b8" bold>
                {total}
              </TD>
              <TD colour="#475569">A score is the share of these conditions met — not a probability.</TD>
            </tr>
          </tbody>
        </table>
      </div>

      <Card>
        <SectionTitle>Thresholds</SectionTitle>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: "0.5rem",
          }}
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

// ─── Styles ───────────────────────────────────────────────────────────────────

const buttonStyle: React.CSSProperties = {
  background: "rgba(99,102,241,0.15)",
  border: "1px solid rgba(99,102,241,0.4)",
  borderRadius: 8,
  color: "#818cf8",
  padding: "0.4rem 0.85rem",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
};

const labelStyle: React.CSSProperties = {
  fontSize: 10,
  color: "#64748b",
  fontWeight: 600,
};

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
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div
        style={{
          display: "flex",
          gap: "0.25rem",
          borderBottom: "1px solid rgba(99,102,241,0.15)",
          paddingBottom: "0.5rem",
        }}
      >
        {SUB_TABS.map((t) => (
          <button
            key={t.id}
            id={`explorer-sub-${t.id}`}
            onClick={() => setSub(t.id)}
            style={{
              padding: "0.4rem 0.9rem",
              fontSize: 12,
              fontWeight: 600,
              borderRadius: "6px 6px 0 0",
              cursor: "pointer",
              background: sub === t.id ? "rgba(99,102,241,0.15)" : "transparent",
              border: "none",
              borderBottom: sub === t.id ? "2px solid #6366f1" : "2px solid transparent",
              color: sub === t.id ? "#818cf8" : "#64748b",
              transition: "all 0.15s",
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      {sub === "signals" && <SignalsPanel />}
      {sub === "analytics" && <AnalyticsPanel />}
      {sub === "effectiveness" && <EffectivenessPanel />}
      {sub === "model" && <ModelPanel />}
    </div>
  );
}
