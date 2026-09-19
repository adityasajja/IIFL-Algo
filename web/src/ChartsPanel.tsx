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
  AlertCircle,
  Check,
  ChevronDown,
  Code,
  Maximize2,
  Minimize2,
  Minus,
  MousePointer,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Square,
  Trash2,
  TrendingUp,
  X,
} from "lucide-react";
import { API_URL, getCandles, getTickCandles, placeOrder, type Candle } from "./api";
import { Button } from "./components/ui/button";
import { Select } from "./components/ui/select";
import { useLiveTicks } from "./lib/useLiveTicks";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";
import { DrawingCanvas, type DrawingTool } from "./components/chart/DrawingCanvas";
import { evaluatePineScript, parsePineInputs, PINE_PRESETS, type PineSeriesResult } from "./lib/pineScript";
import { setVisibleInterval } from "./lib/visibleInterval";

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
  { v: "1s", label: "1s", isTick: true },
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

  // Drawing Tools State
  const [activeTool, setActiveTool] = useState<DrawingTool>("cursor");
  const [canvasDim, setCanvasDim] = useState({ width: 800, height: 600 });

  // Pine Script State
  const [pineEditorOpen, setPineEditorOpen] = useState(false);
  const [pineScript, setPineScript] = useState<string>(() => {
    return localStorage.getItem("atr.chart.pinescript") || `// Custom Strategy Indicator\nfast = ta.ema(close, 9);\nslow = ta.ema(close, 21);\nplot(fast, "Fast EMA", "#00e5ff");\nplot(slow, "Slow EMA", "#ff007f");`;
  });
  const [pineDraft, setPineDraft] = useState<string>(pineScript);
  const [pineError, setPineError] = useState<string | null>(null);
  const [pineAppliedMsg, setPineAppliedMsg] = useState(false);

  // Dynamic Pine Indicator Settings & Styles
  const [pineInputs, setPineInputs] = useState<Record<string, any>>(() => {
    try {
      const saved = localStorage.getItem("atr.chart.pineinputs");
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });
  const [pineStyles, setPineStyles] = useState<Record<string, { color?: string; lineWidth?: number; visible?: boolean }>>(() => {
    try {
      const saved = localStorage.getItem("atr.chart.pinestyles");
      return saved ? JSON.parse(saved) : {};
    } catch {
      return {};
    }
  });
  const [pineSettingsModalOpen, setPineSettingsModalOpen] = useState(false);
  const [pineSettingsActiveTab, setPineSettingsActiveTab] = useState<"inputs" | "style">("inputs");
  const [indicatorLegendValues, setIndicatorLegendValues] = useState<Record<string, number | null>>({});

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
  const pineSubChartRef = useRef<HTMLDivElement>(null);
  const chartApiRef = useRef<IChartApi | null>(null);
  const rsiApiRef = useRef<IChartApi | null>(null);
  const pineChartApiRef = useRef<IChartApi | null>(null);
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

      // 1s timeframe uses tick buffer resampling (real-time only)
      if (targetTf === "1s") {
        try {
          const ticks = await getTickCandles(targetSym, 1);
          if (!ticks || ticks.length === 0) {
            throw new Error("No live tick data - ensure market is open and symbol is subscribed");
          }
          setSymbol(targetSym);
          setCandles(ticks.map((t) => ({
            ts: t.ts,
            open: t.open,
            high: t.high,
            low: t.low,
            close: t.close,
            volume: t.volume,
          })));
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setLoading(false);
        }
        return;
      }

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

    // For 1s timeframe, use tick's epoch time directly
    let timeVal: UTCTimestamp;
    if (timeframe === "1s") {
      timeVal = Math.floor(liveTick.epoch || Date.now() / 1000) as UTCTimestamp;
    } else if (isIntraday) {
      timeVal = Math.floor(new Date(lastBar.ts).getTime() / 1000) as UTCTimestamp;
    } else {
      timeVal = lastBar.ts.slice(0, 10) as unknown as UTCTimestamp;
    }

    try {
      // For 1s, check if we need a new candle or update existing
      if (timeframe === "1s") {
        const lastCandleTime = Math.floor(new Date(lastBar.ts).getTime() / 1000);
        const currentSecond = Math.floor(liveTick.epoch || Date.now() / 1000);

        if (currentSecond > lastCandleTime) {
          // New second - add new candle
          candleSeriesRef.current.update({
            time: currentSecond as UTCTimestamp,
            open: newClose,
            high: newClose,
            low: newClose,
            close: newClose,
          });
        } else {
          // Same second - update current candle
          candleSeriesRef.current.update({
            time: timeVal,
            open: lastBar.open,
            high: Math.max(lastBar.high, newClose),
            low: Math.min(lastBar.low, newClose),
            close: newClose,
          });
        }
      } else {
        candleSeriesRef.current.update({
          time: timeVal,
          open: lastBar.open,
          high: Math.max(lastBar.high, newClose),
          low: Math.min(lastBar.low, newClose),
          close: newClose,
        });
      }
    } catch {
      // ignore
    }
  }, [liveTick, candles, timeframe]);

  // For 1s timeframe, periodically refresh candles from tick buffer
  useEffect(() => {
    if (timeframe !== "1s") return;
    const interval = setVisibleInterval(() => {
      void loadCandles(symbol, "1s");
    }, 1000);
    return () => clearInterval(interval);
  }, [timeframe, symbol, loadCandles]);

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
        secondsVisible: timeframe === "1s",
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

    // Custom Pine Script Indicator Evaluation
    let pineSubChart: IChartApi | null = null;
    let pineFirstSeries: ISeriesApi<any> | null = null;
    try {
      const allPinePlots = evaluatePineScript(pineScript, candles, true, "#2962ff", pineInputs, pineStyles);
      const overlayPlots = allPinePlots.filter((p) => p.isOverlay);
      const panePlots = allPinePlots.filter((p) => !p.isOverlay);

      // 1. Overlay plots on main price chart
      overlayPlots.forEach((p) => {
        const line = priceChart.addLineSeries({
          color: p.color,
          lineWidth: (p.lineWidth as 1 | 2 | 3 | 4) ?? 2,
          priceLineVisible: false,
          lastValueVisible: true,
          title: p.name,
        });
        line.setData(
          candles.flatMap((_, i) =>
            p.values[i] !== null ? [{ time: model.times[i], value: p.values[i] as number }] : []
          )
        );
      });

      // 2. Separate dedicated pane for non-overlay indicators (oscillators like FiboGann)
      if (panePlots.length > 0 && pineSubChartRef.current) {
        pineSubChart = createChart(pineSubChartRef.current, {
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
            scaleMargins: { top: 0.12, bottom: 0.12 },
          },
          timeScale: {
            borderColor: border,
            visible: !showRSI, // Show time scale on the bottom-most pane
            timeVisible: timeframe !== "1d",
          },
          handleScroll: true,
          handleScale: true,
          height: 140,
        });
        pineChartApiRef.current = pineSubChart;

        panePlots.forEach((p, plotIdx) => {
          const s = pineSubChart!.addLineSeries({
            color: p.color,
            lineWidth: (p.lineWidth as 1 | 2 | 3 | 4) ?? 2,
            priceLineVisible: false,
            lastValueVisible: true,
            title: p.name,
          });
          if (plotIdx === 0) pineFirstSeries = s;
          s.setData(
            candles.flatMap((_, i) =>
              p.values[i] !== null ? [{ time: model.times[i], value: p.values[i] as number }] : []
            )
          );
        });

        // Set initial legend values
        const lastIdx = candles.length - 1;
        const initialVals: Record<string, number | null> = {};
        panePlots.forEach((p) => {
          initialVals[p.name] = p.values[lastIdx] ?? null;
        });
        setIndicatorLegendValues(initialVals);

        // Bidirectional timeScale and crosshair synchronization between Price chart and Indicator Pane
        let isSyncingPine = false;
        priceChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
          if (isSyncingPine || !range || !pineSubChart) return;
          isSyncingPine = true;
          pineSubChart.timeScale().setVisibleLogicalRange(range);
          isSyncingPine = false;
        });

        pineSubChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
          if (isSyncingPine || !range) return;
          isSyncingPine = true;
          priceChart.timeScale().setVisibleLogicalRange(range);
          if (rsiChart) rsiChart.timeScale().setVisibleLogicalRange(range);
          isSyncingPine = false;
        });

        priceChart.subscribeCrosshairMove((param) => {
          if (!param.time || !pineSubChart || !pineFirstSeries) return;
          if (!isSyncingPine) {
            isSyncingPine = true;
            pineSubChart.setCrosshairPosition(param.point?.y ?? 50, param.time, pineFirstSeries);
            isSyncingPine = false;
          }
          const timeIndex = new Map<BarTime, number>(model.times.map((tm, i) => [tm, i]));
          const idx = timeIndex.get(param.time as BarTime);
          if (idx !== undefined) {
            const vals: Record<string, number | null> = {};
            panePlots.forEach((p) => {
              vals[p.name] = p.values[idx] ?? null;
            });
            setIndicatorLegendValues(vals);
          }
        });

        pineSubChart.subscribeCrosshairMove((param) => {
          if (!param.time || !pineSubChart) return;
          if (!isSyncingPine) {
            isSyncingPine = true;
            priceChart.setCrosshairPosition(param.point?.y ?? 50, param.time, candleSeries);
            if (rsiChart && rsiLineSeries) {
              rsiChart.setCrosshairPosition(param.point?.y ?? 50, param.time, rsiLineSeries);
            }
            isSyncingPine = false;
          }
          const timeIndex = new Map<BarTime, number>(model.times.map((tm, i) => [tm, i]));
          const idx = timeIndex.get(param.time as BarTime);
          if (idx !== undefined) {
            const vals: Record<string, number | null> = {};
            panePlots.forEach((p) => {
              vals[p.name] = p.values[idx] ?? null;
            });
            setIndicatorLegendValues(vals);
          }
        });
      }

      setPineError(null);
    } catch (err: any) {
      setPineError(err?.message || "Error evaluating script");
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
        handleScroll: true,
        handleScale: true,
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
        if (pineSubChart) pineSubChart.timeScale().setVisibleLogicalRange(range);
        isSyncing = false;
      });

      priceChart.subscribeCrosshairMove((param) => {
        if (isSyncing || !param.time || !rsiChart || !rsiLineSeries) return;
        isSyncing = true;
        rsiChart.setCrosshairPosition(param.point?.y ?? 50, param.time, rsiLineSeries);
        isSyncing = false;
      });

      rsiChart.subscribeCrosshairMove((param) => {
        if (isSyncing || !param.time || !rsiChart) return;
        isSyncing = true;
        priceChart.setCrosshairPosition(param.point?.y ?? 50, param.time, candleSeries);
        if (pineSubChart && pineFirstSeries) {
          pineSubChart.setCrosshairPosition(param.point?.y ?? 50, param.time, pineFirstSeries);
        }
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
      const hasPinePane = pineSubChart !== null;
      const pineH = hasPinePane ? 140 : 0;
      const priceH = Math.max(totalH - rsiH - pineH, 260);

      setCanvasDim({ width: w, height: priceH });
      priceChart.applyOptions({ width: w, height: priceH });
      if (pineSubChart) {
        pineSubChart.applyOptions({ width: w, height: pineH });
      }
      if (rsiChart) {
        rsiChart.applyOptions({ width: w, height: rsiH });
      }
    };

    const ro = new ResizeObserver(handleResize);
    if (chartWrapperRef.current) {
      ro.observe(chartWrapperRef.current);
      const hasPinePane = pineSubChart !== null;
      const rsiH = showRSI ? 110 : 0;
      const pineH = hasPinePane ? 140 : 0;
      setCanvasDim({
        width: chartWrapperRef.current.clientWidth,
        height: Math.max(chartWrapperRef.current.clientHeight - rsiH - pineH, 260),
      });
    }

    return () => {
      ro.disconnect();
      priceChart.remove();
      if (pineSubChart) pineSubChart.remove();
      if (rsiChart) rsiChart.remove();
    };
  }, [candles, model, theme, showRibbon, showEMA200, showBB, showRSI, showVolume, timeframe, pineScript, pineInputs, pineStyles]);

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

          <div className="h-4 w-px bg-[#2a2e39]" />

          {/* Pine Script Editor Toggle */}
          <button
            type="button"
            onClick={() => setPineEditorOpen(!pineEditorOpen)}
            className={cn(
              "flex items-center gap-1.5 rounded px-2.5 py-1 transition-colors text-xs font-medium",
              pineEditorOpen ? "bg-[#2962ff] text-white" : "text-[#d1d4dc] hover:bg-[#2a2e39]"
            )}
            title="Pine Script Indicator Editor"
          >
            <Code size={13} className={pineEditorOpen ? "text-white" : "text-[#787b86]"} />
            <span>Pine Editor</span>
          </button>
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
      {/* 2. MAIN BODY: LEFT DRAWING TOOLS + CHART + RIGHT WATCHLIST     */}
      {/* ------------------------------------------------------------- */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* TradingView Left Drawing Tools Rail */}
        <div className="flex flex-col items-center gap-1 border-r border-[#2a2e39] bg-[#131722] py-2 px-1 text-[#787b86] shrink-0 z-30 select-none">
          <button
            type="button"
            title="Crosshair / Normal Cursor"
            onClick={() => setActiveTool("cursor")}
            className={cn(
              "rounded p-1.5 transition-colors",
              activeTool === "cursor" ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
            )}
          >
            <MousePointer size={15} />
          </button>
          <button
            type="button"
            title="Trendline (Click start & end points)"
            onClick={() => setActiveTool("trendline")}
            className={cn(
              "rounded p-1.5 transition-colors",
              activeTool === "trendline" ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
            )}
          >
            <TrendingUp size={15} />
          </button>
          <button
            type="button"
            title="Extended Ray"
            onClick={() => setActiveTool("ray")}
            className={cn(
              "rounded p-1.5 transition-colors",
              activeTool === "ray" ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
            )}
          >
            <Pencil size={15} />
          </button>
          <button
            type="button"
            title="Horizontal Price Level"
            onClick={() => setActiveTool("hline")}
            className={cn(
              "rounded p-1.5 transition-colors",
              activeTool === "hline" ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
            )}
          >
            <Minus size={15} />
          </button>
          <button
            type="button"
            title="Rectangle / Supply & Demand Zone"
            onClick={() => setActiveTool("rect")}
            className={cn(
              "rounded p-1.5 transition-colors",
              activeTool === "rect" ? "bg-[#2962ff] text-white" : "hover:bg-[#2a2e39] hover:text-white"
            )}
          >
            <Square size={15} />
          </button>
          <div className="my-1 h-px w-4 bg-[#2a2e39]" />
          <button
            type="button"
            title="Clear All Drawings"
            onClick={() => {
              localStorage.removeItem("atr.chart.drawings");
              window.dispatchEvent(new Event("storage"));
              setActiveTool("cursor");
            }}
            className="rounded p-1.5 text-[#787b86] hover:bg-[#f23645]/20 hover:text-[#f23645] transition-colors"
          >
            <Trash2 size={15} />
          </button>
        </div>

        {/* Central Interactive Canvas Area */}
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
                <div ref={priceChartRef} data-lenis-prevent className="flex-1 w-full min-h-[300px] relative">
                  <DrawingCanvas
                    tool={activeTool}
                    onToolSelect={setActiveTool}
                    width={canvasDim.width}
                    height={canvasDim.height}
                  />
                </div>
                {/* Dedicated Subchart Pane for Pine Oscillators / Non-overlay Indicators */}
                {(() => {
                  let hasPanePlots = false;
                  let indicatorName = "Pine Indicator";
                  try {
                    const parsed = evaluatePineScript(pineScript, candles, true, "#2962ff", pineInputs, pineStyles);
                    hasPanePlots = parsed.some((p) => !p.isOverlay);
                    const indMatch = pineScript.match(/indicator\s*\(\s*["']([^"']+)["']/i);
                    if (indMatch) indicatorName = indMatch[1];
                  } catch {}

                  if (!hasPanePlots) return null;

                  return (
                    <div className="border-t border-[#2a2e39] relative group/pane flex flex-col bg-[#131722]">
                      {/* Indicator Header Legend (TradingView Style) */}
                      <div className="absolute top-1.5 left-2 z-20 flex items-center gap-2 text-[11px] font-mono select-none bg-[#131722]/85 px-2 py-0.5 rounded border border-[#2a2e39]/60 backdrop-blur">
                        <span className="font-semibold text-white truncate max-w-[200px]" title={indicatorName}>
                          {indicatorName}
                        </span>

                        {/* Interactive Action Icons (Hover visible or subtle) */}
                        <div className="flex items-center gap-1 opacity-70 group-hover/pane:opacity-100 transition-opacity">
                          <button
                            type="button"
                            title="Indicator Settings"
                            onClick={() => setPineSettingsModalOpen(true)}
                            className="p-1 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-white transition-colors"
                          >
                            <SlidersHorizontal size={12} />
                          </button>
                          <button
                            type="button"
                            title="Remove Indicator"
                            onClick={() => {
                              setPineScript("// No custom indicator");
                              localStorage.removeItem("atr.chart.pinescript");
                            }}
                            className="p-1 rounded hover:bg-[#2a2e39] text-[#787b86] hover:text-[#f23645] transition-colors"
                          >
                            <X size={12} />
                          </button>
                        </div>

                        {/* Real-time Indicator Value readouts */}
                        <div className="flex items-center gap-2 text-[10.5px] ml-1">
                          {Object.entries(indicatorLegendValues).map(([name, val]) => (
                            <span key={name} className="text-[#00e5ff]">
                              <span className="text-[#787b86] font-normal">{name}: </span>
                              <span className="font-semibold">{val !== null ? val.toFixed(2) : "—"}</span>
                            </span>
                          ))}
                        </div>
                      </div>

                      {/* Lightweight Charts Canvas for Pine Subchart */}
                      <div ref={pineSubChartRef} data-lenis-prevent className="w-full h-[140px]" />
                    </div>
                  );
                })()}

                {showRSI && (
                  <div className="border-t border-[#2a2e39] relative">
                    <span className="absolute top-1 left-2 z-10 text-[10px] font-mono text-[#7e57c2] font-semibold">
                      RSI (14)
                    </span>
                    <div ref={rsiChartRef} data-lenis-prevent className="w-full h-[110px]" />
                  </div>
                )}
              </>
            )}
          </div>

          {/* Pine Script Editor Drawer (Bottom Dock) */}
          {pineEditorOpen && (
            <div className="border-t border-[#2a2e39] bg-[#1e222d] flex flex-col h-72 shrink-0 z-40 transition-all">
              {/* Header */}
              <div className="flex items-center justify-between px-3 py-1.5 border-b border-[#2a2e39] bg-[#171b26] text-xs">
                <div className="flex items-center gap-2">
                  <Code size={14} className="text-[#2962ff]" />
                  <span className="font-semibold text-white tracking-wide">Pine Script Indicator Studio</span>
                  <span className="text-[10px] text-[#2962ff] font-mono bg-[#2962ff]/10 px-1.5 py-0.5 rounded border border-[#2962ff]/30 font-semibold">v6 Reference</span>
                </div>
                <div className="flex items-center gap-2">
                  {/* Preset Selector */}
                  <span className="text-[11px] text-[#787b86]">Templates:</span>
                  <div className="flex items-center gap-1">
                    {PINE_PRESETS.map((p) => (
                      <button
                        key={p.name}
                        type="button"
                        onClick={() => {
                          setPineDraft(p.code);
                          setPineError(null);
                        }}
                        className="rounded px-2 py-0.5 text-[10.5px] bg-[#2a2e39]/80 hover:bg-[#2a2e39] text-[#b2b5be] hover:text-white transition-colors"
                        title={p.desc}
                      >
                        {p.name.replace(/ \(.*\)/, "")}
                      </button>
                    ))}
                  </div>

                  <div className="h-4 w-[1px] bg-[#2a2e39] mx-1" />

                  {/* Apply Button */}
                  <Button
                    size="sm"
                    onClick={() => {
                      try {
                        // Validate script compilation first
                        evaluatePineScript(pineDraft, candles, true);
                        setPineScript(pineDraft);
                        localStorage.setItem("atr.chart.pinescript", pineDraft);
                        setPineError(null);
                        setPineAppliedMsg(true);
                        setTimeout(() => setPineAppliedMsg(false), 2000);
                      } catch (err: any) {
                        setPineError(err?.message || "Syntax error in script");
                      }
                    }}
                    className="h-6 px-2.5 text-xs bg-[#2962ff] hover:bg-[#2962ff]/90 text-white font-medium flex items-center gap-1"
                  >
                    {pineAppliedMsg ? <Check size={12} className="text-emerald-300" /> : <Play size={11} fill="currentColor" />}
                    <span>{pineAppliedMsg ? "Applied!" : "Apply to Chart"}</span>
                  </Button>

                  <button
                    type="button"
                    onClick={() => setPineEditorOpen(false)}
                    className="p-1 rounded text-[#787b86] hover:bg-[#2a2e39] hover:text-white"
                  >
                    <X size={14} />
                  </button>
                </div>
              </div>

              {/* Code Editor Body with Line Numbers Gutter */}
              <div className="flex-1 flex bg-[#131722] relative overflow-hidden font-mono text-[12px]">
                {/* Line numbers gutter */}
                <div className="w-10 select-none bg-[#0e1117] text-[#4a4e5d] text-right pr-2.5 pt-2 border-r border-[#2a2e39] font-mono text-[11px] leading-relaxed">
                  {pineDraft.split("\n").map((_, i) => (
                    <div key={i}>{i + 1}</div>
                  ))}
                </div>
                {/* Textarea */}
                <textarea
                  value={pineDraft}
                  onChange={(e) => setPineDraft(e.target.value)}
                  onKeyDown={(e) => {
                    // Tab indent support
                    if (e.key === "Tab") {
                      e.preventDefault();
                      const start = e.currentTarget.selectionStart;
                      const end = e.currentTarget.selectionEnd;
                      const val = pineDraft;
                      setPineDraft(val.substring(0, start) + "    " + val.substring(end));
                      setTimeout(() => {
                        const target = e.target as HTMLTextAreaElement;
                        if (target) {
                          target.selectionStart = target.selectionEnd = start + 4;
                        }
                      }, 0);
                    }
                    // Ctrl+Enter or Cmd+Enter to compile & apply
                    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                      e.preventDefault();
                      try {
                        evaluatePineScript(pineDraft, candles, true);
                        setPineScript(pineDraft);
                        localStorage.setItem("atr.chart.pinescript", pineDraft);
                        setPineError(null);
                        setPineAppliedMsg(true);
                        setTimeout(() => setPineAppliedMsg(false), 2000);
                      } catch (err: any) {
                        setPineError(err?.message || "Syntax error in script");
                      }
                    }
                  }}
                  spellCheck={false}
                  placeholder={`// Full TradingView Pine Script v5\n// e.g.:\nfast = ta.ema(close, 9);\nslow = ta.ema(close, 21);\nplot(fast, "Fast EMA", "#00e5ff");\nplot(slow, "Slow EMA", "#ff007f");`}
                  className="flex-1 h-full bg-transparent text-[#d1d4dc] p-2 leading-relaxed resize-none focus:outline-none selection:bg-[#2962ff]/40 overflow-y-auto whitespace-pre font-mono"
                />
              </div>

              {/* Status / Syntax Console Footer */}
              <div className="px-3 py-1.5 bg-[#171b26] border-t border-[#2a2e39] text-[11px] flex items-center justify-between font-mono">
                <div className="flex items-center gap-2">
                  {pineError ? (
                    <span className="text-[#f23645] flex items-center gap-1.5 font-medium">
                      <AlertCircle size={13} />
                      {pineError}
                    </span>
                  ) : (
                    <span className="text-[#089981] flex items-center gap-1.5 font-medium">
                      <Check size={13} />
                      Compilation successful · Added to chart
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-[#787b86] text-[10.5px]">
                  <span>Shortcut: <kbd className="bg-[#2a2e39] px-1 py-0.5 rounded text-white text-[10px]">Ctrl</kbd> + <kbd className="bg-[#2a2e39] px-1 py-0.5 rounded text-white text-[10px]">Enter</kbd></span>
                  <span>|</span>
                  <span>Built-ins: <span className="text-[#2962ff]">ta.sma</span>, <span className="text-[#2962ff]">ta.ema</span>, <span className="text-[#2962ff]">ta.rsi</span>, <span className="text-[#2962ff]">ta.macd</span>, <span className="text-[#2962ff]">ta.atr</span>, <span className="text-[#2962ff]">plot</span></span>
                </div>
              </div>
            </div>
          )}

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

      {/* ========================================================================= */}
      {/* TRADINGVIEW DYNAMIC INDICATOR SETTINGS MODAL                             */}
      {/* ========================================================================= */}
      {pineSettingsModalOpen && (() => {
        const dynamicInputs = parsePineInputs(pineScript);
        let plots: PineSeriesResult[] = [];
        let indicatorTitle = "Indicator Settings";
        try {
          plots = evaluatePineScript(pineScript, candles, true, "#2962ff", pineInputs, pineStyles);
          const indMatch = pineScript.match(/indicator\s*\(\s*["']([^"']+)["']/i);
          if (indMatch) indicatorTitle = indMatch[1];
        } catch {}

        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-sm animate-in fade-in duration-150">
            <div
              className="bg-[#1e222d] border border-[#2a2e39] rounded-xl shadow-2xl w-[480px] max-w-[92vw] flex flex-col overflow-hidden text-[#d1d4dc] font-sans"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
              <div className="flex items-center justify-between px-5 py-3.5 border-b border-[#2a2e39] bg-[#171b26]">
                <div className="flex items-center gap-2">
                  <SlidersHorizontal size={16} className="text-[#2962ff]" />
                  <span className="font-semibold text-white text-sm truncate max-w-[340px]">
                    {indicatorTitle}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => setPineSettingsModalOpen(false)}
                  className="p-1 rounded-md text-[#787b86] hover:bg-[#2a2e39] hover:text-white transition-colors"
                >
                  <X size={16} />
                </button>
              </div>

              {/* Tabs Bar (TradingView style: Inputs | Style | Visibility) */}
              <div className="flex items-center gap-6 px-5 border-b border-[#2a2e39] bg-[#1e222d] text-xs font-semibold">
                <button
                  type="button"
                  onClick={() => setPineSettingsActiveTab("inputs")}
                  className={cn(
                    "py-2.5 transition-colors border-b-2 font-medium tracking-wide",
                    pineSettingsActiveTab === "inputs"
                      ? "border-[#2962ff] text-[#2962ff]"
                      : "border-transparent text-[#787b86] hover:text-white"
                  )}
                >
                  Inputs
                </button>
                <button
                  type="button"
                  onClick={() => setPineSettingsActiveTab("style")}
                  className={cn(
                    "py-2.5 transition-colors border-b-2 font-medium tracking-wide",
                    pineSettingsActiveTab === "style"
                      ? "border-[#2962ff] text-[#2962ff]"
                      : "border-transparent text-[#787b86] hover:text-white"
                  )}
                >
                  Style
                </button>
              </div>

              {/* Tab Contents Body */}
              <div className="p-5 max-h-[360px] overflow-y-auto space-y-4 text-xs">
                {pineSettingsActiveTab === "inputs" && (
                  <>
                    {dynamicInputs.length === 0 ? (
                      <div className="text-center py-8 text-[#787b86] font-mono text-xs">
                        No configurable inputs defined in this indicator script.
                      </div>
                    ) : (
                      <div className="space-y-3.5">
                        {dynamicInputs.map((inputDef) => {
                          const currentValue = pineInputs[inputDef.id] !== undefined
                            ? pineInputs[inputDef.id]
                            : inputDef.defval;

                          return (
                            <div key={inputDef.id} className="flex items-center justify-between gap-4">
                              <label className="text-white font-medium text-xs truncate max-w-[200px]" title={inputDef.title}>
                                {inputDef.title}
                              </label>

                              <div className="flex items-center">
                                {inputDef.type === "bool" ? (
                                  <input
                                    type="checkbox"
                                    checked={Boolean(currentValue)}
                                    onChange={(e) => {
                                      const updated = { ...pineInputs, [inputDef.id]: e.target.checked };
                                      setPineInputs(updated);
                                      localStorage.setItem("atr.chart.pineinputs", JSON.stringify(updated));
                                    }}
                                    className="h-4 w-4 rounded bg-[#131722] border-[#2a2e39] text-[#2962ff] focus:ring-0 focus:ring-offset-0 cursor-pointer"
                                  />
                                ) : inputDef.type === "color" ? (
                                  <div className="flex items-center gap-2">
                                    <input
                                      type="color"
                                      value={String(currentValue).startsWith("#") ? String(currentValue) : "#2962ff"}
                                      onChange={(e) => {
                                        const updated = { ...pineInputs, [inputDef.id]: e.target.value };
                                        setPineInputs(updated);
                                        localStorage.setItem("atr.chart.pineinputs", JSON.stringify(updated));
                                      }}
                                      className="w-7 h-7 rounded border border-[#2a2e39] bg-transparent cursor-pointer p-0"
                                    />
                                    <span className="font-mono text-[11px] text-[#787b86]">{currentValue}</span>
                                  </div>
                                ) : (
                                  <input
                                    type={inputDef.type === "int" || inputDef.type === "float" ? "number" : "text"}
                                    step={inputDef.step ?? (inputDef.type === "float" ? "0.1" : "1")}
                                    min={inputDef.minval}
                                    max={inputDef.maxval}
                                    value={currentValue ?? ""}
                                    onChange={(e) => {
                                      const raw = e.target.value;
                                      let parsedVal: any = raw;
                                      if (inputDef.type === "int") parsedVal = parseInt(raw, 10) || 0;
                                      else if (inputDef.type === "float") parsedVal = parseFloat(raw) || 0;
                                      const updated = { ...pineInputs, [inputDef.id]: parsedVal };
                                      setPineInputs(updated);
                                      localStorage.setItem("atr.chart.pineinputs", JSON.stringify(updated));
                                    }}
                                    className="w-24 px-2 py-1.5 rounded bg-[#131722] border border-[#2a2e39] text-white text-right font-mono text-xs focus:outline-none focus:border-[#2962ff]"
                                  />
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </>
                )}

                {pineSettingsActiveTab === "style" && (
                  <div className="space-y-3.5">
                    {plots.length === 0 ? (
                      <div className="text-center py-8 text-[#787b86] font-mono text-xs">
                        No plot outputs to customize.
                      </div>
                    ) : (
                      plots.map((p) => {
                        const styleConfig = pineStyles[p.name] || {};
                        const currentColor = styleConfig.color || p.color;
                        const currentWidth = styleConfig.lineWidth ?? p.lineWidth ?? 2;
                        const isVisible = styleConfig.visible !== false;

                        return (
                          <div key={p.name} className="flex items-center justify-between gap-3 border-b border-[#2a2e39]/40 pb-2.5">
                            <div className="flex items-center gap-2">
                              <input
                                type="checkbox"
                                checked={isVisible}
                                onChange={(e) => {
                                  const updated = {
                                    ...pineStyles,
                                    [p.name]: { ...styleConfig, visible: e.target.checked },
                                  };
                                  setPineStyles(updated);
                                  localStorage.setItem("atr.chart.pinestyles", JSON.stringify(updated));
                                }}
                                className="h-4 w-4 rounded bg-[#131722] border-[#2a2e39] text-[#2962ff] cursor-pointer"
                              />
                              <span className="font-medium text-white text-xs truncate max-w-[160px]" title={p.name}>
                                {p.name}
                              </span>
                            </div>

                            <div className="flex items-center gap-3">
                              {/* Color Picker */}
                              <input
                                type="color"
                                value={currentColor.startsWith("#") ? currentColor : "#2962ff"}
                                onChange={(e) => {
                                  const updated = {
                                    ...pineStyles,
                                    [p.name]: { ...styleConfig, color: e.target.value },
                                  };
                                  setPineStyles(updated);
                                  localStorage.setItem("atr.chart.pinestyles", JSON.stringify(updated));
                                }}
                                className="w-6 h-6 rounded border border-[#2a2e39] bg-transparent cursor-pointer p-0"
                              />

                              {/* Line Width Selector */}
                              <Select
                                size="sm"
                                className="w-20"
                                value={String(currentWidth)}
                                onChange={(v) => {
                                  const updated = {
                                    ...pineStyles,
                                    [p.name]: { ...styleConfig, lineWidth: parseInt(v, 10) },
                                  };
                                  setPineStyles(updated);
                                  localStorage.setItem("atr.chart.pinestyles", JSON.stringify(updated));
                                }}
                                options={[
                                  { value: "1", label: "1 px" },
                                  { value: "2", label: "2 px" },
                                  { value: "3", label: "3 px" },
                                  { value: "4", label: "4 px" },
                                ]}
                              />
                            </div>
                          </div>
                        );
                      })
                    )}
                  </div>
                )}
              </div>

              {/* Modal Footer */}
              <div className="flex items-center justify-between px-5 py-3 border-t border-[#2a2e39] bg-[#171b26] text-xs">
                <button
                  type="button"
                  onClick={() => {
                    setPineInputs({});
                    setPineStyles({});
                    localStorage.removeItem("atr.chart.pineinputs");
                    localStorage.removeItem("atr.chart.pinestyles");
                  }}
                  className="text-[#787b86] hover:text-white transition-colors"
                >
                  Reset to Defaults
                </button>

                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setPineSettingsModalOpen(false)}
                    className="h-7 px-3 text-xs bg-[#2a2e39] text-white hover:bg-[#2a2e39]/80"
                  >
                    Close
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => setPineSettingsModalOpen(false)}
                    className="h-7 px-3 text-xs bg-[#2962ff] text-white hover:bg-[#2962ff]/90"
                  >
                    Ok
                  </Button>
                </div>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
