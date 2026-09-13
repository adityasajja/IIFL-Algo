// Typed client for the ATR FastAPI backend (src/atr/api/main.py).

export const API_URL =
  import.meta.env.VITE_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

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
}

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

export const setKillSwitch = (engaged: boolean) =>
  req<{ kill_switch: boolean }>(
    `/risk/kill-switch?engaged=${engaged ? "true" : "false"}`,
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

