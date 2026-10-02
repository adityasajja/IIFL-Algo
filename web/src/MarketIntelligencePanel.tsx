/**
 * MarketIntelligencePanel — Indian Equity Market Intelligence Dashboard
 *
 * MARKET → SECTOR → STOCK → STRATEGY → TRADE → LEARNING
 *
 * Four sub-panels:
 * 1. Market Overview — regime badge, NIFTY trend, A/D ratio, breadth, 52w H/L, volatility
 * 2. Sector Rankings — sortable table of all sectors with RS, breadth, RVOL, breakouts
 * 3. Stock Leaders — sortable leaderboard: top RS, breakouts, near 52w highs
 * 4. Strategy Context — select strategy + axis → empirical performance per market condition
 *
 * Strictly Indian cash-equity. No F&O.
 */

import { useState, useEffect, useCallback, useRef } from "react";
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
import { Switch } from "./components/motion/switch";
import { Tooltip } from "./components/motion/tooltip";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { cn } from "./lib/utils";
import { Card } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { fieldInput, fieldLabel } from "./components/ui/form-styles";
import { Badge } from "./components/ui/stat";
import { toneText, toneOf, type Tone } from "./lib/tone";
import { MoodGauge, WhatChanged, ScreenerJumps, SectorHeatmap, VixCard, LiveTag, useLiveIndices, type ScreenPreset } from "./MarketMood";
import { RefreshCw } from "lucide-react";
import { Chip } from "./components/ui/chip";

// ─── Colour / theme helpers ───────────────────────────────────────────────────

/** The regime in one plain word, with the tone it should be shown in. */
const REGIME_PLAIN: Record<string, { word: string; tone: "good" | "bad" | "warn" | "flat" }> = {
  BULLISH_TREND: { word: "Rising", tone: "good" },
  BEARISH_TREND: { word: "Falling", tone: "bad" },
  SIDEWAYS: { word: "Flat", tone: "flat" },
  HIGH_VOLATILITY: { word: "Choppy", tone: "warn" },
  LOW_VOLATILITY: { word: "Calm", tone: "flat" },
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
    className="inline-flex items-center gap-2 text-caption text-loss bg-loss/12 border border-loss/25 rounded-md py-1 px-2.5"
    >
      <span className="text-xs">⚠</span>
      <span>
        {isNetworkOrThrottle || !msg
          ? `Market data delayed${lastUpdated ? ` · Last updated ${lastUpdated}` : ""}`
          : `Service notice: ${msg.slice(0, 45)}`}
      </span>
      {onRetry && (
        <Button
          size="inline"
          variant="link"
          onClick={onRetry}
          className="ml-0.5 text-caption font-semibold"
        >
          Retry
        </Button>
      )}
    </div>
  );
}

// ─── Sub-panel 1: Market Overview ────────────────────────────────────────────

function MarketOverviewPanel({ onOpenScreen }: { onOpenScreen?: (p: ScreenPreset) => void }) {
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

  // Hooks stay above the early returns below.
  const live = useLiveIndices();

  if (loading && !data) return <PageLoader label="Loading market data" />;
  if (error && !data) return <div className="p-4"><CompactErrorNotice msg={error} onRetry={() => load(true)} /></div>;
  if (!data) return null;

  // The live Nifty replaces the daily close and day move; the month's return stays daily.
  const liveNifty = live?.available ? live.nifty : null;
  const niftyPrice = liveNifty ? liveNifty.ltp : data.nifty_close;
  const niftyDayChg = liveNifty && liveNifty.chg_pct != null ? liveNifty.chg_pct : data.nifty_change_1d_pct;

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
    good: "border border-gain/20 bg-gain/[0.08] text-gain",
    bad: "border border-destructive/20 bg-destructive/[0.08] text-destructive",
    warn: "border border-warning/20 bg-warning/[0.08] text-warning",
    flat: "border border-border/60 bg-muted/50 text-muted-foreground",
  };
  const fit = (v: string) =>
    v === "STRONG" ? { word: "Good fit", cls: tone.good } : v === "NEUTRAL" ? { word: "Okay", cls: tone.flat } : { word: "Poor fit", cls: tone.bad };
  const barTone = (pct: number) => (pct >= 55 ? "bg-gain" : pct >= 40 ? "bg-warning" : "bg-loss");
  const changeCls = (v: number | null | undefined) => (v == null ? "text-muted-foreground" : v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-muted-foreground");
  const signed = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);

  const up = data.advancing_stocks;
  const down = data.declining_stocks;
  const flat = data.unchanged_stocks;
  const total = Math.max(up + down + flat, 1);
  const scoreTone = contextScore >= 65 ? "stroke-gain" : contextScore <= 35 ? "stroke-loss" : "stroke-warning";
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
  const takeaway = `${breadth.toFixed(0)}% above 50-day avg${
    breadthMove === null || Math.abs(breadthMove) < 5 ? "" : ` (was ${monthAgo!.toFixed(0)}%)`
  } · ${up === down ? "Even" : `${up > down ? "Buyers" : "Sellers"} ${ratio.toFixed(1)} : 1`}`;

  const leaders = (data.top_relative_strength_stocks ?? []).slice(0, 5) as Record<string, unknown>[];
  const breakouts = (data.top_breakouts ?? []).slice(0, 5) as Record<string, unknown>[];
  const stockDataBehind =
    !!data.stocks_as_of && !!lastPoint && data.stocks_as_of < lastPoint.d;

  return (
    <div className="space-y-4">
      {/* Verdict + the index. */}
      <div className="flex flex-wrap items-center justify-between gap-6 rounded-xl border border-border bg-card p-5">
        <div>
          <div className="text-xs text-muted-foreground">Market direction</div>
          <div className="mt-2 flex items-center gap-3">
            <span className={cn("rounded-full px-3 py-1 text-2xl font-semibold tracking-tight", tone[regime.tone])}>
              {regime.word}
            </span>
          </div>
          <p className="mt-3 text-sm text-muted-foreground">{takeaway}</p>
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
            {niftyPrice != null ? `₹${niftyPrice.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}
            </div>
            <div className="mt-1 flex justify-end gap-3 text-xs tabular-nums">
              {liveNifty ? <LiveTag live={live} /> : null}
              <span className={changeCls(niftyDayChg)}>{signed(niftyDayChg)} {live?.available && !live.market_open ? "last session" : "today"}</span>
              <span className={changeCls(data.nifty_1m_return_pct)}>{signed(data.nifty_1m_return_pct)} this month</span>
            </div>
          </div>
          <div className="flex flex-col items-end gap-1.5">
            {error && <CompactErrorNotice msg={error} lastUpdated={lastUpdated ?? undefined} onRetry={() => load(true)} />}
            <Tooltip content={lastUpdated ? `Updated ${lastUpdated}` : "Refresh"} side="bottom" delay={400}>
            <Button
              id="market-intel-refresh"
              size="icon-sm"
              variant="quiet"
              onClick={() => load(true)}
              disabled={refreshing}
              aria-label="Refresh"
            >
              <RefreshCw className={cn(refreshing && "animate-spin")} />
            </Button>
            </Tooltip>
          </div>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <MoodGauge data={data} />
        <WhatChanged data={data} />
      </div>
      <VixCard data={data} live={live} />

      {series.length > 1 ? (
        <div className="rounded-xl border border-border bg-card p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="text-xs text-muted-foreground">Nifty 50, last 6 months</div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums">
              {vs50 !== null ? (
                <span className={vs50 >= 0 ? "text-gain" : "text-loss"}>
                  {Math.abs(vs50).toFixed(1)}% {vs50 >= 0 ? "above" : "below"} 50-day average
                </span>
              ) : null}
              {vs200 !== null ? (
                <span className={vs200 >= 0 ? "text-gain" : "text-loss"}>
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
                        className={cn("flex-1 rounded-sm", x.pct >= 0 ? "bg-gain" : "bg-loss")}
                        style={{ height: `${Math.max((Math.abs(x.pct) / maxMove) * 100, 8)}%` }}
                      />
                    ))}
                  </div>
                  <div className="mt-1.5 flex gap-2 text-micro text-muted-foreground">
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
                  <div className="relative mt-6 h-2 rounded-full bg-gradient-to-r from-loss/40 via-warning/40 to-gain/40">
                    <div
                      className="absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary"
                      style={{ left: `${rangePos * 100}%` }}
                    />
                  </div>
                  <div className="mt-2 flex justify-between text-caption tabular-nums text-muted-foreground">
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
        <div className="rounded-xl border border-border bg-card p-5">
          <div className="text-xs text-muted-foreground">Stocks today</div>
          <div className="mt-3 flex h-3 overflow-hidden rounded-full bg-muted">
          <div className="bg-gain" style={{ width: `${(up / total) * 100}%` }} title={`${up} rising`} />
            <div className="bg-muted-foreground/30" style={{ width: `${(flat / total) * 100}%` }} title={`${flat} unchanged`} />
            <div className="bg-loss" style={{ width: `${(down / total) * 100}%` }} title={`${down} falling`} />
          </div>
          <div className="mt-2 flex justify-between text-sm tabular-nums">
            <span className="text-gain">{up} rising</span>
            <span className="text-loss">{down} falling</span>
          </div>

          <div className="mt-5 text-xs text-muted-foreground">Above average</div>
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
                <div className="text-xs text-muted-foreground">50-day, 30 days</div>
                {breadthMove !== null ? (
                  <div className={cn("mt-1 text-sm tabular-nums", breadthMove >= 0 ? "text-gain" : "text-loss")}>
                    {breadthMove >= 0 ? "Up" : "Down"} {Math.abs(breadthMove).toFixed(0)} points
                  </div>
                ) : null}
                {extreme ? <div className="mt-0.5 text-caption text-muted-foreground">{extreme}</div> : null}
              </div>
              <Sparkline data={history.slice(-30).map((h) => h.pct)} width={140} height={40} ariaLabel="Share of stocks above their 50-day average" />
            </div>
          ) : null}
          <div className="mt-4 text-caption text-muted-foreground" title={data.survivorship_safeguard}>
            Based on {data.total_stocks_analyzed} stocks
            {stockDataBehind ? (
              <span className="text-warning"> · stock data to {dateLabel(data.stocks_as_of!)}, index to {dateLabel(lastPoint.d)}</span>
            ) : null}
          </div>
        </div>

        <div className="grid content-start gap-4">
          {/* One number, one word. */}
          <div className="flex items-center gap-5 rounded-xl border border-border bg-card p-5">
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
            <div className="rounded-xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Yearly high / low</div>
              <div className="mt-2 flex items-baseline gap-2 tabular-nums">
                <span className="text-xl font-semibold text-gain">{data.highs_52w_count}</span>
                <span className="text-muted-foreground">/</span>
                <span className="text-xl font-semibold text-loss">{data.lows_52w_count}</span>
              </div>
            </div>
            <div className="rounded-xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Swings</div>
              <div className="mt-2 text-xl font-semibold">{volWord}</div>
            </div>
            <div className="rounded-xl border border-border bg-card p-4">
              <div className="text-xs text-muted-foreground">Sectors rising</div>
              <div className="mt-2 text-xl font-semibold tabular-nums">{secPart.toFixed(0)}%</div>
            </div>
          </div>
        </div>
      </div>

      {/* Which approaches suit today, as chips rather than two tables. */}
      <div className="rounded-xl border border-border bg-card p-5">
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
              <div key={s.label} className="flex items-center justify-between gap-2 rounded-lg border border-border px-3.5 py-2.5">
                <span className="text-sm">{s.label}</span>
                <span className={cn("rounded-full px-2 py-0.5 text-caption font-medium", f.cls)}>{f.word}</span>
              </div>
            );
          })}
        </div>
      </div>

      {data.strongest_sectors?.length || data.weakest_sectors?.length || data.sectors_turning_up?.length || data.sectors_fading?.length ? (
        <div className="rounded-xl border border-border bg-card p-5">
          <div className="text-xs text-muted-foreground">Sectors</div>
          <div className="mt-3 space-y-3">
            {[
              { label: "Leading", items: data.strongest_sectors, cls: tone.good },
              { label: "Lagging", items: data.weakest_sectors, cls: tone.bad },
              { label: "Turning up", items: data.sectors_turning_up, cls: "bg-gain/5 text-gain ring-1 ring-gain/30" },
              { label: "Fading", items: data.sectors_fading, cls: "bg-warning/5 text-warning ring-1 ring-warning/30" },
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
              hint: "vs normal",
              rows: breakouts.map((r) => ({
                symbol: String(r.symbol ?? "").replace("-EQ", ""),
                value: `${Number(r.relative_volume).toFixed(1)}x`,
                good: true,
              })),
            },
          ].map((card) => (
            <div key={card.title} className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-baseline justify-between">
                <span className="text-xs text-muted-foreground">{card.title}</span>
                <span className="text-caption text-muted-foreground/70">{card.hint}</span>
              </div>
              {card.rows.length ? (
                <div className="mt-3 divide-y divide-border">
                  {card.rows.map((r) => (
                    <div key={r.symbol} className="flex items-center justify-between py-2 text-sm">
                      <span className="font-medium">{r.symbol}</span>
                      <span className={cn("tabular-nums", r.good ? "text-gain" : "text-loss")}>{r.value}</span>
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

      {onOpenScreen ? <ScreenerJumps onOpen={onOpenScreen} /> : null}
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
          <path d={line("s50")} fill="none" className="stroke-warning" strokeWidth="1.5" strokeDasharray="4 4" vectorEffect="non-scaling-stroke" />
          <path d={line("c")} fill="none" stroke="var(--primary)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
          {hover !== null ? (
            <line x1={x(hover)} x2={x(hover)} y1={0} y2={H} stroke="var(--muted-foreground)" strokeWidth="1" vectorEffect="non-scaling-stroke" opacity="0.5" />
          ) : null}
        </svg>
        {point ? (
          <div
            className="pointer-events-none absolute top-1 -translate-x-1/2 rounded-xl border border-border bg-card px-2.5 py-1.5 text-xs "
            style={{ left: `${Math.min(88, Math.max(12, (hover! / (series.length - 1)) * 100))}%` }}
          >
            <div className="text-muted-foreground">{dateLabel(point.d)}</div>
            <div className="font-medium tabular-nums">{point.c.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
          </div>
        ) : null}
      </div>
      <div className="mt-2 flex items-center justify-between text-caption text-muted-foreground">
        <span>{dateLabel(series[0].d)}</span>
        <span className="flex items-center gap-4">
          <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-3 bg-primary" /> Nifty</span>
          <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-3 bg-warning" /> 50-day</span>
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
  if (error && sectors.length === 0) return <div className="p-4"><CompactErrorNotice msg={error} onRetry={() => load()} /></div>;

  const value = SECTOR_PERIODS[period].pick;
  const maxAbs = Math.max(...sectors.map((s) => Math.abs(value(s))), 0.01);
  const barTone = (pct: number) => (pct >= 55 ? "bg-gain" : pct >= 40 ? "bg-warning" : "bg-loss");

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

      <SectorHeatmap sectors={sectors} pick={value} periodLabel={SECTOR_PERIODS[period].label} />

      <div className="overflow-hidden rounded-xl border border-border bg-card">
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
                  <div className="mt-0.5 text-caption text-muted-foreground">
                    {s.stock_count} stocks
                    <span className="ml-2 inline-flex gap-0.5 align-middle" title="Today, this week, this month">
                      {[s.return_1d_pct, s.return_1w_pct, s.return_1m_pct].map((v, i) => (
                        <span key={i} className={cn("size-1.5 rounded-full", v > 0 ? "bg-gain" : v < 0 ? "bg-loss" : "bg-muted-foreground/40")} />
                      ))}
                    </span>
                    {s.stock_count >= 5 && s.return_1w_pct > 0 && s.return_1m_pct < 0 ? (
                      <span className="ml-2 rounded-full border border-gain/20 bg-gain/[0.08] px-1.5 py-0.5 text-gain">Turning up</span>
                    ) : null}
                    {s.stock_count >= 5 && s.return_1w_pct < 0 && s.return_1m_pct > 0 ? (
                      <span className="ml-2 rounded-full border border-warning/20 bg-warning/[0.08] px-1.5 py-0.5 text-warning">Fading</span>
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
                    className={cn("absolute inset-y-0 rounded-full transition-all duration-500", v >= 0 ? "bg-gain" : "bg-loss")}
                      style={v >= 0 ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }}
                    />
                  </div>
                  <span className={cn("w-16 shrink-0 text-right text-sm tabular-nums", v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-muted-foreground")}>
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
  if (error && stocks.length === 0) return <div className="p-4"><CompactErrorNotice msg={error} onRetry={() => load()} /></div>;

  const signedCls = (v: number) => (v > 0 ? "text-gain" : v < 0 ? "text-loss" : "text-muted-foreground");
  const pct = (v: number, digits = 1) => `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;

  // The three facts every row shows, plus the one you sorted by if it is not among them.
  const facts = (s: StockContext) => {
    const all: { key: StockSortKey; label: string; text: string; cls: string }[] = [
      { key: "relative_strength_nifty_20d", label: "vs Nifty", text: pct(s.relative_strength_nifty_20d, 0), cls: signedCls(s.relative_strength_nifty_20d) },
      { key: "relative_volume", label: "Volume", text: `${s.relative_volume.toFixed(1)}x`, cls: s.relative_volume >= 1.5 ? "text-primary" : "" },
      { key: "from_52w_high_pct", label: "Yearly high", text: pct(s.from_52w_high_pct, 1), cls: s.from_52w_high_pct > -3 ? "text-warning" : "" },
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
            <Chip
              key={opt.key}
              id={`stock-sort-${opt.key}`}
              selected={sortKey === opt.key}
              onClick={() => setSortKey(opt.key)}
            >
              {opt.label}
            </Chip>
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
        <div className="rounded-xl border border-border bg-card px-5 py-10 text-center text-sm text-muted-foreground">
          Nothing matches right now.
        </div>
      ) : (
        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
          {stocks.map((s, i) => (
            <div key={s.symbol} className="flex items-start gap-3 px-5 py-3">
              <span className="w-6 pt-0.5 text-xs tabular-nums text-muted-foreground">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="font-semibold">{s.symbol.replace("-EQ", "")}</span>
                  {s.is_breakout ? (
                    <span className="rounded-full border border-warning/20 bg-warning/[0.08] px-2 py-0.5 text-micro font-medium text-warning">Breakout</span>
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
  const tone: Tone = note === "forward" ? "good" : note === "insufficient_forward_observations" ? "warn" : "flat";
  const label = note === "forward" ? "Forward" : note === "insufficient_forward_observations" ? "Thin" : "In-sample";
  return <Badge tone={tone}>{label}</Badge>;
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
    <div className="flex flex-col gap-4">
      {/* Controls */}
      <Card padding="md">
        <div className="flex gap-4 flex-wrap items-end">
          <div className="flex flex-col gap-1">
            <label className={fieldLabel}>STRATEGY (optional)</label>
            <input
              id="strategy-context-strategy"
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              placeholder="e.g. MOMENTUM_BREAKOUT"
              className={cn(fieldInput, "w-[200px]")}
            />
          </div>

          <div className="flex flex-col gap-1">
            <label className={fieldLabel} title={axisDesc}>CONDITION AXIS</label>
            <Select
              size="sm"
              className="w-[220px]"
              value={axis}
              onChange={(v) => setAxis(v as MarketContextAxis)}
              options={AXIS_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
            />
          </div>

          <Button id="strategy-context-run" size="xs" onClick={run} disabled={loading}>
            {loading ? "Analyzing…" : "Analyze"}
          </Button>
        </div>
              </Card>

      {error && <div className="py-2 px-0"><CompactErrorNotice msg={error} onRetry={() => run()} /></div>}

      {result && (
        <div className="flex flex-col gap-4">
          {/* Baseline */}
          <Card padding="md">
            <div className="flex gap-8 flex-wrap">
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">STRATEGY</div>
                <div className="font-semibold text-primary-soft">{result.strategy}</div>
              </div>
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">AXIS</div>
                <div className="font-semibold text-foreground">{result.condition_axis}</div>
              </div>
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">METRIC</div>
                <div className="font-semibold text-foreground">{result.metric}</div>
              </div>
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">TOTAL ROWS</div>
                <div className="font-semibold text-muted-foreground">{result.total_rows_scanned}</div>
              </div>
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">BASELINE WIN RATE</div>
                <div className="font-semibold text-muted-foreground">
                  {result.baseline.win_rate != null ? `${result.baseline.win_rate.toFixed(1)}%` : "—"}
                </div>
              </div>
              <div>
                <div className="text-micro text-muted-foreground mb-0.5">BASELINE MEAN</div>
                <div className={cn("font-semibold", toneText[toneOf(result.baseline.mean)])}>
                  {result.baseline.mean != null ? result.baseline.mean.toFixed(2) : "—"}
                </div>
              </div>
            </div>
            {result.caveats.length > 0 && (
              <div className="mt-2 text-caption text-warning">
                ⚠ {result.caveats.join(" · ")}
              </div>
            )}
          </Card>

          {/* Bucket table */}
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-b-primary/15">
                  {["Condition", "Evidence", "N", "Fwd", "Mean", "Median", "Win Rate", "Profit Factor", "CI (95%)"].map(
                    (h) => (
                      <th
                        key={h}
                      className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground whitespace-nowrap"
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
                    className={cn(
                      "border-b border-border/50",
                      b.suppressed ? "bg-muted/30 opacity-50" : i % 2 === 1 && "bg-muted/20",
                    )}
                  >
                    <td className="px-3 py-1.5 font-semibold text-foreground">
                      {b.label.replace(/_/g, " ")}
                      {b.suppressed && (
                        <span className="ml-1.5 text-micro text-muted-foreground">(too few)</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5">
                      <EvidencePip note={b.evidence_note} />
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">{b.n}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{b.n_forward}</td>
                    <td className={cn("px-3 py-1.5 font-semibold", toneText[toneOf(b.mean)])}>
                      {b.mean != null ? b.mean.toFixed(2) : "—"}
                    </td>
                    <td className={cn("px-3 py-1.5", toneText[toneOf(b.median)])}>
                      {b.median != null ? b.median.toFixed(2) : "—"}
                    </td>
                    <td className={cn("px-3 py-1.5", toneText[b.win_rate != null && b.win_rate > 50 ? "good" : "bad"])}>
                      {b.win_rate != null ? `${b.win_rate.toFixed(1)}%` : "—"}
                    </td>
                    <td className={cn("px-3 py-1.5", toneText[b.profit_factor != null && b.profit_factor > 1 ? "good" : "bad"])}>
                      {b.profit_factor != null ? b.profit_factor.toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground text-caption">
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
          <div className="flex gap-4 text-micro text-muted-foreground">
            <span>
              <span className="text-gain" title="Enough out-of-sample observations">● Forward</span>
            </span>
            <span>
              <span className="text-warning" title="Forward evidence below the floor">● Thin</span>
            </span>
            <span>
              <span className="text-muted-foreground" title="Selection history only">● In-sample</span>
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

export default function MarketIntelligencePanel({ onOpenScreen }: { onOpenScreen?: (p: ScreenPreset) => void } = {}) {
  const [sub, setSub] = useState<IntelSub>("overview");

  return (
    <div className="flex flex-col gap-4">
      {/* Sub-tab bar: the same beui Tabs as the Markets row above, in the segment style. */}
      <div>
        <Tabs value={sub} onValueChange={(v) => setSub(v as IntelSub)} variant="segment">
          <TabsList>
        {SUB_TABS.map((t) => (
              <TabsTrigger key={t.id} value={t.id}>
            {t.label}
              </TabsTrigger>
        ))}
          </TabsList>
        </Tabs>
      </div>

      {/* Panel content: Keep sub-panels mounted to prevent re-fetching and flickering when switching tabs */}
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      <div className={sub === "overview" ? "block" : "hidden"}>
        <MarketOverviewPanel onOpenScreen={onOpenScreen} />
      </div>
      <div className={sub === "sectors" ? "block" : "hidden"}>
        <SectorRankingsPanel />
      </div>
      <div className={sub === "stocks" ? "block" : "hidden"}>
        <StockLeadersPanel />
      </div>
      <div className={sub === "context" ? "block" : "hidden"}>
        <StrategyContextPanel />
      </div>
    </div>
  );
}
