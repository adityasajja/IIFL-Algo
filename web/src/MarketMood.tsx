/**
 * Market page extras: a fear/greed mood gauge, a "what changed" strip,
 * a sector heatmap and one-click jumps into the screener.
 *
 * Everything here is derived from data the market-intel endpoints already
 * return, so it adds no requests.
 */

import { useEffect, useState } from "react";
import { getMarketLive, type MarketLive, type MarketSummary, type SectorMetrics } from "./api";
import { cn } from "./lib/utils";
import { Sparkline } from "./components/ui/sparkline";

// ─── Live index quotes ───────────────────────────────────────────────────────

const LIVE_POLL_MS = 5000;

/** Nifty and India VIX from the broker's feed, refreshed while the tab is visible.
 *  Null until the first answer, and after a failure: callers then show daily numbers. */
export function useLiveIndices(): MarketLive | null {
  const [live, setLive] = useState<MarketLive | null>(null);
  useEffect(() => {
    let alive = true;
    const pull = () => {
      if (document.visibilityState !== "visible") return;
      getMarketLive()
        .then((r) => alive && setLive(r))
        .catch(() => alive && setLive(null));
    };
    pull();
    const id = window.setInterval(pull, LIVE_POLL_MS);
    document.addEventListener("visibilitychange", pull);
    return () => {
      alive = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", pull);
    };
  }, []);
  return live;
}

/** A small tag saying whether a number is moving or is the last session's close. */
export function LiveTag({ live }: { live: MarketLive | null }) {
  if (!live || !live.available) return null;
  const moving = live.market_open;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-micro font-medium",
        moving ? "bg-gain/10 text-gain" : "bg-muted text-muted-foreground",
      )}
      title={moving ? "Updating from the exchange feed every few seconds" : "The market is closed; this is the last session"}
    >
      <span className={cn("size-1.5 rounded-full", moving ? "animate-pulse bg-gain" : "bg-muted-foreground/60")} />
      {moving ? "Live" : "Market closed"}
    </span>
  );
}

// ─── Screener presets ────────────────────────────────────────────────────────

export interface ScreenPreset {
  label: string;
  hint: string;
  tree: Record<string, unknown>;
}

/** Stored for the screener to pick up once, then cleared by it. */
export const SCREEN_PRESET_KEY = "atr.screener.preset";

export const SCREEN_PRESETS: ScreenPreset[] = [
  {
    label: "Near 52-week highs",
    hint: "Within 2% of the yearly high, on above-normal volume",
    tree: {
      match: "all",
      conditions: [
        { indicator: "from_52w_high_pct", op: ">=", value: -2, period: 252 },
        { indicator: "rel_volume", op: ">", value: 1, period: 20 },
      ],
    },
  },
  {
    label: "Volume breakouts",
    hint: "Closed above yesterday's high on 2x normal volume",
    tree: {
      match: "all",
      conditions: [
        { indicator: "close", op: ">", rhs_indicator: "prev_high" },
        { indicator: "rel_volume", op: ">", value: 2, period: 20 },
      ],
    },
  },
  {
    label: "Dips in an uptrend",
    hint: "Above the 50-day average but RSI under 40",
    tree: {
      match: "all",
      conditions: [
        { indicator: "close", op: ">", rhs_indicator: "ema", period: 50 },
        { indicator: "rsi", op: "<", value: 40, period: 14 },
      ],
    },
  },
];

// ─── Mood gauge ──────────────────────────────────────────────────────────────

const clamp = (v: number, lo = 0, hi = 100) => Math.min(Math.max(v, lo), hi);

export function moodScore(d: MarketSummary): number {
  const breadth = clamp(d.breadth_above_ema50_pct);
  const moving = d.advancing_stocks + d.declining_stocks;
  const advShare = moving > 0 ? (d.advancing_stocks / moving) * 100 : 50;
  const extremes = d.highs_52w_count + d.lows_52w_count;
  const highShare = extremes > 0 ? (d.highs_52w_count / extremes) * 100 : 50;
  // Calm markets read as confident. India VIX against its own year is the better
  // gauge of that; the index's ATR stands in until VIX has been downloaded.
  const calm = d.vix
    ? clamp(100 - d.vix.percentile_1y)
    : clamp(((1.8 - d.market_volatility_atr_pct) / 1.2) * 100);
  return Math.round(breadth * 0.35 + advShare * 0.25 + highShare * 0.2 + calm * 0.2);
}

const MOODS: { max: number; word: string; line: string; color: string }[] = [
  { max: 20, word: "Extreme fear", line: "Sellers are in a hurry. Bargains show up here, but so do falling knives.", color: "var(--loss)" },
  { max: 40, word: "Fear", line: "The mood is cautious. Most stocks are under pressure.", color: "color-mix(in oklab, var(--loss) 55%, var(--warning))" },
  { max: 60, word: "Neutral", line: "No strong lean either way. Stock picking matters more than the index.", color: "var(--warning)" },
  { max: 80, word: "Greed", line: "Buyers are in control and most stocks are along for the ride.", color: "color-mix(in oklab, var(--warning) 45%, var(--gain))" },
  { max: 101, word: "Extreme greed", line: "Everyone is buying. Strong markets can run, but chasing gets riskier.", color: "var(--gain)" },
];

export function MoodGauge({ data }: { data: MarketSummary }) {
  const score = moodScore(data);
  const mood = MOODS.find((m) => score < m.max) ?? MOODS[MOODS.length - 1];
  // Semicircle: 180° sweep, needle angle from the score.
  const angle = Math.PI * (1 - score / 100);
  const cx = 100;
  const cy = 100;
  const r = 80;
  const nx = cx + (r - 14) * Math.cos(angle);
  const ny = cy - (r - 14) * Math.sin(angle);
  const arc = (from: number, to: number) => {
    const a0 = Math.PI * (1 - from / 100);
    const a1 = Math.PI * (1 - to / 100);
    return `M ${cx + r * Math.cos(a0)} ${cy - r * Math.sin(a0)} A ${r} ${r} 0 0 1 ${cx + r * Math.cos(a1)} ${cy - r * Math.sin(a1)}`;
  };
  const parts = [
    { label: "Stocks above 50-day", v: Math.round(data.breadth_above_ema50_pct) + "%" },
    { label: "Rising vs falling", v: `${data.advancing_stocks} / ${data.declining_stocks}` },
    { label: "Yearly highs vs lows", v: `${data.highs_52w_count} / ${data.lows_52w_count}` },
  ];
  return (
    <div className="flex flex-wrap items-center gap-6 rounded-xl border border-border bg-card p-5">
      <svg viewBox="0 0 200 118" className="w-48 shrink-0" role="img" aria-label={`Market mood ${score} of 100, ${mood.word}`}>
        {MOODS.map((m, i) => (
          <path key={m.word} d={arc(i * 20 + 0.8, (i + 1) * 20 - 0.8)} stroke={m.color} strokeWidth="12" fill="none" opacity={m.word === mood.word ? 1 : 0.28} />
        ))}
        <line x1={cx} y1={cy} x2={nx} y2={ny} stroke="currentColor" strokeWidth="3" strokeLinecap="round" style={{ transition: "all 0.6s ease" }} />
        <circle cx={cx} cy={cy} r="5" fill="currentColor" />
        <text x={cx} y={cy + 16} textAnchor="middle" className="fill-current" fontSize="15" fontWeight="600">{score}</text>
      </svg>
      <div className="min-w-[14rem] flex-1">
        <div className="text-xs text-muted-foreground">Market mood</div>
        <div className="mt-1 text-2xl font-semibold tracking-tight" style={{ color: mood.color }}>{mood.word}</div>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">{mood.line}</p>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
          {parts.map((p) => (
            <span key={p.label}>{p.label}: <span className="tabular-nums text-foreground">{p.v}</span></span>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── India VIX ───────────────────────────────────────────────────────────────

export function VixCard({ data, live }: { data: MarketSummary; live?: MarketLive | null }) {
  const daily = data.vix;
  if (!daily) return null;
  const q = live?.available ? live.vix : null;
  // The live quote replaces the level and the day move; the year's range and
  // percentile stay as of the last close, which is all the history there is.
  const v = q ? { ...daily, close: q.ltp, change_1d_pct: q.chg_pct ?? daily.change_1d_pct } : daily;
  const word = v.close < 13 ? "Calm" : v.close < 18 ? "Normal" : v.close < 25 ? "Nervous" : "Fearful";
  const color = v.close < 13 ? "text-gain" : v.close < 18 ? "text-foreground" : v.close < 25 ? "text-warning" : "text-loss";
  const pos = v.high_1y > v.low_1y ? clamp(((v.close - v.low_1y) / (v.high_1y - v.low_1y)) * 100) : 50;
  const chg = v.change_1d_pct;
  return (
    <div className="flex flex-wrap items-center gap-6 rounded-xl border border-border bg-card p-5">
      <div className="min-w-[10rem]">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span title="India VIX is the market's expected 30-day swing, implied by Nifty option prices">India VIX</span>
          {q ? <LiveTag live={live ?? null} /> : null}
        </div>
        <div className="mt-1 flex items-baseline gap-2">
          <span className="text-3xl font-semibold tracking-tight tabular-nums">{v.close.toFixed(2)}</span>
          {chg != null ? (
            <span className={cn("text-xs tabular-nums", chg > 0 ? "text-loss" : chg < 0 ? "text-gain" : "text-muted-foreground")}>
              {chg > 0 ? "+" : ""}{chg.toFixed(2)}%
            </span>
          ) : null}
        </div>
        <div className={cn("mt-1 text-sm font-medium", color)}>{word}</div>
      </div>
      <div className="min-w-[14rem] flex-1">
        <div className="flex items-baseline justify-between text-xs text-muted-foreground">
          <span>Past year</span>
          <span>Higher than {v.percentile_1y.toFixed(0)}% of sessions</span>
        </div>
        <div className="relative mt-5 h-2 rounded-full bg-gradient-to-r from-gain/40 via-warning/40 to-loss/40">
          <div className="absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-primary" style={{ left: `${pos}%` }} />
        </div>
        <div className="mt-2 flex justify-between text-caption tabular-nums text-muted-foreground">
          <span>{v.low_1y.toFixed(1)}</span>
          <span>{v.high_1y.toFixed(1)}</span>
        </div>
      </div>
      <Sparkline data={v.series} width={140} height={40} ariaLabel="India VIX, last 60 sessions" />
    </div>
  );
}

// ─── What changed ────────────────────────────────────────────────────────────

type Change = { tone: "good" | "bad" | "info"; text: string };

export function deriveChanges(d: MarketSummary): Change[] {
  const out: Change[] = [];
  const s = d.benchmark_series ?? [];
  const h = d.breadth_history ?? [];
  const last = s[s.length - 1];
  const prev = s[s.length - 2];

  if (last && prev) {
    for (const [key, name] of [["s50", "50-day"], ["s200", "200-day"]] as const) {
      const a = last[key];
      const b = prev[key];
      if (a == null || b == null) continue;
      if (prev.c < b && last.c >= a) out.push({ tone: "good", text: `Nifty moved back above its ${name} average` });
      if (prev.c >= b && last.c < a) out.push({ tone: "bad", text: `Nifty slipped below its ${name} average` });
    }
    // A run of same-direction closes.
    let run = 0;
    const dir = Math.sign(last.c - prev.c);
    for (let i = s.length - 1; i > 0 && dir !== 0 && Math.sign(s[i].c - s[i - 1].c) === dir; i--) run++;
    if (run >= 3) out.push({ tone: dir > 0 ? "good" : "bad", text: `Nifty has closed ${dir > 0 ? "up" : "down"} ${run} days in a row` });
    const closes = s.map((p) => p.c);
    if (last.c >= Math.max(...closes) && s.length > 60) out.push({ tone: "good", text: `Nifty is at a ${s.length >= 120 ? "6-month" : `${s.length}-day`} high` });
    if (last.c <= Math.min(...closes) && s.length > 60) out.push({ tone: "bad", text: `Nifty is at a ${s.length >= 120 ? "6-month" : `${s.length}-day`} low` });
  }

  if (h.length >= 2) {
    const now = h[h.length - 1].pct;
    const before = h[h.length - 2].pct;
    for (const level of [60, 50, 40]) {
      if (before < level && now >= level) out.push({ tone: "good", text: `Stocks above their 50-day average rose past ${level}% (now ${now.toFixed(0)}%)` });
      if (before >= level && now < level) out.push({ tone: "bad", text: `Stocks above their 50-day average fell below ${level}% (now ${now.toFixed(0)}%)` });
    }
  }

  if (d.sectors_turning_up?.length) out.push({ tone: "good", text: `Turning up: ${d.sectors_turning_up.join(", ")}` });
  if (d.sectors_fading?.length) out.push({ tone: "bad", text: `Fading: ${d.sectors_fading.join(", ")}` });
  if (d.highs_52w_count >= 3 * Math.max(d.lows_52w_count, 1) && d.highs_52w_count >= 10)
    out.push({ tone: "good", text: `${d.highs_52w_count} stocks hit yearly highs against ${d.lows_52w_count} lows` });
  if (d.lows_52w_count >= 3 * Math.max(d.highs_52w_count, 1) && d.lows_52w_count >= 10)
    out.push({ tone: "bad", text: `${d.lows_52w_count} stocks hit yearly lows against ${d.highs_52w_count} highs` });
  return out;
}

export function WhatChanged({ data }: { data: MarketSummary }) {
  const changes = deriveChanges(data);
  const dot = { good: "bg-gain", bad: "bg-loss", info: "bg-muted-foreground" };
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="text-xs text-muted-foreground">What changed since the last session</div>
      {changes.length ? (
        <ul className="mt-3 space-y-2">
          {changes.map((c) => (
            <li key={c.text} className="flex items-start gap-2.5 text-sm">
              <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", dot[c.tone])} />
              <span>{c.text}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="mt-3 text-sm text-muted-foreground">Nothing major. The market is where it was yesterday.</div>
      )}
    </div>
  );
}

// ─── Jump to the screener ────────────────────────────────────────────────────

export function ScreenerJumps({ onOpen }: { onOpen: (p: ScreenPreset) => void }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="text-xs text-muted-foreground">Dig in with the screener</div>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        {SCREEN_PRESETS.map((p) => (
          <button
            key={p.label}
            onClick={() => onOpen(p)}
            className="rounded-lg border border-border px-3.5 py-2.5 text-left transition-colors hover:border-primary/50 hover:bg-muted/40"
          >
            <div className="text-sm font-medium">{p.label} →</div>
            <div className="mt-0.5 text-caption text-muted-foreground">{p.hint}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Sector heatmap ──────────────────────────────────────────────────────────

export function SectorHeatmap({
  sectors,
  pick,
  periodLabel,
}: {
  sectors: SectorMetrics[];
  pick: (s: SectorMetrics) => number;
  periodLabel: string;
}) {
  const [open, setOpen] = useState<string | null>(null);
  if (!sectors.length) return null;
  const maxAbs = Math.max(...sectors.map((s) => Math.abs(pick(s))), 0.01);
  const selected = sectors.find((s) => s.sector === open) ?? null;
  const fill = (v: number) => {
    const a = 0.18 + 0.62 * Math.min(Math.abs(v) / maxAbs, 1);
    return v >= 0 ? `rgba(16,185,129,${a})` : `rgba(239,68,68,${a})`;
  };
  // Biggest sectors first so the large tiles anchor each row.
  const ordered = [...sectors].sort((a, b) => b.stock_count - a.stock_count);

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-muted-foreground">Sector heatmap, {periodLabel.toLowerCase()}</span>
        <span className="text-caption text-muted-foreground/70">Tile size = number of stocks. Click a tile for detail.</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {ordered.map((s) => {
          const v = pick(s);
          return (
            <button
              key={s.sector}
              onClick={() => setOpen(open === s.sector ? null : s.sector)}
              title={`${s.sector}: ${v > 0 ? "+" : ""}${v.toFixed(2)}%`}
              className={cn(
                "flex min-h-[72px] flex-col justify-between rounded-md p-2.5 text-left transition-transform hover:scale-[1.02]",
                open === s.sector && "ring-2 ring-primary",
              )}
              style={{ flex: `${Math.max(s.stock_count, 4)} 1 ${Math.max(Math.round(s.stock_count * 3), 96)}px`, background: fill(v) }}
            >
              <span className="truncate text-xs font-medium">{s.sector}</span>
              <span className="text-sm font-semibold tabular-nums">{v > 0 ? "+" : ""}{v.toFixed(2)}%</span>
            </button>
          );
        })}
      </div>

      {selected ? (
        <div className="mt-4 grid gap-4 border-t border-border pt-4 sm:grid-cols-[1fr_1fr]">
          <div className="space-y-1.5 text-sm">
            <div className="font-medium">{selected.sector}</div>
            <div className="text-muted-foreground">
              {selected.advancing_count} rising, {selected.declining_count} falling out of {selected.stock_count}
            </div>
            <div className="text-muted-foreground">{selected.above_ema50_pct.toFixed(0)}% above their 50-day average</div>
            <div className="text-muted-foreground">Trading at {selected.volume_multiple.toFixed(1)}x normal volume</div>
            {selected.breakout_count > 0 ? <div className="text-primary">{selected.breakout_count} breaking out</div> : null}
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Leading stocks</div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {(selected.top_stocks ?? []).slice(0, 8).map((t, i) => {
                const sym = String((t as Record<string, unknown>).symbol ?? "").replace("-EQ", "");
                return sym ? (
                  <span key={`${sym}-${i}`} className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium">{sym}</span>
                ) : null;
              })}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
