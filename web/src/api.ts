// Typed client for the ATR FastAPI backend (src/atr/api/main.py).

// Default to a *relative* URL so the dashboard always talks to whatever origin
// served it. `atr serve` puts the SPA and the API on one port, so a relative
// path is correct there and on any port. Hardcoding http://127.0.0.1:8000 broke
// `atr serve --port 8123`: the page loaded from :8123 and every request went to
// :8000, failing CORS on a backend that was never started. Set VITE_API_URL only
// for `atr dev`, where Vite runs on a different port from FastAPI.
export const API_URL =
  import.meta.env.VITE_API_URL?.replace(/\/$/, "") ?? "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else detail = JSON.stringify(body);
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export interface Health {
  status: string;
  env: string;
  database: boolean;
  session_active: boolean;
  execution_mode: "paper" | "live";
  kill_switch: boolean;
}

export interface DashboardSummary {
  as_of: string;
  exchange: string;
  positions?: {
    count: number;
    value?: number;
    invested?: number;
    day_pnl?: number;
    // False when at least one open position had no prior close, i.e. day P&L
    // is unknown rather than zero. Without this the UI cannot tell "flat
    // today" from "we don't know" and would assert the former.
    day_pnl_complete?: boolean;
    // How many prior closes came from the local daily cache because the broker
    // omitted them. Provenance, not decoration — `== count` means every
    // baseline is ours.
    day_pnl_from_cache?: number;
    day_pnl_pct?: number;
    unrealized_pnl?: number;
    last_flat_at?: string | null;
    error?: string;
  };
  performance?: {
    trades?: number;
    win_rate?: number | null;
    wins?: number;
    losses?: number;
    sample?: number;
    realized_pnl?: number;
    current_drawdown_pct?: number;
    max_drawdown_pct?: number;
    sparkline?: number[];
    equity_curve?: number[];
    error?: string;
  };
  breadth?: {
    series?: number[];
    dates?: string[];
    current?: number | null;
    delta_5d?: number | null;
    expanding?: boolean | null;
    error?: string;
  };
}

export const getDashboardSummary = () =>
  req<DashboardSummary>("/dashboard/summary");

export interface BacktestRequest {
  strategy: string;
  symbols: string[];
  initial_cash: number;
  fast: number;
  slow: number;
  slippage_bps: number;
  square_off_eod: boolean;
  futures: boolean;
  source: "synthetic" | "history";
  exchange: string;
}

export interface EquityPoint {
  ts: string;
  value: number;
}

export interface BacktestResponse {
  strategy: string;
  source: string;
  symbols: string[];
  metrics: Record<string, number | string | boolean | null>;
  num_fills: number;
  num_trades: number;
  killed: boolean;
  kill_reason: string | null;
  equity: EquityPoint[];
  trades: Record<string, number | string | null>[];
}

export interface Position {
  symbol: string;
  exchange: string;
  quantity: number;
  avg_price: number;
  last_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
}

export interface OrderRequest {
  symbol: string;
  exchange: string;
  quantity: number; // signed: negative = sell
  order_type: string;
  price?: number | null;
  product?: string | null;
  tag?: string | null;
}

export interface OrderResponse {
  order_id: string;
  broker_order_id: string | null;
  status: string;
  reject_reason: string | null;
}

export const getHealth = () => req<Health>("/health");

export const runBacktest = (body: BacktestRequest) =>
  req<BacktestResponse>("/backtest", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const getPositions = () => req<Position[]>("/positions");

export const placeOrder = (body: OrderRequest) =>
  req<OrderResponse>("/orders", {
    method: "POST",
    body: JSON.stringify(body),
  });

export interface ScanRow {
  symbol: string;
  last: number;
  day_chg_pct: number;
  ret_1m: number;
  vs_high: number;
  trend: "UP" | "DOWN" | "MIXED";
  rsi: number;
  vol_x: number;
  atr_pct: number;
  gold_cross_5d: boolean;
  breakout: boolean;
  score: number;
  bars: number;
}

export interface ScanResponse {
  as_of: string;
  rows: ScanRow[];
  errors: { symbol: string; error: string }[];
}

export const getScan = (symbols?: string) =>
  req<ScanResponse>(symbols ? `/scan?symbols=${encodeURIComponent(symbols)}` : "/scan");

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlesResponse {
  symbol: string;
  exchange: string;
  interval: string;
  candles: Candle[];
}

export const getCandles = (params: {
  symbol: string;
  exchange?: string;
  interval?: string;
  from_date?: string;
  to_date?: string;
}) => {
  const q = new URLSearchParams({
    symbol: params.symbol,
    exchange: params.exchange ?? "NSEEQ",
    interval: params.interval ?? "1d",
    from_date: params.from_date ?? "01-Mar-2026",
    to_date: params.to_date ?? "08-Sep-2026",
  });
  return req<CandlesResponse>(`/candles?${q.toString()}`);
};

export interface AlertRule {
  id: string;
  name: string;
  symbol: string;
  exchange: string;
  kind: string;
  threshold: number;
  cooldown_min: number;
  armed: boolean;
  last_fired_at: string | null;
}

export interface AlertEvent {
  id: string;
  rule_id: string;
  rule: string;
  ts: string;
  message: string;
  channel: string;
  ok: boolean;
}

export const getAlertRules = () => req<AlertRule[]>("/alerts/rules");

export const createAlertRule = (body: {
  name?: string;
  symbol: string;
  exchange?: string;
  kind: string;
  threshold?: number;
  cooldown_min?: number;
}) =>
  req<AlertRule>("/alerts/rules", { method: "POST", body: JSON.stringify(body) });

export const armAlertRule = (id: string, armed: boolean) =>
  req<AlertRule>(`/alerts/rules/${id}?armed=${armed}`, { method: "PATCH" });

export const deleteAlertRule = (id: string) =>
  req<{ deleted: boolean }>(`/alerts/rules/${id}`, { method: "DELETE" });

export const getAlertEvents = () => req<AlertEvent[]>("/alerts/events?limit=50");

export const runAlertCheck = () =>
  req<{ market_open: boolean; fired: AlertEvent[] }>("/alerts/check", {
    method: "POST",
  });

export const sendAlertTest = () =>
  req<{ sent_on: string }>("/alerts/test", { method: "POST" });

export interface IntelligentAlertConfig {
  enabled: boolean;
  universe: "holdings" | "watchlist" | "both";
  interval_min: number;
  cooldown_min: number;
  sell_sma_breakdown: boolean;
  sell_rsi_overbought: boolean;
  sell_rsi_threshold: number;
  sell_take_profit_enabled: boolean;
  sell_take_profit_pct: number;
  sell_stop_loss_enabled: boolean;
  sell_stop_loss_pct: number;
  sell_trailing_stop_enabled: boolean;
  sell_trailing_stop_pct: number;
  buy_golden_cross: boolean;
  buy_rsi_oversold: boolean;
  buy_rsi_threshold: number;
  buy_breakout_vol: boolean;
  buy_dip_sma20: boolean;
}

export interface IntelligentStatus {
  enabled: boolean;
  running: boolean;
  last_run: string | null;
  market_open: boolean;
  interval_min: number;
  universe: string;
  recent_signals: Array<{
    symbol: string;
    action: "BUY" | "SELL";
    reason: string;
    price: number;
    day_chg_pct: number;
    rsi: number | null;
    metric: string;
    ts: string;
    channel: string;
  }>;
}

export const getIntelligentAlerts = () =>
  req<{ config: IntelligentAlertConfig; status: IntelligentStatus }>("/alerts/intelligent/config");

export const updateIntelligentAlerts = (body: Partial<IntelligentAlertConfig>) =>
  req<{ config: IntelligentAlertConfig; status: IntelligentStatus }>("/alerts/intelligent/config", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const evaluateIntelligentAlertsNow = () =>
  req<{
    signals: Array<{
      symbol: string;
      action: "BUY" | "SELL";
      reason: string;
      price: number;
      day_chg_pct: number;
      rsi: number | null;
      metric: string;
      ts: string;
    }>;
    count: number;
    as_of: string;
    status: IntelligentStatus;
  }>("/alerts/intelligent/evaluate", { method: "POST" });

export interface BriefingConfig {
  top_n: number;
  avoid_n: number;
  min_price: number;
  min_day_value_lakh: number;
  min_atr_pct: number;
  min_bars: number;
  universe: string;
  watchlist: string[];
  ranking: string;
  send_enabled: boolean;
}

export const getBriefingConfig = () =>
  req<{ config: BriefingConfig; last_sent: { sent_at: string; channel: string; chars: number } | null }>(
    "/briefing/config",
  );

export const saveBriefingConfig = (cfg: BriefingConfig) =>
  req<BriefingConfig>("/briefing/config", { method: "PUT", body: JSON.stringify(cfg) });

export const previewBriefing = () =>
  req<{ message: string; stats: Record<string, number | string> }>("/briefing/preview", {
    method: "POST",
  });

export interface LoginStatus {
  session_active: boolean;
  client_id: string | null;
  expires_at: string | null;
  login_url: string;
}

export const getLoginStatus = () => req<LoginStatus>("/login/status");

export const loginSubmit = (client_id: string, auth_code: string) =>
  req<{ session_active: boolean; client_id: string; expires_at: string }>("/login", {
    method: "POST",
    body: JSON.stringify({ client_id: client_id.trim(), auth_code: auth_code.trim() }),
  });

export const logoutSession = () =>
  req<{ logged_out: boolean }>("/logout", { method: "POST" });

export const sendBriefing = () =>
  req<{ sent_on: string; stats: Record<string, number | string> }>("/briefing/send", {
    method: "POST",
  });

// ---------------------------------------------------------------------------
// Research — walk-forward validation
// ---------------------------------------------------------------------------

export type ValidationState = "pass" | "fail" | "untested";

export interface StrategyValidation {
  state: ValidationState;
  oos_sharpe?: number | null;
  oos_return_pct?: number | null;
  benchmark_sharpe?: number | null;
  benchmark_return_pct?: number | null;
  deflated_sharpe?: number | null;
  measured_win_rate?: number | null;
  measured_trades?: number | null;
  expectancy_r?: number | null;
  z_vs_control?: number | null;
  as_of?: string | null;
  note?: string;
}

export interface StrategyInfo {
  name: string;
  tunable: string[];
  warmup_bars: number | null;
  validation: StrategyValidation;
}

export interface VerdictCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface FoldRow {
  fold: number;
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  train_sharpe: number;
  test_sharpe: number;
  test_return_pct: number;
  test_max_dd_pct: number;
  test_trades: number;
  trials: number;
  [key: string]: number | string;
}

export interface ResearchRequest {
  strategy: string;
  source: "cache" | "fetch" | "synthetic";
  symbols?: string[];
  exchange?: string;
  cash?: number;
  train?: number | null;
  test?: number | null;
  step?: number | null;
  warmup?: number | null;
  lookback_days?: number;
  search?: boolean;
  min_trades?: number;
  min_folds?: number;
  confidence?: number;
  slippage_bps?: number;
}

export interface ResearchResponse {
  strategy: string;
  source: string;
  symbols: string[];
  bars: number;
  train_bars: number;
  test_bars: number;
  step_bars: number;
  warmup_bars: number;
  n_trials: number;
  required_sharpe: number;
  deflated_sharpe: number;
  verdict: { passed: boolean; checks: VerdictCheck[] };
  warnings: string[];
  oos: Record<string, number>;
  benchmark: Record<string, number>;
  folds: FoldRow[];
  equity: EquityPoint[];
  benchmark_equity: EquityPoint[];
}

export const getStrategies = () =>
  req<{
    strategies: StrategyInfo[];
    validation_as_of?: string | null;
    control_sharpe?: number | null;
  }>("/strategies");

export const runResearch = (body: ResearchRequest) =>
  req<ResearchResponse>("/research", { method: "POST", body: JSON.stringify(body) });

// ---------------------------------------------------------------------------
// Paper-strategy validation (out-of-sample, with a random-selection control)
// ---------------------------------------------------------------------------

export interface ValidationCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface ValidationResult {
  strategy: string;
  passed?: boolean;
  error?: string;
  folds?: number;
  oos_return_pct?: number;
  oos_sharpe?: number;
  oos_max_drawdown_pct?: number;
  oos_trades?: number;
  benchmark_return_pct?: number;
  benchmark_sharpe?: number;
  deflated_sharpe?: number;
  required_sharpe?: number;
  n_trials?: number;
  measured_win_rate?: number;
  measured_trades?: number;
  measured_expectancy_r?: number;
  sharpe_z_vs_control?: number | null;
  checks?: ValidationCheck[];
}

export interface ValidationReport {
  available: boolean;
  hint?: string;
  error?: string;
  generated_at?: string;
  universe?: string[];
  symbol_bars?: number;
  window?: string;
  config?: Record<string, number | string>;
  control?: {
    sharpe_mean: number | null;
    sharpe_sd: number;
    sharpes: number[];
    returns_pct: number[];
  };
  results?: ValidationResult[];
}

export const getValidation = () => req<ValidationReport>("/validation");

// ---------------------------------------------------------------------------
// Episodic Pivot — Pradeep Bonde's playbook, measured
// ---------------------------------------------------------------------------

export interface EpisodicPivotMeasured {
  win_rate: number | null;
  trades: number;
  payoff: number | null;
  expectancy_r: number | null;
  avg_win_return_pct?: number | null;
  avg_loss_return_pct?: number | null;
  best_trade_return_pct?: number | null;
  worst_trade_return_pct?: number | null;
  median_hold_days?: number | null;
}

export interface EpisodicPivotVariant {
  passed?: boolean;
  error?: string;
  folds?: number;
  oos_return_pct?: number;
  oos_sharpe?: number;
  oos_max_drawdown_pct?: number;
  oos_trades?: number;
  benchmark_return_pct?: number;
  benchmark_sharpe?: number;
  deflated_sharpe?: number;
  required_sharpe?: number;
  n_trials?: number;
  sharpe_z_vs_control?: number | null;
  control?: {
    runs: number;
    sharpe_mean?: number | null;
    sharpe_sd?: number;
    return_mean_pct?: number | null;
  };
  measured?: EpisodicPivotMeasured;
  chosen_params?: Record<string, number>[];
  checks?: ValidationCheck[];
}

export interface EpisodicPivotUniverse {
  symbols: number;
  sessions: number;
  symbol_bars: number;
  window: string;
  variants: Record<string, EpisodicPivotVariant>;
}

export interface EpisodicPivotPremise {
  universe: string;
  symbols: number;
  symbol_bars: number;
  window: string;
  gap_days: Record<string, number>;
  gap_pct_of_days: Record<string, number>;
  volume_spike_days: Record<string, number>;
  joint_catalyst_days: number;
  joint_catalyst_pct_of_days: number;
  quiet_tape_pct_of_days: number;
  quiet_and_catalyst_days: number;
  fwd20_unconditional_pct: number | null;
  fwd20_after_catalyst_pct: number | null;
  fwd20_after_quiet_catalyst_pct: number | null;
  fwd20_after_catalyst_hit_rate: number | null;
  fwd20_after_catalyst_n: number;
  best_20d_move_pct: number | null;
  best_20d_symbol: string | null;
  windows_20d_over_50pct: number;
  windows_20d_over_100pct: number;
  windows_20d_over_50pct_share: number;
  one_day_moves_over_10pct: number;
  one_day_moves_over_20pct: number;
}

export interface EpisodicPivotReport {
  available: boolean;
  hint?: string;
  error?: string;
  generated_at?: string;
  config?: Record<string, unknown>;
  universes?: Record<string, EpisodicPivotUniverse>;
  premise?: { generated_at: string; universes: EpisodicPivotPremise[] };
}

export const getEpisodicPivot = () => req<EpisodicPivotReport>("/validation/episodic-pivot");

// ---------------------------------------------------------------------------
// Alpha hunt — pre-registered candidates, each with a matched null
// ---------------------------------------------------------------------------

export interface AlphaHuntMetrics {
  oos_return_pct: number;
  oos_sharpe: number;
  oos_calmar: number;
  oos_max_drawdown_pct: number;
  bench_return_pct: number;
  bench_sharpe: number;
  bench_calmar: number;
  bench_max_drawdown_pct: number;
  avg_exposure_pct: number | null;
  trades: number;
  commission: number;
  n_trials: number;
  deflated_sharpe: number;
}

export interface AlphaHuntHypothesis {
  prior?: string;
  error?: string;
  metrics?: AlphaHuntMetrics;
  control?: { seeds: number; sharpe_mean: number | null; sharpe_sd: number };
  verdict?: {
    passed: boolean;
    sharpe_z_vs_control: number | null;
    t_critical: number | null;
    control_df: number;
    checks: ValidationCheck[];
  };
  folds?: { fold: number; test_start: string; test_sharpe: number; params: Record<string, number> }[];
}

export interface AlphaHuntReport {
  available: boolean;
  hint?: string;
  generated_at?: string;
  config?: Record<string, unknown>;
  pre_registered?: Record<string, { prior: string; grid: Record<string, number[]> }>;
  universes?: Record<string, { symbols: number; sessions: number; window: string; hypotheses: Record<string, AlphaHuntHypothesis> }>;
}

export const getAlphaHunt = () => req<AlphaHuntReport>("/validation/alpha-hunt");

// ---------------------------------------------------------------------------
// Evidence — findings with their credibility verdicts
// ---------------------------------------------------------------------------

export interface EvidenceRegime {
  is_leverage: boolean;
  up_excess_pct: number;
  down_excess_pct: number;
  desc: string;
}

export interface EvidenceStability {
  consistent: boolean;
  positive_share: number;
  desc: string;
}

export interface EvidenceSelectionBias {
  selected_pct: number;
  unbiased_pct: number;
  gap_pct: number;
  meaningful: boolean;
}

export interface EvidenceFinding {
  title: string;
  claim: string;
  headline_pct: number;
  universe: string;
  n_trials: number;
  oos_sharpe: number | null;
  benchmark_sharpe: number | null;
  p_edge_real: number | null;
  max_drawdown_pct: number;
  benchmark_drawdown_pct: number;
  selection_bias: EvidenceSelectionBias | null;
  regime: EvidenceRegime | null;
  stability: EvidenceStability | null;
  notes: string[];
  verdict: { credible: boolean; reasons: string[] };
}

export interface EvidenceReport {
  available: boolean;
  hint?: string;
  error?: string;
  generated_at?: string;
  total?: number;
  credible_count?: number;
  findings?: EvidenceFinding[];
}

export const getEvidence = () => req<EvidenceReport>("/evidence");

// ---------------------------------------------------------------------------
// Portfolio, quotes, caches
// ---------------------------------------------------------------------------

export type PortfolioSection = "limits" | "positions" | "holdings" | "orders" | "trades";

export interface PortfolioResponse {
  sections: Record<
    string,
    { rows: Record<string, unknown>[]; count: number; error?: string }
  >;
  as_of: string;
}

export const getPortfolio = (sections?: PortfolioSection[]) =>
  req<PortfolioResponse>(
    sections?.length ? `/portfolio?sections=${sections.join(",")}` : "/portfolio",
  );

export const getQuote = (symbols: string, exchange = "NSEEQ") =>
  req<{
    exchange: string;
    as_of: string;
    quotes: Record<string, unknown>[];
    failed: { symbol: string; error: string }[];
  }>(`/quote?symbols=${encodeURIComponent(symbols)}&exchange=${encodeURIComponent(exchange)}`);

// ---------------------------------------------------------------------------
// Risk
// ---------------------------------------------------------------------------

export interface RiskLimits {
  capital: number;
  risk_per_trade_pct: number;
  max_active: number;
  rr_ratio: number;
  stop_method: "atr" | "pct" | string;
  stop_atr_mult: number;
  stop_pct: number;
  product: string;
}

export interface RiskStatus {
  kill_switch: boolean;
  execution_mode: "paper" | "live";
  env: string;
  live_orders_allowed: boolean;
  limits: RiskLimits;
  margin: Record<string, number>;
  margin_error: string | null;
}

export const getRiskStatus = () => req<RiskStatus>("/risk/status");

/**
 * Engage or release the global kill switch.
 *
 * A reason is required in **both** directions. Releasing is arguably the more
 * consequential: it re-enables trading, and a release with no recorded reason is
 * indistinguishable from someone clearing it by accident. The backend rejects a
 * blank reason, so this cannot be left to the UI to remember.
 */
export const setKillSwitch = (engaged: boolean, reason: string) =>
  req<{ kill_switch: boolean; reason: string }>(
    `/risk/kill-switch?engaged=${engaged ? "true" : "false"}&reason=${encodeURIComponent(reason)}`,
    { method: "POST" },
  );

export interface ExecutionMode {
  mode: "paper" | "live";
  live: boolean;
  paper: boolean;
  changed_at: string | null;
  changed_by: string | null;
  reason: string | null;
}

export const getExecutionMode = () => req<ExecutionMode>("/risk/execution-mode");

/** Switching to `live` requires a reason — the backend rejects it otherwise. */
export const setExecutionMode = (mode: "paper" | "live", reason: string) =>
  req<ExecutionMode>(
    `/risk/execution-mode?mode=${mode}&reason=${encodeURIComponent(reason)}`,
    { method: "POST" },
  );

export interface AuditEntry {
  ts: string;
  actor: string;
  action: string;
  subject: string;
  detail: string | null;
}

export const getAudit = (limit = 200) =>
  req<{ entries: AuditEntry[]; path: string }>(`/audit?limit=${limit}`);

export interface CacheExchange {
  exchange: string;
  symbols: number;
  size_mb: number;
  last_sync: string;
}

export const getHistoryStatus = () =>
  req<{ cache_root: string; exchanges: CacheExchange[]; total_symbols: number }>(
    "/history/status",
  );

export interface InstrumentSegment {
  segment: string;
  contracts: number | null;
  size_mb: number;
  synced_at: string;
}

export const getInstrumentsStatus = () =>
  req<{ cache_dir: string; segments: InstrumentSegment[]; total_contracts: number }>(
    "/instruments/status",
  );

export interface ScanAllResponse {
  as_of: string;
  universe: number;
  scored: number;
  breadth_up: number;
  rows: ScanRow[];
}

export const scanAll = (exchange = "NSEEQ") =>
  req<ScanAllResponse>(`/scan-all?exchange=${encodeURIComponent(exchange)}`);

export const searchSymbols = (query: string, exchange = "NSEEQ", limit = 25) =>
  req<{ results: { symbol: string; exchange: string; conid: string }[] }>(
    `/symbols?query=${encodeURIComponent(query)}&exchange=${encodeURIComponent(exchange)}&limit=${limit}`,
  );

// ─── Custom Scanner ───────────────────────────────────────────────────────────

export interface ScanCondition {
  indicator: string;        // "rsi", "sma", "close", "vol_x", etc.
  period?: number;          // for sma/ema/rsi/atr/bb_*
  op: string;               // ">", "<", ">=", "<=", "=", "crosses_above", "crosses_below"
  rhs_type: "value" | "indicator";
  rhs_value?: number;
  rhs_indicator?: string;
  rhs_period?: number;
}

export interface SavedScan {
  id: string;
  name: string;
  combine: "AND" | "OR";
  conditions: ScanCondition[];
}

export interface CustomScanRow {
  symbol: string;
  last: number;
  day_chg_pct: number;
  day_high: number;
  day_low: number;
  ret_1m: number;
  vs_high: number;
  trend: string;
  rsi: number;
  vol_x: number;
  atr_pct: number;
  gold_cross_5d: boolean;
  breakout: boolean;
  score: number;
  bars: number;
  _cond_values?: Record<string, number>;
}

export interface CustomScanResponse {
  as_of: string;
  universe_size: number;
  matched: number;
  elapsed_s: number;
  rows: CustomScanRow[];
}

export const runCustomScan = (body: {
  conditions: ScanCondition[];
  combine: "AND" | "OR";
  universe: "all" | "watchlist";
  watchlist?: string[];
  exchange?: string;
}) => req<CustomScanResponse>("/scanner/custom", { method: "POST", body: JSON.stringify(body) });

export const getSavedScans = () =>
  req<{ scans: SavedScan[] }>("/scanner/saved");

export const upsertSavedScan = (id: string, scan: SavedScan) =>
  req<{ saved: SavedScan }>(`/scanner/saved/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(scan),
  });

export const deleteSavedScan = (id: string) =>
  req<{ deleted: string }>(`/scanner/saved/${encodeURIComponent(id)}`, { method: "DELETE" });

// ─── Screener (v1) ────────────────────────────────────────────────────────────
//
// The v1 screener is a different, stricter model than `/scanner/custom` above.
// The meaningful differences the UI has to respect:
//
//  * Conditions are a *tree* (`{match, conditions}` groups nesting arbitrarily),
//    not a flat list with one global AND/OR.
//  * Every row carries `why` — the evidence for each passing leaf, with the
//    measured value and the sentence explaining it. That is the feature.
//  * Some indicators are declared but not computable (no fundamentals feed).
//    They come back with `available: false` and a reason, so the builder greys
//    them out instead of offering a filter that can never match.

/** A condition node: either a group (`match` + `conditions`) or a leaf. */
export interface ScreenerNode {
  match?: "all" | "any";
  conditions?: ScreenerNode[];
  label?: string;
  negate?: boolean;
  // leaf fields
  indicator?: string;
  op?: string;
  value?: number;
  upper?: number;
  period?: number;
  rhs_indicator?: string;
  rhs_period?: number;
}

export interface ScreenerEvidence {
  label: string;
  indicator: string;
  period: number | null;
  op: string;
  value: number | null;
  target: number | null;
  target_indicator: string | null;
  target_period: number | null;
  upper: number | null;
  /** True when the indicator could not be computed. Distinct from "did not
   *  match": the test never happened, and the UI must not imply it failed. */
  unmeasurable: boolean;
  passed: boolean;
  reason: string;
  unit: string;
}

export interface ScreenerRow {
  symbol: string;
  ltp?: number;
  change_pct?: number;
  volume?: number;
  rel_volume?: number;
  rsi14?: number;
  ema20?: number;
  ema50?: number;
  atr_pct?: number;
  setup?: string;
  /** Evidence for the leaves that actually passed. */
  why: ScreenerEvidence[];
  /** Evidence for every leaf, passed or not. */
  evidence: ScreenerEvidence[];
  [column: string]: unknown;
}

export interface ScreenerRunResponse {
  as_of: string | null;
  exchange: string;
  universe: string;
  universe_size: number;
  scanned: number;
  matched: number;
  returned: number;
  sort: string;
  descending: boolean;
  columns: string[];
  conditions: { groups: number; leaves: number; summary: string };
  elapsed_s: number;
  rows: ScreenerRow[];
  warnings: string[];
  errors: { symbol: string; error: string }[];
}

export interface ScreenerUniverse {
  name: string;
  label: string;
  size: number;
  members: number;
  missing_history: number;
}

export interface ScreenerIndicator {
  key: string;
  label: string;
  group: string;
  unit: string;
  takes_period: boolean;
  default_period: number | null;
  available: boolean;
  requires: string;
  description: string;
}

export interface ScreenerColumn {
  key: string;
  label: string;
  indicator: string;
}

export interface ScreenerSaved {
  scan_id: string;
  name: string;
  description: string | null;
  definition: {
    universe?: string;
    exchange?: string;
    conditions?: ScreenerNode;
    columns?: string[];
    sort?: string;
  };
  created_at: string | null;
  updated_at: string | null;
}

export const screenerUniverses = (exchange = "NSEEQ") =>
  req<{ exchange: string; universes: ScreenerUniverse[] }>(
    `/api/v1/screener/universes?exchange=${encodeURIComponent(exchange)}`,
  );

export const screenerIndicators = () =>
  req<{
    indicators: ScreenerIndicator[];
    groups: string[];
    available: string[];
    unavailable: string[];
  }>("/api/v1/screener/indicators");

export const screenerColumns = () =>
  req<{
    columns: ScreenerColumn[];
    sort_fields: { key: string; description: string }[];
    default_sort: string;
  }>("/api/v1/screener/columns");

export const screenerValidate = (conditions: ScreenerNode) =>
  req<{
    valid: boolean;
    error?: string;
    code?: string;
    /** Present only when valid — the human-readable form of the tree. */
    summary?: string;
    groups?: number;
    leaves?: number;
    warnings?: string[];
  }>("/api/v1/screener/validate", {
    method: "POST",
    body: JSON.stringify({ conditions }),
  });

export const screenerRun = (body: {
  universe?: string;
  exchange?: string;
  conditions: ScreenerNode;
  columns?: string[];
  sort?: string;
  descending?: boolean;
  limit?: number;
}) =>
  req<ScreenerRunResponse>("/api/v1/screener/run", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const screenerListSaved = () =>
  req<{ scans: ScreenerSaved[] }>("/api/v1/screener/saved");

export const screenerSave = (body: {
  name: string;
  description?: string;
  definition: ScreenerSaved["definition"];
}) =>
  req<ScreenerSaved>("/api/v1/screener/saved", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const screenerDeleteSaved = (scanId: string) =>
  req<{ deleted: string }>(`/api/v1/screener/saved/${encodeURIComponent(scanId)}`, {
    method: "DELETE",
  });

export const screenerSavedResults = (scanId: string, limit = 50) =>
  req<ScreenerRunResponse>(
    `/api/v1/screener/saved/${encodeURIComponent(scanId)}/results?limit=${limit}`,
  );

// ─── Trade Signals (Semi-auto) ────────────────────────────────────────────────

export interface TradeSignal {
  id: string;
  symbol: string;
  action: "BUY" | "SELL";
  setup: string;
  reason: string;
  entry_price: number;
  stop_loss: number;
  target: number;
  quantity: number;
  risk_amount: number;
  rr_ratio: number;
  rsi: number | null;
  vol_x: number | null;
  paper_citation?: string | null;
  thesis?: string | null;
  confidence_score?: number | null;
  expected_value?: number | null;
  regime_fit?: string | null;
  status: "PENDING" | "ACTIVE" | "DONE" | "SKIPPED";
  created_at: string;
  expires_at: string | null;
  executed_at: string | null;
  entry_order_id: string | null;
  sl_order_id: string | null;
  target_order_id: string | null;
}

export interface TradeSignalSettings {
  capital: number;
  risk_per_trade_pct: number;
  stop_method: "atr" | "pct";
  stop_atr_mult: number;
  stop_pct: number;
  rr_ratio: number;
  max_active: number;
  exchange: string;
  product: string;
}

export interface TradeSignalsResponse {
  signals: TradeSignal[];
  pending: number;
  active: number;
}

export const getTradeSignals = (status?: string) =>
  req<TradeSignalsResponse>(
    `/trade-signals${status ? `?status=${status}` : ""}`,
  );

export const scanTradeSignals = () =>
  req<{ scanned: number; new_signals: number; signals: TradeSignal[] }>(
    "/trade-signals/scan", { method: "POST" },
  );

export const executeTradeSignal = (id: string) =>
  req<{ signal_id: string; entry_order: string; sl_order: string; target_order: string }>(
    `/trade-signals/${encodeURIComponent(id)}/execute`, { method: "POST" },
  );

export const skipTradeSignal = (id: string) =>
  req<{ skipped: string }>(`/trade-signals/${encodeURIComponent(id)}/skip`, { method: "POST" });

export const getTradeSignalSettings = () =>
  req<TradeSignalSettings>("/trade-signals/settings");

export const saveTradeSignalSettings = (s: Partial<TradeSignalSettings>) =>
  req<TradeSignalSettings>("/trade-signals/settings", {
    method: "PUT",
    body: JSON.stringify(s),
  });

// ─── Self-Learning Quant Engine ───────────────────────────────────────────────

export interface StrategyWeightInfo {
  strategy_id: string;
  name: string;
  paper_citation: string;
  alpha_prior: number;
  beta_prior: number;
  total_trades: number;
  winning_trades: number;
  profit_factor: number;
  allocation_weight: number;
  best_regime: string;
  active: boolean;
}

export interface MarketRegimeInfo {
  regime: "BULL_TREND" | "BEAR_TREND" | "RANGEBOUND" | "VOLATILITY_SHOCK" | string;
  breadth_pct: number;
  volatility_ratio: number;
  nifty_trend: string;
  confidence: number;
  as_of: string;
}

export interface SelfLearningStatus {
  market_regime: MarketRegimeInfo;
  strategies: Record<string, StrategyWeightInfo>;
  total_cycles_trained: number;
  last_trained_at: string;
  model_version: string;
}

export const getSelfLearningStatus = () =>
  req<SelfLearningStatus>("/self-learning/status");

export const trainSelfLearningModel = () =>
  req<{ universe_size: number; market_regime: MarketRegimeInfo; cycles_trained: number; strategies: Record<string, StrategyWeightInfo> }>(
    "/self-learning/train", { method: "POST" },
  );

// ═════════════════════════════════════════════════════════════════════════════
// Platform accounts and watchlists — the versioned `/api/v1` surface
// ═════════════════════════════════════════════════════════════════════════════
//
// Two unrelated credentials live in this app, and conflating them causes real
// confusion, so:
//
//   * the **broker session** (`/login`, `getLoginStatus`, `logoutSession`) is a
//     daily IIFL trading credential. It authorises *orders*.
//   * the **platform account** (`/api/v1/auth/*`) is who may use the dashboard
//     at all. It authorises *the human*.
//
// They are independent on purpose. An account with no broker session can read
// the research; a broker session with no account is what a single-operator box
// with `AUTH_REQUIRED=false` looks like.
//
// The account session is an opaque token in an **HttpOnly** cookie, so
// JavaScript cannot read it — that is the whole point of it being HttpOnly. Every
// request therefore has to opt into sending it via `credentials: "include"`.

/** An error from `/api/v1`, carrying the stable machine-readable `code`. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** True when the caller is simply not signed in, as opposed to being refused. */
export const isUnauthorized = (e: unknown): boolean =>
  e instanceof ApiError && e.status === 401;

/** True when the caller is signed in but lacks the permission. */
export const isForbidden = (e: unknown): boolean =>
  e instanceof ApiError && e.status === 403;

/**
 * Pull `{detail, code}` out of an error body.
 *
 * FastAPI produces two shapes and both reach us: a bare `{"detail": "..."}` from
 * a plain `HTTPException`, and `{"detail": {"detail": "...", "code": "..."}}`
 * when the handler passed a dict as the detail — which every route here does, so
 * that a rejection carries a stable `code` the UI can branch on instead of
 * pattern-matching on prose. Middleware (CSRF, rate limiting) returns the flat
 * form. This normalises all three.
 */
function errorParts(body: unknown): { detail: string; code: string } {
  const outer = (body ?? {}) as Record<string, unknown>;
  const inner = outer.detail ?? outer;
  if (typeof inner === "string") return { detail: inner, code: "" };
  const obj = (inner ?? {}) as Record<string, unknown>;
  const detail =
    typeof obj.detail === "string"
      ? obj.detail
      : typeof obj.message === "string"
        ? obj.message
        : "";
  return { detail, code: typeof obj.code === "string" ? obj.code : "" };
}

async function v1<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  // 204 carries no body; parsing "" as JSON would throw and turn a success into
  // a reported failure.
  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!res.ok) {
    const { detail, code } = errorParts(body);
    // The request id is echoed on every response; quoting it turns "it broke"
    // into a line an operator can find in the log.
    const rid = res.headers.get("X-Request-ID");
    throw new ApiError(
      res.status,
      code || `http_${res.status}`,
      detail || `${res.status} ${res.statusText}${rid ? ` [${rid}]` : ""}`,
    );
  }
  return body as T;
}

export interface BootstrapStatus {
  /** No account exists yet, so the first-run screen is the right thing to show. */
  needs_setup: boolean;
  /** Whether self-registration is open on this instance. */
  allow_signup: boolean;
  env: string;
  auth_required: boolean;
}

export interface AppUser {
  user_id: string;
  email: string;
  username: string;
  display_name: string | null;
  role: "viewer" | "researcher" | "trader" | "admin" | "owner" | string;
  is_active: boolean;
  mfa_enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_login_at: string | null;
}

export interface Principal {
  user_id: string;
  username: string;
  email: string;
  display_name: string | null;
  role: string;
  /** What this principal may actually do right now. */
  permissions: string[];
  /** `session` | `api-key` | `anonymous` — how it authenticated. */
  auth_method: string;
  /** False when a password login still owes its TOTP step. */
  mfa_satisfied: boolean;
  /** `/auth/me` only: everything the role grants, for greying out UI. */
  role_permissions?: string[];
}

export interface LoginSuccess {
  token: string;
  expires_at: string;
  user: AppUser;
}

export interface MfaChallenge {
  mfa_required: true;
  detail?: string;
}

export interface SessionRow {
  session_id: string;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface ApiKeyRow {
  key_id: string;
  label: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
}

export interface CreatedApiKey extends Omit<ApiKeyRow, "revoked" | "last_used_at"> {
  /** Returned exactly once, at creation. Never retrievable afterwards. */
  key: string;
  warning?: string;
}

export const getBootstrapStatus = () =>
  v1<BootstrapStatus>("/auth/bootstrap");

/** Create the first account. Succeeds once; refused thereafter. */
export const bootstrapOwner = (body: {
  email: string;
  username: string;
  password: string;
  display_name?: string;
}) =>
  v1<LoginSuccess>("/auth/bootstrap", { method: "POST", body: JSON.stringify(body) });

/** Self-registration. Only available when `allow_signup` is on. */
export const registerAccount = (body: {
  email: string;
  username: string;
  password: string;
  display_name?: string;
}) => v1<{ user: AppUser }>("/auth/register", { method: "POST", body: JSON.stringify(body) });

/**
 * Password login.
 *
 * Returns a `MfaChallenge` (HTTP 202) when the password was accepted but a TOTP
 * code is still owed. That is deliberately not a thrown error: the credentials
 * were correct, and the caller needs to tell the difference to know whether to
 * reveal a code field or show "wrong password".
 */
export const appLogin = async (body: {
  identifier: string;
  password: string;
  totp_code?: string;
}): Promise<LoginSuccess | MfaChallenge> =>
  v1<LoginSuccess | MfaChallenge>("/auth/login", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const appLogout = () => v1<{ logged_out: number }>("/auth/logout", { method: "POST" });

export const appLogoutAll = () =>
  v1<{ sessions_revoked: number }>("/auth/logout-all", { method: "POST" });

export const getMe = () => v1<Principal>("/auth/me");

export const updateMe = (body: { display_name?: string; email?: string }) =>
  v1<{ user: AppUser }>("/auth/me", { method: "PATCH", body: JSON.stringify(body) });

/** Changes the password and revokes every session, including this one. */
export const changePassword = (body: { current_password: string; new_password: string }) =>
  v1<{ changed: boolean; sessions_revoked: boolean }>("/auth/me/password", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const enrollMfa = () =>
  v1<{ secret: string; otpauth_uri: string; enabled: boolean }>("/auth/me/mfa/enroll", {
    method: "POST",
  });

export const activateMfa = (code: string) =>
  v1<{ enabled: boolean }>("/auth/me/mfa/activate", {
    method: "POST",
    body: JSON.stringify({ code }),
  });

export const disableMfa = (password: string) =>
  v1<{ enabled: boolean }>("/auth/me/mfa/disable", {
    method: "POST",
    body: JSON.stringify({ password }),
  });

export const getMySessions = () => v1<{ sessions: SessionRow[] }>("/auth/me/sessions");

export const revokeSession = (sessionId: string) =>
  v1<{ revoked: boolean }>(`/auth/me/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });

export const getMyApiKeys = () => v1<{ keys: ApiKeyRow[] }>("/auth/me/api-keys");

export const createApiKey = (body: {
  label: string;
  scopes?: string[];
  expires_in_days?: number;
}) => v1<CreatedApiKey>("/auth/me/api-keys", { method: "POST", body: JSON.stringify(body) });

export const revokeApiKey = (keyId: string) =>
  v1<{ revoked: boolean }>(`/auth/me/api-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });

// ─── Watchlists ───────────────────────────────────────────────────────────────

export interface WatchlistSummary {
  watchlist_id: string;
  user_id: string;
  name: string;
  exchange: string;
  is_default: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
  item_count: number;
  columns: string[];
}

export interface WatchlistDetail extends WatchlistSummary {
  /** Just the tickers, in display order. */
  items: string[];
}

export interface ColumnSpec {
  key: string;
  label: string;
  group: string;
  kind: "currency" | "number" | "percent" | "integer" | "text" | string;
  /**
   * False when this repo has no data source for the column yet. The UI must
   * render these as "—" with the reason, never as 0 — a zero looks like a
   * measurement, and this platform does not report a missing feed as a value.
   */
  available: boolean;
  requires: string | null;
  description: string;
}

export interface AvailableColumns {
  columns: ColumnSpec[];
  groups: string[];
  available: string[];
  unavailable: string[];
}

export interface WatchlistQuoteRow {
  symbol: string;
  name: string | null;
  industry?: string | null;
  indices?: string[];
  /** True when the price came from the local cache rather than the broker. */
  stale?: boolean;
  /** Set when there is no local history for this symbol at all. */
  error?: string;
  [column: string]: unknown;
}

export interface WatchlistQuotes {
  watchlist_id: string;
  name: string;
  exchange: string;
  as_of: string;
  /** `broker+cache` when at least one live price arrived, else `local_cache`. */
  source: string;
  live_symbols: string[];
  columns: ColumnSpec[];
  rows: WatchlistQuoteRow[];
  count: number;
}

export interface AddItemsResult {
  added: string[];
  /** Already present — reported, not an error. */
  skipped: string[];
  /** Not in the instrument master. Surfaced so a typo is visible. */
  unknown: string[];
}

export const getAvailableColumns = () => v1<AvailableColumns>("/watchlists/columns/available");

export const listWatchlists = () =>
  v1<{ watchlists: WatchlistSummary[] }>("/watchlists");

export const createWatchlist = (body: {
  name: string;
  exchange?: string;
  columns?: string[];
}) => v1<WatchlistDetail>("/watchlists", { method: "POST", body: JSON.stringify(body) });

export const getWatchlist = (id: string) =>
  v1<WatchlistDetail>(`/watchlists/${encodeURIComponent(id)}`);

export const updateWatchlist = (
  id: string,
  body: { name?: string; exchange?: string; is_default?: boolean },
) =>
  v1<WatchlistDetail>(`/watchlists/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const deleteWatchlist = (id: string) =>
  v1<{ deleted: boolean; watchlist_id: string }>(`/watchlists/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });

export const addWatchlistItems = (id: string, symbols: string[]) =>
  v1<AddItemsResult>(`/watchlists/${encodeURIComponent(id)}/items`, {
    method: "POST",
    body: JSON.stringify({ symbols }),
  });

export const removeWatchlistItem = (id: string, symbol: string) =>
  v1<{ removed: string }>(
    `/watchlists/${encodeURIComponent(id)}/items/${encodeURIComponent(symbol)}`,
    { method: "DELETE" },
  );

/** Sets the whole order at once. Idempotent, so a retry cannot double-move. */
export const reorderWatchlistItems = (id: string, symbols: string[]) =>
  v1<WatchlistDetail>(`/watchlists/${encodeURIComponent(id)}/items/order`, {
    method: "PUT",
    body: JSON.stringify({ symbols }),
  });

export const setWatchlistColumns = (id: string, columns: string[]) =>
  v1<WatchlistDetail & { rejected_columns?: string[] }>(
    `/watchlists/${encodeURIComponent(id)}/columns`,
    { method: "PUT", body: JSON.stringify({ columns }) },
  );

/**
 * Every configured column for every symbol.
 *
 * `live=true` asks the broker first and falls back to the cached close per
 * symbol; each row then carries `stale` so a cached price is never displayed as
 * a live one.
 */
export const getWatchlistQuotes = (id: string, live = true) =>
  v1<WatchlistQuotes>(
    `/watchlists/${encodeURIComponent(id)}/quotes?live=${live ? "true" : "false"}`,
  );

// ─── Instrument master ────────────────────────────────────────────────────────

export interface InstrumentRecord {
  symbol: string;
  exchange: string;
  asset_class: string;
  series: string | null;
  name: string | null;
  isin: string | null;
  industry: string | null;
  lot_size: number;
  tick_size: number;
  bars: number;
  first_date: string | null;
  last_date: string | null;
  cache_file: string | null;
  /** Which granularities have local data — `["1d"]`, `["1d", "15m"]`, … */
  timeframes: string[];
  indices: string[];
}

/**
 * Ranked symbol search.
 *
 * Accepts any spelling — `RELIANCE`, `reliance-eq`, `NIFTY 50` — and returns the
 * canonical record. Canonicalisation belongs to the server, so the client never
 * has to guess whether to strip a series suffix.
 */
export const searchInstruments = (
  q: string,
  opts: { exchange?: string; asset_class?: string; limit?: number } = {},
) => {
  const params = new URLSearchParams({ q });
  if (opts.exchange) params.set("exchange", opts.exchange);
  if (opts.asset_class) params.set("asset_class", opts.asset_class);
  if (opts.limit) params.set("limit", String(opts.limit));
  return v1<{ query: string; count: number; results: InstrumentRecord[] }>(
    `/instruments/search?${params.toString()}`,
  );
};

export const getInstrument = (symbol: string) =>
  v1<InstrumentRecord>(`/instruments/${encodeURIComponent(symbol)}`);

export const getInstrumentExchanges = () =>
  v1<{ exchanges: { exchange: string; symbols: number }[] }>("/instruments/exchanges");

/** Provenance of the master: how many symbols, how fresh, built from where. */
export const getInstrumentStatus = () =>
  v1<Record<string, unknown>>("/instruments/status");

export const getInstrumentUniverses = () =>
  v1<{ universes: Record<string, number> }>("/instruments/universes");

export const getUniverse = (name: string) =>
  v1<{ name: string; count: number; symbols: string[] }>(
    `/instruments/universes/${encodeURIComponent(name)}`,
  );

// ─── Audit trail ──────────────────────────────────────────────────────────────
//
// The queryable table, not the JSONL file. `GET /audit` (legacy, above) still
// reads the file — that sink is restart-proof and is the one an operator greps.
// These are the same facts with filters, which is what a UI needs.

export interface AuditEvent {
  event_id: string;
  ts: string;
  user_id: string | null;
  /** Denormalised: survives the user row being deleted. */
  actor: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  strategy_id: string | null;
  strategy_version: number | null;
  request_id: string | null;
  result: "success" | "failure" | "denied" | "pending" | string;
  detail: string | null;
  ip: string | null;
}

export interface AuditQuery {
  user_id?: string;
  action?: string;
  result?: "success" | "failure" | "denied" | "pending";
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export const getAuditEvents = (query: AuditQuery = {}) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return v1<{ events: AuditEvent[]; total: number; limit: number; offset: number }>(
    `/audit/events${qs ? `?${qs}` : ""}`,
  );
};

export const getAuditEvent = (eventId: string) =>
  v1<AuditEvent>(`/audit/events/${encodeURIComponent(eventId)}`);

/** Distinct action names, for building a filter dropdown. */
export const getAuditActions = () => v1<{ actions: string[] }>("/audit/actions");

