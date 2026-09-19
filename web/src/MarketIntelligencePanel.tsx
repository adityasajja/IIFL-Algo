/**
 * MarketIntelligencePanel — Indian Equity Market Intelligence Dashboard
 *
 * MARKET → SECTOR → STOCK → STRATEGY → TRADE → LEARNING
 *
 * Four sub-panels:
 *   1. Market Overview  — regime badge, NIFTY trend, A/D ratio, breadth, 52w H/L, volatility
 *   2. Sector Rankings  — sortable table of all sectors with RS, breadth, RVOL, breakouts
 *   3. Stock Leaders    — sortable leaderboard: top RS, breakouts, near 52w highs
 *   4. Strategy Context — select strategy + axis → empirical performance per market condition
 *
 * Strictly Indian cash-equity. No F&O.
 */

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  getMarketIntelSummary,
  getMarketIntelSectors,
  getMarketIntelStocks,
  getStrategyMarketContext,
  MarketSummary,
  SectorMetrics,
  StockContext,
  MarketContextPerformance,
  MarketContextBucket,
  SectorSortKey,
  StockSortKey,
  MarketContextAxis,
} from "./api";
import { Select } from "./components/ui/select";
import { PageLoader } from "./components/ui/loading";
import { Ring } from "./components/ui/ring";
import { Sparkline } from "./components/ui/sparkline";
import { Switch } from "./components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { cn } from "./lib/utils";
import { RefreshCw } from "lucide-react";

// ─── Colour / theme helpers ───────────────────────────────────────────────────

/** The regime in one plain word, with the tone it should be shown in. */
const REGIME_PLAIN: Record<string, { word: string; tone: "good" | "bad" | "warn" | "flat" }> = {
  BULLISH_TREND: { word: "Rising", tone: "good" },
  BEARISH_TREND: { word: "Falling", tone: "bad" },
  SIDEWAYS: { word: "Flat", tone: "flat" },
  HIGH_VOLATILITY: { word: "Choppy", tone: "warn" },
  LOW_VOLATILITY: { word: "Calm", tone: "flat" },
};

const trend = (v: number | null | undefined): string => {
  if (v == null) return "var(--text-secondary, #94a3b8)";
  return v > 0 ? "#10b981" : v < 0 ? "#ef4444" : "#94a3b8";
};

// ─── Shared sub-components ───────────────────────────────────────────────────

function CompactErrorNotice({
  msg,
  lastUpdated,
  onRetry,
}: {
  msg?: string;
  lastUpdated?: string;
  onRetry?: () => void;
}) {
  const isNetworkOrThrottle =
    msg &&
    (msg.includes("429") ||
      msg.toLowerCase().includes("busy") ||
      msg.toLowerCase().includes("throttl") ||
      msg.toLowerCase().includes("network"));

  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.5rem",
        fontSize: 11,
        color: "#fca5a5",
        background: "rgba(239, 68, 68, 0.12)",
        border: "1px solid rgba(239, 68, 68, 0.25)",
        borderRadius: 6,
        padding: "0.25rem 0.65rem",
      }}
    >
      <span style={{ fontSize: 12 }}>⚠</span>
      <span>
        {isNetworkOrThrottle || !msg
          ? `Market data delayed${lastUpdated ? ` · Last updated ${lastUpdated}` : ""}`
          : `Service notice: ${msg.slice(0, 45)}`}
      </span>
      {onRetry && (
        <button
          onClick={onRetry}
          style={{
            background: "none",
            border: "none",
            color: "#818cf8",
            textDecoration: "underline",
            cursor: "pointer",
            fontSize: 11,
            fontWeight: 600,
            padding: 0,
            marginLeft: "0.15rem",
          }}
        >
          Retry
        </button>
      )}
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

// ─── Sub-panel 1: Market Overview ────────────────────────────────────────────

function MarketOverviewPanel() {
  const [data, setData] = useState<MarketSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  // A ref, not `data`, decides whether to show the full-page loader. Reading
  // `data` here made `load` change on every response, and the effect below
  // re-runs whenever `load` changes: an endless fetch loop hammering the backend.
  const hasData = useRef(false);
  const load = useCallback((refresh = false) => {
    if (refresh) {
      setRefreshing(true);
    } else if (!hasData.current) {
      setLoading(true);
    }
    setError(null);
    getMarketIntelSummary(refresh)
      .then((r) => {
        hasData.current = true;
        setData(r.data);
        const now = new Date();
        setLastUpdated(now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      })
      .catch((e) => setError(String(e)))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !data) return <PageLoader label="Loading market data" />;
  if (error && !data) return <div style={{ padding: "1rem" }}><CompactErrorNotice msg={error} onRetry={() => load(true)} /></div>;
  if (!data) return null;

  const isBullish = data.regime.regime === "BULLISH_TREND";
  const isBearish = data.regime.regime === "BEARISH_TREND";
  const breadth = data.breadth_above_ema50_pct;
  const atr = data.market_volatility_atr_pct;
  const secPart = data.sector_participation_pct;
  const contextScore = Math.round(data.market_trend_strength);

  // How well each style of trading suits today's market, from the same rules as before.
  const momentumLong = isBullish && breadth >= 50 ? "STRONG" : !isBearish && breadth >= 45 ? "NEUTRAL" : "WEAK";
  const breakoutLong = isBullish && secPart >= 50 && atr < 1.8 ? "STRONG" : !isBearish && secPart >= 40 ? "NEUTRAL" : "WEAK";
  const meanReversion = isBearish || breadth < 35 || atr > 1.3 ? "STRONG" : "NEUTRAL";
  const defensive = isBearish || breadth < 40 ? "STRONG" : "NEUTRAL";

  const regime = REGIME_PLAIN[data.regime.regime] ?? {
    word: data.regime.label ?? data.regime.regime.replace(/_/g, " "),
    tone: "flat" as const,
  };
  const tone = {
    good: "bg-emerald-500/10 text-emerald-500",
    bad: "bg-rose-500/10 text-rose-500",
    warn: "bg-amber-500/10 text-amber-500",
    flat: "bg-muted text-muted-foreground",
  };
  const fit = (v: string) =>
    v === "STRONG" ? { word: "Good fit", cls: tone.good } : v === "NEUTRAL" ? { word: "Okay", cls: tone.flat } : { word: "Poor fit", cls: tone.bad };
  const barTone = (pct: number) => (pct >= 55 ? "bg-emerald-500" : pct >= 40 ? "bg-amber-500" : "bg-rose-500");
  const changeCls = (v: number | null | undefined) => (v == null ? "text-muted-foreground" : v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");
  const signed = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);

  const up = data.advancing_stocks;
  const down = data.declining_stocks;
  const flat = data.unchanged_stocks;
  const total = Math.max(up + down + flat, 1);
  const scoreTone = contextScore >= 65 ? "stroke-emerald-500" : contextScore <= 35 ? "stroke-rose-500" : "stroke-amber-500";
  const scoreWord = contextScore >= 65 ? "Favourable" : contextScore <= 35 ? "Defensive" : "Neutral";
  const volWord = atr >= 1.5 ? "Choppy" : atr <= 0.9 ? "Calm" : "Normal";

  const series = data.benchmark_series ?? [];
  const history = data.breadth_history ?? [];
  const lastPoint = series[series.length - 1];
  const gap = (avg: number | null | undefined) =>
    lastPoint && avg ? ((lastPoint.c / avg - 1) * 100) : null;
  const vs50 = gap(lastPoint?.s50);
  const vs200 = gap(lastPoint?.s200);
  // ~22 trading days back is a month; the history is 90 sessions, so index from the end.
  const monthIdx = history.length - 22;
  const monthAgo = monthIdx >= 0 ? history[monthIdx].pct : null;
  const extreme = (() => {
    if (history.length < 40) return null;
    const cur = history[history.length - 1].pct;
    let lower = 0;
    for (let i = history.length - 2; i >= 0 && history[i].pct > cur; i--) lower++;
    let higher = 0;
    for (let i = history.length - 2; i >= 0 && history[i].pct < cur; i--) higher++;
    if (lower >= 20) return `Lowest in ${lower + 1} sessions`;
    if (higher >= 20) return `Highest in ${higher + 1} sessions`;
    return null;
  })();
  const last5 = (
    series.length >= 6
      ? series.slice(-6).map((p, i, a) => (i === 0 ? null : { d: p.d, pct: (p.c / a[i - 1].c - 1) * 100 }))
      : []
  ).filter((x): x is { d: string; pct: number } => x !== null);
  const down5 = last5.filter((x) => x.pct < 0).length;
  const maxMove = Math.max(...last5.map((x) => Math.abs(x.pct)), 0.01);
  const hi52 = data.nifty_52w_high;
  const lo52 = data.nifty_52w_low;
  const rangePos =
    hi52 && lo52 && hi52 > lo52 && lastPoint
      ? Math.min(Math.max((lastPoint.c - lo52) / (hi52 - lo52), 0), 1)
      : null;
  const belowHigh = hi52 && lastPoint ? (lastPoint.c / hi52 - 1) * 100 : null;
  const breadthMove = monthAgo === null ? null : breadth - monthAgo;
  const ratio = Math.max(up, down) / Math.max(Math.min(up, down), 1);
  const dateLabel = (iso: string) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

  // One line that says what the numbers add up to.
  const takeaway = `${breadth.toFixed(0)}% of stocks are above their 50-day average${
    breadthMove === null || Math.abs(breadthMove) < 5
      ? ""
      : breadthMove > 0
        ? `, up from ${monthAgo!.toFixed(0)}% a month ago`
        : `, down from ${monthAgo!.toFixed(0)}% a month ago`
  }. ${up === down ? "Buyers and sellers are even" : `${up > down ? "Buyers" : "Sellers"} outnumber ${up > down ? "sellers" : "buyers"} ${ratio.toFixed(1)} to 1`}.`;

  const leaders = (data.top_relative_strength_stocks ?? []).slice(0, 5) as Record<string, unknown>[];
  const breakouts = (data.top_breakouts ?? []).slice(0, 5) as Record<string, unknown>[];
  const stockDataBehind =
    !!data.stocks_as_of && !!lastPoint && data.stocks_as_of < lastPoint.d;

  return (
    <div className="space-y-4">
      {/* Verdict + the index, in plain words. */}
      <div className="flex flex-wrap items-center justify-between gap-6 rounded-2xl border border-border bg-card p-5">
        <div>
          <div className="text-xs text-muted-foreground">Market direction</div>
          <div className="mt-2 flex items-center gap-3">
            <span className={cn("rounded-full px-3 py-1 text-2xl font-semibold tracking-tight", tone[regime.tone])}>
              {regime.word}
            </span>
          </div>
          <p className="mt-3 max-w-md text-sm text-muted-foreground">{takeaway}</p>
        </div>
        <div className="flex items-center gap-6">
          <div className="text-right">
            <div className="text-xs text-muted-foreground">
              {data.benchmark_provenance.is_proxy ? (
                <span title="Nifty 50 ETF. It follows the index closely, so the % moves match, but the price is per ETF unit, not the index level.">
                  Nifty 50 ETF
                </span>
              ) : (
                "Nifty 50"
              )}
            </div>
            <div className="mt-1 text-3xl font-semibold tracking-tight tabular-nums">
              {data.nifty_close != null ? `₹${data.nifty_close.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}
            </div>
            <div className="mt-1 flex justify-end gap-3 text-xs tabular-nums">
              <span className={changeCls(data.nifty_change_1d_pct)}>{signed(data.nifty_change_1d_pct)} today</span>
              <span className={changeCls(data.nifty_1m_return_pct)}>{signed(data.nifty_1m_return_pct)} this month</span>
            </div>
          </div>
          <div className="flex flex-col items-end gap-1.5">
            {error && <CompactErrorNotice msg={error} lastUpdated={lastUpdated ?? undefined} onRetry={() => load(true)} />}
            <button
              id="market-intel-refresh"
              onClick={() => load(true)}
              disabled={refreshing}
              title={lastUpdated ? `Updated ${lastUpdated}` : "Refresh"}
              aria-label="Refresh"
              className="grid size-8 place-items-center rounded-full border border-border text-muted-foreground transition-colors hover:text-foreground"
            >
              <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />
            </button>
          </div>
        </div>
      </div>

      {series.length > 1 ? (
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="text-xs text-muted-foreground">Nifty 50, last 6 months</div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums">
              {vs50 !== null ? (
                <span className={vs50 >= 0 ? "text-emerald-500" : "text-rose-500"}>
                  {Math.abs(vs50).toFixed(1)}% {vs50 >= 0 ? "above" : "below"} 50-day average
                </span>
              ) : null}
              {vs200 !== null ? (
                <span className={vs200 >= 0 ? "text-emerald-500" : "text-rose-500"}>
                  {Math.abs(vs200).toFixed(1)}% {vs200 >= 0 ? "above" : "below"} 200-day average
                </span>
              ) : null}
            </div>
          </div>
          <TrendChart series={series} dateLabel={dateLabel} />
          {last5.length || rangePos !== null ? (
            <div className="mt-5 grid gap-6 border-t border-border pt-4 sm:grid-cols-2">
              {last5.length ? (
                <div>
                  <div className="flex items-baseline justify-between text-xs text-muted-foreground">
                    <span>Last 5 days</span>
                    <span>
                      {down5 === 0 ? "Up every day" : down5 === last5.length ? "Down every day" : `Down ${down5} of ${last5.length}`}
                    </span>
                  </div>
                  <div className="mt-3 flex h-12 items-end gap-2">
                    {last5.map((x) => (
                      <div
                        key={x.d}
                        title={`${dateLabel(x.d)}: ${signed(x.pct)}`}
                        className={cn("flex-1 rounded-sm", x.pct >= 0 ? "bg-emerald-500" : "bg-rose-500")}
                        style={{ height: `${Math.max((Math.abs(x.pct) / maxMove) * 100, 8)}%` }}
                      />
                    ))}
                  </div>
                  <div className="mt-1.5 flex gap-2 text-[10px] text-muted-foreground">
                    {last5.map((x) => (
                      <span key={x.d} className="flex-1 text-center">
                        {new Date(`${x.d}T00:00:00`).toLocaleDateString("en-IN", { weekday: "short" })}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              {rangePos !== null && belowHigh !== null ? (
                <div>
                  <div className="flex items-baseline justify-between text-xs text-muted-foreground">
                    <span>52-week range</span>
                    <span>{belowHigh >= -0.05 ? "At its yearly high" : `${Math.abs(belowHigh).toFixed(1)}% below yearly high`}</span>
                  </div>
                  <div className="relative mt-6 h-2 rounded-full bg-gradient-to-r from-rose-500/40 via-amber-500/40 to-emerald-500/40">
                    <div
                      className="absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
                      style={{ left: `${rangePos * 100}%` }}
                    />
                  </div>
                  <div className="mt-2 flex justify-between text-[11px] tabular-nums text-muted-foreground">
                    <span>{lo52!.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                    <span>{hi52!.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* Rising vs falling, as one bar. */}
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="text-xs text-muted-foreground">Stocks today</div>
          <div className="mt-3 flex h-3 overflow-hidden rounded-full bg-muted">
            <div className="bg-emerald-500" style={{ width: `${(up / total) * 100}%` }} title={`${up} rising`} />
            <div className="bg-muted-foreground/30" style={{ width: `${(flat / total) * 100}%` }} title={`${flat} unchanged`} />
            <div className="bg-rose-500" style={{ width: `${(down / total) * 100}%` }} title={`${down} falling`} />
          </div>
          <div className="mt-2 flex justify-between text-sm tabular-nums">
            <span className="text-emerald-500">{up} rising</span>
            <span className="text-rose-500">{down} falling</span>
          </div>

          <div className="mt-5 text-xs text-muted-foreground">Above their average price</div>
          <div className="mt-3 space-y-3">
            {[
              { label: "20-day", value: data.breadth_above_ema20_pct },
              { label: "50-day", value: data.breadth_above_ema50_pct },
              { label: "200-day", value: data.breadth_above_sma200_pct },
            ].map((b) => (
              <div key={b.label} className="flex items-center gap-3 text-sm">
                <span className="w-16 shrink-0 whitespace-nowrap text-muted-foreground">{b.label}</span>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                  <div className={cn("h-full rounded-full transition-all duration-500", barTone(b.value))} style={{ width: `${Math.min(Math.max(b.value, 0), 100)}%` }} />
                </div>
                <span className="w-12 shrink-0 text-right tabular-nums">{b.value.toFixed(0)}%</span>
              </div>
            ))}
          </div>
          {history.length > 2 ? (
            <div className="mt-5 flex items-center justify-between gap-4">
              <div>
                <div className="text-xs text-muted-foreground">Above 50-day average, over 30 days</div>
                {breadthMove !== null ? (
                  <div className={cn("mt-1 text-sm tabular-nums", breadthMove >= 0 ? "text-emerald-500" : "text-rose-500")}>
                    {breadthMove >= 0 ? "Up" : "Down"} {Math.abs(breadthMove).toFixed(0)} points
                  </div>
                ) : null}
                {extreme ? <div className="mt-0.5 text-[11px] text-muted-foreground">{extreme}</div> : null}
              </div>
              <Sparkline data={history.slice(-30).map((h) => h.pct)} width={140} height={40} ariaLabel="Share of stocks above their 50-day average" />
            </div>
          ) : null}
          <div className="mt-4 text-[11px] text-muted-foreground" title={data.survivorship_safeguard}>
            Based on {data.total_stocks_analyzed} stocks
            {stockDataBehind ? (
              <span className="text-amber-500"> · stock data to {dateLabel(data.stocks_as_of!)}, index to {dateLabel(lastPoint.d)}</span>
            ) : null}
          </div>
        </div>

        <div className="grid content-start gap-4">
          {/* One number, one word. */}
          <div className="flex items-center gap-5 rounded-2xl border border-border bg-card p-5">
            <Ring value={contextScore} size={88} toneClass={scoreTone}>
              <span className="text-xl font-semibold tabular-nums">{contextScore}</span>
            </Ring>
            <div>
              <div className="text-xs text-muted-foreground">Market strength</div>
              <div className="mt-1 text-xl font-semibold tracking-tight">{scoreWord}</div>
              <div className="text-xs text-muted-foreground">out of 100</div>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-4">
            <div className="rounded-2xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Yearly high / low</div>
              <div className="mt-2 flex items-baseline gap-2 tabular-nums">
                <span className="text-xl font-semibold text-emerald-500">{data.highs_52w_count}</span>
                <span className="text-muted-foreground">/</span>
                <span className="text-xl font-semibold text-rose-500">{data.lows_52w_count}</span>
              </div>
            </div>
            <div className="rounded-2xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Swings</div>
              <div className="mt-2 text-xl font-semibold">{volWord}</div>
            </div>
            <div className="rounded-2xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Sectors rising</div>
              <div className="mt-2 text-xl font-semibold tabular-nums">{secPart.toFixed(0)}%</div>
            </div>
          </div>
        </div>
      </div>

      {/* Which approaches suit today, as chips rather than two tables. */}
      <div className="rounded-2xl border border-border bg-card p-5">
        <div className="text-xs text-muted-foreground">What suits this market</div>
        <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[
            { label: "Riding trends", v: momentumLong },
            { label: "Breakouts", v: breakoutLong },
            { label: "Buying dips", v: meanReversion },
            { label: "Playing safe", v: defensive },
          ].map((s) => {
            const f = fit(s.v);
            return (
              <div key={s.label} className="flex items-center justify-between gap-2 rounded-xl border border-border px-3.5 py-2.5">
                <span className="text-sm">{s.label}</span>
                <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", f.cls)}>{f.word}</span>
              </div>
            );
          })}
        </div>
      </div>

      {data.strongest_sectors?.length || data.weakest_sectors?.length || data.sectors_turning_up?.length || data.sectors_fading?.length ? (
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="text-xs text-muted-foreground">Sectors</div>
          <div className="mt-3 space-y-3">
            {[
              { label: "Leading", items: data.strongest_sectors, cls: tone.good },
              { label: "Lagging", items: data.weakest_sectors, cls: tone.bad },
              { label: "Turning up", items: data.sectors_turning_up, cls: "bg-emerald-500/5 text-emerald-500 ring-1 ring-emerald-500/30" },
              { label: "Fading", items: data.sectors_fading, cls: "bg-amber-500/5 text-amber-500 ring-1 ring-amber-500/30" },
            ].map((row) =>
              row.items?.length ? (
                <div key={row.label} className="flex flex-wrap items-center gap-2">
                  <span className="w-20 shrink-0 text-sm text-muted-foreground">{row.label}</span>
                  {row.items.map((name) => (
                    <span key={name} className={cn("rounded-full px-2.5 py-1 text-xs font-medium", row.cls)}>
                      {name}
                    </span>
                  ))}
                </div>
              ) : null,
            )}
          </div>
        </div>
      ) : null}

      {leaders.length || breakouts.length ? (
        <div className="grid gap-4 lg:grid-cols-2">
          {[
            {
              title: "Strongest stocks",
              hint: "vs Nifty, 20 days",
              rows: leaders.map((r) => ({
                symbol: String(r.symbol ?? "").replace("-EQ", ""),
                value: `${Number(r.relative_strength_nifty_20d) > 0 ? "+" : ""}${Number(r.relative_strength_nifty_20d).toFixed(0)}%`,
                good: Number(r.relative_strength_nifty_20d) >= 0,
              })),
            },
            {
              title: "Breakouts",
              hint: "trading volume vs normal",
              rows: breakouts.map((r) => ({
                symbol: String(r.symbol ?? "").replace("-EQ", ""),
                value: `${Number(r.relative_volume).toFixed(1)}x`,
                good: true,
              })),
            },
          ].map((card) => (
            <div key={card.title} className="rounded-2xl border border-border bg-card p-5">
              <div className="flex items-baseline justify-between">
                <span className="text-xs text-muted-foreground">{card.title}</span>
                <span className="text-[11px] text-muted-foreground/70">{card.hint}</span>
              </div>
              {card.rows.length ? (
                <div className="mt-3 divide-y divide-border">
                  {card.rows.map((r) => (
                    <div key={r.symbol} className="flex items-center justify-between py-2 text-sm">
                      <span className="font-medium">{r.symbol}</span>
                      <span className={cn("tabular-nums", r.good ? "text-emerald-500" : "text-rose-500")}>{r.value}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-3 text-sm text-muted-foreground">None today</div>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ─── The Nifty line with its 50 and 200-day averages, and a hover read-out.
function TrendChart({
  series,
  dateLabel,
}: {
  series: { d: string; c: number; s50: number | null; s200: number | null }[];
  dateLabel: (iso: string) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640;
  const H = 180;
  const pad = 10;
  const values = series.flatMap((p) => [p.c, p.s50, p.s200]).filter((v): v is number => v !== null);
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const x = (i: number) => (i / (series.length - 1)) * W;
  const y = (v: number) => pad + (H - pad * 2) - ((v - min) / span) * (H - pad * 2);
  const line = (key: "c" | "s50" | "s200") => {
    let d = "";
    series.forEach((p, i) => {
      const v = p[key];
      if (v !== null) d += `${d ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    });
    return d;
  };
  const point = hover !== null ? series[hover] : null;

  return (
    <div className="mt-4">
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="h-44 w-full cursor-crosshair"
          onMouseMove={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            setHover(Math.min(series.length - 1, Math.max(0, Math.round(((e.clientX - r.left) / r.width) * (series.length - 1)))));
          }}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <linearGradient id="nifty-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.22" />
              <stop offset="100%" stopColor="var(--primary)" stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={`${line("c")}L${W},${H} L0,${H} Z`} fill="url(#nifty-fill)" />
          <path d={line("s200")} fill="none" stroke="var(--muted-foreground)" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" opacity="0.7" />
          <path d={line("s50")} fill="none" stroke="#f59e0b" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
          <path d={line("c")} fill="none" stroke="var(--primary)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
          {hover !== null ? (
            <line x1={x(hover)} x2={x(hover)} y1={0} y2={H} stroke="var(--muted-foreground)" strokeWidth="1" vectorEffect="non-scaling-stroke" opacity="0.5" />
          ) : null}
        </svg>
        {point ? (
          <div
            className="pointer-events-none absolute top-1 -translate-x-1/2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs shadow-lg"
            style={{ left: `${Math.min(88, Math.max(12, (hover! / (series.length - 1)) * 100))}%` }}
          >
            <div className="text-muted-foreground">{dateLabel(point.d)}</div>
            <div className="font-medium tabular-nums">{point.c.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
          </div>
        ) : null}
      </div>
      <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{dateLabel(series[0].d)}</span>
        <span className="flex items-center gap-4">
          <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-3 bg-primary" /> Nifty</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-3 bg-amber-500" /> 50-day</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-3 bg-muted-foreground" /> 200-day</span>
        </span>
        <span>{dateLabel(series[series.length - 1].d)}</span>
      </div>
    </div>
  );
}

// ─── Sub-panel 2: Sector Rankings ────────────────────────────────────────────

const SECTOR_PERIODS = {
  "1d": { key: "return_1d_pct" as SectorSortKey, label: "Today", pick: (s: SectorMetrics) => s.return_1d_pct },
  "1w": { key: "return_1w_pct" as SectorSortKey, label: "This week", pick: (s: SectorMetrics) => s.return_1w_pct },
  "1m": { key: "return_1m_pct" as SectorSortKey, label: "This month", pick: (s: SectorMetrics) => s.return_1m_pct },
};
type SectorPeriod = keyof typeof SECTOR_PERIODS;

function SectorRankingsPanel() {
  const [period, setPeriod] = useState<SectorPeriod>("1m");
  const [sectors, setSectors] = useState<SectorMetrics[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Seq guards stale responses; a ref (not `.length`) keeps `load` stable so a
  // first response does not trigger a second identical fetch.
  const seq = useRef(0);
  const loaded = useRef(false);
  const load = useCallback(() => {
    const mine = ++seq.current;
    if (!loaded.current) setLoading(true);
    setError(null);
    getMarketIntelSectors(SECTOR_PERIODS[period].key, true)
      .then((r) => {
        if (mine !== seq.current) return;
        loaded.current = r.sectors.length > 0;
        setSectors(r.sectors);
      })
      .catch((e) => mine === seq.current && setError(String(e)))
      .finally(() => mine === seq.current && setLoading(false));
  }, [period]);

  useEffect(() => { load(); }, [load]);

  if (loading && sectors.length === 0) return <PageLoader label="Loading sectors" />;
  if (error && sectors.length === 0) return <div style={{ padding: "1rem" }}><CompactErrorNotice msg={error} onRetry={() => load()} /></div>;

  const value = SECTOR_PERIODS[period].pick;
  const maxAbs = Math.max(...sectors.map((s) => Math.abs(value(s))), 0.01);
  const barTone = (pct: number) => (pct >= 55 ? "bg-emerald-500" : pct >= 40 ? "bg-amber-500" : "bg-rose-500");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={period} onValueChange={(v) => setPeriod(v as SectorPeriod)} variant="segment">
          <TabsList>
            {(Object.keys(SECTOR_PERIODS) as SectorPeriod[]).map((p) => (
              <TabsTrigger key={p} value={p}>{SECTOR_PERIODS[p].label}</TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        {error ? <CompactErrorNotice msg={error} onRetry={() => load()} /> : null}
      </div>

      <div className="overflow-hidden rounded-2xl border border-border bg-card">
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_84px] gap-4 border-b border-border px-5 py-3 text-xs text-muted-foreground">
          <span>Sector</span>
          <span>Return {SECTOR_PERIODS[period].label.toLowerCase()}</span>
          <span title="Share of the sector's stocks above their 50-day average" className="text-right">Healthy</span>
        </div>
        <div className="divide-y divide-border">
          {sectors.map((s) => {
            const v = value(s);
            const width = (Math.abs(v) / maxAbs) * 50;
            return (
              <div key={s.sector} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_84px] items-center gap-4 px-5 py-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{s.sector}</div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {s.stock_count} stocks
                    <span className="ml-2 inline-flex gap-0.5 align-middle" title="Today, this week, this month">
                      {[s.return_1d_pct, s.return_1w_pct, s.return_1m_pct].map((v, i) => (
                        <span key={i} className={cn("size-1.5 rounded-full", v > 0 ? "bg-emerald-500" : v < 0 ? "bg-rose-500" : "bg-muted-foreground/40")} />
                      ))}
                    </span>
                    {s.stock_count >= 5 && s.return_1w_pct > 0 && s.return_1m_pct < 0 ? (
                      <span className="ml-2 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-emerald-500">Turning up</span>
                    ) : null}
                    {s.stock_count >= 5 && s.return_1w_pct < 0 && s.return_1m_pct > 0 ? (
                      <span className="ml-2 rounded-full bg-amber-500/10 px-1.5 py-0.5 text-amber-500">Fading</span>
                    ) : null}
                    {s.breakout_count > 0 ? (
                      <span className="ml-2 rounded-full bg-primary/10 px-1.5 py-0.5 text-primary">
                        {s.breakout_count} {s.breakout_count === 1 ? "breakout" : "breakouts"}
                      </span>
                    ) : null}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <div className="relative h-2.5 flex-1 rounded-full bg-muted/60">
                    <div className="absolute inset-y-0 left-1/2 w-px bg-border" />
                    <div
                      className={cn("absolute inset-y-0 rounded-full transition-all duration-500", v >= 0 ? "bg-emerald-500" : "bg-rose-500")}
                      style={v >= 0 ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }}
                    />
                  </div>
                  <span className={cn("w-16 shrink-0 text-right text-sm tabular-nums", v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground")}>
                    {v > 0 ? "+" : ""}{v.toFixed(2)}%
                  </span>
                </div>
                <div className="flex items-center justify-end gap-2" title={`${s.above_ema50_count} of ${s.stock_count} stocks above their 50-day average`}>
                  <div className="h-1.5 w-10 overflow-hidden rounded-full bg-muted">
                    <div className={cn("h-full rounded-full", barTone(s.above_ema50_pct))} style={{ width: `${Math.min(Math.max(s.above_ema50_pct, 0), 100)}%` }} />
                  </div>
                  <span className="w-9 text-right text-xs tabular-nums text-muted-foreground">{s.above_ema50_pct.toFixed(0)}%</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ─── Sub-panel 3: Stock Leaders ───────────────────────────────────────────────

const STOCK_SORT_OPTIONS: { key: StockSortKey; label: string }[] = [
  { key: "relative_strength_nifty_20d", label: "Beating the Nifty" },
  { key: "relative_volume", label: "Unusual volume" },
  { key: "from_52w_high_pct", label: "Near yearly high" },
  { key: "change_1d_pct", label: "Today's movers" },
  { key: "trend_pct", label: "Trending up" },
  { key: "atr_pct", label: "Big swings" },
];

function StockLeadersPanel() {
  const [sortKey, setSortKey] = useState<StockSortKey>("relative_strength_nifty_20d");
  const [breakoutsOnly, setBreakoutsOnly] = useState(false);
  const [stocks, setStocks] = useState<StockContext[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);

  const seq = useRef(0);
  const loaded = useRef(false);
  const load = useCallback(() => {
    const mine = ++seq.current;
    if (!loaded.current) setLoading(true);
    setError(null);
    const descending = sortKey !== "from_52w_high_pct"; // proximity to 52w high → ascending means closest
    getMarketIntelStocks({ sortBy: sortKey, descending, breakoutsOnly, limit })
      .then((r) => {
        if (mine !== seq.current) return;
        loaded.current = r.stocks.length > 0;
        setStocks(r.stocks);
      })
      .catch((e) => mine === seq.current && setError(String(e)))
      .finally(() => mine === seq.current && setLoading(false));
  }, [sortKey, breakoutsOnly, limit]);

  useEffect(() => { load(); }, [load]);

  if (loading && stocks.length === 0) return <PageLoader label="Loading stocks" />;
  if (error && stocks.length === 0) return <div style={{ padding: "1rem" }}><CompactErrorNotice msg={error} onRetry={() => load()} /></div>;

  const signedCls = (v: number) => (v > 0 ? "text-emerald-500" : v < 0 ? "text-rose-500" : "text-muted-foreground");
  const pct = (v: number, digits = 1) => `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;

  // The three facts every row shows, plus the one you sorted by if it is not among them.
  const facts = (s: StockContext) => {
    const all: { key: StockSortKey; label: string; text: string; cls: string }[] = [
      { key: "relative_strength_nifty_20d", label: "vs Nifty", text: pct(s.relative_strength_nifty_20d, 0), cls: signedCls(s.relative_strength_nifty_20d) },
      { key: "relative_volume", label: "Volume", text: `${s.relative_volume.toFixed(1)}x`, cls: s.relative_volume >= 1.5 ? "text-primary" : "" },
      { key: "from_52w_high_pct", label: "Yearly high", text: pct(s.from_52w_high_pct, 1), cls: s.from_52w_high_pct > -3 ? "text-amber-500" : "" },
      { key: "trend_pct", label: "Trend", text: pct(s.trend_pct, 1), cls: signedCls(s.trend_pct) },
      { key: "atr_pct", label: "Swings", text: `${s.atr_pct.toFixed(1)}%`, cls: "" },
    ];
    const base: StockSortKey[] = ["relative_strength_nifty_20d", "relative_volume", "from_52w_high_pct"];
    return all.filter((f) => base.includes(f.key) || f.key === sortKey);
  };

  const bySector = (() => {
    const counts = new Map<string, number>();
    for (const s of stocks) counts.set(s.sector ?? "Other", (counts.get(s.sector ?? "Other") ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4);
  })();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2">
          {STOCK_SORT_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              id={`stock-sort-${opt.key}`}
              type="button"
              onClick={() => setSortKey(opt.key)}
              aria-pressed={sortKey === opt.key}
              className={cn(
                "rounded-full border px-3 py-1 text-xs transition-colors",
                sortKey === opt.key
                  ? "border-primary/40 bg-primary/10 text-foreground"
                  : "border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <Switch checked={breakoutsOnly} onCheckedChange={setBreakoutsOnly} label="Breakouts only" />
          <Select
            size="sm"
            className="w-24"
            value={String(limit)}
            onChange={(v) => setLimit(Number(v))}
            options={[25, 50, 100].map((v) => ({ value: String(v), label: `Top ${v}` }))}
          />
        </div>
      </div>

      {stocks.length >= 10 && bySector.length ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">Mostly from</span>
          {bySector.map(([name, n]) => (
            <span key={name} className="rounded-full bg-muted px-2.5 py-1">
              {name} <span className="text-muted-foreground">{n}</span>
            </span>
          ))}
        </div>
      ) : null}

      {stocks.length === 0 ? (
        <div className="rounded-2xl border border-border bg-card px-5 py-10 text-center text-sm text-muted-foreground">
          Nothing matches right now.
        </div>
      ) : (
        <div className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card">
          {stocks.map((s, i) => (
            <div key={s.symbol} className="flex items-start gap-3 px-5 py-3">
              <span className="w-6 pt-0.5 text-xs tabular-nums text-muted-foreground">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="font-semibold">{s.symbol.replace("-EQ", "")}</span>
                  {s.is_breakout ? (
                    <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-500">Breakout</span>
                  ) : null}
                  <span className="truncate text-xs text-muted-foreground">{s.sector ?? ""}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
                  {facts(s).map((f) => (
                    <span key={f.key} className={cn("whitespace-nowrap", f.key === sortKey ? "font-medium text-foreground" : "text-muted-foreground")}>
                      {f.label} <span className={cn("tabular-nums", f.cls)}>{f.text}</span>
                    </span>
                  ))}
                </div>
              </div>
              <div className="text-right">
                <div className="font-medium tabular-nums">
                  {s.close != null ? s.close.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}
                </div>
                <div className={cn("text-xs tabular-nums", signedCls(s.change_1d_pct))}>{pct(s.change_1d_pct, 2)}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Sub-panel 4: Strategy Market-Context Analytics ──────────────────────────

const AXIS_OPTIONS: { key: MarketContextAxis; label: string; description: string }[] = [
  {
    key: "market_regime",
    label: "Market Regime",
    description: "Bullish trend, bearish trend, sideways, high volatility or low volatility",
  },
  {
    key: "nifty_trend_bucket",
    label: "NIFTY Trend",
    description: "Uptrend, sideways or downtrend (vs SMA50)",
  },
  {
    key: "breadth_bucket",
    label: "Market Breadth",
    description: "High, normal or low breadth (% of stocks above EMA50)",
  },
  {
    key: "sector_strength_bucket",
    label: "Sector Strength",
    description: "Strong, mild outperformer, mild laggard or weak sector (relative strength vs NIFTY)",
  },
  {
    key: "stock_rs_bucket",
    label: "Stock RS vs NIFTY",
    description: "Strongly outperforming, outperforming, underperforming or strongly underperforming NIFTY",
  },
  {
    key: "volatility_regime",
    label: "Volatility Regime",
    description: "Low, normal or high (based on ATR %)",
  },
];

function EvidencePip({ note }: { note: MarketContextBucket["evidence_note"] }) {
  const colour =
    note === "forward"
      ? "#10b981"
      : note === "insufficient_forward_observations"
      ? "#eab308"
      : "#64748b";
  const label =
    note === "forward"
      ? "Forward"
      : note === "insufficient_forward_observations"
      ? "Thin"
      : "In-sample";
  return (
    <span
      style={{
        background: `${colour}20`,
        color: colour,
        borderRadius: 4,
        padding: "0.1rem 0.4rem",
        fontSize: 10,
        fontWeight: 700,
      }}
    >
      {label}
    </span>
  );
}

function StrategyContextPanel() {
  const [axis, setAxis] = useState<MarketContextAxis>("market_regime");
  const [strategy, setStrategy] = useState("");
  const [result, setResult] = useState<MarketContextPerformance | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(() => {
    setLoading(true);
    setError(null);
    getStrategyMarketContext({
      conditionAxis: axis,
      strategy: strategy.trim() || undefined,
    })
      .then((r) => setResult(r))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [axis, strategy]);

  const axisDesc = AXIS_OPTIONS.find((o) => o.key === axis)?.description ?? "";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {/* Controls */}
      <Card>
        <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 10, color: "#64748b", fontWeight: 600 }}>STRATEGY (optional)</label>
            <input
              id="strategy-context-strategy"
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              placeholder="e.g. MOMENTUM_BREAKOUT"
              style={{
                background: "rgba(15,23,42,0.5)",
                border: "1px solid rgba(99,102,241,0.2)",
                borderRadius: 6,
                color: "#e2e8f0",
                padding: "0.4rem 0.6rem",
                fontSize: 12,
                width: 200,
              }}
            />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 10, color: "#64748b", fontWeight: 600 }}>CONDITION AXIS</label>
            <Select
              size="sm"
              className="w-[220px]"
              value={axis}
              onChange={(v) => setAxis(v as MarketContextAxis)}
              options={AXIS_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
            />
          </div>

          <button
            id="strategy-context-run"
            onClick={run}
            disabled={loading}
            style={{
              background: "rgba(99,102,241,0.2)",
              border: "1px solid rgba(99,102,241,0.4)",
              borderRadius: 8,
              color: "#818cf8",
              padding: "0.5rem 1rem",
              fontSize: 13,
              fontWeight: 700,
              cursor: loading ? "wait" : "pointer",
            }}
          >
            {loading ? "Analyzing…" : "Analyze"}
          </button>
        </div>
        <div style={{ marginTop: 6, fontSize: 11, color: "#475569" }}>{axisDesc}</div>
      </Card>

      {error && <div style={{ padding: "0.5rem 0" }}><CompactErrorNotice msg={error} onRetry={() => run()} /></div>}

      {result && (
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
          {/* Baseline */}
          <Card>
            <div style={{ display: "flex", gap: "2rem", flexWrap: "wrap" }}>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>STRATEGY</div>
                <div style={{ fontWeight: 700, color: "#818cf8" }}>{result.strategy}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>AXIS</div>
                <div style={{ fontWeight: 600, color: "#e2e8f0" }}>{result.condition_axis}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>METRIC</div>
                <div style={{ fontWeight: 600, color: "#e2e8f0" }}>{result.metric}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>TOTAL ROWS</div>
                <div style={{ fontWeight: 600, color: "#94a3b8" }}>{result.total_rows_scanned}</div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>BASELINE WIN RATE</div>
                <div style={{ fontWeight: 600, color: "#94a3b8" }}>
                  {result.baseline.win_rate != null ? `${result.baseline.win_rate.toFixed(1)}%` : "—"}
                </div>
              </div>
              <div>
                <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>BASELINE MEAN</div>
                <div style={{ fontWeight: 600, color: trend(result.baseline.mean) }}>
                  {result.baseline.mean != null ? result.baseline.mean.toFixed(2) : "—"}
                </div>
              </div>
            </div>
            {result.caveats.length > 0 && (
              <div style={{ marginTop: "0.5rem", fontSize: 11, color: "#eab308" }}>
                ⚠ {result.caveats.join(" · ")}
              </div>
            )}
          </Card>

          {/* Bucket table */}
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid rgba(99,102,241,0.15)" }}>
                  {["Condition", "Evidence", "N", "Fwd", "Mean", "Median", "Win Rate", "Profit Factor", "CI (95%)"].map(
                    (h) => (
                      <th
                        key={h}
                        style={{
                          padding: "0.4rem 0.75rem",
                          textAlign: "left",
                          fontWeight: 700,
                          color: "#64748b",
                          fontSize: 10,
                          textTransform: "uppercase",
                          letterSpacing: "0.05em",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {h}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {result.all_buckets.map((b, i) => (
                  <tr
                    key={b.label}
                    style={{
                      borderBottom: "1px solid rgba(99,102,241,0.07)",
                      background: b.suppressed
                        ? "rgba(15,23,42,0.3)"
                        : i % 2 === 0
                        ? "transparent"
                        : "rgba(30,41,59,0.2)",
                      opacity: b.suppressed ? 0.5 : 1,
                    }}
                  >
                    <td style={{ padding: "0.4rem 0.75rem", fontWeight: 700, color: "#e2e8f0" }}>
                      {b.label.replace(/_/g, " ")}
                      {b.suppressed && (
                        <span style={{ marginLeft: 6, fontSize: 10, color: "#475569" }}>(too few)</span>
                      )}
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem" }}>
                      <EvidencePip note={b.evidence_note} />
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem", color: "#94a3b8" }}>{b.n}</td>
                    <td style={{ padding: "0.4rem 0.75rem", color: "#64748b" }}>{b.n_forward}</td>
                    <td style={{ padding: "0.4rem 0.75rem", color: trend(b.mean), fontWeight: 600 }}>
                      {b.mean != null ? b.mean.toFixed(2) : "—"}
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem", color: trend(b.median) }}>
                      {b.median != null ? b.median.toFixed(2) : "—"}
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem", color: b.win_rate != null && b.win_rate > 50 ? "#10b981" : "#ef4444" }}>
                      {b.win_rate != null ? `${b.win_rate.toFixed(1)}%` : "—"}
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem", color: b.profit_factor != null && b.profit_factor > 1 ? "#10b981" : "#ef4444" }}>
                      {b.profit_factor != null ? b.profit_factor.toFixed(2) : "—"}
                    </td>
                    <td style={{ padding: "0.4rem 0.75rem", color: "#475569", fontSize: 11 }}>
                      {b.ci_low != null && b.ci_high != null
                        ? `[${b.ci_low.toFixed(2)}, ${b.ci_high.toFixed(2)}]`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Evidence legend */}
          <div style={{ display: "flex", gap: "1rem", fontSize: 10, color: "#475569" }}>
            <span>
              <span style={{ color: "#10b981" }}>● Forward</span> — enough out-of-sample observations
            </span>
            <span>
              <span style={{ color: "#eab308" }}>● Thin</span> — forward evidence exists but below floor
            </span>
            <span>
              <span style={{ color: "#64748b" }}>● In-sample</span> — selection-history only
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Main export ──────────────────────────────────────────────────────────────

type IntelSub = "overview" | "sectors" | "stocks" | "context";

const SUB_TABS: { id: IntelSub; label: string }[] = [
  { id: "overview", label: "Market Overview" },
  { id: "sectors", label: "Sector Rankings" },
  { id: "stocks", label: "Stock Leaders" },
  { id: "context", label: "Strategy Context" },
];

export default function MarketIntelligencePanel() {
  const [sub, setSub] = useState<IntelSub>("overview");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {/* Sub-tab bar */}
      <div style={{ display: "flex", gap: "0.25rem", borderBottom: "1px solid rgba(99,102,241,0.15)", paddingBottom: "0.5rem" }}>
        {SUB_TABS.map((t) => (
          <button
            key={t.id}
            id={`intel-sub-${t.id}`}
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

      {/* Panel content: Keep sub-panels mounted to prevent re-fetching and flickering when switching tabs */}
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      <div style={{ display: sub === "overview" ? "block" : "none" }}>
        <MarketOverviewPanel />
      </div>
      <div style={{ display: sub === "sectors" ? "block" : "none" }}>
        <SectorRankingsPanel />
      </div>
      <div style={{ display: sub === "stocks" ? "block" : "none" }}>
        <StockLeadersPanel />
      </div>
      <div style={{ display: sub === "context" ? "block" : "none" }}>
        <StrategyContextPanel />
      </div>
    </div>
  );
}
