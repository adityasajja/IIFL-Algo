import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import {
  ChevronDown,
  Maximize2,
  Minimize2,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { API_URL, getCandles, placeOrder, type Candle } from "./api";
import { Button } from "./components/ui/button";
import { useLiveTicks } from "./lib/useLiveTicks";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

type BarTime = string | UTCTimestamp;

interface WatchRow {
  symbol: string;
  name?: string;
  last: number;
  chg: number;
  chg_pts?: number;
  vol?: number;
}

const DEFAULT_WATCH: WatchRow[] = [
  { symbol: "RELIANCE-EQ", name: "Reliance Industries", last: 2980.5, chg: 0.85, chg_pts: 25.1, vol: 6540000 },
  { symbol: "HDFCBANK-EQ", name: "HDFC Bank", last: 1650.0, chg: -0.32, chg_pts: -5.3, vol: 9200000 },
  { symbol: "INFY-EQ", name: "Infosys Ltd", last: 1820.25, chg: 1.15, chg_pts: 20.7, vol: 4800000 },
  { symbol: "TCS-EQ", name: "Tata Consultancy", last: 4210.0, chg: 0.45, chg_pts: 18.9, vol: 2300000 },
  { symbol: "SBIN-EQ", name: "State Bank of India", last: 815.4, chg: -0.65, chg_pts: -5.3, vol: 11400000 },
  { symbol: "NIFTYBEES-EQ", name: "Nifty 50 ETF", last: 265.8, chg: 0.28, chg_pts: 0.74, vol: 3500000 },
];

const TIMEFRAMES = [
  { v: "5m", label: "5m" },
  { v: "15m", label: "15m" },
  { v: "60m", label: "1h" },
  { v: "1d", label: "D" },
  { v: "1w", label: "W" },
  { v: "1mo", label: "M" },
];

const RANGES = [
  { label: "1D", days: 1 },
  { label: "5D", days: 5 },
  { label: "1M", months: 1 },
  { label: "3M", months: 3 },
  { label: "6M", months: 6 },
  { label: "YTD", ytd: true },
  { label: "1Y", months: 12 },
  { label: "5Y", months: 60 },
  { label: "All", months: 120 },
];

function fmtDate(d: Date): string {
  const m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${String(d.getDate()).padStart(2, "0")}-${m[d.getMonth()]}-${d.getFullYear()}`;
}

// Indicator calculators
function sma(values: number[], window: number): (number | null)[] {
  return values.map((_, i) =>
    i + 1 >= window ? values.slice(i + 1 - window, i + 1).reduce((a, b) => a + b, 0) / window : null,
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

export default function ChartsPanel({ theme = "dark" }: { theme?: "dark" | "light" }) {
  const { toast } = useToast();
  const [symbol, setSymbol] = useState("RELIANCE-EQ");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchResults, setSearchResults] = useState<{ symbol: string; exchange: string }[]>([]);
  const [timeframe, setTimeframe] = useState("1d");
  const [activeRange, setActiveRange] = useState("3M");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Watchlist Add Symbol Modal State
  const [addSymbolOpen, setAddSymbolOpen] = useState(false);
  const [addSymbolQuery, setAddSymbolQuery] = useState("");
  const [addSymbolResults, setAddSymbolResults] = useState<{ symbol: string; exchange: string }[]>([]);

  // Indicators toggle
  const [showIndicatorsMenu, setShowIndicatorsMenu] = useState(false);
  const [showRibbon, setShowRibbon] = useState(true);
  const [showEMA200, setShowEMA200] = useState(true);
  const [showBB, setShowBB] = useState(true);
  const [showRSI, setShowRSI] = useState(true);
  const [showVolume, setShowVolume] = useState(true);

  // Active hover OHLC stats
  const [hoverData, setHoverData] = useState<{
    open: number;
    high: number;
    low: number;
    close: number;
    change: number;
    volume: number;
  } | null>(null);

  // Watchlist
  const [watchlist, setWatchlist] = useState<WatchRow[]>(() => {
    try {
      const saved = localStorage.getItem("atr.tv.watchlist");
      return saved ? JSON.parse(saved) : DEFAULT_WATCH;
    } catch {
      return DEFAULT_WATCH;
    }
  });

  // Fullscreen state
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartWrapperRef = useRef<HTMLDivElement>(null);
  const priceChartRef = useRef<HTMLDivElement>(null);
  const rsiChartRef = useRef<HTMLDivElement>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const rsiApiRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  // Live price tick
  const { getTick } = useLiveTicks(useMemo(() => [symbol], [symbol]));
  const liveTick = getTick(symbol);

  // Save watchlist
  useEffect(() => {
    localStorage.setItem("atr.tv.watchlist", JSON.stringify(watchlist));
  }, [watchlist]);

  // Search autocomplete
  useEffect(() => {
    if (searchQuery.trim().length < 2) {
      setSearchResults([]);
      return;
    }
    const t = setTimeout(() => {
      fetch(`${API_URL}/symbols?query=${encodeURIComponent(searchQuery.trim().toUpperCase())}&limit=8`)
        .then((r) => (r.ok ? r.json() : { results: [] }))
        .then((j) => setSearchResults(j.results ?? []))
        .catch(() => setSearchResults([]));
    }, 200);
    return () => clearTimeout(t);
  }, [searchQuery]);

  // Watchlist Add Symbol search autocomplete
  useEffect(() => {
    if (addSymbolQuery.trim().length < 2) {
      setAddSymbolResults([]);
      return;
    }
    const t = setTimeout(() => {
      fetch(`${API_URL}/symbols?query=${encodeURIComponent(addSymbolQuery.trim().toUpperCase())}&limit=12`)
        .then((r) => (r.ok ? r.json() : { results: [] }))
        .then((j) => setAddSymbolResults(j.results ?? []))
        .catch(() => setAddSymbolResults([]));
    }, 200);
    return () => clearTimeout(t);
  }, [addSymbolQuery]);

  // Load candle data
  const loadCandles = useCallback(
    async (sym?: string, tf?: string, rangeLabel = activeRange) => {
      const targetSym = (sym ?? symbol).trim().toUpperCase();
      const targetTf = tf ?? timeframe;
      setLoading(true);
      setError(null);
      setSearchOpen(false);

      const now = new Date();
      let fromDate = new Date(now);

      switch (rangeLabel) {
        case "1D":
          fromDate.setDate(now.getDate() - 3);
          break;
        case "5D":
          fromDate.setDate(now.getDate() - 8);
          break;
        case "1M":
          fromDate.setMonth(now.getMonth() - 1);
          break;
        case "3M":
          fromDate.setMonth(now.getMonth() - 3);
          break;
        case "6M":
          fromDate.setMonth(now.getMonth() - 6);
          break;
        case "YTD":
          fromDate = new Date(now.getFullYear(), 0, 1);
          break;
        case "1Y":
          fromDate.setFullYear(now.getFullYear() - 1);
          break;
        case "5Y":
          fromDate.setFullYear(now.getFullYear() - 5);
          break;
        case "All":
          fromDate.setFullYear(now.getFullYear() - 10);
          break;
        default:
          fromDate.setMonth(now.getMonth() - 3);
      }

      try {
        const res = await getCandles({
          symbol: targetSym,
          interval: targetTf,
          from_date: fmtDate(fromDate),
          to_date: fmtDate(now),
        });
        if (!res.candles || res.candles.length === 0) {
          throw new Error("No candle data available for this range");
        }
        setSymbol(targetSym);
        setCandles(res.candles);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [symbol, timeframe, activeRange],
  );

  // Check incoming selection from Scanner or Global search
  useEffect(() => {
    let pending: string | null = null;
    try {
      pending = sessionStorage.getItem("atr.chartSymbol");
      sessionStorage.removeItem("atr.chartSymbol");
    } catch {
      // ignore
    }
    if (pending) {
      setSymbol(pending);
      void loadCandles(pending);
    } else if (candles.length === 0) {
      void loadCandles();
    }
  }, [loadCandles, candles.length]);

  // Sync latest live candle update
  useEffect(() => {
    if (!liveTick || !candleSeriesRef.current || candles.length === 0) return;
    const lastBar = candles[candles.length - 1];
    const newClose = liveTick.ltp;
    const isIntraday = timeframe !== "1d";
    const timeVal = isIntraday
      ? (Math.floor(new Date(lastBar.ts).getTime() / 1000) as UTCTimestamp)
      : lastBar.ts.slice(0, 10);

    try {
      candleSeriesRef.current.update({
        time: timeVal,
        open: lastBar.open,
        high: Math.max(lastBar.high, newClose),
        low: Math.min(lastBar.low, newClose),
        close: newClose,
      });
    } catch {
      // ignore
    }
  }, [liveTick, candles, timeframe]);

  // Indicators calculations
  const model = useMemo(() => {
    if (!candles || candles.length === 0) return null;
    const isIntraday = timeframe !== "1d";
    const closes = candles.map((c) => c.close);
    const times = candles.map((c): BarTime =>
      isIntraday
        ? (Math.floor(new Date(c.ts).getTime() / 1000) as UTCTimestamp)
        : c.ts.slice(0, 10),
    );

    return {
      times,
      closes,
      ema9: ema(closes, 9),
      ema21: ema(closes, 21),
      ema50: ema(closes, 50),
      ema200: ema(closes, 200),
      bb: bollinger(closes, 20, 2),
      rsi: rsi(closes, 14),
    };
  }, [candles, timeframe]);

  // Chart Rendering
  useEffect(() => {
    if (!candles || candles.length === 0 || !model || !priceChartRef.current) return;

    const isDark = theme === "dark";
    const bg = isDark ? "#131722" : "#ffffff";
    const text = isDark ? "#787b86" : "#6a6d78";
    const border = isDark ? "#2a2e39" : "#e0e3eb";
    const grid = isDark ? "#1e222d" : "#f0f3fa";

    // Create Main Price Chart
    const priceChart = createChart(priceChartRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: bg },
        textColor: text,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: grid },
        horzLines: { color: grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: isDark ? "#50535e" : "#b2b5be", width: 1, style: 3 },
        horzLine: { color: isDark ? "#50535e" : "#b2b5be", width: 1, style: 3 },
      },
      rightPriceScale: {
        borderColor: border,
        scaleMargins: { top: 0.1, bottom: showVolume ? 0.22 : 0.1 },
      },
      timeScale: {
        borderColor: border,
        timeVisible: timeframe !== "1d",
        secondsVisible: false,
      },
      handleScroll: true,
      handleScale: true,
    });
    chartApiRef.current = priceChart;

    // TradingView Candlestick series
    const candleSeries = priceChart.addCandlestickSeries({
      upColor: "#089981",
      downColor: "#f23645",
      wickUpColor: "#089981",
      wickDownColor: "#f23645",
      borderVisible: false,
    });
    candleSeriesRef.current = candleSeries;
    candleSeries.setData(
      candles.map((c, i) => ({
        time: model.times[i],
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );

    // EMA Ribbon (9, 21, 50)
    if (showRibbon) {
      const e9Series = priceChart.addLineSeries({
        color: "#2962ff",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      e9Series.setData(
        candles.flatMap((_, i) =>
          model.ema9[i] !== null ? [{ time: model.times[i], value: model.ema9[i] as number }] : [],
        ),
      );

      const e21Series = priceChart.addLineSeries({
        color: "#ff6d00",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      e21Series.setData(
        candles.flatMap((_, i) =>
          model.ema21[i] !== null ? [{ time: model.times[i], value: model.ema21[i] as number }] : [],
        ),
      );

      const e50Series = priceChart.addLineSeries({
        color: "#9c27b0",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      e50Series.setData(
        candles.flatMap((_, i) =>
          model.ema50[i] !== null ? [{ time: model.times[i], value: model.ema50[i] as number }] : [],
        ),
      );
    }

    // 200 EMA
    if (showEMA200) {
      const e200Series = priceChart.addLineSeries({
        color: "#ffeb3b",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
      });
      e200Series.setData(
        candles.flatMap((_, i) =>
          model.ema200[i] !== null ? [{ time: model.times[i], value: model.ema200[i] as number }] : [],
        ),
      );
    }

    // Bollinger Bands
    if (showBB) {
      const bbUpper = priceChart.addLineSeries({
        color: "rgba(33, 150, 243, 0.4)",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      bbUpper.setData(
        candles.flatMap((_, i) =>
          model.bb.upper[i] !== null ? [{ time: model.times[i], value: model.bb.upper[i] as number }] : [],
        ),
      );

      const bbLower = priceChart.addLineSeries({
        color: "rgba(33, 150, 243, 0.4)",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      bbLower.setData(
        candles.flatMap((_, i) =>
          model.bb.lower[i] !== null ? [{ time: model.times[i], value: model.bb.lower[i] as number }] : [],
        ),
      );
    }

    // Volume Histogram (overlaid at bottom of price chart like TradingView)
    if (showVolume) {
      const volSeries = priceChart.addHistogramSeries({
        priceScaleId: "volume_scale",
        priceFormat: { type: "volume" },
      });
      priceChart.priceScale("volume_scale").applyOptions({
        scaleMargins: { top: 0.82, bottom: 0 },
      });
      volSeries.setData(
        candles.map((c, i) => ({
          time: model.times[i],
          value: c.volume,
          color: c.close >= c.open ? "rgba(8, 153, 129, 0.25)" : "rgba(242, 54, 69, 0.25)",
        })),
      );
    }

    // RSI Sub-Chart (if enabled)
    let rsiChart: IChartApi | null = null;
    let rsiLineSeries: ISeriesApi<"Line"> | null = null;
    if (showRSI && rsiChartRef.current) {
      rsiChart = createChart(rsiChartRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: bg },
          textColor: text,
          fontSize: 10,
        },
        grid: {
          vertLines: { color: grid },
          horzLines: { color: grid },
        },
        crosshair: {
          mode: CrosshairMode.Normal,
          vertLine: { color: isDark ? "#50535e" : "#b2b5be", width: 1, style: 3 },
          horzLine: { color: isDark ? "#50535e" : "#b2b5be", width: 1, style: 3 },
        },
        rightPriceScale: {
          borderColor: border,
          scaleMargins: { top: 0.1, bottom: 0.1 },
        },
        timeScale: {
          borderColor: border,
          visible: true,
          timeVisible: timeframe !== "1d",
        },
        height: 110,
      });
      rsiApiRef.current = rsiChart;

      rsiLineSeries = rsiChart.addLineSeries({
        color: "#7e57c2",
        lineWidth: 1,
        priceLineVisible: false,
      });
      rsiLineSeries.setData(candles.map((_, i) => ({ time: model.times[i], value: model.rsi[i] })));

      rsiLineSeries.createPriceLine({
        price: 70,
        color: "rgba(242, 54, 69, 0.5)",
        lineStyle: 2,
        lineWidth: 1,
        title: "70",
      });
      rsiLineSeries.createPriceLine({
        price: 30,
        color: "rgba(8, 153, 129, 0.5)",
        lineStyle: 2,
        lineWidth: 1,
        title: "30",
      });

      // Synchronize time scales and crosshair between price and RSI
      let isSyncing = false;
      priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (isSyncing || !range || !rsiChart) return;
        isSyncing = true;
        rsiChart.timeScale().setVisibleLogicalRange(range);
        isSyncing = false;
      });
      rsiChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (isSyncing || !range) return;
        isSyncing = true;
        priceChart.timeScale().setVisibleLogicalRange(range);
        isSyncing = false;
      });

      priceChart.subscribeCrosshairMove((param) => {
        if (isSyncing || !param.time || !rsiChart || !rsiLineSeries) return;
        isSyncing = true;
        rsiChart.setCrosshairPosition(param.point?.y ?? 50, param.time, rsiLineSeries);
        isSyncing = false;
      });
    }

    // Track crosshair hover for TradingView HUD info
    const lastBar = candles[candles.length - 1];
    setHoverData({
      open: lastBar.open,
      high: lastBar.high,
      low: lastBar.low,
      close: lastBar.close,
      change: +((lastBar.close / lastBar.open - 1) * 100).toFixed(2),
      volume: lastBar.volume,
    });

    const timeIndex = new Map<BarTime, number>(model.times.map((tm, i) => [tm, i]));
    priceChart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoverData({
          open: lastBar.open,
          high: lastBar.high,
          low: lastBar.low,
          close: lastBar.close,
          change: +((lastBar.close / lastBar.open - 1) * 100).toFixed(2),
          volume: lastBar.volume,
        });
        return;
      }
      const idx = timeIndex.get(param.time as BarTime);
      if (idx !== undefined && candles[idx]) {
        const c = candles[idx];
        setHoverData({
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
          change: +((c.close / c.open - 1) * 100).toFixed(2),
          volume: c.volume,
        });
      }
    });

    // Auto-fit contents and handle responsive resizing
    priceChart.timeScale().fitContent();

    const handleResize = () => {
      if (!chartWrapperRef.current) return;
      const w = chartWrapperRef.current.clientWidth;
      const totalH = chartWrapperRef.current.clientHeight;
      const rsiH = showRSI ? 110 : 0;
      const priceH = Math.max(totalH - rsiH, 300);

      priceChart.applyOptions({ width: w, height: priceH });
      if (rsiChart) {
        rsiChart.applyOptions({ width: w, height: rsiH });
      }
    };

    const ro = new ResizeObserver(handleResize);
    if (chartWrapperRef.current) ro.observe(chartWrapperRef.current);

    return () => {
      ro.disconnect();
      priceChart.remove();
      if (rsiChart) rsiChart.remove();
    };
  }, [candles, model, theme, showRibbon, showEMA200, showBB, showRSI, showVolume, timeframe]);

  // Quick Order Action
  const handleQuickOrder = async (isBuy: boolean) => {
    const lastPrice = hoverData?.close ?? candles[candles.length - 1]?.close ?? 0;
    try {
      await placeOrder({
        symbol,
        exchange: "NSEEQ",
        quantity: isBuy ? 1 : -1,
        order_type: "MARKET",
        price: lastPrice,
      });
      toast({
        title: `${isBuy ? "BUY" : "SELL"} Order Executed`,
        description: `1 unit of ${symbol} @ ₹${lastPrice}`,
        status: "success",
      });
    } catch (e) {
      toast({
        title: "Order Failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    }
  };

  const currentPrice = hoverData?.close ?? candles[candles.length - 1]?.close ?? 0;
  const currentChg = hoverData?.change ?? 0;
  const isUp = currentChg >= 0;

  return (
    <div
      ref={containerRef}
      className={cn(
        "flex flex-col bg-[#131722] text-[#d1d4dc] font-sans antialiased overflow-hidden select-none",
        isFullscreen ? "fixed inset-0 z-50 h-screen w-screen" : "h-[calc(100vh-80px)] rounded-xl border border-[#2a2e39]"
      )}
    >
      {/* ------------------------------------------------------------- */}
      {/* 1. TOP TRADINGVIEW TOOLBAR                                    */}
      {/* ------------------------------------------------------------- */}
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-[#2a2e39] bg-[#131722] px-3 text-xs">
        <div className="flex items-center gap-2">
          {/* Symbol Search Picker */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setSearchOpen(!searchOpen)}
              className="flex items-center gap-1.5 rounded bg-[#2a2e39]/60 px-2.5 py-1 font-semibold text-white transition-colors hover:bg-[#2a2e39]"
            >
              <Search size={13} className="text-[#787b86]" />
              <span>{symbol.replace("-EQ", "")}</span>
              <span className="text-[10px] text-[#787b86]">NSE</span>
              <ChevronDown size={12} className="text-[#787b86]" />
            </button>

            {searchOpen && (
              <div className="absolute left-0 top-full z-50 mt-1.5 w-72 rounded-lg border border-[#2a2e39] bg-[#1e222d] p-2 shadow-2xl">
                <input
                  type="text"
                  placeholder="Search symbol (e.g. TATA, INFY)..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  autoFocus
                  className="w-full rounded bg-[#131722] px-2.5 py-1.5 text-xs text-white placeholder-[#787b86] outline-none border border-[#2a2e39] focus:border-[#2962ff]"
                />
                <div className="mt-2 max-h-56 overflow-y-auto space-y-0.5">
                  {searchResults.map((r) => (
                    <button
                      key={r.symbol}
                      type="button"
                      onClick={() => {
                        setSymbol(r.symbol);
                        setSearchOpen(false);
                        setSearchQuery("");
                        void loadCandles(r.symbol);
                      }}
                      className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs text-[#d1d4dc] hover:bg-[#2a2e39]"
                    >
                      <span className="font-semibold">{r.symbol}</span>
                      <span className="text-[10px] text-[#787b86]">{r.exchange}</span>
                    </button>
                  ))}
                  {searchResults.length === 0 && searchQuery.length >= 2 && (
                    <div className="py-3 text-center text-xs text-[#787b86]">No symbols found</div>
                  )}
                </div>
              </div>
            )}
          </div>

          <div className="h-4 w-px bg-[#2a2e39]" />

          {/* Quick Buy / Sell Execution Pills (Like TV Pro) */}
          <div className="flex items-center rounded border border-[#2a2e39] overflow-hidden">
            <button
              type="button"
              onClick={() => void handleQuickOrder(false)}
              className="flex items-center gap-1.5 bg-[#f23645]/15 px-2.5 py-1 text-xs font-semibold text-[#f23645] hover:bg-[#f23645]/25 transition-colors"
            >
              <span>SELL</span>
              <span className="tabular-nums font-mono">{currentPrice.toFixed(2)}</span>
            </button>
            <div className="w-px bg-[#2a2e39] self-stretch" />
            <button
              type="button"
              onClick={() => void handleQuickOrder(true)}
              className="flex items-center gap-1.5 bg-[#089981]/15 px-2.5 py-1 text-xs font-semibold text-[#089981] hover:bg-[#089981]/25 transition-colors"
            >
              <span>BUY</span>
              <span className="tabular-nums font-mono">{currentPrice.toFixed(2)}</span>
            </button>
          </div>

          <div className="h-4 w-px bg-[#2a2e39]" />

          {/* Timeframe selector (5m, 15m, 1h, D, W, M) */}
          <div className="flex items-center gap-0.5">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf.v}
                type="button"
                onClick={() => {
                  setTimeframe(tf.v);
                  void loadCandles(symbol, tf.v);
                }}
                className={cn(
                  "rounded px-2 py-1 font-medium transition-colors",
                  timeframe === tf.v
                    ? "bg-[#2962ff] text-white font-semibold"
                    : "text-[#787b86] hover:bg-[#2a2e39] hover:text-white"
                )}
              >
                {tf.label}
              </button>
            ))}
          </div>

          <div className="h-4 w-px bg-[#2a2e39]" />

          {/* Indicators Dropdown */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowIndicatorsMenu(!showIndicatorsMenu)}
              className={cn(
                "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors",
                showIndicatorsMenu ? "bg-[#2a2e39] text-white" : "text-[#d1d4dc] hover:bg-[#2a2e39]"
              )}
            >
              <SlidersHorizontal size={13} className="text-[#2962ff]" />
              <span>Indicators</span>
              <ChevronDown size={11} className="text-[#787b86]" />
            </button>

            {showIndicatorsMenu && (
              <div className="absolute left-0 top-full z-50 mt-1.5 w-60 rounded-lg border border-[#2a2e39] bg-[#1e222d] p-2.5 shadow-2xl space-y-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-[#787b86]">
                  Active Indicators
                </div>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-[#2962ff]" />
                    EMA Ribbon (9, 21, 50)
                  </span>
                  <input
                    type="checkbox"
                    checked={showRibbon}
                    onChange={(e) => setShowRibbon(e.target.checked)}
                    className="accent-[#2962ff]"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-[#ffeb3b]" />
                    200 EMA
                  </span>
                  <input
                    type="checkbox"
                    checked={showEMA200}
                    onChange={(e) => setShowEMA200(e.target.checked)}
                    className="accent-[#ffeb3b]"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-[#2196f3]" />
                    Bollinger Bands
                  </span>
                  <input
                    type="checkbox"
                    checked={showBB}
                    onChange={(e) => setShowBB(e.target.checked)}
                    className="accent-[#2196f3]"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-[#7e57c2]" />
                    RSI (14) Pane
                  </span>
                  <input
                    type="checkbox"
                    checked={showRSI}
                    onChange={(e) => setShowRSI(e.target.checked)}
                    className="accent-[#7e57c2]"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-[#089981]" />
                    Volume Overlay
                  </span>
                  <input
                    type="checkbox"
                    checked={showVolume}
                    onChange={(e) => setShowVolume(e.target.checked)}
                    className="accent-[#089981]"
                  />
                </label>
              </div>
            )}
          </div>
        </div>

        {/* Right Toolbar: Refresh, Fullscreen */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void loadCandles()}
            disabled={loading}
            title="Reload candles"
            className="rounded p-1 text-[#787b86] hover:bg-[#2a2e39] hover:text-white transition-colors"
          >
            <RefreshCw size={13} className={cn(loading && "animate-spin text-[#2962ff]")} />
          </button>
          <button
            type="button"
            onClick={() => setIsFullscreen(!isFullscreen)}
            title={isFullscreen ? "Exit Fullscreen" : "Fullscreen Chart"}
            className="rounded p-1 text-[#787b86] hover:bg-[#2a2e39] hover:text-white transition-colors"
          >
            {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>
      </div>

      {/* ------------------------------------------------------------- */}
      {/* 2. MAIN BODY: LEFT CHART + RIGHT WATCHLIST & DETAILS          */}
      {/* ------------------------------------------------------------- */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Left Interactive Canvas Area */}
        <div className="flex flex-1 flex-col min-w-0 relative">
          {/* TradingView Legend & OHLC HUD Bar */}
          <div className="absolute top-2 left-3 z-10 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] font-mono pointer-events-none bg-[#131722]/85 px-2 py-1 rounded border border-[#2a2e39]/60 backdrop-blur">
            <span className="font-semibold text-white">{symbol.replace("-EQ", "")}</span>
            <span className="text-[#787b86]">· {timeframe.toUpperCase()}</span>
            <span className="text-[#787b86]">· NSE</span>
            {hoverData && (
              <>
                <span className="text-[#787b86]">
                  O <span className="text-white">{hoverData.open.toFixed(2)}</span>
                </span>
                <span className="text-[#787b86]">
                  H <span className="text-white">{hoverData.high.toFixed(2)}</span>
                </span>
                <span className="text-[#787b86]">
                  L <span className="text-white">{hoverData.low.toFixed(2)}</span>
                </span>
                <span className="text-[#787b86]">
                  C <span className={cn(isUp ? "text-[#089981]" : "text-[#f23645]")}>{hoverData.close.toFixed(2)}</span>
                </span>
                <span className={cn("font-semibold", isUp ? "text-[#089981]" : "text-[#f23645]")}>
                  {isUp ? `+${hoverData.change}%` : `${hoverData.change}%`}
                </span>
                {showVolume && (
                  <span className="text-[#787b86]">
                    Vol <span className="text-white">{(hoverData.volume / 100000).toFixed(2)}L</span>
                  </span>
                )}
              </>
            )}
          </div>

          {/* Chart Wrapper Container */}
          <div ref={chartWrapperRef} className="flex-1 flex flex-col min-h-0 w-full relative">
            {error ? (
              <div className="m-auto text-center">
                <div className="text-sm font-semibold text-[#f23645]">Failed to load candles</div>
                <div className="text-xs text-[#787b86] mt-1">{error}</div>
                <Button size="sm" variant="secondary" onClick={() => void loadCandles()} className="mt-3">
                  Retry
                </Button>
              </div>
            ) : (
              <>
                <div ref={priceChartRef} className="flex-1 w-full min-h-[300px]" />
                {showRSI && (
                  <div className="border-t border-[#2a2e39] relative">
                    <span className="absolute top-1 left-2 z-10 text-[10px] font-mono text-[#7e57c2] font-semibold">
                      RSI (14)
                    </span>
                    <div ref={rsiChartRef} className="w-full h-[110px]" />
                  </div>
                )}
              </>
            )}
          </div>

          {/* Bottom Date Range Bar (1D, 5D, 1M, 3M, 6M, YTD, 1Y, 5Y, ALL) */}
          <div className="flex h-8 shrink-0 items-center justify-between border-t border-[#2a2e39] bg-[#131722] px-3 text-[11px]">
            <div className="flex items-center gap-1">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  type="button"
                  onClick={() => {
                    setActiveRange(r.label);
                    void loadCandles(symbol, timeframe, r.label);
                  }}
                  className={cn(
                    "rounded px-2 py-0.5 font-medium transition-colors",
                    activeRange === r.label
                      ? "bg-[#2a2e39] text-white font-semibold"
                      : "text-[#787b86] hover:bg-[#2a2e39]/60 hover:text-white"
                  )}
                >
                  {r.label}
                </button>
              ))}
            </div>
            <div className="text-[10px] text-[#787b86]">
              IST (UTC+5:30) · Realtime Data Feed
            </div>
          </div>
        </div>

        {/* ----------------------------------------------------------- */}
        {/* 3. RIGHT SIDEBAR: WATCHLIST & INSTRUMENT DETAILS            */}
        {/* ----------------------------------------------------------- */}
        <div className="w-80 shrink-0 border-l border-[#2a2e39] bg-[#131722] flex flex-col">
          {/* Top Watchlist Header with Add Symbol */}
          <div className="relative flex h-9 items-center justify-between border-b border-[#2a2e39] px-3 text-xs font-semibold uppercase tracking-wider text-[#787b86]">
            <span>Watchlist</span>
            <button
              type="button"
              title="Add symbol to watchlist"
              onClick={() => {
                setAddSymbolOpen((prev) => !prev);
                setAddSymbolQuery("");
              }}
              className={cn(
                "rounded p-1 transition-colors",
                addSymbolOpen ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
              )}
            >
              <Plus size={14} />
            </button>

            {/* Add Symbol Dropdown / Search Modal */}
            {addSymbolOpen && (
              <div className="absolute right-2 top-9 z-50 w-72 rounded-lg border border-[#2a2e39] bg-[#1e222d] p-2.5 shadow-2xl normal-case">
                <div className="flex items-center justify-between border-b border-[#2a2e39] pb-2 text-xs font-semibold text-white">
                  <span>Add Symbol</span>
                  <button
                    type="button"
                    onClick={() => setAddSymbolOpen(false)}
                    className="rounded p-0.5 text-[#787b86] hover:bg-[#2a2e39] hover:text-white"
                  >
                    <X size={13} />
                  </button>
                </div>
                <div className="relative mt-2">
                  <input
                    type="text"
                    placeholder="Search e.g. TATAMOTORS, INFY..."
                    value={addSymbolQuery}
                    onChange={(e) => setAddSymbolQuery(e.target.value)}
                    autoFocus
                    className="w-full rounded bg-[#131722] pl-7 pr-2.5 py-1.5 text-xs text-white placeholder-[#787b86] outline-none border border-[#2a2e39] focus:border-[#2962ff]"
                  />
                  <Search size={12} className="absolute left-2 top-2 text-[#787b86]" />
                </div>

                <div className="mt-2 max-h-52 overflow-y-auto divide-y divide-[#2a2e39]/50">
                  {addSymbolResults.length > 0 ? (
                    addSymbolResults.map((r) => {
                      const alreadyInWatch = watchlist.some((w) => w.symbol === r.symbol);
                      return (
                        <button
                          key={r.symbol}
                          type="button"
                          onClick={() => {
                            if (!alreadyInWatch) {
                              setWatchlist((prev) => [
                                ...prev,
                                {
                                  symbol: r.symbol,
                                  name: r.symbol.replace("-EQ", ""),
                                  last: 0,
                                  chg: 0,
                                },
                              ]);
                            }
                            setSymbol(r.symbol);
                            void loadCandles(r.symbol);
                            setAddSymbolOpen(false);
                            setAddSymbolQuery("");
                          }}
                          className="flex w-full items-center justify-between px-2 py-1.5 text-left text-xs transition-colors hover:bg-[#2a2e39]"
                        >
                          <div>
                            <span className="font-semibold text-white">{r.symbol}</span>
                            <span className="ml-1.5 text-[10px] text-[#787b86]">{r.exchange}</span>
                          </div>
                          {alreadyInWatch ? (
                            <span className="text-[10px] text-[#787b86]">Added</span>
                          ) : (
                            <span className="text-[10px] text-[#2962ff] font-semibold hover:underline">+ Add</span>
                          )}
                        </button>
                      );
                    })
                  ) : addSymbolQuery.trim().length >= 2 ? (
                    <div className="py-4 text-center text-xs text-[#787b86]">No symbols found</div>
                  ) : (
                    <div className="py-3 text-center text-[11px] text-[#787b86]">
                      Type 2+ characters to search NSE symbols
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Watchlist Items */}
          <div className="flex-1 overflow-y-auto divide-y divide-[#2a2e39]/40">
            {watchlist.map((item) => {
              const active = item.symbol === symbol;
              const up = item.chg >= 0;
              return (
                <div
                  key={item.symbol}
                  onClick={() => {
                    setSymbol(item.symbol);
                    void loadCandles(item.symbol);
                  }}
                  className={cn(
                    "group flex items-center justify-between px-3 py-2 cursor-pointer transition-colors text-xs",
                    active ? "bg-[#2a2e39]" : "hover:bg-[#1e222d]"
                  )}
                >
                  <div className="min-w-0 pr-2">
                    <div className="font-semibold text-white truncate">{item.symbol.replace("-EQ", "")}</div>
                    <div className="text-[10px] text-[#787b86] truncate">{item.name ?? "Equity"}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="text-right tabular-nums">
                      <div className="font-medium text-white">₹{item.last.toLocaleString("en-IN")}</div>
                      <div className={cn("text-[10.5px] font-semibold", up ? "text-[#089981]" : "text-[#f23645]")}>
                        {up ? `+${item.chg.toFixed(2)}%` : `${item.chg.toFixed(2)}%`}
                      </div>
                    </div>
                    <button
                      type="button"
                      title="Remove from watchlist"
                      onClick={(e) => {
                        e.stopPropagation();
                        setWatchlist((prev) => prev.filter((w) => w.symbol !== item.symbol));
                      }}
                      className="opacity-0 group-hover:opacity-100 p-1 rounded text-[#787b86] hover:text-[#f23645] transition-opacity"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Bottom Instrument Details Pane (Like TradingView Right Pane) */}
          <div className="border-t border-[#2a2e39] bg-[#1e222d]/60 p-3 text-xs">
            <div className="flex items-baseline justify-between">
              <div>
                <div className="font-bold text-white text-sm">{symbol.replace("-EQ", "")}</div>
                <div className="text-[10px] text-[#787b86]">NSE Equity · Market Open</div>
              </div>
              <div className="text-right">
                <div className="font-bold text-base text-white tabular-nums">₹{currentPrice.toFixed(2)}</div>
                <div className={cn("text-xs font-semibold tabular-nums", isUp ? "text-[#089981]" : "text-[#f23645]")}>
                  {isUp ? `+${currentChg}%` : `${currentChg}%`}
                </div>
              </div>
            </div>

            <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] border-t border-[#2a2e39] pt-2 text-[#787b86]">
              <div>
                <span>High: </span>
                <span className="text-white font-mono">{hoverData?.high.toFixed(2) ?? "—"}</span>
              </div>
              <div>
                <span>Low: </span>
                <span className="text-white font-mono">{hoverData?.low.toFixed(2) ?? "—"}</span>
              </div>
              <div>
                <span>Volume: </span>
                <span className="text-white font-mono">
                  {hoverData?.volume ? `${(hoverData.volume / 100000).toFixed(2)}L` : "—"}
                </span>
              </div>
              <div>
                <span>Timeframe: </span>
                <span className="text-white font-mono">{timeframe.toUpperCase()}</span>
              </div>
            </div>

            {/* Direct Order Actions */}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <Button
                size="sm"
                onClick={() => void handleQuickOrder(true)}
                className="bg-[#089981] hover:bg-[#089981]/90 text-white font-semibold h-7 text-xs"
              >
                Buy
              </Button>
              <Button
                size="sm"
                onClick={() => void handleQuickOrder(false)}
                className="bg-[#f23645] hover:bg-[#f23645]/90 text-white font-semibold h-7 text-xs"
              >
                Sell
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
