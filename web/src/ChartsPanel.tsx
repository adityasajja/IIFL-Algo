import { chartColor, chartSurface } from "./lib/chart-theme";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
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
import { Tooltip } from "./components/motion/tooltip";
import { Button } from "./components/ui/button";
import { Chip } from "./components/ui/chip";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
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
    const surf = chartSurface(isDark);
    const { bg, text, border, grid } = surf;

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
        vertLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
        horzLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
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
    const candleSeries = priceChart.addSeries(CandlestickSeries, {
      upColor: chartColor("up"),
      downColor: chartColor("down"),
      wickUpColor: chartColor("up"),
      wickDownColor: chartColor("down"),
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
      const e9Series = priceChart.addSeries(LineSeries, {
        color: chartColor("blue"),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      e9Series.setData(
        candles.flatMap((_, i) =>
          model.ema9[i] !== null ? [{ time: model.times[i], value: model.ema9[i] as number }] : [],
        ),
      );

      const e21Series = priceChart.addSeries(LineSeries, {
        color: chartColor("orange"),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      e21Series.setData(
        candles.flatMap((_, i) =>
          model.ema21[i] !== null ? [{ time: model.times[i], value: model.ema21[i] as number }] : [],
        ),
      );

      const e50Series = priceChart.addSeries(LineSeries, {
        color: chartColor("purple"),
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
      const e200Series = priceChart.addSeries(LineSeries, {
        color: chartColor("yellow"),
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
      const bbUpper = priceChart.addSeries(LineSeries, {
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

      const bbLower = priceChart.addSeries(LineSeries, {
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
      const volSeries = priceChart.addSeries(HistogramSeries, {
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
      const allPinePlots = evaluatePineScript(pineScript, candles, true, chartColor("blue"), pineInputs, pineStyles);
      const overlayPlots = allPinePlots.filter((p) => p.isOverlay);
      const panePlots = allPinePlots.filter((p) => !p.isOverlay);

      // 1. Overlay plots on main price chart
      overlayPlots.forEach((p) => {
        const line = priceChart.addSeries(LineSeries, {
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
            vertLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
            horzLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
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
          const s = pineSubChart!.addSeries(LineSeries, {
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
          vertLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
          horzLine: { color: chartSurface(isDark).crosshair, width: 1, style: 3 },
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

      rsiLineSeries = rsiChart.addSeries(LineSeries, {
        color: chartColor("violet"),
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
        "flex flex-col bg-card text-foreground font-sans antialiased overflow-hidden select-none",
        isFullscreen ? "fixed inset-0 z-50 h-screen w-screen" : "h-[calc(100vh-80px)] rounded-md border border-border"
      )}
    >
      {/* ------------------------------------------------------------- */}
      {/* 1. TOP TRADINGVIEW TOOLBAR */}
      {/* ------------------------------------------------------------- */}
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-border bg-card px-3 text-xs">
        <div className="flex items-center gap-2">
          {/* Symbol Search Picker */}
          <div className="relative">
            <Button
              size="xs"
              variant="quiet"
              aria-expanded={searchOpen}
              onClick={() => setSearchOpen(!searchOpen)}
            >

              <Search size={13} />
              <span className="font-semibold text-foreground">{symbol.replace("-EQ", "")}</span>
              <span className="text-micro">NSE</span>
              <ChevronDown size={12} />
            </Button>

            {searchOpen && (
              <div className="absolute left-0 top-full z-50 mt-1.5 w-72 rounded-md border border-border bg-muted p-2 ">
                <input
                  type="text"
                  placeholder="Search symbol (e.g. TATA, INFY)..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  autoFocus
                  className="w-full rounded-md bg-card px-2.5 py-1.5 text-xs text-white placeholder-muted-foreground outline-none border border-border focus:border-primary"
                />
                <div data-lenis-prevent className="mt-2 max-h-56 overflow-y-auto space-y-0.5">
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
                      className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs text-foreground hover:bg-border"
                    >
                      <span className="font-semibold">{r.symbol}</span>
                      <span className="text-micro text-muted-foreground">{r.exchange}</span>
                    </button>
                  ))}
                  {searchResults.length === 0 && searchQuery.length >= 2 && (
                    <div className="py-3 text-center text-xs text-muted-foreground">No symbols found</div>
                  )}
                </div>
              </div>
            )}
          </div>

          <div className="h-4 w-px bg-border" />

          {/* Quick Buy / Sell Execution Pills (Like TV Pro) */}
          <div className="flex items-center gap-1.5">
            <Button
              size="xs"
              variant="outline"
              className="border-loss/40 text-loss hover:border-loss hover:bg-loss/10"
              onClick={() => void handleQuickOrder(false)}
            >
              <span>Sell</span>
              <span className="tabular-nums font-mono">{currentPrice.toFixed(2)}</span>
            </Button>
            <Button
              size="xs"
              variant="outline"
              className="border-gain/40 text-gain hover:border-gain hover:bg-gain/10"
              onClick={() => void handleQuickOrder(true)}
            >
              <span>Buy</span>
              <span className="tabular-nums font-mono">{currentPrice.toFixed(2)}</span>
            </Button>
          </div>

          <div className="h-4 w-px bg-border" />

          {/* Timeframe selector (5m, 15m, 1h, D, W, M) */}
          <Tabs
            value={timeframe}
            onValueChange={(v) => {
              setTimeframe(v);
              void loadCandles(symbol, v);
            }}
            variant="segment"
          >
            <TabsList>
              {TIMEFRAMES.map((tf) => (
                <TabsTrigger key={tf.v} value={tf.v}>{tf.label}</TabsTrigger>
              ))}
            </TabsList>
          </Tabs>

          <div className="h-4 w-px bg-border" />

          {/* Indicators Dropdown */}
          <div className="relative">
            <Button
              size="xs"
              variant={showIndicatorsMenu ? "outline" : "quiet"}
              aria-expanded={showIndicatorsMenu}
              onClick={() => setShowIndicatorsMenu(!showIndicatorsMenu)}
            >
              <SlidersHorizontal size={13} />
              <span>Indicators</span>
              <ChevronDown size={11} />
            </Button>

            {showIndicatorsMenu && (
              <div className="absolute left-0 top-full z-50 mt-1.5 w-60 rounded-md border border-border bg-muted p-2.5 space-y-2">
                <div className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">
                  Active Indicators
                </div>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-primary" />
                    EMA Ribbon (9, 21, 50)
                  </span>
                  <input
                    type="checkbox"
                    checked={showRibbon}
                    onChange={(e) => setShowRibbon(e.target.checked)}
                    className="accent-chart-blue"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-chart-yellow" />
                    200 EMA
                  </span>
                  <input
                    type="checkbox"
                    checked={showEMA200}
                    onChange={(e) => setShowEMA200(e.target.checked)}
                    className="accent-chart-yellow"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-chart-sky" />
                    Bollinger Bands
                  </span>
                  <input
                    type="checkbox"
                    checked={showBB}
                    onChange={(e) => setShowBB(e.target.checked)}
                    className="accent-chart-sky"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-chart-violet" />
                    RSI (14) Pane
                  </span>
                  <input
                    type="checkbox"
                    checked={showRSI}
                    onChange={(e) => setShowRSI(e.target.checked)}
                    className="accent-chart-violet"
                  />
                </label>
                <label className="flex items-center justify-between text-xs hover:text-white cursor-pointer py-0.5">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-gain" />
                    Volume Overlay
                  </span>
                  <input
                    type="checkbox"
                    checked={showVolume}
                    onChange={(e) => setShowVolume(e.target.checked)}
                    className="accent-chart-up"
                  />
                </label>
              </div>
            )}
          </div>

          <div className="h-4 w-px bg-border" />

          {/* Pine Script Editor Toggle */}
          <Tooltip content="Pine Script Indicator Editor" side="bottom" delay={400}>
          <Button
            size="xs"
            variant={pineEditorOpen ? "primary" : "quiet"}
            aria-pressed={pineEditorOpen}
            onClick={() => setPineEditorOpen(!pineEditorOpen)}
          >
            <Code size={13} />
            <span>Pine Editor</span>
          </Button>
          </Tooltip>
        </div>

        {/* Right Toolbar: Refresh, Fullscreen */}
        <div className="flex items-center gap-2">
          <Tooltip content="Reload candles" side="bottom" delay={400}>
          <Button
            size="icon-sm"
            variant="plain"
            onClick={() => void loadCandles()}
            disabled={loading}
            aria-label="Reload candles"
          >
            <RefreshCw size={13} className={cn(loading && "animate-spin text-primary")} />
          </Button>
          </Tooltip>
          <Tooltip content={isFullscreen ? "Exit Fullscreen" : "Fullscreen Chart"} side="bottom" delay={400}>
          <Button
            size="icon-sm"
            variant="plain"
            aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen chart"}
            onClick={() => setIsFullscreen(!isFullscreen)}
          >
            {isFullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </Button>
          </Tooltip>
        </div>
      </div>

      {/* ------------------------------------------------------------- */}
      {/* 2. MAIN BODY: LEFT DRAWING TOOLS + CHART + RIGHT WATCHLIST */}
      {/* ------------------------------------------------------------- */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* TradingView Left Drawing Tools Rail */}
        <div className="flex flex-col items-center gap-1 border-r border-border bg-card py-2 px-1 text-muted-foreground shrink-0 z-30 select-none">
          <Tooltip content="Crosshair / Normal Cursor" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant={activeTool === "cursor" ? "primary" : "plain"}
            aria-pressed={activeTool === "cursor"}
            aria-label="cursor"
            onClick={() => setActiveTool("cursor")}
          >
            <MousePointer size={15} />
          </Button>
          </Tooltip>
          <Tooltip content="Trendline (Click start & end points)" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant={activeTool === "trendline" ? "primary" : "plain"}
            aria-pressed={activeTool === "trendline"}
            aria-label="trendline"
            onClick={() => setActiveTool("trendline")}
          >
            <TrendingUp size={15} />
          </Button>
          </Tooltip>
          <Tooltip content="Extended Ray" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant={activeTool === "ray" ? "primary" : "plain"}
            aria-pressed={activeTool === "ray"}
            aria-label="ray"
            onClick={() => setActiveTool("ray")}
          >
            <Pencil size={15} />
          </Button>
          </Tooltip>
          <Tooltip content="Horizontal Price Level" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant={activeTool === "hline" ? "primary" : "plain"}
            aria-pressed={activeTool === "hline"}
            aria-label="hline"
            onClick={() => setActiveTool("hline")}
          >
            <Minus size={15} />
          </Button>
          </Tooltip>
          <Tooltip content="Rectangle / Supply & Demand Zone" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant={activeTool === "rect" ? "primary" : "plain"}
            aria-pressed={activeTool === "rect"}
            aria-label="rect"
            onClick={() => setActiveTool("rect")}
          >
            <Square size={15} />
          </Button>
          </Tooltip>
          <div className="my-1 h-px w-4 bg-border" />
          <Tooltip content="Clear All Drawings" side="right" delay={400}>
          <Button
            size="icon-sm"
            variant="plain"
            className="hover:bg-destructive/10 hover:text-destructive"
            onClick={() => {
              localStorage.removeItem("atr.chart.drawings");
              window.dispatchEvent(new Event("storage"));
              setActiveTool("cursor");
            }}
            aria-label="Clear drawings"
          >
            <Trash2 size={15} />
          </Button>
          </Tooltip>
        </div>

        {/* Central Interactive Canvas Area */}
        <div className="flex flex-1 flex-col min-w-0 relative">
          {/* TradingView Legend & OHLC HUD Bar */}
          <div className="absolute top-2 left-3 z-10 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption font-mono pointer-events-none bg-card/85 px-2 py-1 rounded-md border border-border/60 backdrop-blur">
            <span className="font-semibold text-white">{symbol.replace("-EQ", "")}</span>
            <span className="text-muted-foreground">· {timeframe.toUpperCase()}</span>
            <span className="text-muted-foreground">· NSE</span>
            {hoverData && (
              <>
                <span className="text-muted-foreground">
                  O <span className="text-white">{hoverData.open.toFixed(2)}</span>
                </span>
                <span className="text-muted-foreground">
                  H <span className="text-white">{hoverData.high.toFixed(2)}</span>
                </span>
                <span className="text-muted-foreground">
                  L <span className="text-white">{hoverData.low.toFixed(2)}</span>
                </span>
                <span className="text-muted-foreground">
                C <span className={cn(isUp ? "text-gain" : "text-loss")}>{hoverData.close.toFixed(2)}</span>
                </span>
                <span className={cn("font-semibold", isUp ? "text-gain" : "text-loss")}>
                  {isUp ? `+${hoverData.change}%` : `${hoverData.change}%`}
                </span>
                {showVolume && (
                  <span className="text-muted-foreground">
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
                <div className="text-sm font-semibold text-loss">Failed to load candles</div>
                <div className="text-xs text-muted-foreground mt-1">{error}</div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void loadCandles()}
                  className="mt-3"
                >
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
                    const parsed = evaluatePineScript(pineScript, candles, true, chartColor("blue"), pineInputs, pineStyles);
                    hasPanePlots = parsed.some((p) => !p.isOverlay);
                    const indMatch = pineScript.match(/indicator\s*\(\s*["']([^"']+)["']/i);
                    if (indMatch) indicatorName = indMatch[1];
                  } catch {}

                  if (!hasPanePlots) return null;

                  return (
                    <div className="border-t border-border relative group/pane flex flex-col bg-card">
                      {/* Indicator Header Legend (TradingView Style) */}
                      <div className="absolute top-1.5 left-2 z-20 flex items-center gap-2 text-caption font-mono select-none bg-card/85 px-2 py-0.5 rounded-md border border-border/60 backdrop-blur">
                        <span className="font-semibold text-white truncate max-w-[200px]" title={indicatorName}>
                          {indicatorName}
                        </span>

                        {/* Interactive Action Icons (Hover visible or subtle) */}
                        <div className="flex items-center gap-1 opacity-70 group-hover/pane:opacity-100 transition-opacity">
                          <Tooltip content="Indicator Settings" side="bottom" delay={400}>
                          <Button
                            size="icon-sm"
                            variant="plain"
                            onClick={() => setPineSettingsModalOpen(true)}
                            aria-label="Indicator settings"
                          >
                            <SlidersHorizontal size={12} />
                          </Button>
                          </Tooltip>
                          <Tooltip content="Remove Indicator" side="bottom" delay={400}>
                          <Button
                            size="icon-sm"
                            variant="plain"
                            className="hover:bg-destructive/10 hover:text-destructive"
                            onClick={() => {
                              setPineScript("// No custom indicator");
                              localStorage.removeItem("atr.chart.pinescript");
                            }}
                            aria-label="Remove custom indicator"
                          >
                            <X size={12} />
                          </Button>
                          </Tooltip>
                        </div>

                        {/* Real-time Indicator Value readouts */}
                        <div className="flex items-center gap-2 text-[10.5px] ml-1">
                          {Object.entries(indicatorLegendValues).map(([name, val]) => (
                            <span key={name} className="text-chart-cyan">
                              <span className="text-muted-foreground font-normal">{name}: </span>
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
                  <div className="border-t border-border relative">
                    <span className="absolute top-1 left-2 z-10 text-micro font-mono text-chart-violet font-semibold">
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
            <div className="border-t border-border bg-muted flex flex-col h-72 shrink-0 z-40 transition-all">
              {/* Header */}
              <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-card text-xs">
                <div className="flex items-center gap-2">
                  <Code size={14} className="text-primary" />
                  <span className="font-semibold text-white tracking-wide">Pine Script Indicator Studio</span>
                  <span className="text-micro text-primary font-mono bg-primary/10 px-1.5 py-0.5 rounded-md border border-primary/30 font-semibold">v6 Reference</span>
                </div>
                <div className="flex items-center gap-2">
                  {/* Preset Selector */}
                  <span className="text-caption text-muted-foreground">Templates:</span>
                  <div className="flex items-center gap-1">
                    {PINE_PRESETS.map((p) => (
                      <Tooltip content={p.desc} side="top" delay={400}>
                      <Chip
                        key={p.name}
                        className="min-h-0 px-2 py-0.5 text-caption"
                        onClick={() => {
                          setPineDraft(p.code);
                          setPineError(null);
                        }}
                      >
                        {p.name.replace(/ \(.*\)/, "")}
                      </Chip>
                      </Tooltip>
                    ))}
                  </div>

                  <div className="h-4 w-[1px] bg-border mx-1" />

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
                    className="h-6 px-2.5 text-xs bg-primary hover:bg-primary/90 text-white font-medium flex items-center gap-1"
                  >
                  {pineAppliedMsg ? <Check size={12} className="text-gain" /> : <Play size={11} fill="currentColor" />}
                    <span>{pineAppliedMsg ? "Applied!" : "Apply to Chart"}</span>
                  </Button>

                  <Button
                    size="icon-sm"
                    variant="plain"
                    onClick={() => setPineEditorOpen(false)}
                    aria-label="Close editor"
                  >
                    <X size={14} />
                  </Button>
                </div>
              </div>

              {/* Code Editor Body with Line Numbers Gutter */}
              <div className="flex-1 flex bg-card relative overflow-hidden font-mono text-xs">
                {/* Line numbers gutter */}
                <div className="w-10 select-none bg-background text-muted-foreground text-right pr-2.5 pt-2 border-r border-border font-mono text-caption leading-relaxed">
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
                      setPineDraft(val.substring(0, start) + " " + val.substring(end));
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
                  data-lenis-prevent
                  className="flex-1 h-full bg-transparent text-foreground p-2 leading-relaxed resize-none focus:outline-none selection:bg-primary/40 overflow-y-auto whitespace-pre font-mono"
                />
              </div>

              {/* Status / Syntax Console Footer */}
              <div className="px-3 py-1.5 bg-card border-t border-border text-caption flex items-center justify-between font-mono">
                <div className="flex items-center gap-2">
                  {pineError ? (
                    <span className="text-loss flex items-center gap-1.5 font-medium">
                      <AlertCircle size={13} />
                      {pineError}
                    </span>
                  ) : (
                    <span className="text-gain flex items-center gap-1.5 font-medium">
                      <Check size={13} />
                      Compilation successful · Added to chart
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-muted-foreground text-[10.5px]">
                <span>Shortcut: <kbd className="bg-border px-1 py-0.5 rounded-md text-white text-micro">Ctrl</kbd> + <kbd className="bg-border px-1 py-0.5 rounded-md text-white text-micro">Enter</kbd></span>
                  <span>|</span>
                  <span>Built-ins: <span className="text-primary">ta.sma</span>, <span className="text-primary">ta.ema</span>, <span className="text-primary">ta.rsi</span>, <span className="text-primary">ta.macd</span>, <span className="text-primary">ta.atr</span>, <span className="text-primary">plot</span></span>
                </div>
              </div>
            </div>
          )}

          {/* Bottom Date Range Bar (1D, 5D, 1M, 3M, 6M, YTD, 1Y, 5Y, ALL) */}
          <div className="flex h-8 shrink-0 items-center justify-between border-t border-border bg-card px-3 text-caption">
            <div className="flex items-center gap-1">
              {RANGES.map((r) => (
                <Chip
                  key={r.label}
                  selected={activeRange === r.label}
                  className="min-h-0 px-2 py-0.5 text-caption"
                  onClick={() => {
                    setActiveRange(r.label);
                    void loadCandles(symbol, timeframe, r.label);
                  }}
                >
                  {r.label}
                </Chip>
              ))}
            </div>
            <div className="text-micro text-muted-foreground">
              IST (UTC+5:30) · Realtime Data Feed
            </div>
          </div>
        </div>

        {/* ----------------------------------------------------------- */}
        {/* 3. RIGHT SIDEBAR: WATCHLIST & INSTRUMENT DETAILS */}
        {/* ----------------------------------------------------------- */}
        <div className="w-80 shrink-0 border-l border-border bg-card flex flex-col">
          {/* Top Watchlist Header with Add Symbol */}
          <div className="relative flex h-9 items-center justify-between border-b border-border px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            <span>Watchlist</span>
            <Tooltip content="Add symbol to watchlist" side="left" delay={400}>
            <Button
              size="icon-sm"
              variant="plain"
              onClick={() => {
                setAddSymbolOpen((prev) => !prev);
                setAddSymbolQuery("");
              }}
              aria-label="Add symbol"
            >
              <Plus size={14} />
            </Button>
            </Tooltip>

            {/* Add Symbol Dropdown / Search Modal */}
            {addSymbolOpen && (
              <div className="absolute right-2 top-9 z-50 w-72 rounded-md border border-border bg-muted p-2.5 normal-case">
                <div className="flex items-center justify-between border-b border-border pb-2 text-xs font-semibold text-white">
                  <span>Add Symbol</span>
                  <Button
                    size="icon-sm"
                    variant="plain"
                    onClick={() => setAddSymbolOpen(false)}
                    aria-label="Close search"
                  >
                    <X size={13} />
                  </Button>
                </div>
                <div className="relative mt-2">
                  <input
                    type="text"
                    placeholder="Search e.g. TATAMOTORS, INFY..."
                    value={addSymbolQuery}
                    onChange={(e) => setAddSymbolQuery(e.target.value)}
                    autoFocus
                    className="w-full rounded-md bg-card pl-7 pr-2.5 py-1.5 text-xs text-white placeholder-muted-foreground outline-none border border-border focus:border-primary"
                  />
                  <Search size={12} className="absolute left-2 top-2 text-muted-foreground" />
                </div>

                <div data-lenis-prevent className="mt-2 max-h-52 overflow-y-auto divide-y divide-border/50">
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
                          className="flex w-full items-center justify-between px-2 py-1.5 text-left text-xs transition-colors hover:bg-border"
                        >
                          <div>
                            <span className="font-semibold text-white">{r.symbol}</span>
                            <span className="ml-1.5 text-micro text-muted-foreground">{r.exchange}</span>
                          </div>
                          {alreadyInWatch ? (
                            <span className="text-micro text-muted-foreground">Added</span>
                          ) : (
                            <span className="text-micro text-primary font-semibold hover:underline">+ Add</span>
                          )}
                        </button>
                      );
                    })
                  ) : addSymbolQuery.trim().length >= 2 ? (
                    <div className="py-4 text-center text-xs text-muted-foreground">No symbols found</div>
                  ) : (
                    <div className="py-3 text-center text-caption text-muted-foreground">
                      Type 2+ characters to search NSE symbols
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Watchlist Items */}
          <div data-lenis-prevent className="flex-1 overflow-y-auto divide-y divide-border/40">
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
                    active ? "bg-border" : "hover:bg-muted"
                  )}
                >
                  <div className="min-w-0 pr-2">
                    <div className="font-semibold text-white truncate">{item.symbol.replace("-EQ", "")}</div>
                    <div className="text-micro text-muted-foreground truncate">{item.name ?? "Equity"}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="text-right tabular-nums">
                      <div className="font-medium text-white">₹{item.last.toLocaleString("en-IN")}</div>
                      <div className={cn("text-[10.5px] font-semibold", up ? "text-gain" : "text-loss")}>
                        {up ? `+${item.chg.toFixed(2)}%` : `${item.chg.toFixed(2)}%`}
                      </div>
                    </div>
                    <Tooltip content="Remove from watchlist" side="left" delay={400}>
                    <Button
                      size="icon-sm"
                      variant="plain"
                      className="hover:bg-destructive/10 hover:text-destructive opacity-0 group-hover:opacity-100"
                      onClick={(e) => {
                        e.stopPropagation();
                        setWatchlist((prev) => prev.filter((w) => w.symbol !== item.symbol));
                      }}
                      aria-label="Remove from watchlist"
                    >
                      <Trash2 size={12} />
                    </Button>
                    </Tooltip>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Bottom Instrument Details Pane (Like TradingView Right Pane) */}
          <div className="border-t border-border bg-muted/60 p-3 text-xs">
            <div className="flex items-baseline justify-between">
              <div>
                <div className="font-semibold text-white text-sm">{symbol.replace("-EQ", "")}</div>
                <div className="text-micro text-muted-foreground">NSE Equity · Market Open</div>
              </div>
              <div className="text-right">
              <div className="font-semibold text-base text-white tabular-nums">₹{currentPrice.toFixed(2)}</div>
              <div className={cn("text-xs font-semibold tabular-nums", isUp ? "text-gain" : "text-loss")}>
                  {isUp ? `+${currentChg}%` : `${currentChg}%`}
                </div>
              </div>
            </div>

            <div className="mt-3 grid grid-cols-2 gap-2 text-caption border-t border-border pt-2 text-muted-foreground">
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
                size="xs"
                variant="outline"
                onClick={() => void handleQuickOrder(true)}
                className="border-gain/40 text-gain hover:border-gain hover:bg-gain/10"
              >
                Buy
              </Button>
              <Button
                size="xs"
                variant="outline"
                onClick={() => void handleQuickOrder(false)}
                className="border-loss/40 text-loss hover:border-loss hover:bg-loss/10"
              >
                Sell
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* ========================================================================= */}
      {/* TRADINGVIEW DYNAMIC INDICATOR SETTINGS MODAL */}
      {/* ========================================================================= */}
      {pineSettingsModalOpen && (() => {
        const dynamicInputs = parsePineInputs(pineScript);
        let plots: PineSeriesResult[] = [];
        let indicatorTitle = "Indicator Settings";
        try {
          plots = evaluatePineScript(pineScript, candles, true, chartColor("blue"), pineInputs, pineStyles);
          const indMatch = pineScript.match(/indicator\s*\(\s*["']([^"']+)["']/i);
          if (indMatch) indicatorTitle = indMatch[1];
        } catch {}

        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 backdrop-blur-sm animate-in fade-in duration-150">
            <div
                className="bg-muted border border-border rounded-md w-[480px] max-w-[92vw] flex flex-col overflow-hidden text-foreground font-sans"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
                <div className="flex items-center justify-between px-5 py-3.5 border-b border-border bg-card">
                <div className="flex items-center gap-2">
                    <SlidersHorizontal size={16} className="text-primary" />
                  <span className="font-semibold text-white text-sm truncate max-w-[340px]">
                    {indicatorTitle}
                  </span>
                </div>
                <Button
                  size="icon-sm"
                  variant="plain"
                  onClick={() => setPineSettingsModalOpen(false)}
                  aria-label="Close settings"
                >
                  <X size={16} />
                </Button>
              </div>

              {/* Tabs Bar (TradingView style: Inputs | Style | Visibility) */}
                <div className="border-b border-border bg-muted px-5">
                  <Tabs
                    value={pineSettingsActiveTab}
                    onValueChange={(v) => setPineSettingsActiveTab(v as typeof pineSettingsActiveTab)}
                    variant="underline"
                  >
                    <TabsList>
                      <TabsTrigger value="inputs">Inputs</TabsTrigger>
                      <TabsTrigger value="style">Style</TabsTrigger>
                    </TabsList>
                  </Tabs>
                </div>

              {/* Tab Contents Body */}
              <div data-lenis-prevent className="p-5 max-h-[360px] overflow-y-auto space-y-4 text-xs">
                {pineSettingsActiveTab === "inputs" && (
                  <>
                    {dynamicInputs.length === 0 ? (
                        <div className="text-center py-8 text-muted-foreground font-mono text-xs">
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
                                      className="h-4 w-4 rounded-md bg-card border-border text-primary focus:ring-0 focus:ring-offset-0 cursor-pointer"
                                  />
                                ) : inputDef.type === "color" ? (
                                  <div className="flex items-center gap-2">
                                    <input
                                      type="color"
                                      value={String(currentValue).startsWith("#") ? String(currentValue) : chartColor("blue")}
                                      onChange={(e) => {
                                        const updated = { ...pineInputs, [inputDef.id]: e.target.value };
                                        setPineInputs(updated);
                                        localStorage.setItem("atr.chart.pineinputs", JSON.stringify(updated));
                                      }}
                                        className="w-7 h-7 rounded-md border border-border bg-transparent cursor-pointer p-0"
                                    />
                                    <span className="font-mono text-caption text-muted-foreground">{currentValue}</span>
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
                                      className="w-24 px-2 py-1.5 rounded-md bg-card border border-border text-white text-right font-mono text-xs focus:outline-none focus:border-primary"
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
                        <div className="text-center py-8 text-muted-foreground font-mono text-xs">
                        No plot outputs to customize.
                      </div>
                    ) : (
                      plots.map((p) => {
                        const styleConfig = pineStyles[p.name] || {};
                        const currentColor = styleConfig.color || p.color;
                        const currentWidth = styleConfig.lineWidth ?? p.lineWidth ?? 2;
                        const isVisible = styleConfig.visible !== false;

                        return (
                          <div key={p.name} className="flex items-center justify-between gap-3 border-b border-border/40 pb-2.5">
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
                                  className="h-4 w-4 rounded-md bg-card border-border text-primary cursor-pointer"
                              />
                              <span className="font-medium text-white text-xs truncate max-w-[160px]" title={p.name}>
                                {p.name}
                              </span>
                            </div>

                            <div className="flex items-center gap-3">
                              {/* Color Picker */}
                              <input
                                type="color"
                                value={currentColor.startsWith("#") ? currentColor : chartColor("blue")}
                                onChange={(e) => {
                                  const updated = {
                                    ...pineStyles,
                                    [p.name]: { ...styleConfig, color: e.target.value },
                                  };
                                  setPineStyles(updated);
                                  localStorage.setItem("atr.chart.pinestyles", JSON.stringify(updated));
                                }}
                                  className="w-6 h-6 rounded-md border border-border bg-transparent cursor-pointer p-0"
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
                <div className="flex items-center justify-between px-5 py-3 border-t border-border bg-card text-xs">
                <Button
                  size="inline"
                  variant="link"
                  onClick={() => {
                    setPineInputs({});
                    setPineStyles({});
                    localStorage.removeItem("atr.chart.pineinputs");
                    localStorage.removeItem("atr.chart.pinestyles");
                  }}
                >
                  Reset to defaults
                </Button>

                <div className="flex items-center gap-2">
                  <Button
                    size="xs"
                    variant="quiet"
                    onClick={() => setPineSettingsModalOpen(false)}
                  >
                    Close
                  </Button>
                  <Button size="xs" onClick={() => setPineSettingsModalOpen(false)}>
                    OK
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
