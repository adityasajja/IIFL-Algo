// Pine Script v6 Full-Fledged Runtime Engine for Technical Analysis
// Spec compliance: https://www.tradingview.com/pine-script-reference/v6/

export interface PineSeriesResult {
  times: (string | number)[];
  values: (number | null)[];
  color: string;
  name: string;
  isOverlay: boolean;
  style?: "line" | "histogram" | "cross" | "circles";
  lineWidth?: number;
}

export interface PineCandle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PineInputDef {
  id: string;
  title: string;
  type: "int" | "float" | "bool" | "string" | "color";
  defval: any;
  minval?: number;
  maxval?: number;
  step?: number;
  options?: string[];
}

export function parsePineInputs(code: string): PineInputDef[] {
  const inputs: PineInputDef[] = [];
  const lines = code.split("\n");

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line.startsWith("//") || line.startsWith("/*")) continue;

    // Matches: varName = input(...) or varName = input.int(...) or [type] varName = input...
    const m = line.match(/(?:(?:bool|color|float|int|string|var|varip|series|simple)\s+)?([a-zA-Z_]\w*)\s*=\s*input(?:\.(int|float|bool|string|color|source|timeframe))?\s*\((.*)\)/);
    if (!m) continue;

    const varName = m[1];
    const explicitType = m[2];
    const argsStr = m[3] || "";

    let title = varName;
    const titleMatch = argsStr.match(/title\s*=\s*["']([^"']+)["']/);
    if (titleMatch) {
      title = titleMatch[1];
    }

    let minval: number | undefined;
    const minvalMatch = argsStr.match(/minval\s*=\s*([0-9.-]+)/);
    if (minvalMatch) minval = parseFloat(minvalMatch[1]);

    let maxval: number | undefined;
    const maxvalMatch = argsStr.match(/maxval\s*=\s*([0-9.-]+)/);
    if (maxvalMatch) maxval = parseFloat(maxvalMatch[1]);

    let step: number | undefined;
    const stepMatch = argsStr.match(/step\s*=\s*([0-9.-]+)/);
    if (stepMatch) step = parseFloat(stepMatch[1]);

    // Extract first positional argument (defval) if present
    const firstArgMatch = argsStr.match(/^\s*([^,)]+)/);
    let rawFirst = firstArgMatch ? firstArgMatch[1].trim() : "";
    if (rawFirst.startsWith("defval")) {
      const defMatch = argsStr.match(/defval\s*=\s*([^,)]+)/);
      if (defMatch) rawFirst = defMatch[1].trim();
    }

    let type: "int" | "float" | "bool" | "string" | "color" = "int";
    let defval: any = 0;

    if (explicitType === "int") {
      type = "int";
      defval = parseInt(rawFirst, 10) || 0;
    } else if (explicitType === "float") {
      type = "float";
      defval = parseFloat(rawFirst) || 0;
    } else if (explicitType === "bool") {
      type = "bool";
      defval = rawFirst === "true";
    } else if (explicitType === "string") {
      type = "string";
      defval = rawFirst.replace(/["']/g, "");
    } else if (explicitType === "color") {
      type = "color";
      defval = rawFirst.replace(/["']/g, "");
    } else {
      // Inferred type
      if (rawFirst === "true" || rawFirst === "false") {
        type = "bool";
        defval = rawFirst === "true";
      } else if (!isNaN(Number(rawFirst)) && rawFirst !== "") {
        if (rawFirst.includes(".")) {
          type = "float";
          defval = parseFloat(rawFirst);
        } else {
          type = "int";
          defval = parseInt(rawFirst, 10);
        }
      } else if (rawFirst.startsWith("#") || rawFirst.startsWith("color.")) {
        type = "color";
        defval = rawFirst.replace(/["']/g, "");
      } else {
        type = "string";
        defval = rawFirst.replace(/["']/g, "");
      }
    }

    inputs.push({
      id: varName,
      title,
      type,
      defval,
      minval,
      maxval,
      step,
    });
  }

  return inputs;
}

export function evaluatePineScript(
  code: string,
  candles: PineCandle[],
  isOverlay = true,
  defaultColor = "#2962ff",
  customInputs?: Record<string, any>,
  customStyles?: Record<string, { color?: string; lineWidth?: number; visible?: boolean }>
): PineSeriesResult[] {
  if (!candles || candles.length === 0) return [];

  const n = candles.length;
  const open = candles.map((c) => c.open);
  const high = candles.map((c) => c.high);
  const low = candles.map((c) => c.low);
  const close = candles.map((c) => c.close);
  const volume = candles.map((c) => c.volume);
  const hl2 = candles.map((c) => (c.high + c.low) / 2);
  const hlc3 = candles.map((c) => (c.high + c.low + c.close) / 3);
  const ohlc4 = candles.map((c) => (c.open + c.high + c.low + c.close) / 4);
  const bar_index = candles.map((_, i) => i);
  const times = candles.map((c) => c.ts.slice(0, 10));

  // Helper to ensure input is a number series array
  const toSeries = (val: any): (number | null)[] => {
    if (Array.isArray(val)) return val;
    if (typeof val === "number") return Array(n).fill(val);
    return close;
  };

  // Math object conforming to Pine v6 math.*
  const math = {
    abs: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.abs(v))) : Math.abs(val)),
    max: (...args: any[]) => {
      if (args.length === 0) return 0;
      if (Array.isArray(args[0])) {
        return bar_index.map((i) => {
          const vals = args.map((a) => (Array.isArray(a) ? a[i] : a)).filter((v) => v != null);
          return vals.length > 0 ? Math.max(...vals) : null;
        });
      }
      return Math.max(...args);
    },
    min: (...args: any[]) => {
      if (args.length === 0) return 0;
      if (Array.isArray(args[0])) {
        return bar_index.map((i) => {
          const vals = args.map((a) => (Array.isArray(a) ? a[i] : a)).filter((v) => v != null);
          return vals.length > 0 ? Math.min(...vals) : null;
        });
      }
      return Math.min(...args);
    },
    pow: (base: any, exp: number) =>
      Array.isArray(base) ? base.map((v) => (v == null ? null : Math.pow(v, exp))) : Math.pow(base, exp),
    sqrt: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.sqrt(v))) : Math.sqrt(val)),
    round: (val: any, decimals = 2) => {
      const f = Math.pow(10, decimals);
      return Array.isArray(val)
        ? val.map((v) => (v == null ? null : Math.round(v * f) / f))
        : Math.round(val * f) / f;
    },
    floor: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.floor(v))) : Math.floor(val)),
    ceil: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.ceil(v))) : Math.ceil(val)),
    sign: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.sign(v))) : Math.sign(val)),
    log: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.log(v))) : Math.log(val)),
    exp: (val: any) => (Array.isArray(val) ? val.map((v) => (v == null ? null : Math.exp(v))) : Math.exp(val)),
    avg: (...series: any[]) => {
      return bar_index.map((i) => {
        let sum = 0;
        let count = 0;
        for (const s of series) {
          const v = Array.isArray(s) ? s[i] : s;
          if (v != null) {
            sum += v;
            count++;
          }
        }
        return count > 0 ? sum / count : null;
      });
    },
  };

  // Color object conforming to Pine v6 color.*
  const color = {
    red: "#f23645",
    green: "#089981",
    blue: "#2962ff",
    orange: "#ff9800",
    purple: "#ab47bc",
    yellow: "#ffeb3b",
    teal: "#00897b",
    white: "#ffffff",
    black: "#000000",
    gray: "#787b86",
    aqua: "#00e5ff",
    lime: "#00e676",
    fuchsia: "#e040fb",
    silver: "#b2b5be",
    navy: "#1a237e",
    maroon: "#880e4f",
    olive: "#827717",
    new: (col: string, _transp = 0) => col,
    from_gradient: (val: number, _bottomVal: number, topVal: number, bottomCol: string, topCol: string) =>
      val >= topVal ? topCol : bottomCol,
  };

  // Technical Analysis Library conforming to Pine v6 ta.*
  const ta = {
    // 1. Moving Averages
    sma: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const res: (number | null)[] = [];
      let sum = 0;
      for (let i = 0; i < n; i++) {
        const val = s[i] ?? 0;
        sum += val;
        if (i >= len) sum -= s[i - len] ?? 0;
        res.push(i >= len - 1 ? sum / len : null);
      }
      return res;
    },

    ema: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const res: (number | null)[] = [];
      const k = 2 / (len + 1);
      let e: number | null = null;
      for (let i = 0; i < n; i++) {
        const v = s[i];
        if (v == null) {
          res.push(null);
          continue;
        }
        e = e === null ? v : (v - e) * k + e;
        res.push(i >= len - 1 ? e : null);
      }
      return res;
    },

    wma: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const res: (number | null)[] = [];
      const weightSum = (len * (len + 1)) / 2;
      for (let i = 0; i < n; i++) {
        if (i < len - 1) {
          res.push(null);
          continue;
        }
        let sum = 0;
        for (let j = 0; j < len; j++) {
          sum += (s[i - len + 1 + j] ?? 0) * (j + 1);
        }
        res.push(sum / weightSum);
      }
      return res;
    },

    rma: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const alpha = 1 / len;
      const res: (number | null)[] = [];
      let sum = 0;
      for (let i = 0; i < n; i++) {
        const v = s[i] ?? 0;
        sum = i === 0 ? v : alpha * v + (1 - alpha) * sum;
        res.push(i >= len - 1 ? sum : null);
      }
      return res;
    },

    vwma: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const pv = s.map((v, i) => (v != null ? v * volume[i] : 0));
      const sumPv = ta.sma(pv, len);
      const sumV = ta.sma(volume, len);
      return sumPv.map((val, i) => (val != null && sumV[i] ? val / sumV[i]! : null));
    },

    hma: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const halfLen = Math.floor(len / 2);
      const sqrtLen = Math.floor(Math.sqrt(len));
      const wma1 = ta.wma(s, halfLen);
      const wma2 = ta.wma(s, len);
      const diff = wma1.map((v1, i) => (v1 != null && wma2[i] != null ? 2 * v1 - wma2[i]! : 0));
      return ta.wma(diff, sqrtLen);
    },

    // 2. Oscillators & Momentum
    rsi: (src: any, len = 14): (number | null)[] => {
      const s = toSeries(src);
      const res: (number | null)[] = [];
      if (s.length < len + 1) return s.map(() => null);
      let gains = 0;
      let losses = 0;
      for (let i = 1; i <= len; i++) {
        const d = (s[i] ?? 0) - (s[i - 1] ?? 0);
        if (d >= 0) gains += d;
        else losses -= d;
      }
      let avgG = gains / len;
      let avgL = losses / len;
      for (let i = 0; i < len; i++) res.push(null);
      res.push(avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL));

      for (let i = len + 1; i < n; i++) {
        const d = (s[i] ?? 0) - (s[i - 1] ?? 0);
        avgG = (avgG * (len - 1) + (d > 0 ? d : 0)) / len;
        avgL = (avgL * (len - 1) + (d < 0 ? -d : 0)) / len;
        res.push(avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL));
      }
      return res;
    },

    macd: (src: any, fastLen = 12, slowLen = 26, sigLen = 9) => {
      const s = toSeries(src);
      const fast = ta.ema(s, fastLen);
      const slow = ta.ema(s, slowLen);
      const macdLine: (number | null)[] = [];
      for (let i = 0; i < n; i++) {
        if (fast[i] === null || slow[i] === null) macdLine.push(null);
        else macdLine.push((fast[i] as number) - (slow[i] as number));
      }
      const validMacd = macdLine.map((v) => (v === null ? 0 : v));
      const signal = ta.ema(validMacd, sigLen);
      const hist: (number | null)[] = [];
      for (let i = 0; i < n; i++) {
        if (macdLine[i] === null || signal[i] === null) hist.push(null);
        else hist.push((macdLine[i] as number) - (signal[i] as number));
      }
      return [macdLine, signal, hist];
    },

    stoch: (closeSrc: any, highSrc: any, lowSrc: any, len = 14): (number | null)[] => {
      const c = toSeries(closeSrc);
      const h = toSeries(highSrc);
      const l = toSeries(lowSrc);
      const hh = ta.highest(h, len);
      const ll = ta.lowest(l, len);
      return c.map((val, i) => {
        if (val == null || hh[i] == null || ll[i] == null || hh[i] === ll[i]) return null;
        return ((val - ll[i]!) / (hh[i]! - ll[i]!)) * 100;
      });
    },

    cci: (src: any, len = 20): (number | null)[] => {
      const s = toSeries(src);
      const smaVal = ta.sma(s, len);
      return s.map((val, i) => {
        if (val == null || smaVal[i] == null) return null;
        let meanDev = 0;
        for (let j = i - len + 1; j <= i; j++) meanDev += Math.abs((s[j] ?? 0) - smaVal[i]!);
        meanDev /= len;
        return meanDev === 0 ? 0 : (val - smaVal[i]!) / (0.015 * meanDev);
      });
    },

    cmo: (src: any, len = 14): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i < len) return null;
        let sumU = 0;
        let sumD = 0;
        for (let j = i - len + 1; j <= i; j++) {
          const diff = (s[j] ?? 0) - (s[j - 1] ?? 0);
          if (diff >= 0) sumU += diff;
          else sumD += -diff;
        }
        return sumU + sumD === 0 ? 0 : ((sumU - sumD) / (sumU + sumD)) * 100;
      });
    },

    mfi: (src: any, len = 14): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i < len) return null;
        let posFlow = 0;
        let negFlow = 0;
        for (let j = i - len + 1; j <= i; j++) {
          const mf = (s[j] ?? 0) * volume[j];
          if ((s[j] ?? 0) > (s[j - 1] ?? 0)) posFlow += mf;
          else if ((s[j] ?? 0) < (s[j - 1] ?? 0)) negFlow += mf;
        }
        if (negFlow === 0) return 100;
        const mr = posFlow / negFlow;
        return 100 - 100 / (1 + mr);
      });
    },

    // 3. Volatility & Bands
    tr: (): number[] => {
      const res: number[] = [];
      for (let i = 0; i < n; i++) {
        if (i === 0) res.push(high[0] - low[0]);
        else {
          const h_l = high[i] - low[i];
          const h_pc = Math.abs(high[i] - close[i - 1]);
          const l_pc = Math.abs(low[i] - close[i - 1]);
          res.push(Math.max(h_l, h_pc, l_pc));
        }
      }
      return res;
    },

    atr: (len = 14): (number | null)[] => {
      return ta.rma(ta.tr(), len);
    },

    bb: (src: any, len = 20, mult = 2) => {
      const s = toSeries(src);
      const basis = ta.sma(s, len);
      const dev = ta.stdev(s, len);
      const upper = basis.map((b, i) => (b != null && dev[i] != null ? b + mult * dev[i]! : null));
      const lower = basis.map((b, i) => (b != null && dev[i] != null ? b - mult * dev[i]! : null));
      return [basis, upper, lower];
    },

    kc: (src: any, len = 20, mult = 1.5, useTrueRange = true) => {
      const s = toSeries(src);
      const basis = ta.ema(s, len);
      const range = useTrueRange ? ta.atr(len) : ta.ema(high.map((h, i) => h - low[i]), len);
      const upper = basis.map((b, i) => (b != null && range[i] != null ? b + mult * range[i]! : null));
      const lower = basis.map((b, i) => (b != null && range[i] != null ? b - mult * range[i]! : null));
      return [basis, upper, lower];
    },

    supertrend: (factor = 3, atrPeriod = 10) => {
      const atrVal = ta.atr(atrPeriod);
      const trend: (number | null)[] = [];
      const dir: (number | null)[] = [];
      let prevUp = 0;
      let prevDn = 0;
      let currentDir = 1;

      for (let i = 0; i < n; i++) {
        if (atrVal[i] == null) {
          trend.push(null);
          dir.push(null);
          continue;
        }
        const med = hl2[i];
        let up = med - factor * atrVal[i]!;
        let dn = med + factor * atrVal[i]!;

        if (i > 0 && close[i - 1] > prevUp) up = Math.max(up, prevUp);
        if (i > 0 && close[i - 1] < prevDn) dn = Math.min(dn, prevDn);

        if (currentDir === 1) {
          if (close[i] < prevUp) {
            currentDir = -1;
            trend.push(dn);
          } else {
            trend.push(up);
          }
        } else {
          if (close[i] > prevDn) {
            currentDir = 1;
            trend.push(up);
          } else {
            trend.push(dn);
          }
        }
        dir.push(currentDir);
        prevUp = up;
        prevDn = dn;
      }
      return [trend, dir];
    },

    // 4. Statistical Functions
    stdev: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      const res: (number | null)[] = [];
      for (let i = 0; i < n; i++) {
        if (i < len - 1) {
          res.push(null);
          continue;
        }
        let sum = 0;
        for (let j = i - len + 1; j <= i; j++) sum += s[j] ?? 0;
        const mean = sum / len;
        let varSum = 0;
        for (let j = i - len + 1; j <= i; j++) varSum += Math.pow((s[j] ?? 0) - mean, 2);
        res.push(Math.sqrt(varSum / len));
      }
      return res;
    },

    highest: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i + 1 < len) return null;
        let m = -Infinity;
        for (let j = i + 1 - len; j <= i; j++) {
          const v = s[j];
          if (v != null && v > m) m = v;
        }
        return m === -Infinity ? null : m;
      });
    },

    lowest: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i + 1 < len) return null;
        let m = Infinity;
        for (let j = i + 1 - len; j <= i; j++) {
          const v = s[j];
          if (v != null && v < m) m = v;
        }
        return m === Infinity ? null : m;
      });
    },

    highestbars: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i + 1 < len) return null;
        let maxVal = -Infinity;
        let offset = 0;
        for (let j = i; j > i - len; j--) {
          const v = s[j];
          if (v != null && v > maxVal) {
            maxVal = v;
            offset = j - i;
          }
        }
        return offset;
      });
    },

    lowestbars: (src: any, len: number): (number | null)[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i + 1 < len) return null;
        let minVal = Infinity;
        let offset = 0;
        for (let j = i; j > i - len; j--) {
          const v = s[j];
          if (v != null && v < minVal) {
            minVal = v;
            offset = j - i;
          }
        }
        return offset;
      });
    },

    // 5. Direction & Crosses
    crossover: (a: any, b: any): boolean[] => {
      const sa = toSeries(a);
      const sb = toSeries(b);
      return sa.map((val, i) => {
        if (i === 0 || val == null || sb[i] == null || sa[i - 1] == null || sb[i - 1] == null) return false;
        return sa[i - 1]! <= sb[i - 1]! && val > sb[i]!;
      });
    },

    crossunder: (a: any, b: any): boolean[] => {
      const sa = toSeries(a);
      const sb = toSeries(b);
      return sa.map((val, i) => {
        if (i === 0 || val == null || sb[i] == null || sa[i - 1] == null || sb[i - 1] == null) return false;
        return sa[i - 1]! >= sb[i - 1]! && val < sb[i]!;
      });
    },

    cross: (a: any, b: any): boolean[] => {
      const up = ta.crossover(a, b);
      const dn = ta.crossunder(a, b);
      return up.map((v, i) => v || dn[i]);
    },

    rising: (src: any, len: number): boolean[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i < len) return false;
        for (let j = i; j > i - len; j--) {
          if ((s[j] ?? 0) <= (s[j - 1] ?? 0)) return false;
        }
        return true;
      });
    },

    falling: (src: any, len: number): boolean[] => {
      const s = toSeries(src);
      return s.map((_, i) => {
        if (i < len) return false;
        for (let j = i; j > i - len; j--) {
          if ((s[j] ?? 0) >= (s[j - 1] ?? 0)) return false;
        }
        return true;
      });
    },

    change: (src: any, len = 1): (number | null)[] => {
      const s = toSeries(src);
      return s.map((v, i) => (i >= len && v != null && s[i - len] != null ? v - s[i - len]! : null));
    },

    cum: (src: any): (number | null)[] => {
      const s = toSeries(src);
      let acc = 0;
      return s.map((v) => {
        if (v != null) acc += v;
        return acc;
      });
    },
  };

  // Detect overlay property from indicator/strategy declaration if present
  let detectedOverlay = isOverlay;
  const overlayMatch = code.match(/indicator\s*\([^)]*overlay\s*=\s*(true|false)/i);
  if (overlayMatch) {
    detectedOverlay = overlayMatch[1].toLowerCase() === "true";
  }

  const plots: PineSeriesResult[] = [];

  // Plotting API conforming to Pine v6 plot() and hline()
  const plot = (data: any, title = "Custom Plot", plotColor: any = defaultColor, width = 2) => {
    const values = toSeries(data);
    let resolvedColor = Array.isArray(plotColor)
      ? (plotColor[plotColor.length - 1] || defaultColor)
      : (plotColor || defaultColor);
    let resolvedWidth = typeof width === "number" ? width : 2;

    if (customStyles && customStyles[title]) {
      if (customStyles[title].visible === false) return;
      if (customStyles[title].color) resolvedColor = customStyles[title].color!;
      if (customStyles[title].lineWidth) resolvedWidth = customStyles[title].lineWidth!;
    }

    plots.push({
      times,
      values,
      color: resolvedColor,
      name: title,
      isOverlay: detectedOverlay,
      lineWidth: resolvedWidth,
    });
  };

  const hline = (level: number, title = "Level", lineCol: any = "rgba(120, 123, 134, 0.6)", _style?: any) => {
    const values = times.map(() => level);
    let resolvedColor = Array.isArray(lineCol)
      ? (lineCol[lineCol.length - 1] || "rgba(120, 123, 134, 0.6)")
      : (lineCol || "rgba(120, 123, 134, 0.6)");
    let resolvedWidth = 1;

    if (customStyles && customStyles[title]) {
      if (customStyles[title].visible === false) return;
      if (customStyles[title].color) resolvedColor = customStyles[title].color!;
      if (customStyles[title].lineWidth) resolvedWidth = customStyles[title].lineWidth!;
    }

    plots.push({
      times,
      values,
      color: resolvedColor,
      name: title,
      isOverlay: detectedOverlay,
      lineWidth: resolvedWidth,
    });
  };

  // Input system conforming to Pine v6 input.* with user overrides
  const input = {
    int: (defval: number, _options?: any) => defval,
    float: (defval: number, _options?: any) => defval,
    bool: (defval: boolean, _options?: any) => defval,
    string: (defval: string, _options?: any) => defval,
    color: (defval: string, _options?: any) => defval,
    source: (defval: any, _options?: any) => defval,
    timeframe: (defval: string, _options?: any) => defval,
  };

  const hline_style = {
    style_solid: 0,
    style_dotted: 1,
    style_dashed: 2,
  };

  // Pine Script Execution Engine
  try {
    const scope: Record<string, any> = {
      open,
      high,
      low,
      close,
      volume,
      hl2,
      hlc3,
      ohlc4,
      bar_index,
      ta,
      math,
      color,
      input,
      plot,
      hline,
      hline_style,
      toSeries,
    };

    const rawLines = code
      .replace(/\/\/.*$/gm, "") // Strip single-line comments
      .replace(/\/\*[\s\S]*?\*\//gm, "") // Strip block comments
      .replace(/@version=\d+/gi, "") // Strip version tag
      .replace(/^[ \t]*(indicator|strategy)\b.*$/gm, "") // Strip indicator/strategy lines cleanly
      .replace(/indicator\([^)]*\);?/gi, "") // Extra safety for inline
      .replace(/strategy\([^)]*\);?/gi, "")
      .replace(/color\.new\(([^,]+),[^)]*\)/g, "$1") // Simplify color.new
      // Remove Pine type keywords (e.g., bool isUp =, color lineC =, float x =, int y =, series float z =)
      .replace(/\b(bool|color|float|int|string|var|varip|series|simple)\s+([a-zA-Z_]\w*\s*=)/g, "$2")
      // Convert raw hex colors (#RRGGBB / #RRGGBBAA) into quoted strings "#RRGGBB"
      .replace(/(?<!["'\w])#([0-9a-fA-F]{3,8})\b/g, '"#$1"')
      // Replace logical 'and'/'or'/'not' with JS '&&'/'||'/'!'
      .replace(/\band\b/g, "&&")
      .replace(/\bor\b/g, "||")
      .replace(/\bnot\b/g, "!")
      // Replace hline.style_* references
      .replace(/hline\.style_dashed/g, "2")
      .replace(/hline\.style_dotted/g, "1")
      .replace(/hline\.style_solid/g, "0")
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length > 0);

    const statements: string[] = [];
    for (const raw of rawLines) {
      const parts = raw.split(";").map((p) => p.trim()).filter((p) => p.length > 0);
      statements.push(...parts);
    }

    for (const stmt of statements) {
      const assignMatch = stmt.match(/^([a-zA-Z_]\w*)\s*=\s*(.*)$/);
      const tupleMatch = stmt.match(/^\[([a-zA-Z0-9_,\s]+)\]\s*=\s*(.*)$/);

      if (tupleMatch) {
        const vars = tupleMatch[1].split(",").map((v) => v.trim());
        const rhs = tupleMatch[2].trim();
        const keys = Object.keys(scope);
        const vals = keys.map((k) => scope[k]);
        const fn = new Function(...keys, `return (${rhs});`);
        const res = fn(...vals);
        if (Array.isArray(res)) {
          vars.forEach((v, idx) => {
            scope[v] = res[idx];
          });
        }
        continue;
      }

      if (assignMatch) {
        const varName = assignMatch[1];
        const rhs = assignMatch[2].trim();

        // If a user has provided a custom input for this variable (from the settings modal), use it
        if (customInputs && customInputs[varName] !== undefined) {
          scope[varName] = customInputs[varName];
          continue;
        }

        // Check if RHS is a direct function call on ta.*, input.*, math.* without arithmetic operators outside parentheses
        let isDirectFn = false;
        if (
          /^(ta\.|input\.|math\.)[a-zA-Z_]\w*\s*\(.*\)$/.test(rhs) &&
          !/[+\-*\/?:<>]/.test(rhs.replace(/\([^)]*\)/g, ""))
        ) {
          try {
            const keys = Object.keys(scope);
            const vals = keys.map((k) => scope[k]);
            const fn = new Function(...keys, `return (${rhs});`);
            const res = fn(...vals);
            scope[varName] = res;
            isDirectFn = true;
          } catch {
            isDirectFn = false;
          }
        }

        if (!isDirectFn) {
          // Series/bar-by-bar expression:
          // Transform history references: identifier[offset] -> _hist("identifier", _i, offset)
          const transformedRhs = rhs.replace(/([a-zA-Z_]\w*)\[(\d+)\]/g, '_hist("$1", _i, $2)');

          const seriesKeys = Object.keys(scope).filter(
            (k) => Array.isArray(scope[k]) && scope[k].length === n
          );
          const nonSeriesKeys = Object.keys(scope).filter((k) => !seriesKeys.includes(k));

          const evaluatorFn = new Function(
            ...nonSeriesKeys,
            ...seriesKeys,
            "_i",
            "_hist",
            `
            try {
              return (${transformedRhs});
            } catch(e) {
              return null;
            }
            `
          );

          const nonSeriesVals = nonSeriesKeys.map((k) => scope[k]);

          const _hist = (name: string, currIdx: number, offset: number) => {
            const arr = scope[name];
            if (Array.isArray(arr)) {
              const idx = currIdx - offset;
              return idx >= 0 ? arr[idx] : null;
            }
            return arr;
          };

          const resultSeries: any[] = [];
          for (let i = 0; i < n; i++) {
            const currentSeriesVals = seriesKeys.map((k) => scope[k][i]);
            const val = evaluatorFn(...nonSeriesVals, ...currentSeriesVals, i, _hist);
            resultSeries.push(val);
          }

          scope[varName] = resultSeries;
        }
      } else {
        // Direct statement such as plot(...) or hline(...)
        const keys = Object.keys(scope);
        const vals = keys.map((k) => scope[k]);
        try {
          const fn = new Function(...keys, stmt);
          fn(...vals);
        } catch (e) {
          console.warn("Statement execution error:", stmt, e);
        }
      }
    }
  } catch (err: any) {
    console.warn("Pine Script evaluation error:", err);
    throw err;
  }

  return plots;
}

export const PINE_PRESETS: { name: string; desc: string; code: string }[] = [
  {
    name: "Golden EMA Cross (9 / 21)",
    desc: "Fast trend direction using Exponential Moving Averages",
    code: `//@version=6
// Golden EMA Cross Strategy
fast = ta.ema(close, 9);
slow = ta.ema(close, 21);
plot(fast, "EMA 9", color.aqua, 2);
plot(slow, "EMA 21", color.fuchsia, 2);`,
  },
  {
    name: "Supertrend (10, 3.0)",
    desc: "Volatility trailing stop and reversal trend indicator",
    code: `//@version=6
// Classic Supertrend
[trend, dir] = ta.supertrend(3.0, 10);
plot(trend, "Supertrend", color.orange, 2);`,
  },
  {
    name: "Bollinger Bands (20, 2.0)",
    desc: "Standard deviation bands around 20-period SMA",
    code: `//@version=6
// Bollinger Bands v6
[basis, upper, lower] = ta.bb(close, 20, 2.0);
plot(basis, "Basis", color.blue, 1);
plot(upper, "Upper Band", color.green, 2);
plot(lower, "Lower Band", color.red, 2);`,
  },
  {
    name: "Hull Moving Average (HMA 20)",
    desc: "Extremely smooth low-lag moving average",
    code: `//@version=6
// Hull Moving Average
h = ta.hma(close, 20);
plot(h, "HMA 20", color.lime, 2);`,
  },
  {
    name: "Keltner Channels (20, 1.5)",
    desc: "ATR volatility envelope around EMA",
    code: `//@version=6
// Keltner Channels
[basis, upper, lower] = ta.kc(close, 20, 1.5);
plot(basis, "KC Basis", color.yellow, 1);
plot(upper, "KC Upper", color.teal, 2);
plot(lower, "KC Lower", color.teal, 2);`,
  },
  {
    name: "RSI Momentum Bands",
    desc: "Custom overlay using 14-period RSI levels",
    code: `//@version=6
// RSI with overbought / oversold horizontal levels
r = ta.rsi(close, 14);
plot(r, "RSI 14", color.purple, 2);
hline(70, "Overbought", color.red);
hline(30, "Oversold", color.green);`,
  },
];
