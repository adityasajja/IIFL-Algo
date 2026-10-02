import { AlertTriangle, ChevronDown, Code2, ListChecks, Play, Plus, Trash2, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createSavedStrategy,
  createStrategyVersion,
  listDeployments,
  listSavedStrategies,
  removeSavedStrategy,
  createDeployment,
  deleteStrategyVersion,
  getStrategyVersion,
  getResearchedStocks,
  startDeployment,
  listStrategyVersions,
  type Deployment,
  type SavedStrategy,
  type StrategyValidation,
  type StrategyVersion,
} from "./api";
import { Button } from "./components/ui/button";
import { Tooltip } from "./components/motion/tooltip";
import { BouncyAccordion } from "./components/motion/bouncy-accordion";
import { Card, CardHeader, ErrorBox } from "./components/ui/card";
import { useDialog } from "./components/ui/dialog-context";
import { Input } from "./components/motion/input";
import { Select } from "./components/ui/select";
import { Badge, fmtMoney } from "./components/ui/stat";
import { strategyLabel } from "./lib/format";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/** One cell in the 4-up summary grid at the top of the Strategies page. */
function SummaryStat({
  label,
  value,
  sub,
  accent = false,
  bad = false,
  isMoney = false,
  onClick,
}: {
  label: string;
  value: number | null;
  sub: string;
  accent?: boolean;
  bad?: boolean;
  isMoney?: boolean;
  onClick?: () => void;
}) {
  const base = "flex flex-col gap-1 rounded-xl border border-border/60 bg-card p-4 text-left transition-colors";
  const valueColor = accent ? "text-gain" : bad ? "text-loss" : "text-foreground";

  let display: React.ReactNode = "—";
  if (value !== null) {
    if (isMoney) {
      const abs = Math.abs(value);
      const sign = value > 0 ? "+ " : value < 0 ? "− " : "";
      display = <>{sign}{fmtMoney(abs)}</>;
    } else {
      display = value;
    }
  }

  const inner = (
    <>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <span className={cn("text-3xl font-semibold tabular-nums tracking-tight leading-none", valueColor)}>
        {display}
      </span>
      <span className="text-xs text-muted-foreground">{sub}</span>
    </>
  );

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={cn(base, "cursor-pointer hover:bg-muted/50")}>
        {inner}
      </button>
    );
  }
  return <div className={base}>{inner}</div>;
}


/**
 * Strategies — what is running, and the rules you have built.
 *
 * The status tiles at the top show what is doing something now. The 18 built-in
 * strategies that used to be listed here were removed: none had passed validation, so
 * the list was noise. Their engines still exist for backtests and the Evidence page.
 *
 * **Your strategies** is the authoring half: create a strategy, append an
 * immutable version, validate it, and then deploy that exact version on the Paper
 * screen. A deployment pins `(strategy_id, strategy_version)`, and this is the
 * only place that pair can be produced from the screen. Nothing here claims a
 * strategy works; validation answers "will this execute", and the panel says so
 * in as many words rather than letting a green tick imply the other question.
 */
export default function StrategiesPanel({
  onOpenPaper,
}: {
  onOpenPaper: () => void;
}) {
  const [deployments, setDeployments] = useState<Deployment[]>([]);

  useEffect(() => {
    listDeployments()
      .then((r) => setDeployments(r?.deployments ?? []))
      .catch(() => setDeployments([]));
  }, []);

  return (
    <Authoring
      onOpenPaper={onOpenPaper}
      deployments={deployments}
    />
  );
}

/** The server's own sentence, not the JSON it arrived in. */
function readable(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    const body = JSON.parse(raw) as { detail?: string | { detail?: string } };
    const d = body.detail;
    const text = typeof d === "string" ? d : d?.detail;
    if (text) return text;
  } catch {
    // not JSON: use it as it is
  }
  return raw;
}

// ─── Custom formula builder ────────────────────────────────────────────────
// Assembles the same `custom_entry_formula` / `custom_exit_formula` strings
// that `atr.signals.formula.build_context` reads — a strategy built here is
// not a different, weaker thing from one written by hand, it produces the
// identical definition JSON. See `atr/signals/formula.py` and
// `atr/signals/chart_patterns.py` / `candlestick_patterns.py` for the full
// set this only shows a curated slice of.
type FieldType = "number" | "bool";
type FieldOption = {
  value: string;
  label: string;
  type: FieldType;
  /** True for the 8 functions `formula.py` exposes as `name(n)` — any period,
   * not just the handful `build_context` happens to precompute a fixed name
   * for (`rsi14`, `sma20`, ...). Rendered with an extra period input, and
   * compiled as a call: `rsi(20)`, not the string `"rsi20"`. */
  parametric?: boolean;
  defaultPeriod?: number;
};

const CONDITION_FIELDS: FieldOption[] = [
  { value: "price", label: "Price", type: "number" },
  { value: "pnl_pct", label: "P&L since entry (%)", type: "number" },
  { value: "day_chg_pct", label: "Change since yesterday (%)", type: "number" },
  { value: "rsi", label: "RSI", type: "number", parametric: true, defaultPeriod: 14 },
  { value: "sma", label: "Simple moving average", type: "number", parametric: true, defaultPeriod: 20 },
  { value: "ema", label: "Exponential moving average", type: "number", parametric: true, defaultPeriod: 20 },
  { value: "wma", label: "Weighted moving average", type: "number", parametric: true, defaultPeriod: 20 },
  { value: "atr", label: "ATR (volatility)", type: "number", parametric: true, defaultPeriod: 14 },
  { value: "stoch_k", label: "Stochastic %K", type: "number", parametric: true, defaultPeriod: 14 },
  { value: "highest", label: "Highest high over", type: "number", parametric: true, defaultPeriod: 20 },
  { value: "lowest", label: "Lowest low over", type: "number", parametric: true, defaultPeriod: 20 },
  { value: "macd_hist", label: "MACD histogram", type: "number" },
  { value: "bb_width_pct", label: "Bollinger band width (%)", type: "number" },
  { value: "roc20", label: "20-day return (%)", type: "number" },
  { value: "streak_up", label: "Consecutive up days", type: "number" },
  { value: "streak_down", label: "Consecutive down days", type: "number" },
  { value: "volume", label: "Volume", type: "number" },
  { value: "vol_avg20", label: "20-day average volume", type: "number" },
  { value: "peak20", label: "20-day high", type: "number" },
  { value: "peak60", label: "60-day high", type: "number" },
  // --- trendlines & Fibonacci — atr.signals.trendlines ---
  { value: "resistance_trendline", label: "Trendline: resistance level", type: "number" },
  { value: "support_trendline", label: "Trendline: support level", type: "number" },
  { value: "trendline_breakout_up", label: "Trendline: broke out upward", type: "bool" },
  { value: "trendline_breakout_down", label: "Trendline: broke down", type: "bool" },
  { value: "fib_236", label: "Fibonacci 23.6% level", type: "number" },
  { value: "fib_382", label: "Fibonacci 38.2% level", type: "number" },
  { value: "fib_500", label: "Fibonacci 50% level", type: "number" },
  { value: "fib_618", label: "Fibonacci 61.8% level", type: "number" },
  { value: "fib_786", label: "Fibonacci 78.6% level", type: "number" },
  { value: "fib_0", label: "Fibonacci swing start", type: "number" },
  { value: "fib_100", label: "Fibonacci swing end", type: "number" },
  // --- intraday session context — 0 on daily bars, real only on 1m/5m/etc ---
  { value: "session_vwap", label: "Session VWAP (intraday only)", type: "number" },
  { value: "vwap_upper_1", label: "VWAP +1 std dev (intraday only)", type: "number" },
  { value: "vwap_lower_1", label: "VWAP −1 std dev (intraday only)", type: "number" },
  { value: "vwap_upper_2", label: "VWAP +2 std dev (intraday only)", type: "number" },
  { value: "vwap_lower_2", label: "VWAP −2 std dev (intraday only)", type: "number" },
  { value: "minutes_since_open", label: "Minutes since session open (intraday only)", type: "number" },
  { value: "or_high", label: "Opening-range high, 15 min (intraday only)", type: "number" },
  { value: "or_low", label: "Opening-range low, 15 min (intraday only)", type: "number" },
  { value: "today_open", label: "Today's open (intraday only)", type: "number" },
  { value: "open_eq_high_day", label: "Open has been the day's high so far (intraday only)", type: "bool" },
  { value: "open_eq_low_day", label: "Open has been the day's low so far (intraday only)", type: "bool" },
  // --- chart patterns — atr.signals.chart_patterns (all 19) ---
  { value: "head_and_shoulders", label: "Pattern: head & shoulders (top)", type: "bool" },
  { value: "inverse_head_and_shoulders", label: "Pattern: head & shoulders (bottom)", type: "bool" },
  { value: "double_top", label: "Pattern: double top", type: "bool" },
  { value: "double_bottom", label: "Pattern: double bottom", type: "bool" },
  { value: "triple_top", label: "Pattern: triple top", type: "bool" },
  { value: "triple_bottom", label: "Pattern: triple bottom", type: "bool" },
  { value: "ascending_triangle", label: "Pattern: ascending triangle", type: "bool" },
  { value: "descending_triangle", label: "Pattern: descending triangle", type: "bool" },
  { value: "symmetric_triangle", label: "Pattern: symmetric triangle", type: "bool" },
  { value: "rising_wedge", label: "Pattern: rising wedge", type: "bool" },
  { value: "falling_wedge", label: "Pattern: falling wedge", type: "bool" },
  { value: "rectangle", label: "Pattern: rectangle / sideways range", type: "bool" },
  { value: "broadening_formation", label: "Pattern: broadening formation", type: "bool" },
  { value: "bull_flag", label: "Pattern: bull flag", type: "bool" },
  { value: "bear_flag", label: "Pattern: bear flag", type: "bool" },
  { value: "bullish_pennant", label: "Pattern: bullish pennant", type: "bool" },
  { value: "bearish_pennant", label: "Pattern: bearish pennant", type: "bool" },
  { value: "cup_and_handle", label: "Pattern: cup and handle", type: "bool" },
  { value: "rounding_bottom", label: "Pattern: rounding bottom", type: "bool" },
  { value: "rounding_top", label: "Pattern: rounding top", type: "bool" },
  { value: "double_bottom_breakout", label: "Pattern: double bottom (confirmed breakout)", type: "bool" },
  { value: "double_top_breakdown", label: "Pattern: double top (confirmed breakdown)", type: "bool" },
  { value: "inverse_hs_breakout", label: "Pattern: inverse H&S (confirmed breakout)", type: "bool" },
  { value: "head_shoulders_breakdown", label: "Pattern: head & shoulders (confirmed breakdown)", type: "bool" },
  { value: "ascending_triangle_breakout", label: "Pattern: ascending triangle (confirmed breakout)", type: "bool" },
  { value: "rectangle_breakout", label: "Pattern: rectangle (confirmed breakout)", type: "bool" },
  // --- candlesticks — atr.signals.candlestick_patterns (all 14) ---
  { value: "doji", label: "Candle: doji", type: "bool" },
  { value: "marubozu_bull", label: "Candle: bullish marubozu (no wicks)", type: "bool" },
  { value: "marubozu_bear", label: "Candle: bearish marubozu (no wicks)", type: "bool" },
  { value: "hammer", label: "Candle: hammer", type: "bool" },
  { value: "shooting_star", label: "Candle: shooting star", type: "bool" },
  { value: "spinning_top", label: "Candle: spinning top (indecision)", type: "bool" },
  { value: "bullish_engulfing", label: "Candle: bullish engulfing", type: "bool" },
  { value: "bearish_engulfing", label: "Candle: bearish engulfing", type: "bool" },
  { value: "piercing_line", label: "Candle: piercing line", type: "bool" },
  { value: "dark_cloud_cover", label: "Candle: dark cloud cover", type: "bool" },
  { value: "three_white_soldiers", label: "Candle: three white soldiers", type: "bool" },
  { value: "three_black_crows", label: "Candle: three black crows", type: "bool" },
  { value: "morning_star", label: "Candle: morning star", type: "bool" },
  { value: "evening_star", label: "Candle: evening star", type: "bool" },
];
const FIELD_MAP = new Map(CONDITION_FIELDS.map((f) => [f.value, f]));

const NUMBER_OPS = [
  { value: "<", label: "is below" },
  { value: "<=", label: "is at or below" },
  { value: ">", label: "is above" },
  { value: ">=", label: "is at or above" },
  { value: "==", label: "equals" },
] as const;

const BOOL_OPS = [
  { value: "is", label: "just happened" },
  { value: "not", label: "did not happen" },
] as const;

type Condition = { id: string; field: string; op: string; value: string; period: string };
let conditionSeq = 0;
const newCondition = (): Condition => ({ id: `c${++conditionSeq}`, field: "rsi", op: "<", value: "30", period: "14" });

/** One condition row into its formula fragment — "rsi(20) < 30" for a
 * parametric field (any period the user typed), "sma200 > price" for a fixed
 * precomputed name, or "hammer == 1" for a pattern flag. */
function conditionToFormula(c: Condition): string | null {
  const field = FIELD_MAP.get(c.field);
  if (!field) return null;
  if (field.type === "bool") return `${c.field} == ${c.op === "not" ? 0 : 1}`;
  const n = Number(c.value);
  if (!Number.isFinite(n)) return null;
  if (field.parametric) {
    const period = Math.max(1, Math.round(Number(c.period)) || field.defaultPeriod || 14);
    return `${c.field}(${period}) ${c.op} ${n}`;
  }
  return `${c.field} ${c.op} ${n}`;
}

/** A row list + join word into one formula string ATR's backend evaluates. */
function conditionsToFormula(conditions: Condition[], join: "and" | "or"): string {
  const parts = conditions.map(conditionToFormula).filter((p): p is string => p !== null);
  return parts.join(` ${join} `);
}

type Rules = {
  kind: "breakout" | "triple" | "gap" | "custom";
  gap: string;
  market: string;
  lookback: string;
  volume: string;
  stop: string;
  target: string;
  below: string;
  sellAt: string;
  // custom formula builder state
  entryConditions: Condition[];
  entryJoin: "and" | "or";
  exitConditions: Condition[];
  exitJoin: "and" | "or";
  customMinHistory: string;
  customAdvanced: boolean;
  customEntryFormula: string;
  customExitFormula: string;
  // Off by default: "Buy when" above is the *entire* entry condition. On
  // means also fire on any of the 5 built-in setups (trend-pullback,
  // breakout, RSI-oversold, triple-RSI, Monday-gap) at their own default
  // thresholds, independent of everything built above — a real but
  // easy-to-not-notice mode, so it needs an explicit, visible switch rather
  // than silently happening because no `setup` field was set.
  customBlendBuiltins: boolean;
  // advanced exit knobs — every one optional/off by default, matching
  // `ExitRules`' own defaults, so leaving this section untouched behaves
  // exactly like the plain custom-formula strategy did before it existed.
  customShowAdvancedExit: boolean;
  customTakeProfit: string; // "" = off
  customTrailingStop: string; // "" = off
  customRsiOverbought: string; // "" = off
  customRsiPeriod: string;
  customTrendSma: string; // "0" = off
  customTrendConfirmBars: string;
  customExitAtWeekEnd: boolean;
  customCloseOnly: boolean;
  customAtrChandelier: boolean;
  customAtrPeriod: string;
  customAtrMultWide: string;
  customAtrMultTight: string;
  customAtrTightenAt: string;
  // sizing — how much capital and how many names, per run
  customAllocation: string; // % of paper capital per position
  customMaxPositions: string;
};

const START_RULES: Rules = {
  kind: "breakout",
  lookback: "20",
  volume: "1.5",
  stop: "5",
  target: "10",
  below: "30",
  sellAt: "50",
  gap: "1",
  market: "1",
  entryConditions: [newCondition()],
  entryJoin: "and",
  exitConditions: [{ id: "x0", field: "pnl_pct", op: ">=", value: "10", period: "14" }],
  exitJoin: "or",
  customMinHistory: "210",
  customAdvanced: false,
  customEntryFormula: "",
  customBlendBuiltins: false,
  customExitFormula: "",
  customShowAdvancedExit: false,
  customTakeProfit: "",
  customTrailingStop: "",
  customRsiOverbought: "",
  customRsiPeriod: "14",
  customTrendSma: "0",
  customTrendConfirmBars: "3",
  customExitAtWeekEnd: false,
  customCloseOnly: false,
  customAtrChandelier: false,
  customAtrPeriod: "14",
  customAtrMultWide: "3",
  customAtrMultTight: "1.5",
  customAtrTightenAt: "5",
  customAllocation: "10",
  customMaxPositions: "5",
};

/** The rules a person fills in, turned into the stored definition (same shape the engine reads). */
function buildDefinition(r: Rules) {
  const stop = Number(r.stop);
  const off = { trailing_stop_pct: null, trend_sma: 0, trend_confirm_bars: 3, min_history_bars: 60 };
  if (r.kind === "gap") {
    // Monday only, after a week the market rose, buy what opens lower, sell at target, stop or Friday's close.
    const entry = {
      setup: "gap_down",
      gap_down_pct: Number(r.gap),
      gap_market_min_pct: Number(r.market),
      gap_weekday: 0,
      gap_entry_minutes: 15,
      min_history_bars: 60,
    };
    const exit = { stop_loss_pct: stop, take_profit_pct: Number(r.target), rsi_overbought: null, exit_at_week_end: true, ...off };
    return { rules: { entry, exit } };
  }
  if (r.kind === "triple") {
    // Buy after 3 falling days with the 5-day RSI low, in a stock above its 200-day average.
    const entry = {
      setup: "triple_rsi",
      triple_rsi_period: 5,
      triple_rsi_below: Number(r.below),
      triple_rsi_prior_below: 60,
      triple_rsi_trend_sma: 200,
      min_history_bars: 210,
      close_only: true, // the research buys at the close, so decide in the last minutes of the day
    };
    const exit = { stop_loss_pct: stop, take_profit_pct: null, rsi_overbought: Number(r.sellAt), rsi_period: 5, close_only: true, ...off };
    return { rules: { entry, exit }, engine_key: "signals_entry", params: { ...entry, ...exit, lookback: 260, allocation: 0.1, max_positions: 5 } };
  }
  if (r.kind === "custom") {
    const minHistory = Number(r.customMinHistory) || 60;
    const entryFormula = r.customAdvanced ? r.customEntryFormula : conditionsToFormula(r.entryConditions, r.entryJoin);
    const exitFormula = r.customAdvanced ? r.customExitFormula : conditionsToFormula(r.exitConditions, r.exitJoin);
    const entry = {
      custom_entry_formula: entryFormula,
      min_history_bars: minHistory,
      // Explicit, not implied: saved into the definition itself so a
      // strategy's isolation (or deliberate blending) is visible in what
      // actually gets stored, not just an engine-side default.
      setup: r.customBlendBuiltins ? null : "custom_entry",
    };
    const exit = {
      custom_exit_formula: exitFormula || null,
      stop_loss_pct: stop,
      min_history_bars: minHistory,
      take_profit_pct: r.customTakeProfit ? Number(r.customTakeProfit) : null,
      trailing_stop_pct: r.customTrailingStop ? Number(r.customTrailingStop) : null,
      rsi_overbought: r.customRsiOverbought ? Number(r.customRsiOverbought) : null,
      rsi_period: Number(r.customRsiPeriod) || 14,
      trend_sma: Number(r.customTrendSma) || 0,
      trend_confirm_bars: Number(r.customTrendConfirmBars) || 3,
      exit_at_week_end: r.customExitAtWeekEnd,
      close_only: r.customCloseOnly,
      atr_chandelier_enabled: r.customAtrChandelier,
      atr_period: Number(r.customAtrPeriod) || 14,
      atr_mult_wide: Number(r.customAtrMultWide) || 3,
      atr_mult_tight: Number(r.customAtrMultTight) || 1.5,
      atr_tighten_at_pct: Number(r.customAtrTightenAt) || 5,
    };
    const allocation = Math.min(Math.max(Number(r.customAllocation) || 10, 1), 100) / 100;
    const maxPositions = Math.max(1, Math.round(Number(r.customMaxPositions)) || 5);
    return {
      rules: { entry, exit },
      engine_key: "signals_entry",
      params: { ...entry, ...exit, lookback: Math.max(minHistory, 260), allocation, max_positions: maxPositions },
    };
  }
  const lookback = Number(r.lookback);
  const entry = { breakout_lookback: lookback, breakout_proximity_pct: 2.0, volume_multiple: Number(r.volume), volume_lookback: lookback, setup: "breakout", min_history_bars: 60 };
  const exit = { stop_loss_pct: stop, take_profit_pct: Number(r.target), rsi_overbought: null, ...off };
  return { rules: { entry, exit }, engine_key: "signals_entry", params: { ...entry, ...exit, lookback: 120, allocation: 0.1, max_positions: 5 } };
}

/** The form values a stored version was made from, so Edit can start from them. */
function rulesFrom(definition: Record<string, unknown> | null): Rules {
  const blocks = (definition?.rules ?? {}) as { entry?: Record<string, unknown>; exit?: Record<string, unknown> };
  const entry = blocks.entry ?? {};
  const exit = blocks.exit ?? {};
  const kind =
    typeof entry.custom_entry_formula === "string"
      ? "custom"
      : entry.setup === "gap_down"
        ? "gap"
        : entry.setup === "triple_rsi"
          ? "triple"
          : "breakout";
  const pick = (value: unknown, fallback: string) => (typeof value === "number" ? String(value) : fallback);
  return {
    ...START_RULES,
    kind,
    lookback: pick(entry.breakout_lookback, START_RULES.lookback),
    volume: pick(entry.volume_multiple, START_RULES.volume),
    stop: pick(exit.stop_loss_pct, START_RULES.stop),
    target: pick(exit.take_profit_pct, START_RULES.target),
    below: pick(entry.triple_rsi_below, START_RULES.below),
    sellAt: pick(exit.rsi_overbought, START_RULES.sellAt),
    gap: pick(entry.gap_down_pct, START_RULES.gap),
    market: pick(entry.gap_market_min_pct, START_RULES.market),
    // A formula built by hand (or by an earlier version of this builder that
    // used different fields) can't be reliably reverse-parsed into rows, so
    // editing an existing custom strategy always opens in raw/advanced mode
    // with its actual formula shown — never a guessed, possibly-wrong set of
    // rows silently different from what is really saved.
    customAdvanced: kind === "custom",
    customEntryFormula: typeof entry.custom_entry_formula === "string" ? entry.custom_entry_formula : "",
    customExitFormula: typeof exit.custom_exit_formula === "string" ? exit.custom_exit_formula : "",
    customMinHistory: pick(entry.min_history_bars, START_RULES.customMinHistory),
    customTakeProfit: pick(exit.take_profit_pct, START_RULES.customTakeProfit),
    customTrailingStop: pick(exit.trailing_stop_pct, START_RULES.customTrailingStop),
    customRsiOverbought: pick(exit.rsi_overbought, START_RULES.customRsiOverbought),
    customRsiPeriod: pick(exit.rsi_period, START_RULES.customRsiPeriod),
    customTrendSma: pick(exit.trend_sma, START_RULES.customTrendSma),
    customTrendConfirmBars: pick(exit.trend_confirm_bars, START_RULES.customTrendConfirmBars),
    customExitAtWeekEnd: exit.exit_at_week_end === true,
    customCloseOnly: exit.close_only === true,
    customAtrChandelier: exit.atr_chandelier_enabled === true,
    customAtrPeriod: pick(exit.atr_period, START_RULES.customAtrPeriod),
    customAtrMultWide: pick(exit.atr_mult_wide, START_RULES.customAtrMultWide),
    customAtrMultTight: pick(exit.atr_mult_tight, START_RULES.customAtrMultTight),
    customAtrTightenAt: pick(exit.atr_tighten_at_pct, START_RULES.customAtrTightenAt),
    // `params.allocation` is a fraction (0.1); the form shows a percentage.
    customAllocation:
    typeof (definition as { params?: Record<string, unknown> } | null)?.params?.allocation === "number"
    ? String(((definition as { params: { allocation: number } }).params.allocation) * 100)
        : START_RULES.customAllocation,
    customMaxPositions: pick(
      (definition as { params?: Record<string, unknown> } | null)?.params?.max_positions,
      START_RULES.customMaxPositions,
    ),
    // Only an *explicit* `setup: null` (this builder's way of saying "blend")
    // reads as blended. A record saved before this checkbox existed has no
    // `setup` key at all (`undefined`, not `null`) — that now runs isolated
    // by the engine's own corrected default, so the checkbox should show
    // unchecked for it too, not silently re-enable blending on open.
    customBlendBuiltins: kind === "custom" && entry.setup === null,
    customShowAdvancedExit:
      exit.take_profit_pct != null ||
      exit.trailing_stop_pct != null ||
      exit.rsi_overbought != null ||
      Boolean(exit.trend_sma) ||
      exit.exit_at_week_end === true ||
      exit.atr_chandelier_enabled === true,
  };
}

const FIELD_SELECT_OPTIONS = CONDITION_FIELDS.map((f) => ({ value: f.value, label: f.label }));

/** One "[field] [operator] [value]" row of a custom entry/exit rule. */
function ConditionRow({
  condition,
  onChange,
  onRemove,
  removable,
}: {
  condition: Condition;
  onChange: (next: Condition) => void;
  onRemove: () => void;
  removable: boolean;
}) {
  const field = FIELD_MAP.get(condition.field) ?? CONDITION_FIELDS[0];
  const ops = field.type === "bool" ? BOOL_OPS : NUMBER_OPS;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        size="sm"
        className="min-w-48 grow"
        value={condition.field}
        onChange={(value) => {
          const nextField = FIELD_MAP.get(value);
          const stillValidOp = nextField && ops.some((o) => o.value === condition.op);
          const nextOps = nextField?.type === "bool" ? BOOL_OPS : NUMBER_OPS;
          onChange({
            ...condition,
            field: value,
            op: stillValidOp && nextField?.type === field.type ? condition.op : nextOps[0].value,
            period: nextField?.parametric ? String(nextField.defaultPeriod ?? 14) : condition.period,
          });
        }}
        options={FIELD_SELECT_OPTIONS}
      />
      {field.parametric ? (
        <div className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          <span>over</span>
          <input
            value={condition.period}
            onChange={(e) => onChange({ ...condition, period: e.target.value })}
            inputMode="numeric"
            title="Period (number of bars)"
            className="h-9 w-14 rounded-lg border border-border bg-transparent px-2 text-center text-sm text-foreground outline-none focus:border-foreground/40 focus:ring-2 focus:ring-ring/40"
          />
          <span>days</span>
        </div>
      ) : null}
      <Select
        size="sm"
        className="w-44 shrink-0"
        value={condition.op}
        onChange={(op) => onChange({ ...condition, op })}
        options={ops as unknown as { value: string; label: string }[]}
      />
      {field.type === "number" ? (
        <input
          value={condition.value}
          onChange={(e) => onChange({ ...condition, value: e.target.value })}
          inputMode="decimal"
          placeholder="value"
          className="h-9 w-24 shrink-0 rounded-lg border border-border bg-transparent px-2.5 text-sm text-foreground outline-none focus:border-foreground/40 focus:ring-2 focus:ring-ring/40"
        />
      ) : null}
      <Tooltip content="Remove condition" side="left" delay={400}>
        <button
          type="button"
          onClick={onRemove}
          disabled={!removable}
          className="grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:pointer-events-none disabled:opacity-30"
        >
          <Trash2 className="size-3.5" />
        </button>
      </Tooltip>
    </div>
  );
}

/** A named group of condition rows (an entry rule set, or an exit rule set),
 * joined by a single AND/OR — matches what one formula string can express. */
function ConditionGroup({
  title,
  hint,
  conditions,
  join,
  onConditionsChange,
  onJoinChange,
}: {
  title: string;
  hint: string;
  conditions: Condition[];
  join: "and" | "or";
  onConditionsChange: (next: Condition[]) => void;
  onJoinChange: (next: "and" | "or") => void;
}) {
  return (
    <div className="space-y-2.5 rounded-lg border border-border p-3.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-foreground">{title}</div>
        {conditions.length > 1 && (
          <div className="flex items-center gap-1 rounded-full bg-muted p-0.5 text-xs">
            {(["and", "or"] as const).map((j) => (
              <button
                key={j}
                type="button"
                onClick={() => onJoinChange(j)}
                className={cn(
                  "rounded-full px-2.5 py-1 font-medium transition-colors",
                  join === j ? "bg-card text-foreground " : "text-muted-foreground",
                )}
              >
                {j === "and" ? "all must be true" : "any can be true"}
              </button>
            ))}
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{hint}</p>
      <div className="space-y-2">
        {conditions.map((c, i) => (
          <ConditionRow
            key={c.id}
            condition={c}
            removable={conditions.length > 1}
            onChange={(next) => onConditionsChange(conditions.map((x, xi) => (xi === i ? next : x)))}
            onRemove={() => onConditionsChange(conditions.filter((_, xi) => xi !== i))}
          />
        ))}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => onConditionsChange([...conditions, newCondition()])}
      >
        <Plus className="size-3.5" />
        Add condition
      </Button>
    </div>
  );
}

function Authoring({ deployments, onOpenPaper }: { deployments: Deployment[]; onOpenPaper: () => void }) {
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [name, setName] = useState("");
  const [about, setAbout] = useState("");
  const [creating, setCreating] = useState(false);
  const [rules, setRules] = useState<Rules>(START_RULES);
  const [runFor, setRunFor] = useState<number | null>(null);
  const [stocks, setStocks] = useState("");
  const [capital, setCapital] = useState("500000");
  const [report, setReport] = useState<StrategyValidation | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async (keep?: string | null) => {
    try {
      const r = await listSavedStrategies();
      setSaved(r.strategies);
      const next = keep ?? r.strategies[0]?.strategy_id ?? null;
      setSelected(next);
      setVersions(next ? (await listStrategyVersions(next)).versions : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const pick = async (id: string) => {
    setSelected(id);
    setReport(null);
    try {
      setVersions((await listStrategyVersions(id)).versions);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(readable(e));
    } finally {
      setBusy(null);
    }
  };


  const sortedSaved = useMemo(() => {
    // Deployable first (the ones actually worth acting on), then most recently touched.
    return [...saved].sort((a, b) => {
      if (a.deployable !== b.deployable) return a.deployable ? -1 : 1;
      return b.updated_at.localeCompare(a.updated_at);
    });
  }, [saved]);
  const [editing, setEditing] = useState(false);
  const dialog = useDialog();

  /** Remove a strategy from the list. The server refuses while a paper run still uses it, and says why. */
  const remove = async (s: SavedStrategy) => {
    const ok = await dialog.confirm({
      title: `Remove ${s.name}?`,
      description: "This erases it, with its versions, backtests and paper history. It cannot be undone.",
      confirmLabel: "Remove",
      tone: "danger",
    });
    if (!ok) return;
    await run("remove", async () => {
      await removeSavedStrategy(s.strategy_id);
      setEditing(false);
      setReport(null);
      await refresh(null);
      setNotice(`Removed ${s.name}.`);
    });
  };

  /** Open the rules form with a version's values, ready to save as the next version. */
  const editVersion = (version: number) =>
    run("edit", async () => {
      const detail = await getStrategyVersion(selected as string, version);
      setRules(rulesFrom(detail.definition));
      setEditing(true);
      setNotice(`Editing from v${version}. Saving makes a new version.`);
    });

  const removeVersion = async (version: number) => {
    const ok = await dialog.confirm({
      title: `Delete v${version}?`,
      description: "This erases it, with the paper runs and backtests that used it. It cannot be undone.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    await run("delete-version", async () => {
      await deleteStrategyVersion(selected as string, version);
      await refresh(selected);
      setNotice(`Deleted v${version}.`);
    });
  };

  const ghost =
    "inline-flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-xs transition-colors hover:bg-muted disabled:opacity-50";

  /** Versions, deploy-to-paper, and the rule editor for whichever strategy is
   * selected. Shared by the single-strategy layout and by each expanded
   * accordion row — defined once, as a closure over this component's own
   * state, so neither caller has to prop-drill the dozen pieces it reads. */
  const renderVersionsBody = () => (
          <>
                <div className="divide-y divide-border">
                  {versions.map((v) => (
                    <div key={v.version} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm">
                          <span className="font-medium">v{v.version}</span>
                          {v.deployable ? (
                            <Badge tone="good">ready to deploy</Badge>
                          ) : (
                            <Badge tone="warn">can't deploy</Badge>
                          )}
                          {v.is_deployed && <Badge tone="flat">deployed</Badge>}
                        </div>
                        {!v.deployable && v.not_deployable_reason && (
                          <div className="mt-0.5 text-xs text-muted-foreground">{v.not_deployable_reason}</div>
                        )}
                        {v.change_note && <div className="mt-0.5 text-xs text-muted-foreground">{v.change_note}</div>}
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground">
                          <RelativeTime value={v.created_at} absolute={false} className="text-muted-foreground" />
                        </span>
                        {v.deployable && (
                          <button type="button" onClick={() => setRunFor(v.version)} className={ghost}>
                            <Play className="h-3 w-3" />
                            Run on paper
                          </button>
                        )}
                        <button type="button" disabled={busy !== null} onClick={() => void editVersion(v.version)} className={ghost}>
                          Edit
                        </button>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => void removeVersion(v.version)}
                className={cn(ghost, "text-muted-foreground hover:text-loss")}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  ))}
                  {versions.length === 0 && (
                    <div className="px-4 py-3 text-xs text-muted-foreground">No versions yet.</div>
                  )}
                </div>

                {runFor !== null && (
                  <div className="space-y-2 border-t border-border p-4">
                    <div className="flex justify-end">
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() =>
                          run("stocks", async () => setStocks((await getResearchedStocks()).symbols.join(", ")))
                        }
                        className={cn(ghost, "text-muted-foreground")}
                      >
                        {busy === "stocks" ? "Loading…" : "Use the researched stocks"}
                      </button>
                    </div>
                    <input
                      autoFocus
                      value={stocks}
                      onChange={(e) => setStocks(e.target.value)}
                      placeholder="Stocks, e.g. RELIANCE, TCS, INFY"
            className="w-full rounded-xl border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="flex items-center gap-2 text-xs text-muted-foreground">
                        Practice money ₹
                        <input
                          value={capital}
                          onChange={(e) => setCapital(e.target.value)}
                          inputMode="numeric"
                className="w-28 rounded-xl border border-border bg-transparent px-3 py-1.5 text-sm text-foreground outline-none focus:border-primary/50"
                        />
                      </label>
                      <button
                        type="button"
                        disabled={busy !== null || !stocks.trim() || !Number(capital)}
                        onClick={() =>
                          run("paper", async () => {
                            const symbols = stocks.split(/[\s,]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
                            const made = await createDeployment({
                              strategy_id: selected as string,
                              strategy_version: runFor,
                              capital: Number(capital),
                              mode: "PAPER",
                              config: { symbols, exchange: "NSEEQ", timeframe: "1d", max_open_positions: 20 },
                            });
                            await startDeployment(made.deployment_id);
                            setRunFor(null);
                            onOpenPaper();
                          })
                        }
                        className={ghost}
                      >
                        {busy === "paper" ? "Starting…" : "Start"}
                      </button>
                      <button type="button" onClick={() => setRunFor(null)} className={cn(ghost, "text-muted-foreground")}>
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {editing ? (
        <div className="space-y-5 border-t border-border bg-muted/20 p-5">
          <div>
            <div className="mb-2 text-sm font-medium text-foreground">Strategy type</div>
            <div className="grid grid-cols-2 gap-2 sm:w-[30rem] sm:grid-cols-4">
                      {(
                        [
                          ["breakout", "Breakout"],
                          ["triple", "Triple RSI"],
                          ["gap", "Monday gap"],
                  ["custom", "Custom"],
                        ] as const
                      ).map(([kind, label]) => (
                <Button
                          key={kind}
                          type="button"
                  variant={rules.kind === kind ? "primary" : "secondary"}
                  size="sm"
                  className="rounded-md"
                          onClick={() =>
                            setRules({ ...rules, kind, stop: kind === "triple" ? "8" : "5", target: kind === "gap" ? "3" : "10" })
                          }
                        >
                          {label}
                </Button>
                      ))}
                    </div>
                    {rules.kind === "triple" && (
              <p className="mt-2.5 text-xs text-muted-foreground">
                        Buys after three falling days, in a stock above its 200-day average. Decides in the last 15 minutes of the day. Sells when the 5-day RSI recovers.
                      </p>
                    )}
                    {rules.kind === "gap" && (
              <p className="mt-2.5 text-xs text-muted-foreground">
                        Mondays only, within 15 minutes of the open. Sells at the target or stop, else at Friday's close. Needs the broker login for live prices.
                      </p>
                    )}
            {rules.kind === "custom" && (
              <p className="mt-2.5 text-xs text-muted-foreground">
              Build buy/sell conditions from indicators and chart patterns — no code. Runs on the same rule
              engine as every other strategy here.
              </p>
            )}
          </div>

          {rules.kind === "custom" ? (
            <div className="space-y-4">
              <div className="flex items-center justify-end">
                <button
                  type="button"
                  onClick={() =>
                    setRules(
                      rules.customAdvanced
                        ? { ...rules, customAdvanced: false }
                        : {
                            // Carry the builder's rows into the text box as the
                            // formula they already compile to — switching modes
                            // must never silently blank out what was just built.
                            ...rules,
                            customAdvanced: true,
                            customEntryFormula: conditionsToFormula(rules.entryConditions, rules.entryJoin),
                            customExitFormula: conditionsToFormula(rules.exitConditions, rules.exitJoin),
                          },
                    )
                  }
                  className="inline-flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
                >
                {rules.customAdvanced ? <ListChecks className="size-3.5" /> : <Code2 className="size-3.5" />}
                  {rules.customAdvanced ? "Switch to builder" : "Edit formula as text"}
                </button>
              </div>

              {rules.customAdvanced ? (
                <div className="space-y-3">
                  <label className="block space-y-1.5">
                    <span className="px-1 text-sm font-medium text-foreground">Buy when</span>
                    <textarea
                      value={rules.customEntryFormula}
                      onChange={(e) => setRules({ ...rules, customEntryFormula: e.target.value })}
                      placeholder="rsi14 < 30 and price > sma200"
                      rows={2}
                      className="w-full rounded-lg border border-border bg-transparent px-3 py-2 font-mono text-sm text-foreground outline-none focus:border-foreground/40 focus:ring-2 focus:ring-ring/40"
                    />
                  </label>
                  <label className="block space-y-1.5">
                    <span className="px-1 text-sm font-medium text-foreground">Sell when</span>
                    <textarea
                      value={rules.customExitFormula}
                      onChange={(e) => setRules({ ...rules, customExitFormula: e.target.value })}
                      placeholder="pnl_pct >= 10 or rsi14 > 70"
                      rows={2}
                      className="w-full rounded-lg border border-border bg-transparent px-3 py-2 font-mono text-sm text-foreground outline-none focus:border-foreground/40 focus:ring-2 focus:ring-ring/40"
                    />
                  </label>
                </div>
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  <ConditionGroup
                    title="Buy when"
                    hint="What has to be true to open a position."
                    conditions={rules.entryConditions}
                    join={rules.entryJoin}
                    onConditionsChange={(entryConditions) => setRules({ ...rules, entryConditions })}
                    onJoinChange={(entryJoin) => setRules({ ...rules, entryJoin })}
                  />
                  <ConditionGroup
                    title="Sell when"
                    hint="Checked every day a position is open, alongside the stop-loss below."
                    conditions={rules.exitConditions}
                    join={rules.exitJoin}
                    onConditionsChange={(exitConditions) => setRules({ ...rules, exitConditions })}
                    onJoinChange={(exitJoin) => setRules({ ...rules, exitJoin })}
                  />
                </div>
              )}

              <label className="flex items-start gap-2.5 text-sm text-foreground">
                <input
                  type="checkbox"
                  checked={rules.customBlendBuiltins}
                  onChange={(e) => setRules({ ...rules, customBlendBuiltins: e.target.checked })}
                  className="mt-0.5 size-4 rounded-md border-border accent-primary"
                />
                <span>
                Also fire on the built-in setups (trend pullback, breakout, RSI oversold, triple RSI, Monday
                gap) at their own default settings, independent of "Buy when" above.
                  <span className="block text-xs text-muted-foreground">
                  Off means "Buy when" is the entire entry condition — nothing else can open a position.
                  </span>
                </span>
              </label>

              <div className="grid grid-cols-2 gap-3 sm:w-72">
                <Input
                  label="Stop loss %"
                  value={rules.stop}
                  onChange={(v) => setRules({ ...rules, stop: v })}
                  inputMode="decimal"
                  classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                />
                <Input
                  label="History needed (days)"
                  value={rules.customMinHistory}
                  onChange={(v) => setRules({ ...rules, customMinHistory: v })}
                  inputMode="numeric"
                  classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                />
              </div>

              <div className="rounded-lg border border-border">
                <button
                  type="button"
                  onClick={() => setRules({ ...rules, customShowAdvancedExit: !rules.customShowAdvancedExit })}
                  className="flex w-full items-center justify-between px-3.5 py-3 text-left"
                >
                  <div>
                  <div className="text-sm font-medium text-foreground">Advanced exit &amp; sizing</div>
                    <div className="text-xs text-muted-foreground">
                    Take-profit, trailing stop, RSI/trend exits, ATR chandelier, allocation, position count — every knob "Sell when" doesn't cover.
                    </div>
                  </div>
                  <ChevronDown
                  className={cn("size-4 shrink-0 text-muted-foreground transition-transform", rules.customShowAdvancedExit && "rotate-180")}
                  />
                </button>
                {rules.customShowAdvancedExit && (
                  <div className="space-y-4 border-t border-border p-3.5">
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                      <Input
                        label="Take profit % (blank = off)"
                        value={rules.customTakeProfit}
                        onChange={(v) => setRules({ ...rules, customTakeProfit: v })}
                        inputMode="decimal"
                        placeholder="off"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="Trailing stop % (blank = off)"
                        value={rules.customTrailingStop}
                        onChange={(v) => setRules({ ...rules, customTrailingStop: v })}
                        inputMode="decimal"
                        placeholder="off"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="Sell when RSI passes (blank = off)"
                        value={rules.customRsiOverbought}
                        onChange={(v) => setRules({ ...rules, customRsiOverbought: v })}
                        inputMode="decimal"
                        placeholder="off"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="...that RSI's period"
                        value={rules.customRsiPeriod}
                        onChange={(v) => setRules({ ...rules, customRsiPeriod: v })}
                        inputMode="numeric"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="Sell below this average (0 = off)"
                        value={rules.customTrendSma}
                        onChange={(v) => setRules({ ...rules, customTrendSma: v })}
                        inputMode="numeric"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="...for this many closes running"
                        value={rules.customTrendConfirmBars}
                        onChange={(v) => setRules({ ...rules, customTrendConfirmBars: v })}
                        inputMode="numeric"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="Allocation per trade (%)"
                        value={rules.customAllocation}
                        onChange={(v) => setRules({ ...rules, customAllocation: v })}
                        inputMode="decimal"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                      <Input
                        label="Max concurrent positions"
                        value={rules.customMaxPositions}
                        onChange={(v) => setRules({ ...rules, customMaxPositions: v })}
                        inputMode="numeric"
                        classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                      />
                    </div>

                    <div className="flex flex-wrap items-center gap-5">
                      <label className="flex items-center gap-2 text-sm text-foreground">
                        <input
                          type="checkbox"
                          checked={rules.customExitAtWeekEnd}
                          onChange={(e) => setRules({ ...rules, customExitAtWeekEnd: e.target.checked })}
                          className="size-4 rounded-md border-border accent-primary"
                        />
                        Sell at the week's last close if nothing else has
                      </label>
                      <label className="flex items-center gap-2 text-sm text-foreground">
                        <input
                          type="checkbox"
                          checked={rules.customCloseOnly}
                          onChange={(e) => setRules({ ...rules, customCloseOnly: e.target.checked })}
                          className="size-4 rounded-md border-border accent-primary"
                        />
                        Only decide in the session's final minutes
                      </label>
                    </div>

                    <label className="flex items-center gap-2 text-sm text-foreground">
                      <input
                        type="checkbox"
                        checked={rules.customAtrChandelier}
                        onChange={(e) => setRules({ ...rules, customAtrChandelier: e.target.checked })}
                        className="size-4 rounded-md border-border accent-primary"
                      />
                      ATR chandelier trail — trails the recent high by a multiple of the stock's own volatility instead of a fixed %
                    </label>
                    {rules.customAtrChandelier && (
                      <div className="grid grid-cols-2 gap-3 pl-6 sm:grid-cols-4">
                        <Input
                          label="ATR period"
                          value={rules.customAtrPeriod}
                          onChange={(v) => setRules({ ...rules, customAtrPeriod: v })}
                          inputMode="numeric"
                          classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                        />
                        <Input
                          label="Wide multiple (early)"
                          value={rules.customAtrMultWide}
                          onChange={(v) => setRules({ ...rules, customAtrMultWide: v })}
                          inputMode="decimal"
                          classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                        />
                        <Input
                          label="Tight multiple (once up)"
                          value={rules.customAtrMultTight}
                          onChange={(v) => setRules({ ...rules, customAtrMultTight: v })}
                          inputMode="decimal"
                          classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                        />
                        <Input
                          label="Tightens once up (%)"
                          value={rules.customAtrTightenAt}
                          onChange={(v) => setRules({ ...rules, customAtrTightenAt: v })}
                          inputMode="decimal"
                          classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                        />
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-3 gap-y-4 sm:grid-cols-4">
                      {(
                        (rules.kind === "triple"
                          ? [
                              ["below", "Buy when 5-day RSI is under"],
                              ["sellAt", "Sell when RSI passes"],
                              ["stop", "Stop loss %"],
                            ]
                          : rules.kind === "gap"
                          ? [
                              ["gap", "Buy if it opens this % below Friday"],
                              ["market", "Only after a week the market rose over %"],
                              ["target", "Take profit %"],
                              ["stop", "Stop loss %"],
                            ]
                          : [
                              ["lookback", "Buy a breakout over (days)"],
                              ["volume", "Volume times normal"],
                              ["stop", "Stop loss %"],
                              ["target", "Take profit %"],
                            ]) as [keyof Rules, string][]
                      ).map(([key, label]) => (
                <Input
                  key={key}
                  label={label}
                  value={rules[key] as string}
                  onChange={(v) => setRules({ ...rules, [key]: v })}
                            inputMode="decimal"
                  classNames={{ label: "text-xs font-normal text-muted-foreground" }}
                          />
                      ))}
                    </div>
          )}

          <Tooltip content="Saves these rules as a new version. A saved version can't be edited later." side="top" delay={400}>
            <Button
                      type="button"
              variant="primary"
              size="sm"
              disabled={
                busy !== null ||
                !selected ||
                (rules.kind === "custom"
                  ? !Number(rules.stop) ||
                  (rules.customAdvanced ? !rules.customEntryFormula.trim() : rules.entryConditions.every((c) => !conditionToFormula(c)))
                  : rules.kind === "triple"
                    ? [rules.below, rules.sellAt, rules.stop].some((x) => !Number(x))
                    : rules.kind === "gap"
                      ? [rules.gap, rules.market, rules.target, rules.stop].some((x) => !Number(x))
                      : [rules.lookback, rules.volume, rules.stop, rules.target].some((x) => !Number(x)))
              }
                      onClick={() =>
                        run("version", async () => {
                          const created = await createStrategyVersion(selected as string, {
                            definition: buildDefinition(rules),
                          });
                          setReport(created.validation);
                          setNotice(`Saved version ${created.version}.`);
                          setEditing(false);
                          await pick(selected as string);
                        })
                      }
                    >
              <Plus className="h-3.5 w-3.5" />
                      {busy === "version" ? "Saving…" : "Save rules"}
            </Button>
          </Tooltip>
        </div>
      ) : null}
    </>
  );

  const deployableCount = saved.filter((s) => s.deployable).length;
  const totalVersions = saved.reduce((sum, s) => sum + s.version_count, 0);
  const noVersionCount = saved.filter((s) => s.latest_version === null).length;

  // Deployment-derived stats (RUNNING paper deployments only)
  const runningDeps = deployments.filter((d) => d.status === "RUNNING" && d.mode === "PAPER");
  const capitalDeployed = runningDeps.reduce((sum, d) => sum + (d.capital ?? 0), 0);
  const totalPnl = runningDeps.reduce((sum, d) => sum + (d.pnl?.total_pnl ?? 0), 0);
  const hasPnl = runningDeps.some((d) => d.pnl != null);

  // Strategies that have never been deployed at all
  const deployedStrategyIds = new Set(deployments.map((d) => d.strategy_id));
  const neverDeployedCount = saved.filter((s) => !deployedStrategyIds.has(s.strategy_id)).length;

  return (
    <Card>
      <CardHeader title="Strategies" />

      <div className="space-y-4 px-5 py-4">
        {/* Summary grid — all figures derived from live data */}
        {saved.length > 0 && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <SummaryStat
              label="Authored"
              value={saved.length}
              sub={`${deployableCount} ready · ${totalVersions} versions`}
            />
            <SummaryStat
              label="On paper now"
              value={runningDeps.length}
              sub={capitalDeployed > 0 ? `${fmtMoney(capitalDeployed)} deployed` : "none running"}
              accent={runningDeps.length > 0}
              onClick={onOpenPaper}
            />
            <SummaryStat
              label="Paper P&L"
              value={hasPnl ? totalPnl : null}
              sub={hasPnl ? (totalPnl > 0 ? "unrealised gain" : totalPnl < 0 ? "unrealised loss" : "flat P&L") : "no data yet"}
              accent={hasPnl && totalPnl > 0}
              bad={hasPnl && totalPnl < 0}
              isMoney
              onClick={hasPnl ? onOpenPaper : undefined}
            />
            <SummaryStat
              label="Never deployed"
              value={neverDeployedCount}
              sub={neverDeployedCount === 0 ? "all tested on paper" : "untested strategies"}
              accent={false}
            />
          </div>
        )}

        {/* Attention callout — only when something genuinely needs action */}
        {noVersionCount > 0 && (
          <div className="flex items-start gap-2.5 rounded-xl border border-warning/20 bg-warning/5 px-3.5 py-2.5 text-xs text-warning">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>
            <span className="font-medium">{noVersionCount} {noVersionCount === 1 ? "strategy has" : "strategies have"} no version yet</span>
              {" — "}set rules and save a version before you can deploy or backtest.
            </span>
          </div>
        )}

        {error && <ErrorBox>{error}</ErrorBox>}
        {notice && (
          <div className="rounded-xl border border-border bg-muted/40 px-3.5 py-2.5 text-body">{notice}</div>
        )}

        {saved.length === 0 ? (
          <div className="py-4 text-center text-sm text-muted-foreground">No strategy of your own yet.</div>
        ) : (
          <>

            {/* A picker only earns its place when there is more than one to pick between. */}
            <BouncyAccordion
              value={selected}
              onValueChange={(id) => {
                if (id) void pick(id);
                else {
                  setSelected(null);
                  setVersions([]);
                }
              }}
              classNames={{
                item: "border border-border rounded-xl overflow-hidden",
                trigger: "items-start !min-h-0 py-4",
                title: "whitespace-normal overflow-visible",
                chevron: "mt-0.5",
              }}
              items={sortedSaved.map((s) => ({
                id: s.strategy_id,
                title: (
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                    <span className={cn("text-sm font-medium", s.strategy_id === selected ? "text-primary" : "text-foreground")}>
                        {s.name}
                      </span>
                      {s.engine_key && (
                        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-caption text-muted-foreground">
                          {strategyLabel(s.engine_key)}
                        </span>
                      )}
                      {s.latest_version !== null ? (
                        <Badge tone={s.deployable ? "good" : "flat"}>v{s.latest_version}</Badge>
                      ) : (
                        <Badge tone="flat">no version yet</Badge>
                      )}
                      <span className="ml-auto shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                      <RelativeTime value={s.updated_at} absolute={false} className="text-muted-foreground" />
                        {s.version_count > 1 && <span> · {s.version_count} versions</span>}
                      </span>
                    </div>
                    {s.description && (
                      <div className="mt-1.5 line-clamp-2 pr-8 text-xs leading-relaxed text-muted-foreground">
                        {s.description}
                      </div>
                    )}
                  </div>
                ),
                description:
                  s.strategy_id === selected ? (
                    <div className="-mx-5 -mt-2 border-t border-border">
                      <div className="flex items-center justify-end gap-2 px-5 py-2.5">
                      <button type="button" onClick={() => setEditing((v) => !v)} className={ghost}>
                          {editing ? "Close" : "Set rules"}
                        </button>
                        <button
                          type="button"
                          disabled={busy !== null}
                          onClick={() => void remove(s)}
                          className={cn(ghost, "text-muted-foreground hover:text-loss")}
                        >
                          Remove
                    </button>
                  </div>
                      {renderVersionsBody()}
              </div>
                  ) : null,
              }))}
            />

          </>
        )}

        {!creating ? (
          <div>
            <button type="button" onClick={() => setCreating(true)} className={ghost}>
              <Plus className="h-3 w-3" />
              New strategy
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Name"
              className="w-full rounded-xl border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
            />
            <input
              value={about}
              onChange={(e) => setAbout(e.target.value)}
              placeholder="What it does (optional)"
              className="w-full rounded-xl border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={busy !== null || !name.trim()}
                onClick={() =>
                  run("create", async () => {
                    const row = await createSavedStrategy({
                      name: name.trim(),
                      kind: "rules",
                      description: about.trim() || null,
                    });
                    setName("");
                    setAbout("");
                    setCreating(false);
                    setNotice(`Created “${row.name}”. Set its rules, then run it on paper.`);
                    await refresh(row.strategy_id);
                    setEditing(true);
                  })
                }
                className={ghost}
              >
                {busy === "create" ? "Creating…" : "Create"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setCreating(false);
                  setName("");
                  setAbout("");
                }}
                className={cn(ghost, "text-muted-foreground")}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {report && <ValidationReport report={report} />}
      </div>
    </Card>
  );
}

function ValidationReport({ report }: { report: StrategyValidation }) {
  return (
    <div className="rounded-lg border border-border/60 px-3.5 py-3 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-2">
        {report.ok ? <Badge tone="good">structurally valid</Badge> : <Badge tone="bad">will not run</Badge>}
        <span className="text-muted-foreground">
          {report.checked === "draft" ? "draft, nothing saved" : `stored version ${report.version}`}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className={cn(report.paper.resolvable ? "text-gain" : "text-destructive")}>
          {report.paper.resolvable ? "paper: deployable" : "paper: not deployable"}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className={cn(report.backtest.resolvable ? "text-gain" : "text-muted-foreground")}>
          {report.backtest.resolvable ? "backtest: runnable" : "backtest: not runnable"}
        </span>
      </div>

      {!report.paper.resolvable && report.paper.reason && (
        <div className="mt-1.5 text-muted-foreground">{report.paper.reason}</div>
      )}
      {report.backtest.resolvable === false && report.backtest.reason && report.paper.resolvable && (
        <div className="mt-1 text-muted-foreground">{report.backtest.reason}</div>
      )}

      {report.errors.length > 0 && (
        <ul className="mt-2 space-y-1">
          {report.errors.map((e) => (
            <li key={`${e.code}-${e.field}`} className="flex gap-1.5 text-destructive">
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <code className="text-[11.5px]">{e.field}</code> — {e.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      {report.warnings.length > 0 && (
        <ul className="mt-2 space-y-1">
          {report.warnings.map((w) => (
            <li key={`${w.code}-${w.field}`} className="flex gap-1.5 text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <code className="text-[11.5px]">{w.field}</code> — {w.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      {report.paper.default_fields && report.paper.default_fields.length > 0 && (
        <div className="mt-2 text-[11.5px] text-muted-foreground">
          {report.paper.default_fields.length} rule fields left at their defaults
          {report.paper.authored_fields?.length
            ? `; you set ${report.paper.authored_fields.length}`
            : ""}
          . Those defaults are the module's starting points, not findings.
        </div>
      )}

      <div className="mt-2 text-[11.5px] text-muted-foreground">
        {report.statistical_validation.reason}
      </div>
    </div>
  );
}

