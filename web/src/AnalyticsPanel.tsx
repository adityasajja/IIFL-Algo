/**
 * AnalyticsPanel — post-trade attribution, made inspectable.
 *
 * SIGNAL → CONTEXT → SIZING → RISK → GATE → OMS → FILL → CLOSED TRADE
 *        → POST-TRADE ATTRIBUTION → FORWARD LEARNING DATASET
 *
 * This is the screen at the end of that chain. It answers three questions and
 * refuses to answer a fourth:
 *
 *   **What happened?**      Overview — P&L, hit rate, costs, slippage, holding.
 *   **Why did it happen?**  Attribution — the nine branches, and which of them
 *                           are *decisions* versus *execution outcomes*.
 *   **How well was it done?** MAE/MFE and Execution — the path the trade took
 *                           while it was open, and the quality of the fills.
 *
 * The fourth question — *which strategy is best?* — is deliberately not answered.
 * There is no ranking anywhere on this screen. A comparison across strategies,
 * regimes and buckets is here; a leaderboard is not, because a leaderboard on a
 * book of this size would be a ranking of noise, and the one strategy that wins
 * it would change with the window.
 *
 * What every panel says about its own evidence
 * --------------------------------------------
 *
 * Every figure carries the grade counts it was computed over, and a book with no
 * forward trades says so in plain words at the top. A trade measured on the
 * history its rule was chosen on is `in_sample`; it is shown, labelled, and never
 * pooled with a forward one as though the two were the same claim.
 *
 * Indian cash equities only. No F&O. Nothing here places, changes or promotes.
 */

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Crosshair,
  Filter,
  Gauge,
  Info,
  RefreshCw,
  Target,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  AnalyticsBucket,
  AnalyticsSummary,
  AnalyticsFilters,
  AttributedTradeRow,
  EvidenceGrade,
  ExecutionDistribution,
  MaeMfeAnalytics,
  StrategyComparison,
  TradeAttributionDetail,
  TradeSource,
  getAnalyticsByContext,
  getAnalyticsByExecution,
  getAnalyticsByRegime,
  getAnalyticsBySizing,
  getAnalyticsByStrategy,
  getAnalyticsMaeMfe,
  getAnalyticsSummary,
  getAttributedTrades,
  getTradeAttribution,
} from "./api";
import { Card, CardHeader, ErrorBox } from "./components/ui/card";
import { Select } from "./components/ui/select";
import { Badge, Callout, Stat } from "./components/ui/stat";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import {
  EM_DASH,
  BucketView,
  branchViews,
  bucketViews,
  bucketedAccounting,
  bps,
  codeBasis,
  codeGroups,
  coverageView,
  distributionView,
  duration,
  evidenceCountsView,
  filterChips,
  gradeView,
  isEmptyBook,
  methodologyView,
  missingFieldsView,
  money,
  num,
  pct,
  profitFactor,
  rMultiple,
  scatterPoints,
  scopeNote,
  tradeRowViews,
} from "./lib/analytics-view";

// ─── shared bits ──────────────────────────────────────────────────────────────

type Tone = "good" | "bad" | "warn" | "info" | "flat";

const TONE_TEXT: Record<Tone, string> = {
  good: "text-emerald-600 dark:text-emerald-400",
  bad: "text-destructive",
  warn: "text-amber-600 dark:text-amber-400",
  info: "text-sky-600 dark:text-sky-400",
  flat: "text-muted-foreground",
};

/** A horizontal bar row used by every bucket table. */
function BucketRow({ view, max }: { view: BucketView; max: number }) {
  const width = max > 0 ? Math.max(2, Math.round((view.n / max) * 100)) : 0;
  return (
    <div className="border-b border-border/60 py-2 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium">{view.label}</span>
            {view.suppressed ? (
              <Badge tone="warn" className="shrink-0">
                n={view.n}
              </Badge>
            ) : null}
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary/45"
              style={{ width: `${width}%` }}
            />
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className={`text-[13px] font-bold tabular-nums ${TONE_TEXT[view.netPnl == null ? "flat" : view.netPnl > 0 ? "good" : view.netPnl < 0 ? "bad" : "flat"]}`}>
            {money(view.netPnl)}
          </div>
          <div className="text-[11px] tabular-nums text-muted-foreground">
            {view.n} trade{view.n === 1 ? "" : "s"}
            {view.winRate != null ? ` \u00b7 ${(view.winRate * 100).toFixed(0)}% win` : ""}
          </div>
        </div>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] tabular-nums text-muted-foreground">
        <span>
          {view.suppressed
            ? "mean withheld below the floor"
            : `expectancy ${money(view.expect)}`}
        </span>
        <span>PF {profitFactor(view.profitFactor)}</span>
        <span>{duration(view.holdingSec)}</span>
        <span>{view.evidence}</span>
      </div>
    </div>
  );
}

function BucketTable({
  title,
  sub,
  buckets,
  total,
}: {
  title: string;
  sub?: string;
  buckets: AnalyticsBucket[];
  total: number;
}) {
  const views = bucketViews(buckets);
  const accounting = bucketedAccounting(views, total);
  const max = views.reduce((m, v) => Math.max(m, v.n), 0);
  return (
    <Card className="px-5 py-4">
      <CardHeader title={title} sub={sub} />
      <div className="mt-2 px-0">
        {views.length === 0 ? (
          <div className="py-3 text-[13px] text-muted-foreground">
            No attributed trade falls into any bucket on this dimension yet.
          </div>
        ) : (
          views.map((v) => <BucketRow key={v.key} view={v} max={max} />)
        )}
      </div>
      {accounting.note ? (
        <div className="mt-3 border-t border-border pt-2 text-[11.5px] text-muted-foreground">
          {accounting.note}
        </div>
      ) : null}
    </Card>
  );
}

// ─── overview ─────────────────────────────────────────────────────────────────

function OverviewTab({ summary }: { summary: AnalyticsSummary }) {
  const ev = evidenceCountsView(summary.counts);
  const coverage = coverageView(summary.attribution_coverage);
  const empty = isEmptyBook(summary.counts);

  if (empty) {
    return (
      <Callout tone="info" title="Nothing to summarise yet">
        No trade has been attributed for this account. An attribution row is
        written for a closed trade that has a journal episode, so this is empty
        until a live or paper deployment has traded and closed. Every figure on
        this screen would be a blank, not a zero.
      </Callout>
    );
  }

  const bestShare = summary.best_trade_share_of_gross_profit;

  return (
    <div className="space-y-4">
      {coverage.warning ? (
        <Callout tone="warn" title="Partial attribution">
          {coverage.warning}
        </Callout>
      ) : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Net P&L"
          value={money(summary.net_pnl)}
          tone={summary.net_pnl == null ? "neutral" : summary.net_pnl > 0 ? "good" : summary.net_pnl < 0 ? "bad" : "neutral"}
          sub={`gross ${money(summary.gross_pnl)}`}
          hint="Rupees after transaction costs. Net, not gross, is what a decision is made on."
        />
        <Stat
          label="Win rate"
          value={summary.win_rate == null ? EM_DASH : `${(summary.win_rate * 100).toFixed(1)}%`}
          sub={`${summary.wins}W / ${summary.losses}L / ${summary.flat} flat`}
          hint="A rate with no denominator is undefined, not zero."
        />
        <Stat
          label="Profit factor"
          value={profitFactor(summary.profit_factor)}
          sub={
            summary.profit_factor == null
              ? "undefined — no losses or no wins"
              : "gross win ÷ gross loss"
          }
          hint="Undefined when there are no losses. An infinite figure would outrank every real strategy."
        />
        <Stat
          label="Expectancy"
          value={money(summary.expectancy)}
          tone={summary.expectancy == null ? "neutral" : summary.expectancy > 0 ? "good" : "bad"}
          sub="mean net P&L per trade"
        />
        <Stat label="Average win" value={money(summary.average_win)} tone="good" sub={`largest ${money(summary.largest_win)}`} />
        <Stat label="Average loss" value={money(summary.average_loss)} tone="bad" sub={`largest ${money(summary.largest_loss)}`} />
        <Stat
          label="Average holding"
          value={duration(summary.mean_holding_sec)}
          sub={`median ${duration(summary.median_holding_sec)}`}
        />
        <Stat
          label="Total costs"
          value={money(summary.total_costs)}
          sub={summary.costs_per_trade == null ? "per trade unknown" : `${money(summary.costs_per_trade)} per trade`}
          hint="Commission and fees recorded on the trade."
        />
      </div>

      <Card className="px-5 py-4">
        <CardHeader
          title="The number beside the headline"
          sub="One trade carrying a book is the most common way a small sample lies about itself."
        />
        <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-2">
          <div className="rounded-xl border border-border bg-muted/30 px-3.5 py-3">
            <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
              Best trade as a share of gross profit
            </div>
            <div className={`mt-1 text-[19px] font-bold tabular-nums ${bestShare != null && bestShare > 0.5 ? TONE_TEXT.warn : ""}`}>
              {bestShare == null ? EM_DASH : `${(bestShare * 100).toFixed(0)}%`}
            </div>
            <div className="mt-0.5 text-[11.5px] text-muted-foreground">
              {bestShare == null
                ? "undefined — the book has no gross profit"
                : bestShare > 0.5
                  ? "Over half the book's gross profit came from a single trade. Treat the mean with suspicion."
                  : "No single trade dominates the gross profit."}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-muted/30 px-3.5 py-3">
            <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
              Net P&L without the best trade
            </div>
            <div className={`mt-1 text-[19px] font-bold tabular-nums ${summary.net_without_best == null ? "" : summary.net_without_best > 0 ? TONE_TEXT.good : TONE_TEXT.bad}`}>
              {money(summary.net_without_best)}
            </div>
            <div className="mt-0.5 text-[11.5px] text-muted-foreground">
              {summary.net_without_best != null && summary.net_without_best <= 0
                ? "The book is not profitable without its single best trade."
                : "The book stands up without its single best trade."}
            </div>
          </div>
        </div>
      </Card>

      <Card className="px-5 py-4">
        <CardHeader title="What this describes" sub="Scope, stated rather than implied." />
        <div className="mt-2 space-y-1.5 text-[12.5px] text-muted-foreground">
          <div>
            <span className="font-medium text-foreground">{ev.label}</span> — grade counts for
            every figure above.
          </div>
          <div>{coverage.text}.</div>
          <div>
            Costs and slippage: mean total slippage {bps(summary.total_slippage_bps_mean)}; sum{" "}
            {bps(summary.total_slippage_bps_sum)}. Slippage is averaged over measurable legs
            only — a trade with no recorded reference price has no slippage measurement, which
            is not the same as zero.
          </div>
        </div>
      </Card>
    </div>
  );
}

// ─── attribution ──────────────────────────────────────────────────────────────

const BRANCH_SPECS: { key: keyof StrategyComparison["branches"]; title: string; sub: string }[] = [
  { key: "signal", title: "Signal", sub: "Did the trade have a traceable signal link?" },
  { key: "context", title: "Market context", sub: "The recorded context class at signal time." },
  { key: "sizing", title: "Position sizing", sub: "The sizing method and whether a cap bound it." },
  { key: "risk", title: "Risk", sub: "The risk the position actually carried." },
  { key: "execution", title: "Execution", sub: "Fill quality, independent of the signal." },
  { key: "exit", title: "Exit", sub: "Why each trade closed." },
];

function AttributionTab({ summary, filters }: { summary: AnalyticsSummary; filters: AnalyticsFilters }) {
  const [branches, setBranches] = useState<StrategyComparison["branches"] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getAnalyticsByStrategy(filters);
        const first = res.data?.strategies?.[0];
        if (!cancelled) setBranches(first?.branches ?? null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters)]);

  if (error) return <ErrorBox>{error}</ErrorBox>;

  const total = summary.n_trades;
  if (total === 0) {
    return (
      <Callout tone="info" title="No attributed trades">
        There is nothing to attribute yet. The branches below describe how trades
        classify, and there are none.
      </Callout>
    );
  }

  return (
    <div className="space-y-4">
      <Callout tone="info" title="Each bucket is a classification, not a share of the P&L">
        A trade's rupees cannot be split into "signal rupees" and "execution
        rupees" — the signal earned nothing without an execution. What these
        tables say is how trades *classified* a given way performed, with the
        counts attached.
      </Callout>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {BRANCH_SPECS.map((spec) => (
          <BucketTable
            key={spec.key}
            title={spec.title}
            sub={spec.sub}
            buckets={(branches?.[spec.key] as AnalyticsBucket[]) ?? []}
            total={total}
          />
        ))}
      </div>
    </div>
  );
}

// ─── MAE / MFE ────────────────────────────────────────────────────────────────

function ScatterChart({
  points,
  axis,
}: {
  points: ReturnType<typeof scatterPoints>;
  axis: "mfe" | "mae";
}) {
  if (points.length === 0) {
    return (
      <div className="py-6 text-center text-[13px] text-muted-foreground">
        No trade has both a measured{" "}
        {axis === "mfe" ? "favourable" : "adverse"} excursion and a recorded outcome,
        so there is no relationship to plot.
      </div>
    );
  }

  const w = 640;
  const h = 260;
  const pad = 34;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(0, ...xs);
  const maxX = Math.max(...xs, 1);
  const minY = Math.min(0, ...ys);
  const maxY = Math.max(...ys, 1);

  const sx = (v: number) => pad + ((v - minX) / (maxX - minX || 1)) * (w - pad * 2);
  const sy = (v: number) => h - pad - ((v - minY) / (maxY - minY || 1)) * (h - pad * 2);

  const zeroY = sy(0);
  const zeroX = sx(0);

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full" role="img" aria-label={`${axis.toUpperCase()} against outcome`}>
      {/* axes */}
      <line x1={pad} y1={zeroY} x2={w - pad} y2={zeroY} stroke="#64748b" strokeWidth="1" strokeDasharray="3 3" />
      {minX < 0 && maxX > 0 ? (
        <line x1={zeroX} y1={pad} x2={zeroX} y2={h - pad} stroke="#64748b" strokeWidth="1" strokeDasharray="3 3" />
      ) : null}

      {/* gridlines at the quartiles */}
      {[0.25, 0.5, 0.75].map((f) => (
        <line
          key={f}
          x1={pad}
          y1={pad + f * (h - pad * 2)}
          x2={w - pad}
          y2={pad + f * (h - pad * 2)}
          stroke="currentColor"
          className="text-border"
          strokeWidth="0.5"
        />
      ))}

      {points.map((p, i) => (
        <circle
          key={`${p.tradeId ?? i}`}
          cx={sx(p.x)}
          cy={sy(p.y)}
          r={p.forced ? 4.5 : 3.5}
          fill={p.forced ? "#ef4444" : p.y >= 0 ? "#10b981" : "#f97316"}
          fillOpacity={0.75}
          stroke={p.forced ? "#ef4444" : "none"}
          strokeWidth={p.forced ? 1 : 0}
        >
          <title>
            {`${p.symbol ?? "?"} — ${axis.toUpperCase()} ${num(p.x)}R, outcome ${money(p.y)}${p.forced ? " (stop-driven)" : ""}`}
          </title>
        </circle>
      ))}

      <text x={w / 2} y={h - 6} textAnchor="middle" className="fill-muted-foreground" fontSize="11">
        {axis === "mfe" ? "MFE / initial risk (R)" : "|MAE| / initial risk (R)"}
      </text>
    </svg>
  );
}

function MaeMfeTab({ data, filters }: { data: MaeMfeAnalytics | null; filters: AnalyticsFilters }) {
  const [includePoints, setIncludePoints] = useState(true);
  const [loaded, setLoaded] = useState<MaeMfeAnalytics | null>(data);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getAnalyticsMaeMfe({ ...filters, includePoints });
        if (!cancelled) setLoaded(res.data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), includePoints]);

  if (error) return <ErrorBox>{error}</ErrorBox>;
  if (!loaded || loaded.n === 0) {
    return (
      <Callout tone="info" title="No excursions measured">
        No attributed trade has a usable price series between entry and exit, so
        there is nothing to plot. An unmeasured excursion is not a flat one.
      </Callout>
    );
  }

  const mfe = distributionView(loaded.mfe_over_risk, loaded.n);
  const mae = distributionView(loaded.mae_over_risk, loaded.n);
  const mfePoints = scatterPoints(loaded.points, "mfe");
  const maePoints = scatterPoints(loaded.points, "mae");
  const method = methodologyView(loaded.methodology);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Mean MFE / risk"
          value={rMultiple(mfe.mean)}
          tone="good"
          sub={mfe.note}
          hint="How far the trade ran in your favour, as a multiple of the risk it was sized to take."
        />
        <Stat label="Median MFE / risk" value={rMultiple(mfe.median)} sub={`${mfe.measured} measured`} />
        <Stat
          label="Mean |MAE| / risk"
          value={mae.mean == null ? EM_DASH : `${Math.abs(mae.mean).toFixed(2)}R`}
          tone="bad"
          sub={mae.note}
          hint="How far the trade went against you before it resolved."
        />
        <Stat
          label="Mean realized R"
          value={rMultiple(loaded.realized_over_risk.mean)}
          tone={loaded.realized_over_risk.mean == null ? "neutral" : loaded.realized_over_risk.mean > 0 ? "good" : "bad"}
          sub={`${loaded.realized_over_risk.measured} measured`}
        />
      </div>

      <Callout tone="info" title="Reading the gap between MFE and realized R">
        A mean MFE well above the mean realized R is a management problem, not a
        signal problem: the trades reached a profit the exits did not keep. A mean
        MFE barely above zero is a signal problem — the trades never went anywhere.
        These are different diagnoses and the two figures above separate them.
      </Callout>

      <Card className="px-5 py-4">
        <CardHeader
          title="Favourable excursion vs outcome"
          sub="Each point is one trade. Hollow red points were stopped out — their adverse excursion reached the risk the position was sized for."
          action={
            <button
              type="button"
              onClick={() => setIncludePoints((v) => !v)}
              className="rounded-lg border border-border px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:text-foreground"
            >
              {includePoints ? "Summarise only" : "Show trades"}
            </button>
          }
        />
        <div className="mt-3">
          {includePoints ? <ScatterChart points={mfePoints} axis="mfe" /> : null}
        </div>
      </Card>

      <Card className="px-5 py-4">
        <CardHeader title="Adverse excursion vs outcome" sub="The same trades, against how far they went against the position." />
        <div className="mt-3">
          {includePoints ? <ScatterChart points={maePoints} axis="mae" /> : null}
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card className="px-5 py-4">
          <CardHeader title="MFE distribution" sub="Bucketed by R multiple." />
          <div className="mt-2 space-y-1.5">
            {loaded.mfe_over_risk.buckets.map((b) => (
              <div key={b.key} className="flex items-center justify-between text-[12.5px]">
                <span className="text-muted-foreground">{b.key.replace(/_/g, " ")}</span>
                <span className="tabular-nums font-medium">{b.n}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card className="px-5 py-4">
          <CardHeader title="MAE distribution" sub="Bucketed by R multiple." />
          <div className="mt-2 space-y-1.5">
            {loaded.mae_over_risk.buckets.map((b) => (
              <div key={b.key} className="flex items-center justify-between text-[12.5px]">
                <span className="text-muted-foreground">{b.key.replace(/_/g, " ")}</span>
                <span className="tabular-nums font-medium">{b.n}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card className="px-5 py-4">
        <CardHeader
          title="How these figures were produced"
          sub="An excursion is only interpretable alongside the window it was measured over."
        />
        <div className="mt-2 space-y-1.5">
          {method.map((m) => (
            <div key={m.key} className="text-[12.5px]">
              <span className="font-medium">{m.label}:</span>{" "}
              <span className="text-muted-foreground">{m.value}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

// ─── execution ────────────────────────────────────────────────────────────────

function ExecutionTab({
  distribution,
  buckets,
  total,
}: {
  distribution: ExecutionDistribution | null;
  buckets: {
    by_quality: AnalyticsBucket[];
    by_entry_quality: AnalyticsBucket[];
    by_slippage_bucket: AnalyticsBucket[];
  } | null;
  total: number;
}) {
  if (!distribution) {
    return (
      <Callout tone="info" title="No execution measurements">
        No attributed trade has a recorded fill reference, so slippage and delay
        are unmeasurable. That is different from a cost of zero.
      </Callout>
    );
  }

  const slip = distribution.slippage;
  const delays = distribution.delays ?? {};
  const costs = distribution.costs ?? {};

  return (
    <div className="space-y-4">
      <Callout tone="warn" title="Execution is its own dimension">
        A profitable strategy with poor execution reads differently from a strong
        execution of a weak signal. Nothing on this tab is folded into a single
        score, so the two cannot be confused.
      </Callout>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Mean slippage"
          value={bps(slip?.mean_bps)}
          tone={slip?.mean_bps == null ? "neutral" : slip.mean_bps > 15 ? "warn" : slip.mean_bps < 0 ? "good" : "neutral"}
          sub={`median ${bps(slip?.median_bps)}`}
          hint="Adverse-positive: a negative figure means the fills beat their reference prices."
        />
        <Stat
          label="Measurable legs"
          value={num(slip?.measured, 0)}
          sub={
            slip?.unmeasurable
              ? `${num(slip.unmeasurable, 0)} unmeasurable — no reference price`
              : "every leg measured"
          }
        />
        <Stat label="Total slippage cost" value={money(slip?.total_amount)} tone="bad" sub="on filled quantity" />
        <Stat
          label="Signal → order"
          value={duration(delays.signal_to_order_sec ?? null)}
          sub={`order → fill ${duration(delays.order_to_fill_sec ?? null)}`}
          hint="Where the latency sits, which decides whether the fix is the rule or the plumbing."
        />
      </div>

      {costs.total != null || costs.per_trade != null ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Transaction costs" value={money(costs.total)} sub="recorded on the trade" />
          <Stat label="Costs per trade" value={money(costs.per_trade)} />
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <BucketTable
          title="By execution quality"
          sub="The overall classification of how the round trip was executed."
          buckets={buckets?.by_quality ?? []}
          total={total}
        />
        <BucketTable
          title="By entry quality"
          sub="Entry fills only."
          buckets={buckets?.by_entry_quality ?? []}
          total={total}
        />
        <BucketTable
          title="By slippage band"
          sub="Adverse-positive basis points across measurable legs."
          buckets={buckets?.by_slippage_bucket ?? []}
          total={total}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BucketTable
          title="By symbol"
          sub="Where the friction actually lives. A symbol-level pattern is a venue or liquidity finding, not a strategy finding."
          buckets={distribution.by_symbol ?? []}
          total={total}
        />
        <BucketTable
          title="By time of day"
          sub="The same reading, against the clock."
          buckets={distribution.by_time_of_day ?? []}
          total={total}
        />
      </div>

      {distribution.partial_fills ? (
        <Card className="px-5 py-4">
          <CardHeader title="Partial fills" sub="A partial fill is a sizing discovery, not an execution failure by itself." />
          <div className="mt-2 flex flex-wrap gap-4">
            {Object.entries(distribution.partial_fills).map(([k, v]) => (
              <div key={k} className="text-[12.5px]">
                <span className="text-muted-foreground">{k.replace(/_/g, " ")}: </span>
                <span className="font-medium tabular-nums">{num(v, 0)}</span>
              </div>
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  );
}

// ─── strategies ───────────────────────────────────────────────────────────────

function StrategiesTab({
  strategies,
  note,
}: {
  strategies: StrategyComparison[];
  note: string;
}) {
  if (strategies.length === 0) {
    return (
      <Callout tone="info" title="No attributed trades">
        No strategy has an attributed trade yet, so there is nothing to compare.
      </Callout>
    );
  }

  return (
    <div className="space-y-4">
      <Callout tone="warn" title="This is a comparison, not a ranking">
        {note} A strategy missing from this list has no attributed trades, which is
        different from having performed badly.
      </Callout>

      {strategies.map((s) => {
        const ev = evidenceCountsView(s.summary.counts);
        return (
          <Card key={s.key} className="px-5 py-4">
            <CardHeader
              title={s.key}
              sub={`${s.n} attributed trade${s.n === 1 ? "" : "s"}`}
              action={<Badge tone={ev.tone}>{ev.label}</Badge>}
            />
            <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
              <Stat label="Net P&L" value={money(s.summary.net_pnl)} tone={s.summary.net_pnl == null ? "neutral" : s.summary.net_pnl > 0 ? "good" : "bad"} />
              <Stat label="Win rate" value={s.summary.win_rate == null ? EM_DASH : `${(s.summary.win_rate * 100).toFixed(0)}%`} sub={`${s.summary.wins}W / ${s.summary.losses}L`} />
              <Stat label="Profit factor" value={profitFactor(s.summary.profit_factor)} />
              <Stat label="Mean slippage" value={bps(s.summary.total_slippage_bps_mean)} />
              <Stat label="Mean holding" value={duration(s.summary.mean_holding_sec)} />
            </div>

            <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
              {BRANCH_SPECS.map((spec) => {
                const buckets = (s.branches?.[spec.key] as AnalyticsBucket[]) ?? [];
                if (buckets.length === 0) return null;
                return (
                  <BucketTable
                    key={spec.key}
                    title={spec.title}
                    sub={spec.sub}
                    buckets={buckets}
                    total={s.n}
                  />
                );
              })}
            </div>
          </Card>
        );
      })}
    </div>
  );
}

// ─── trade detail ─────────────────────────────────────────────────────────────

function TradeDetail({ tradeId, onClose }: { tradeId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<TradeAttributionDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getTradeAttribution(tradeId);
        if (!cancelled) setDetail(res.data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tradeId]);

  if (error) return <ErrorBox>{error}</ErrorBox>;
  if (!detail) return <div className="py-6 text-center text-[13px] text-muted-foreground">Loading attribution…</div>;

  const branches = branchViews(detail);
  const groups = codeGroups(detail.reason_codes ?? detail.attribution?.reason_codes);
  const detailMap = (detail.attribution?.reason_detail ?? null) as Record<string, unknown> | null;
  const missing = missingFieldsView(detail.missing_fields, detail.attribution?.missing_fields);
  const grade = gradeView(detail.evidence_grade, detail.evidence_class);

  return (
    <Card className="px-5 py-4">
      <CardHeader
        title={`${detail.symbol} \u00b7 ${String(detail.side).toUpperCase()}`}
        sub={`Trade ${detail.trade_id} \u00b7 ${detail.source}`}
        action={
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:text-foreground"
          >
            Close
          </button>
        }
      />

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Badge tone={grade.tone === "flat" ? "flat" : grade.tone}>{grade.label}</Badge>
        {detail.simulated ? (
          <Badge tone="info">Simulated fills</Badge>
        ) : null}
        <span className="text-[11.5px] text-muted-foreground">{grade.hint}</span>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        {branches.map((b) => (
          <div key={b.key} className="rounded-xl border border-border bg-card px-3.5 py-3">
            <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
              {b.label}
            </div>
            <div className={`mt-1 text-[15px] font-bold tabular-nums ${TONE_TEXT[b.tone]}`}>{b.headline}</div>
            <div className="mt-1 text-[11.5px] leading-snug text-muted-foreground">{b.reading}</div>
          </div>
        ))}
      </div>

      {groups.length > 0 ? (
        <div className="mt-4">
          <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
            Reason codes
          </div>
          <div className="mt-2 space-y-2">
            {groups.map((g) => (
              <div key={g.family}>
                <div className="flex items-center gap-2">
                  <Badge tone={g.tone === "flat" ? "flat" : g.tone}>{g.label}</Badge>
                </div>
                <div className="mt-1 space-y-0.5">
                  {g.codes.map((c) => {
                    const raw = c.replace(/ /g, "_");
                    const basis = codeBasis(detailMap, raw);
                    return (
                      <div key={c} className="text-[12.5px]">
                        <span className="font-medium">{c}</span>
                        {basis ? <span className="text-muted-foreground"> — {basis}</span> : null}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {missing.length > 0 ? (
        <div className="mt-4">
          <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
            What could not be resolved
          </div>
          <div className="mt-1.5 space-y-0.5 text-[12.5px] text-muted-foreground">
            {missing.map((m) => (
              <div key={m.field}>
                <span className="font-medium text-foreground">{m.field}</span> — {m.reason}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {detail.input_fingerprint ? (
        <div className="mt-4 border-t border-border pt-2 text-[11px] text-muted-foreground">
          Computed {detail.computed_at ? new Date(detail.computed_at).toLocaleString("en-IN") : EM_DASH} ·
          fingerprint {detail.input_fingerprint.slice(0, 12)}…
        </div>
      ) : null}
    </Card>
  );
}

// ─── trade list ───────────────────────────────────────────────────────────────

function TradesTab({
  rows,
  total,
}: {
  rows: AttributedTradeRow[];
  total: number;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const views = useMemo(() => tradeRowViews(rows), [rows]);

  if (total === 0) {
    return (
      <Callout tone="info" title="No attributed trades">
        No trade has been attributed for these filters. An attribution is written
        for a closed trade with a journal episode — nothing has closed under this
        scope yet.
      </Callout>
    );
  }

  return (
    <div className="space-y-4">
      {selected ? <TradeDetail tradeId={selected} onClose={() => setSelected(null)} /> : null}

      <Card className="overflow-hidden">
        <CardHeader
          title="Attributed trades"
          sub={`${total} trade${total === 1 ? "" : "s"} under the current filters. Select a row for the full attribution tree.`}
        />
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-[0.05em] text-muted-foreground">
                <th className="px-3 pb-2 font-medium">Symbol</th>
                <th className="px-3 pb-2 font-medium">Side</th>
                <th className="px-3 pb-2 text-right font-medium">Net P&amp;L</th>
                <th className="px-3 pb-2 text-right font-medium">Return</th>
                <th className="px-3 pb-2 text-right font-medium">MFE</th>
                <th className="px-3 pb-2 text-right font-medium">MAE</th>
                <th className="px-3 pb-2 text-right font-medium">Captured</th>
                <th className="px-3 pb-2 text-right font-medium">Slippage</th>
                <th className="px-3 pb-2 font-medium">Held</th>
                <th className="px-3 pb-2 font-medium">Exit</th>
                <th className="px-3 pb-2 font-medium">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {views.map((r) => {
                const g = gradeView(r.grade, r.klass);
                return (
                  <tr
                    key={r.tradeId}
                    onClick={() => setSelected(r.tradeId)}
                    className="cursor-pointer border-b border-border/60 transition hover:bg-muted/40 last:border-b-0"
                  >
                    <td className="px-3 py-2 font-medium">{r.symbol}</td>
                    <td className="px-3 py-2 text-muted-foreground">{r.side}</td>
                    <td className={`px-3 py-2 text-right font-bold tabular-nums ${TONE_TEXT[r.pnlTone]}`}>
                      {money(r.netPnl)}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${TONE_TEXT[r.pnlTone]}`}>
                      {pct(r.netReturnPct)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-emerald-600 dark:text-emerald-400">
                      {r.mfePct == null ? EM_DASH : `${r.mfePct.toFixed(2)}%`}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-destructive">
                      {r.maePct == null ? EM_DASH : `${r.maePct.toFixed(2)}%`}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {r.capture == null ? EM_DASH : `${r.capture.toFixed(0)}%`}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${r.slippageBps == null ? "" : r.slippageBps > 15 ? TONE_TEXT.warn : ""}`}>
                      {bps(r.slippageBps)}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{duration(r.holdingSec)}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {r.exitReason ? r.exitReason.replace(/_/g, " ").toLowerCase() : EM_DASH}
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={g.tone === "flat" ? "flat" : g.tone}>{g.label}</Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

// ─── panel ────────────────────────────────────────────────────────────────────

export default function AnalyticsPanel() {
  const [tab, setTab] = useState("overview");
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [trades, setTrades] = useState<{ items: AttributedTradeRow[]; total: number }>({
    items: [],
    total: 0,
  });
  const [strategies, setStrategies] = useState<{ list: StrategyComparison[]; note: string }>({
    list: [],
    note: "",
  });
  const [regime, setRegime] = useState<AnalyticsBucket[]>([]);
  const [context, setContext] = useState<{ by_class: AnalyticsBucket[]; by_score_band: AnalyticsBucket[] } | null>(null);
  const [sizing, setSizing] = useState<{ by_method: AnalyticsBucket[]; by_cap: AnalyticsBucket[] } | null>(null);
  const [execution, setExecution] = useState<{
    distribution: ExecutionDistribution | null;
    buckets: {
      by_quality: AnalyticsBucket[];
      by_entry_quality: AnalyticsBucket[];
      by_slippage_bucket: AnalyticsBucket[];
    } | null;
  }>({ distribution: null, buckets: null });
  const [maeMfe, setMaeMfe] = useState<MaeMfeAnalytics | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showFilters, setShowFilters] = useState(false);

  // Filters. Kept as strings so an empty input means "no filter" rather than
  // "filter on the empty string", which would return nothing.
  const [fStrategy, setFStrategy] = useState("");
  const [fSymbol, setFSymbol] = useState("");
  const [fSource, setFSource] = useState<"" | TradeSource>("");
  const [fGrade, setFGrade] = useState<"" | EvidenceGrade>("");

  const filters: AnalyticsFilters = useMemo(
    () => ({
      ...(fStrategy ? { strategyId: fStrategy } : {}),
      ...(fSymbol ? { symbol: fSymbol } : {}),
      ...(fSource ? { source: fSource } : {}),
      ...(fGrade ? { provenance: fGrade } : {}),
    }),
    [fStrategy, fSymbol, fSource, fGrade]
  );
  const filterKey = JSON.stringify(filters);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      // The summary and the list are the two reads the whole panel depends on;
      // the rest are tab-scoped and failing soft leaves a usable screen.
      const [summaryRes, tradesRes] = await Promise.all([
        getAnalyticsSummary(filters),
        getAttributedTrades({ ...filters, limit: 200 }),
      ]);
      setSummary(summaryRes.data);
      setTrades({ items: tradesRes.data.items ?? [], total: tradesRes.data.total ?? 0 });

      const [strategyRes, regimeRes, contextRes, sizingRes, execRes, maeRes] =
        await Promise.allSettled([
          getAnalyticsByStrategy(filters),
          getAnalyticsByRegime(filters),
          getAnalyticsByContext(filters),
          getAnalyticsBySizing(filters),
          getAnalyticsByExecution(filters),
          getAnalyticsMaeMfe(filters),
        ]);

      if (strategyRes.status === "fulfilled") {
        setStrategies({
          list: strategyRes.value.data.strategies ?? [],
          note: strategyRes.value.data.note ?? "",
        });
      }
      if (regimeRes.status === "fulfilled") setRegime(regimeRes.value.data.buckets ?? []);
      if (contextRes.status === "fulfilled") {
        setContext({
          by_class: contextRes.value.data.by_class ?? [],
          by_score_band: contextRes.value.data.by_score_band ?? [],
        });
      }
      if (sizingRes.status === "fulfilled") {
        setSizing({
          by_method: sizingRes.value.data.by_method ?? [],
          by_cap: sizingRes.value.data.by_cap ?? [],
        });
      }
      if (execRes.status === "fulfilled") {
        setExecution({
          distribution: execRes.value.data.distribution ?? null,
          buckets: {
            by_quality: execRes.value.data.by_quality ?? [],
            by_entry_quality: execRes.value.data.by_entry_quality ?? [],
            by_slippage_bucket: execRes.value.data.by_slippage_bucket ?? [],
          },
        });
      }
      if (maeRes.status === "fulfilled") setMaeMfe(maeRes.value.data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [filterKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void load();
  }, [load]);

  const counts = summary?.counts;
  const chips = filterChips(
    Object.fromEntries(
      Object.entries(filters).map(([k, v]) => [
        k.replace(/([A-Z])/g, "_$1").toLowerCase(),
        v as unknown,
      ])
    )
  );

  const totals = { n: summary?.n_trades ?? 0 };

  return (
    <div className="space-y-4">
      {/* Header: scope first, then the controls. */}
      <Card className="px-5 py-4">
        <CardHeader
          title="Post-trade attribution"
          sub="What actually happened to each closed trade, why, and how well it was executed."
          action={
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setShowFilters((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition hover:text-foreground"
              >
                <Filter size={13} />
                Filters
                {chips.length > 0 ? <Badge tone="info">{chips.length}</Badge> : null}
              </button>
              <button
                type="button"
                onClick={() => void load()}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition hover:text-foreground disabled:opacity-50"
              >
                <RefreshCw size={13} className={busy ? "animate-spin" : ""} />
                Refresh
              </button>
            </div>
          }
        />

        <div className="mt-3 rounded-xl bg-muted/40 px-3.5 py-2.5 text-[12.5px] text-muted-foreground">
          {scopeNote(counts, summary?.attribution_coverage)}
        </div>

        {showFilters ? (
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-4">
            <label className="text-[11.5px]">
              <span className="mb-1 block font-medium uppercase tracking-[0.05em] text-muted-foreground">
                Strategy
              </span>
              <input
                value={fStrategy}
                onChange={(e) => setFStrategy(e.target.value)}
                placeholder="any"
                className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px]"
              />
            </label>
            <label className="text-[11.5px]">
              <span className="mb-1 block font-medium uppercase tracking-[0.05em] text-muted-foreground">
                Symbol
              </span>
              <input
                value={fSymbol}
                onChange={(e) => setFSymbol(e.target.value)}
                placeholder="any"
                className="w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12.5px]"
              />
            </label>
            <label className="text-[11.5px]">
              <span className="mb-1 block font-medium uppercase tracking-[0.05em] text-muted-foreground">
                Source
              </span>
              <Select
                value={fSource}
                onChange={(v) => setFSource(v as "" | TradeSource)}
                options={[
                  { value: "", label: "any" },
                  { value: "LIVE", label: "Live (broker-reported)" },
                  { value: "PAPER", label: "Paper (simulated)" },
                  { value: "BACKTEST", label: "Backtest (simulated)" },
                ]}
                className="w-full"
              />
            </label>
            <label className="text-[11.5px]">
              <span className="mb-1 block font-medium uppercase tracking-[0.05em] text-muted-foreground">
                Evidence grade
              </span>
              <Select
                value={fGrade}
                onChange={(v) => setFGrade(v as "" | EvidenceGrade)}
                options={[
                  { value: "", label: "both grades" },
                  { value: "forward", label: "Forward only" },
                  { value: "in_sample", label: "In-sample only" },
                ]}
                className="w-full"
              />
            </label>
          </div>
        ) : null}

        {chips.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {chips.map((c) => (
              <Badge key={c} tone="flat">
                {c}
              </Badge>
            ))}
          </div>
        ) : null}
      </Card>

      {error ? <ErrorBox>{error}</ErrorBox> : null}

      {summary == null ? (
        <div className="py-10 text-center text-[13px] text-muted-foreground">
          Loading attribution…
        </div>
      ) : (
        <Tabs value={tab} onValueChange={setTab} variant="underline">
          <TabsList>
            <TabsTrigger value="overview">
              <span className="inline-flex items-center gap-1.5">
                <BarChart3 size={14} /> Overview
              </span>
            </TabsTrigger>
            <TabsTrigger value="attribution">
              <span className="inline-flex items-center gap-1.5">
                <Activity size={14} /> Attribution
              </span>
            </TabsTrigger>
            <TabsTrigger value="excursions">
              <span className="inline-flex items-center gap-1.5">
                <Crosshair size={14} /> MAE / MFE
              </span>
            </TabsTrigger>
            <TabsTrigger value="execution">
              <span className="inline-flex items-center gap-1.5">
                <Gauge size={14} /> Execution
              </span>
            </TabsTrigger>
            <TabsTrigger value="context">
              <span className="inline-flex items-center gap-1.5">
                <Target size={14} /> Context &amp; sizing
              </span>
            </TabsTrigger>
            <TabsTrigger value="strategies">
              <span className="inline-flex items-center gap-1.5">
                <BarChart3 size={14} /> Strategies
              </span>
            </TabsTrigger>
            <TabsTrigger value="trades">
              <span className="inline-flex items-center gap-1.5">
                <Info size={14} /> Trades
              </span>
            </TabsTrigger>
          </TabsList>

          <div className="mt-4">
            <TabsContent value="overview">
              <OverviewTab summary={summary} />
            </TabsContent>

            <TabsContent value="attribution">
              <AttributionTab summary={summary} filters={filters} />
            </TabsContent>

            <TabsContent value="excursions">
              <MaeMfeTab data={maeMfe} filters={filters} />
            </TabsContent>

            <TabsContent value="execution">
              <ExecutionTab
                distribution={execution.distribution}
                buckets={execution.buckets}
                total={totals.n}
              />
            </TabsContent>

            <TabsContent value="context">
              <div className="space-y-4">
                <Callout tone="info" title="The recorded context, never a re-score">
                  The context below is the classification the signal engine made at
                  signal time, under the model version recorded with the trade. It
                  is not recomputed here, so this screen cannot silently restate
                  history under a newer model.
                </Callout>
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <BucketTable
                    title="By market regime"
                    sub="The regime recorded at entry."
                    buckets={regime}
                    total={totals.n}
                  />
                  <BucketTable
                    title="By context class"
                    sub="The context engine's classification at signal time."
                    buckets={context?.by_class ?? []}
                    total={totals.n}
                  />
                  <BucketTable
                    title="By context score band"
                    sub="The recorded score, bucketed. Not a probability of profit."
                    buckets={context?.by_score_band ?? []}
                    total={totals.n}
                  />
                  <BucketTable
                    title="By sizing method"
                    sub="Which rule decided the position size."
                    buckets={sizing?.by_method ?? []}
                    total={totals.n}
                  />
                  <BucketTable
                    title="By sizing cap"
                    sub="Whether a risk cap bound the size, and which one."
                    buckets={sizing?.by_cap ?? []}
                    total={totals.n}
                  />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="strategies">
              <StrategiesTab strategies={strategies.list} note={strategies.note} />
            </TabsContent>

            <TabsContent value="trades">
              <TradesTab rows={trades.items} total={trades.total} />
            </TabsContent>
          </div>
        </Tabs>
      )}

      <div className="flex items-start gap-2 rounded-xl border border-border bg-muted/30 px-3.5 py-2.5 text-[11.5px] leading-relaxed text-muted-foreground">
        <AlertTriangle size={13} className="mt-0.5 shrink-0" />
        <div>
          Every figure here is descriptive. Nothing on this screen changes a
          strategy, promotes a version, or places an order; the attribution layer
          is a reader, and it holds no method that acts on what it reads. Indian
          cash equities only — no F&amp;O.
        </div>
      </div>
    </div>
  );
}
