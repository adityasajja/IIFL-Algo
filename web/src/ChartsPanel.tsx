import { useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { API_URL, getCandles, type Candle } from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Switch } from "./components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { cn } from "./lib/utils";

type BarTime = string | UTCTimestamp;
type Theme = "dark" | "light";

function sma(values: number[], window: number): (number | null)[] {
  return values.map((_, i) =>
    i + 1 >= window
      ? values.slice(i + 1 - window, i + 1).reduce((a, b) => a + b, 0) / window
      : null,
  );
}

function ema(values: number[], span: number): (number | null)[] {
  const k = 2 / (span + 1);
  let e: number | null = null;
  return values.map((v, i) => {
    e = i === 0 ? v : (v - (e as number)) * k + (e as number);
    return i + 1 >= span ? e : null;
  });
}

function bollinger(values: number[], window = 20, mult = 2) {
  const mid = sma(values, window);
  const sd = values.map((_, i) => {
    if (i + 1 < window) return null;
    const slice = values.slice(i + 1 - window, i + 1);
    const m = slice.reduce((a, b) => a + b, 0) / window;
    return Math.sqrt(slice.reduce((a, b) => a + (b - m) ** 2, 0) / window);
  });
  return {
    mid,
    upper: mid.map((m, i) => (m === null || sd[i] === null ? null : (m as number) + mult * (sd[i] as number))),
    lower: mid.map((m, i) => (m === null || sd[i] === null ? null : (m as number) - mult * (sd[i] as number))),
  };
}

function rsi(values: number[], window = 14): number[] {
  const out: number[] = [];
  let gain = 0;
  let loss = 0;
  values.forEach((v, i) => {
    if (i === 0) {
      out.push(50);
      return;
    }
    const d = v - values[i - 1];
    const g = Math.max(d, 0);
    const l = Math.max(-d, 0);
    if (i <= window) {
      gain += g;
      loss += l;
      out.push(i === window ? 100 - 100 / (1 + gain / Math.max(loss, 1e-9)) : 50);
    } else {
      gain = (gain * (window - 1) + g) / window;
      loss = (loss * (window - 1) + l) / window;
      out.push(loss === 0 ? 100 : 100 - 100 / (1 + gain / loss));
    }
  });
  return out;
}

interface SymbolHit {
  symbol: string;
  exchange: string;
  conid: string;
}

interface WatchRow {
  symbol: string;
  last: number;
  chg: number;
}

const DEFAULT_WATCH = [
  "NIFTYBEES-EQ",
  "RELIANCE-EQ",
  "HDFCBANK-EQ",
  "INFY-EQ",
  "TCS-EQ",
  "SBIN-EQ",
  "KOTAKBANK-EQ",
  "ICICIBANK-EQ",
];

const INTERVALS = [
  { v: "5m", label: "5m" },
  { v: "15m", label: "15m" },
  { v: "60m", label: "1H" },
  { v: "1d", label: "D" },
  { v: "1w", label: "W" },
  { v: "1mo", label: "M" },
];

const RANGES = [
  { label: "1M", months: 1 },
  { label: "3M", months: 3 },
  { label: "6M", months: 6 },
  { label: "1Y", months: 12 },
  { label: "All", months: 60 },
];

function fmtDate(d: Date): string {
  const m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${String(d.getDate()).padStart(2, "0")}-${m[d.getMonth()]}-${d.getFullYear()}`;
}

function defaultDates() {
  const to = new Date();
  const from = new Date(to);
  from.setMonth(from.getMonth() - 3);
  return { from: fmtDate(from), to: fmtDate(to) };
}

function themeOpts(theme: Theme) {
  const dark = theme === "dark";
  return {
    bg: dark ? "#161a23" : "#ffffff",
    text: dark ? "#8b93a2" : "#697386",
    grid: dark ? "#262b36" : "#e3e6ec",
    border: dark ? "#262b36" : "#e3e6ec",
  };
}

export default function ChartsPanel({ theme = "dark" }: { theme?: "dark" | "light" }) {
  const [symbol, setSymbol] = useState("RELIANCE-EQ");
  const [query, setQuery] = useState("RELIANCE-EQ");
  const [hits, setHits] = useState<SymbolHit[]>([]);
  const [showHits, setShowHits] = useState(false);
  const [interval, setInterval] = useState("1d");
  const initialDates = useMemo(defaultDates, []);
  const [fromDate, setFromDate] = useState(initialDates.from);
  const [toDate, setToDate] = useState(initialDates.to);
  const [data, setData] = useState<Candle[] | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSma, setShowSma] = useState(true);
  const [showEma, setShowEma] = useState(true);
  const [showBb, setShowBb] = useState(true);
  const [legend, setLegend] = useState("");
  const [watch, setWatch] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("atr.watch") ?? "null") ?? DEFAULT_WATCH;
    } catch {
      return DEFAULT_WATCH;
    }
  });
  const [quotes, setQuotes] = useState<Record<string, WatchRow>>({});

  const priceRef = useRef<HTMLDivElement>(null);
  const rsiRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    localStorage.setItem("atr.watch", JSON.stringify(watch));
  }, [watch]);

  // symbol search (cached master, no session needed)
  useEffect(() => {
    if (query.trim().length < 2) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      fetch(
        `${API_URL}/symbols?query=${encodeURIComponent(query.trim().toUpperCase())}&limit=8`,
      )
        .then((r) => (r.ok ? r.json() : { results: [] }))
        .then((j) => setHits(j.results ?? []))
        .catch(() => setHits([]));
    }, 250);
    return () => clearTimeout(t);
  }, [query]);

  // watchlist quotes off the scan cache (fast, no session needed)
  useEffect(() => {
    fetch(`${API_URL}/scan-all`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j?.rows) return;
        const q: Record<string, WatchRow> = {};
        for (const r of j.rows) q[r.symbol] = { symbol: r.symbol, last: r.last, chg: r.day_chg_pct };
        setQuotes(q);
      })
      .catch(() => undefined);
  }, []);

  // prefill from the scanner's "open chart" button
  useEffect(() => {
    let pending: string | null = null;
    try {
      pending = sessionStorage.getItem("atr.chartSymbol");
      sessionStorage.removeItem("atr.chartSymbol");
    } catch {
      /* ignore */
    }
    if (pending) void load(pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const model = useMemo(() => {
    if (!data) return null;
    const stepped = interval !== "1d";
    const closes = data.map((d) => d.close);
    const bb = bollinger(closes);
    return {
      stepped,
      times: data.map((d): BarTime =>
        stepped
          ? (Math.floor(new Date(d.ts).getTime() / 1000) as UTCTimestamp)
          : d.ts.slice(0, 10),
      ),
      s20: sma(closes, 20),
      s50: sma(closes, 50),
      e9: ema(closes, 9),
      e21: ema(closes, 21),
      bb,
      r: rsi(closes),
    };
  }, [data, interval]);

  // build charts
  useEffect(() => {
    if (!data || !model || !priceRef.current || !rsiRef.current) return;
    const t = themeOpts(theme);
    const priceChart: IChartApi = createChart(priceRef.current, {
      layout: { background: { type: ColorType.Solid, color: t.bg }, textColor: t.text, fontSize: 11 },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: t.border },
      timeScale: { borderColor: t.border, timeVisible: true },
      height: 430,
    });
    const rsiChart: IChartApi = createChart(rsiRef.current, {
      layout: { background: { type: ColorType.Solid, color: t.bg }, textColor: t.text, fontSize: 11 },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: t.border },
      timeScale: { borderColor: t.border, timeVisible: true },
      height: 130,
    });

    const candles: ISeriesApi<"Candlestick"> = priceChart.addCandlestickSeries({
      upColor: "#26a69a",
      downColor: "#ef5350",
      wickUpColor: "#26a69a",
      wickDownColor: "#ef5350",
      borderVisible: false,
    });
    candles.setData(
      data.map((d, i) => ({
        time: model.times[i], open: d.open, high: d.high, low: d.low, close: d.close,
      })),
    );

    const addLine = (
      chart: IChartApi, values: (number | null)[], color: string, width = 1,
    ) => {
      const s = chart.addLineSeries({
        color, lineWidth: width as 1, priceLineVisible: false, lastValueVisible: false,
      });
      s.setData(
        data.flatMap((_, i) =>
          values[i] === null ? [] : [{ time: model.times[i], value: values[i] as number }],
        ),
      );
    };
    if (showSma) {
      addLine(priceChart, model.s20, "#f5a524");
      addLine(priceChart, model.s50, "#5b8def");
    }
    if (showEma) {
      addLine(priceChart, model.e9, "#00bcd4");
      addLine(priceChart, model.e21, "#e040fb");
    }
    if (showBb) {
      addLine(priceChart, model.bb.upper, "#ef5350");
      addLine(priceChart, model.bb.mid, "#9aa4b2");
      addLine(priceChart, model.bb.lower, "#26a69a");
    }

    const vol: ISeriesApi<"Histogram"> = priceChart.addHistogramSeries({
      priceScaleId: "",
      priceFormat: { type: "volume" },
    });
    priceChart.priceScale("").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    vol.setData(
      data.map((d, i) => ({
        time: model.times[i],
        value: d.volume,
        color: d.close >= d.open ? "#26a69a88" : "#ef535088",
      })),
    );

    const rsiLine: ISeriesApi<"Line"> = rsiChart.addLineSeries({
      color: "#b48ef2", lineWidth: 1, priceLineVisible: false,
    });
    rsiLine.setData(data.map((_, i) => ({ time: model.times[i], value: model.r[i] })));
    rsiLine.createPriceLine({ price: 70, color: "#5c2528", lineStyle: 2, lineWidth: 1, title: "" });
    rsiLine.createPriceLine({ price: 30, color: "#5c2528", lineStyle: 2, lineWidth: 1, title: "" });
    rsiChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.15, bottom: 0.15 } });

    // OHLC legend follows the crosshair
    const timeIndex = new Map<BarTime, number>(model.times.map((tm, i) => [tm, i]));
    const showBar = (i: number) => {
      const d = data[i];
      if (!d) return;
      const chg = ((d.close / d.open - 1) * 100).toFixed(2);
      setLegend(
        `O ${d.open}  H ${d.high}  L ${d.low}  C ${d.close}  ${Number(chg) >= 0 ? "+" : ""}${chg}%`,
      );
    };
    showBar(data.length - 1);
    priceChart.subscribeCrosshairMove((param) => {
      if (!param.time) return;
      const i = timeIndex.get(param.time as BarTime);
      if (i !== undefined) showBar(i);
    });

    let syncing = false;
    const link = (a: IChartApi, b: IChartApi) => {
      a.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (syncing || !range) return;
        syncing = true;
        b.timeScale().setVisibleLogicalRange(range);
        syncing = false;
      });
      a.subscribeCrosshairMove((param) => {
        if (syncing || !param.time) return;
        syncing = true;
        b.setCrosshairPosition(param.point?.y ?? 50, param.time, rsiLine);
        syncing = false;
      });
    };
    link(priceChart, rsiChart);
    link(rsiChart, priceChart);

    priceChart.timeScale().fitContent();
    const ro = new ResizeObserver((entries) => {
      const w = entries[0].contentRect.width;
      priceChart.applyOptions({ width: w });
      rsiChart.applyOptions({ width: w });
    });
    if (priceRef.current.parentElement) ro.observe(priceRef.current.parentElement);

    return () => {
      ro.disconnect();
      priceChart.remove();
      rsiChart.remove();
    };
  }, [data, model, theme, showSma, showEma, showBb]);

  async function load(sym?: string) {
    const target = (sym ?? symbol).trim().toUpperCase();
    setBusy(true);
    setError(null);
    setShowHits(false);
    try {
      const res = await getCandles({
        symbol: target, interval, from_date: fromDate.trim(), to_date: toDate.trim(),
      });
      if (res.candles.length === 0) throw new Error("no candles returned");
      setSymbol(target);
      setData(res.candles);
      setTitle(`${res.symbol} · ${res.exchange} · ${res.interval}`);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function preset(months: number) {
    const to = new Date();
    const from = new Date(to);
    from.setMonth(from.getMonth() - months);
    setFromDate(fmtDate(from));
    setToDate(fmtDate(to));
  }

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
        <Tabs value={interval} onValueChange={setInterval} variant="segment">
          <TabsList>
            {INTERVALS.map((o) => (
              <TabsTrigger key={o.v} value={o.v}>
                {o.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <span className="hidden h-5 w-px bg-border sm:inline-block" />
        <Switch checked={showSma} onCheckedChange={setShowSma} label="SMA" />
        <Switch checked={showEma} onCheckedChange={setShowEma} label="EMA ribbon" />
        <Switch checked={showBb} onCheckedChange={setShowBb} label="Bollinger" />
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_240px]">
        <div className="min-w-0">
          <div className="grid gap-3.5 sm:grid-cols-3">
            <div className="relative">
              <Input
                label="Symbol"
                value={query}
                onChange={(v) => {
                  setQuery(v);
                  setShowHits(true);
                }}
                placeholder="Search 2600+ NSE names…"
              />
              {showHits && hits.length > 0 && (
                <div className="absolute inset-x-0 top-full z-30 mt-1.5 overflow-hidden rounded-xl border border-border bg-card shadow-2xl">
                  {hits.map((h) => (
                    <button
                      key={`${h.exchange}:${h.symbol}`}
                      type="button"
                      onClick={() => {
                        setQuery(h.symbol);
                        void load(h.symbol);
                      }}
                      className="flex w-full items-center justify-between px-3.5 py-2 text-left text-[13px] transition-colors hover:bg-primary/[0.06]"
                    >
                      <span className="font-medium">{h.symbol}</span>
                      <span className="text-xs text-muted-foreground">{h.exchange}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <Input label="From (dd-MMM-yyyy)" value={fromDate} onChange={setFromDate} />
            <Input label="To (dd-MMM-yyyy)" value={toDate} onChange={setToDate} />
          </div>

          <div className="mt-3.5 flex flex-wrap items-center gap-2">
            <Button disabled={busy} onClick={() => void load()}>
              {busy ? "Loading…" : "Load chart"}
            </Button>
            {RANGES.map((r) => (
              <Button key={r.label} size="sm" variant="ghost" onClick={() => preset(r.months)}>
                {r.label}
              </Button>
            ))}
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                if (!watch.includes(symbol)) setWatch((w) => [...w, symbol]);
              }}
            >
              + Watch
            </Button>
          </div>

          {error && (
            <div className="mt-3">
              <ErrorBox>{error}</ErrorBox>
            </div>
          )}

          {data && (
            <div className="mt-3.5">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <strong className="text-sm font-semibold">{title}</strong>
                <span className="font-mono text-xs text-muted-foreground">{legend}</span>
              </div>
              <div ref={priceRef} className="mt-2 [&_canvas]:rounded-xl" />
              <div ref={rsiRef} className="mt-2 [&_canvas]:rounded-xl" />
              <Hint className="mt-2">Drag to pan · scroll to zoom · right-click to reset</Hint>
            </div>
          )}
          {!data && !error && (
            <Hint className="mt-3.5">Search any NSE name, or pick from the watchlist →</Hint>
          )}
        </div>

        <aside className="min-w-0 rounded-xl border border-border bg-background/40 p-3">
          <h4 className="px-1 pb-2 text-xs font-semibold uppercase tracking-[0.06em] text-muted-foreground">
            Watchlist
          </h4>
          <div className="space-y-0.5">
            {watch.map((w) => {
              const q = quotes[w];
              const up = (q?.chg ?? 0) >= 0;
              return (
                <div
                  key={w}
                  className={cn(
                    "group flex items-center gap-1 rounded-lg px-1 py-0.5 transition-colors",
                    w === symbol ? "bg-primary/[0.08]" : "hover:bg-primary/[0.04]",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => {
                      setQuery(w);
                      void load(w);
                    }}
                    className="grid min-w-0 flex-1 grid-cols-[1fr_auto_auto] items-center gap-2 px-1.5 py-1.5 text-left text-[13px]"
                  >
                    <span className="truncate font-medium">{w.replace("-EQ", "")}</span>
                    <span className="tabular-nums text-muted-foreground">
                      {q ? q.last.toLocaleString("en-IN") : "…"}
                    </span>
                    <span
                      className={cn(
                        "tabular-nums",
                        up ? "text-emerald-600 dark:text-emerald-400" : "text-destructive",
                      )}
                    >
                      {q ? `${up ? "+" : ""}${q.chg.toFixed(2)}%` : ""}
                    </span>
                  </button>
                  <button
                    type="button"
                    title="remove"
                    onClick={() => setWatch((prev) => prev.filter((s) => s !== w))}
                    className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-muted-foreground opacity-0 transition-all hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100"
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </aside>
      </div>
    </Card>
  );
}
