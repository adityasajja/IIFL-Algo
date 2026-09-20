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
  try {
    return (await res.json()) as T;
  } catch {
    // A 2xx response that isn't JSON means we hit something other than the
    // API (a proxy fallback, an SPA route, a misconfigured reverse proxy) —
    // surface that plainly instead of dumping a raw JSON.parse message.
    throw new Error(`${path} did not return JSON — the API may be unreachable`);
  }
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

/** Get sub-second/minute candles resampled from live tick buffer */
export const getTickCandles = (symbol: string, intervalSeconds: number) =>
  req<Candle[]>(`/ticks/candles?symbol=${encodeURIComponent(symbol)}&interval=${intervalSeconds}`);

export interface SizingConfigPayload {
  method?: string;
  capital_allocation?: number | null;
  risk_per_trade_pct?: number | null;
  risk_per_trade_rupees?: number | null;
  fixed_quantity?: number | null;
  fixed_rupee_value?: number | null;
  capital_fraction?: number | null;
  max_quantity?: number | null;
  max_position_value?: number | null;
  max_portfolio_exposure_pct?: number | null;
  min_quantity?: number | null;
  rounding_rule?: string;
  atr_multiplier?: number;
}

export interface SizingPreviewRequest {
  entry_price: number;
  capital: number;
  available_capital?: number | null;
  stop_price?: number | null;
  stop_loss_pct?: number | null;
  atr?: number | null;
  current_stock_exposure?: number;
  max_stock_exposure?: number | null;
  current_portfolio_exposure?: number;
  max_total_portfolio_exposure?: number | null;
  current_sector_exposure?: number;
  max_sector_exposure?: number | null;
  sizing?: SizingConfigPayload;
}

export interface SizingPreviewResult {
  method: string;
  raw_quantity: number;
  final_quantity: number;
  entry_price: number;
  stop_price: number | null;
  risk_per_share: number | null;
  risk_amount: number | null;
  stop_distance: number | null;
  atr: number | null;
  atr_multiplier: number | null;
  capital_allocated: number;
  capital_available: number;
  position_value: number;
  portfolio_impact_pct: number;
  sector_impact_pct: number | null;
  remaining_capital: number;
  capped_by: string | null;
  cap_reasons: string[];
  rejection_reason: string | null;
}

export const previewPositionSize = (body: SizingPreviewRequest) =>
  req<SizingPreviewResult>("/strategies/sizing/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });


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
// Strategy authoring — /api/v1/strategies
//
// The authoring surface, and the only way to produce the `(strategy_id,
// strategy_version)` pair a paper deployment pins. `getStrategies()` above reads
// the *code* registry: those strategies are compiled in, have no version history
// and cannot be deployed. A deployable strategy is one of these.
//
// Two fields carry the honesty of the whole surface and neither is decorative:
//
//   `deployable`  — computed by the runner's own resolver, so a version marked
//                   deployable is one the live loop will actually trade. A
//                   version that cannot resolve to entry/exit rules is reported
//                   as undeployable *with its reason*, because its failure mode
//                   is silent: the deployment runs and places no orders.
//   `statistical_validation` — always `performed: false`. Structural validation
//                   answers "will this execute", not "will this make money", and
//                   a green tick is otherwise read as the second one.
// ---------------------------------------------------------------------------

/** One issue from `validateStrategy`. `field` names the path, e.g. `exit.stop_loss_pct`. */
export interface StrategyIssue {
  code: string;
  field: string;
  message: string;
}

/** The structural verdict on a definition. */
export interface StrategyValidation {
  ok: boolean;
  errors: StrategyIssue[];
  warnings: StrategyIssue[];
  /** Whether the live paper loop can turn this into entry/exit rules. */
  paper: {
    resolvable: boolean;
    source?: string | null;
    reason?: string | null;
    /** Rule fields the author set, and the ones left at the module's defaults. */
    authored_fields?: string[];
    default_fields?: string[];
  };
  /** Whether a backtest can run it, via `engine_key` + `params`. */
  backtest: {
    engine_key?: string | null;
    resolvable: boolean;
    reason?: string | null;
    params?: string[];
  };
  /** Always `performed: false` here — see the note above. */
  statistical_validation: {
    performed: boolean;
    reason: string;
    how_to_measure: string;
  };
  strategy_id?: string;
  version?: number | null;
  /** `"draft"` when a definition was passed, `"stored"` when a version was read. */
  checked?: "draft" | "stored";
  stored?: boolean;
}

/** A version of a saved strategy. Immutable — there is no edit call. */
export interface StrategyVersion {
  strategy_id: string;
  version: number;
  created_at: string | null;
  change_note: string | null;
  definition_hash: string;
  is_deployed: boolean;
  deployable: boolean;
  not_deployable_reason: string | null;
}

/** A version plus its definition, from `getStrategyVersion`. */
export interface StrategyVersionDetail extends StrategyVersion {
  definition: Record<string, unknown> | null;
  canonical_definition: string | null;
  definition_parse_error?: string | null;
  author_user_id?: string;
}

/** A saved strategy. `latest_version: null` means it cannot be deployed yet. */
export interface SavedStrategy {
  strategy_id: string;
  user_id: string;
  name: string;
  description: string | null;
  kind: string;
  engine_key: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  latest_version: number | null;
  version_count: number;
  deployable: boolean;
}

export const listSavedStrategies = () =>
  req<{ strategies: SavedStrategy[]; total: number; kinds: string[] }>(
    "/api/v1/strategies",
  );

export const createSavedStrategy = (body: {
  name: string;
  kind?: string;
  description?: string | null;
  engine_key?: string | null;
}) =>
  req<SavedStrategy>("/api/v1/strategies", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const listStrategyVersions = (strategyId: string) =>
  req<{
    strategy_id: string;
    versions: StrategyVersion[];
    total: number;
    deployable_versions: number[];
  }>(`/api/v1/strategies/${encodeURIComponent(strategyId)}/versions`);

export const getStrategyVersion = (strategyId: string, version: number) =>
  req<StrategyVersionDetail>(
    `/api/v1/strategies/${encodeURIComponent(strategyId)}/versions/${version}`,
  );

export const createStrategyVersion = (
  strategyId: string,
  body: { definition: unknown; change_note?: string | null; force?: boolean },
) =>
  req<StrategyVersion & { validation: StrategyValidation }>(
    `/api/v1/strategies/${encodeURIComponent(strategyId)}/versions`,
    { method: "POST", body: JSON.stringify(body) },
  );

/** Validate a draft definition, or a stored version when `definition` is omitted. */
export const validateStrategy = (
  strategyId: string,
  body: { definition?: unknown; version?: number | null },
) =>
  req<StrategyValidation>(
    `/api/v1/strategies/${encodeURIComponent(strategyId)}/validate`,
    { method: "POST", body: JSON.stringify(body) },
  );

/** Create the worked example strategy and its version 1. Idempotent. */
export const seedExampleStrategy = (name?: string) =>
  req<{
    strategy: SavedStrategy;
    version: StrategyVersion & { validation?: StrategyValidation };
    created: boolean;
    note: string | null;
  }>(
    "/api/v1/strategies/seed" +
      (name ? `?name=${encodeURIComponent(name)}` : ""),
    { method: "POST" },
  );

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

export interface TrackedSignal {
  symbol: string;
  entry_date: string;
  entry_close: number;
  rsi?: number;
  status: "open" | "closed";
  exit_close?: number;
  ret_pct?: number;
  hit?: boolean;
}

export interface ForwardTracker {
  description: string;
  state: "collecting" | "working" | "not_working" | "inconclusive";
  verdict: string;
  graded: number;
  needed: number;
  hits: number;
  hit_rate_pct: number | null;
  range_pct: [number, number] | null;
  avg_net_pct: number | null;
  base_rate_pct: number;
  claimed_rate_pct: number;
  open: TrackedSignal[];
  recent: TrackedSignal[];
}

export const getForwardTracker = () => req<ForwardTracker>("/tracker/oversold-volatile");

export interface GapPlanStats {
  graded: number;
  open: number;
  win_rate_pct: number | null;
  avg_net_pct: number | null;
  median_net_pct: number | null;
  weeks: number;
}

export interface GapPlanTrade {
  symbol: string;
  entry_date: string;
  entry: number;
  gap_pct: number;
  status: "open" | "closed";
  exit_reason?: "target" | "stop" | "friday";
  exit_price?: number;
  net_pct?: number;
  source: "live" | "replay";
}

export interface GapPlan {
  plan: { target_pct: number; stop_pct: number; gap_pct: number; market_min_pct: number };
  this_week: { as_of: string | null; median_pct: number | null; needed_pct: number; status: "trade" | "skip" | "unknown" };
  verdict: string;
  live_from: string | null;
  live: GapPlanStats;
  replay: GapPlanStats;
  when_market_did_not_qualify: GapPlanStats;
  open: GapPlanTrade[];
  recent: GapPlanTrade[];
  needed: number;
}

export const getGapPlan = () => req<GapPlan>("/tracker/gap-plan");

export interface PnlDay {
  date: string;
  pnl: number;
  trades: number;
  wins: number;
  losses: number;
  estimated: boolean;
}

export interface PnlMonth {
  scope: "paper" | "real";
  month: string;
  days: PnlDay[];
  total: number;
  traded_days: number;
  green_days: number;
  red_days: number;
  best_day: { date: string; pnl: number } | null;
  worst_day: { date: string; pnl: number } | null;
  current_green: number;
  best_green: number;
  worst_red: number;
  months_with_data: string[];
  any_estimated: boolean;
  ticket: number | null;
  holdings_source?: string;
  unpriced?: string[];
}

export const getPnlCalendar = (scope: "paper" | "real", month?: string) =>
  req<PnlMonth>(`/pnl/calendar?scope=${scope}${month ? `&month=${month}` : ""}`);

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

// ─── Backtests (Phase 2) ──────────────────────────────────────────────────────
//
// The asynchronous surface, not the legacy synchronous `POST /backtest` above.
// The split matters: a real-history run over eleven years of daily bars takes
// long enough that holding an HTTP connection open for it is a bug waiting to
// happen. So the submit call returns a run_id immediately and everything else
// is read back by that id.
//
// A run is *reproducible*: `config_fingerprint` hashes the request and
// `data_fingerprint` hashes the input series, so two runs agreeing on both
// really did compute the same thing from the same numbers. That is the point of
// persisting the whole config rather than a summary of it.

/** One version of a saved strategy. */
export interface StrategyVersionOption {
  version: number;
  created_at: string | null;
  change_note: string | null;
}

/** One selectable strategy in the config form.
 *
 *  `kind` is the load-bearing field. `builtin` names a strategy in the code
 *  registry by `key`; `saved` names a row in the user's strategy table by
 *  `strategy_id`, and *that* is what makes a specific version reproducible — a
 *  built-in has no version history to pin. */
export interface StrategyOption {
  kind: "builtin" | "saved";
  /** The engine that executes it. `null` only if a saved row lost its engine. */
  key: string | null;
  name: string;
  strategy_id: string | null;
  description?: string | null;
  /** Built-ins report `[]` — they are not versioned. */
  versions: StrategyVersionOption[];
}

/** A named symbol list the user can pick instead of typing tickers. */
export interface UniverseOption {
  name: string;
  count?: number;
  [k: string]: unknown;
}

/** A dropdown entry. `value` is what goes on the wire, `label` what is shown. */
export interface ChoiceOption {
  value: string;
  label: string;
  /** Present on timeframes: false means the form must grey it out. */
  available?: boolean;
  /** Why it is unavailable — shown, not hidden, so the user is not left
   *  wondering whether they imagined the option. */
  reason?: string | null;
}

/** Everything the config form needs to render itself, in one round trip. */
export interface BacktestOptions {
  strategies: StrategyOption[];
  timeframes: ChoiceOption[];
  cost_models: ChoiceOption[];
  sizing_modes: ChoiceOption[];
  sources: ChoiceOption[];
  exchanges: string[];
  universes: UniverseOption[];
  limits: {
    max_symbols?: number;
    min_capital?: number;
    max_persisted_trades?: number;
  };
}

/** The run configuration as the backend stores it. Mirrors `BacktestRunConfig`. */
export interface BacktestConfig {
  /** Human-readable strategy name. Stored alongside `engine_key`. */
  strategy?: string;
  engine_key?: string | null;
  strategy_id?: string | null;
  strategy_version?: number | null;
  params?: Record<string, unknown>;
  universe?: string | null;
  symbols?: string[] | null;
  exchange?: string;
  timeframe?: string;
  start?: string | null;
  end?: string | null;
  source?: "cache" | "synthetic";
  initial_cash?: number;
  sizing?: {
    mode?: string;
    percent?: number | null;
    quantity?: number | null;
    max_position_pct?: number | null;
  };
  stops?: {
    stop_loss_pct?: number | null;
    take_profit_pct?: number | null;
    trailing_stop_pct?: number | null;
  };
  costs?: { model?: string; slippage_bps?: number };
  allow_short?: boolean;
  square_off_eod?: boolean;
  risk_free_rate?: number;
  benchmark?: string | null;
}

export type BacktestStatus =
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

/** Summary row in the run history. */
export interface BacktestRunSummary {
  run_id: string;
  status: BacktestStatus;
  /** 0.0 → 1.0. Coarse by design — see `GET /{run_id}`. */
  progress: number;
  /** The engine key a builtin run used. */
  strategy: string | null;
  strategy_id: string | null;
  strategy_version: number | null;
  created_at: string | null;
  finished_at: string | null;
  /** Either a symbol count (a typed list) or a universe name. */
  symbols: number | string | null;
  total_return_pct: number | null;
  sharpe: number | null;
  max_drawdown_pct: number | null;
  num_trades: number | null;
  error: string | null;
}

/** A single run's full status. */
export interface BacktestRun extends BacktestRunSummary {
  engine_key: string | null;
  started_at: string | null;
  data_fingerprint: string | null;
  config: BacktestConfig | null;
  metrics: Record<string, number | string | boolean | null> | null;
  /** True once a data fingerprint exists, i.e. the run can be replayed. */
  reproducible: boolean;
}

export interface BacktestMetrics {
  run_id: string;
  metrics: Record<string, number | string | boolean | null>;
  data_fingerprint: string | null;
}

export interface CurvePoint {
  ts: string;
  value: number;
}

/** Equity, drawdown and exposure share one response and one round trip —
 *  a client forced to make three calls to draw one chart makes three calls. */
export interface BacktestCurves {
  run_id: string;
  equity: CurvePoint[];
  drawdown: CurvePoint[];
  exposure: CurvePoint[];
}

export interface BacktestTrade {
  seq: number;
  symbol: string;
  direction: "LONG" | "SHORT" | string;
  quantity: number;
  entry_ts: string;
  entry_price: number;
  exit_ts: string | null;
  exit_price: number | null;
  gross_pnl: number | null;
  commission: number | null;
  net_pnl: number | null;
  return_pct: number | null;
  duration_days: number | null;
  /** Why the trade closed: `signal`, `stop_loss`, `take_profit`, `trailing_stop`. */
  exit_reason: string | null;
  /** Why it opened, in the strategy's own words. `null` when the strategy
   *  cannot explain itself — the page says "not recorded" rather than
   *  inventing a reason. */
  signal_reason: string | null;
  strategy_version: number | null;
}

/** One trade plus the config it was produced under. Only `GET /trades/{seq}`
 *  returns this; the list endpoint returns the flat shape above. */
export interface BacktestTradeDetail extends BacktestTrade {
  /** The strategy version that produced this trade. `strategy_id` is nested
   *  here rather than flat, because it describes the *version*, not the fill. */
  strategy: {
    engine_key: string | null;
    strategy_id: string | null;
    version: number | null;
    params: Record<string, unknown>;
  };
  exits: {
    stop_loss_pct: number | null;
    take_profit_pct: number | null;
    trailing_stop_pct: number | null;
  };
  costs: { model?: string; slippage_bps?: number };
  sizing: { mode?: string; percent?: number | null; quantity?: number | null };
}

export interface BacktestTradePage {
  run_id: string;
  trades: BacktestTrade[];
  total: number;
  limit: number;
  offset: number;
}

/** One row of the monthly matrix. `months[m]` is month `m + 1`; `null` is a
 *  month in which the series had no bars, which is not the same as 0.00%. */
export interface MonthlyRow {
  year: number;
  months: (number | null)[];
  /** Compounding the twelve months, carried so the matrix is checkable
   *  rather than decorative. */
  year_total: number | null;
}

export interface BacktestMonthly {
  run_id: string;
  matrix: MonthlyRow[];
}

export const backtestOptions = () =>
  v1<BacktestOptions>("/backtests/options");

export const submitBacktest = (body: BacktestConfig) =>
  v1<{
    run_id: string;
    status: BacktestStatus;
    progress: number;
    config: BacktestConfig;
    fingerprint: string;
  }>("/backtests", { method: "POST", body: JSON.stringify(body) });

export const listBacktests = (params: {
  status?: BacktestStatus | "ALL";
  limit?: number;
  offset?: number;
} = {}) => {
  const qs = new URLSearchParams();
  if (params.status && params.status !== "ALL") qs.set("status", params.status);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  const suffix = qs.toString();
  return v1<{ runs: BacktestRunSummary[]; total: number }>(
    `/backtests${suffix ? `?${suffix}` : ""}`,
  );
};

export const getBacktest = (runId: string) =>
  v1<BacktestRun>(`/backtests/${encodeURIComponent(runId)}`);

export const backtestMetrics = (runId: string) =>
  v1<BacktestMetrics>(`/backtests/${encodeURIComponent(runId)}/metrics`);

export const backtestCurves = (runId: string) =>
  v1<BacktestCurves>(`/backtests/${encodeURIComponent(runId)}/equity`);

export const backtestTrades = (
  runId: string,
  params: { limit?: number; offset?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  const suffix = qs.toString();
  return v1<BacktestTradePage>(
    `/backtests/${encodeURIComponent(runId)}/trades${suffix ? `?${suffix}` : ""}`,
  );
};

export const backtestTrade = (runId: string, seq: number) =>
  v1<BacktestTradeDetail>(
    `/backtests/${encodeURIComponent(runId)}/trades/${encodeURIComponent(String(seq))}`,
  );

export const backtestMonthly = (runId: string) =>
  v1<BacktestMonthly>(`/backtests/${encodeURIComponent(runId)}/monthly`);

export const cancelBacktest = (runId: string) =>
  v1<{ run_id: string; status: BacktestStatus; cancelled: boolean }>(
    `/backtests/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  );

// ─── Paper deployments ────────────────────────────────────────────────────────
//
// The deployment is the unit that turns a strategy into a *running* thing: it
// names one immutable strategy version, an allocation of capital, and a
// universe. Everything below is scoped to one deployment id, because a paper
// account with capital but no deployment has no starting cash and reports zero
// rather than a number somebody chose.
//
// Two rules run through this whole section and the UI must honour both:
//
// * Every action that stops or restarts trading carries a **reason**. The
//   backend rejects a blank one, so this is not a UI nicety — a pause with no
//   recorded reason is indistinguishable from an accident when somebody asks
//   why the strategy stopped.
// * `STOPPED` is terminal. `start` accepts only `PENDING` and `PAUSED`; the way
//   to run a stopped strategy again is `reset`, which returns a **new**
//   deployment. That is why `resetDeployment` returns a different id.

export type DeploymentStatus = "PENDING" | "RUNNING" | "PAUSED" | "STOPPED";

/** The deployment row, plus the folded P&L the list endpoint attaches. */
export interface Deployment {
  deployment_id: string;
  user_id?: string;
  strategy_id: string;
  strategy_version: number;
  mode: "PAPER" | "LIVE" | string;
  status: DeploymentStatus | string;
  capital: number;
  broker_account: string | null;
  /** Stored as a JSON string on the row; `status.symbols` parses it for the UI. */
  config: string | Record<string, unknown> | null;
  started_at: string | null;
  stopped_at: string | null;
  stop_reason: string | null;
  created_at: string;
  updated_at?: string;
  /** Present on `list` only. Marks are deliberately omitted there — valuing
   *  every deployment's positions to render a list of rows is N cache reads
   *  for information the row does not need. */
  pnl?: PaperSnapshot;
  /** Present on `reset` only: the deployment this one replaced. Its history
   *  stays readable, so the previous run's P&L is still available. */
  reset_from?: string;
  reset_reason?: string;
}

/** The folded account. `positions` are the only rows that carry a valuation. */
export interface PaperSnapshot {
  initial_cash: number;
  cash: number;
  equity: number;
  market_value: number;
  realized_pnl: number;
  unrealized_pnl: number;
  commission_paid: number;
  /** Net of frictions — the only figure worth showing next to a gross one. */
  net_realized_pnl: number;
  total_pnl: number;
  gross_exposure: number;
  net_exposure: number;
  positions: PaperPosition[];
  /** False when at least one held symbol could not be marked. The totals then
   *  cover only the priced positions, and `unpriced_symbols` names the rest.
   *  A UI that ignores this shows a partial total as a whole one. */
  complete: boolean;
  unpriced_symbols: string[];
  deployment_id?: string | null;
}

export interface PaperPosition {
  symbol: string;
  exchange: string;
  quantity: number;
  avg_price: number;
  /** `null` — not `0` — when the position could not be marked. Zero would read
   *  as a total loss of the cost basis. */
  last_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number;
  direction: string;
  priced: boolean;
}

/** What the continuous loop is doing, platform-wide. */
export interface RunnerStatus {
  running: boolean;
  deployments?: number;
  in_market_hours?: boolean;
  [k: string]: unknown;
}

export const createDeployment = (body: {
  strategy_id: string;
  strategy_version: number;
  capital: number;
  mode?: "PAPER" | "LIVE";
  broker_account?: string | null;
  config?: Record<string, unknown> | null;
}) => v1<Deployment>("/paper/deployments", { method: "POST", body: JSON.stringify(body) });

export const listDeployments = () =>
  v1<{ deployments: Deployment[]; total: number }>("/paper/deployments");

export const getDeployment = (deploymentId: string) =>
  v1<Deployment>(`/paper/deployments/${encodeURIComponent(deploymentId)}`);

/** Start or resume. Only `PENDING` and `PAUSED` accept this — see the note above. */
export const startDeployment = (deploymentId: string) =>
  v1<Deployment>(`/paper/deployments/${encodeURIComponent(deploymentId)}/start`, {
    method: "POST",
  });

export const pauseDeployment = (deploymentId: string, reason: string) =>
  v1<Deployment>(`/paper/deployments/${encodeURIComponent(deploymentId)}/pause`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const stopDeployment = (deploymentId: string, reason: string) =>
  v1<Deployment>(`/paper/deployments/${encodeURIComponent(deploymentId)}/stop`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

/** Stop this deployment and return a **new** one carrying the same config.
 *
 *  Not a delete: `order_events` is append-only, so the old account's history
 *  cannot be erased and its P&L stays comparable. The response's `reset_from`
 *  names the deployment it replaced. */
export const resetDeployment = (deploymentId: string, reason: string) =>
  v1<Deployment>(`/paper/deployments/${encodeURIComponent(deploymentId)}/reset`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const getRunnerStatus = () => v1<RunnerStatus>("/paper/runner");

// ─── Champion vs challenger ───────────────────────────────────────────────
// Two versions of one strategy trading side by side in PAPER. Launch copies
// the champion's capital, universe and config — only the version differs —
// and the comparison below only ever reports readiness verdicts
// (INSUFFICIENT EVIDENCE / EARLY EVIDENCE / COMPARISON READY), never a
// winner, and promotes nothing: there is no promotion route.

export interface ChallengerParity {
  champion_deployment_id: string | null;
  capital_copied: number;
  config_copied: Record<string, unknown> | null;
  venue_shared: boolean;
  note: string;
}

export type ChallengerDeployment = Deployment & { parity?: ChallengerParity };

export const launchChallenger = (body: {
  strategy_id: string;
  champion_deployment_id: string;
  challenger_version: number;
}) =>
  v1<ChallengerDeployment>("/paper/challengers", {
    method: "POST",
    body: JSON.stringify(body),
  });

export type ComparisonVerdict =
  | "INSUFFICIENT EVIDENCE"
  | "EARLY EVIDENCE"
  | "COMPARISON READY";

export interface ComparisonArmSummary {
  n: number;
  n_with_metric: number;
  metric: string;
  stats: LearningBucketStats;
  max_drawdown: number | null;
  score_bands: { band: string; forward_trades: number; required: number | null; status: string }[];
  score_coverage: { with_score: number; scanned: number };
  regime_distribution: Record<string, number>;
  sector_distribution: Record<string, number>;
  recent_vs_historical: {
    window_days: number;
    recent_n: number;
    recent_mean: number | null;
    historical_n: number;
    historical_mean: number | null;
  };
  evidence_status: string;
  next_gate: { required: number | null; have: number; status: string };
}

export interface ComparisonArmContext {
  forward_n: number;
  forward_with_metric: number;
  bands: Record<string, number | null>;
  monotonic_high_is_better: boolean | null;
  may_claim: boolean;
  statement: string;
  unavailable?: boolean;
}

export interface ChampionComparison {
  strategy_id: string;
  champion_version: number;
  challenger_version: number;
  generated_at: string | null;
  metric: string;
  metric_note: string | null;
  duplicates_dropped: number;
  comparison: {
    metric: string;
    champion: ComparisonArmSummary;
    challenger: ComparisonArmSummary;
    deltas: { mean: number | null; median: number | null; win_rate: number | null; profit_factor: number | null };
    verdict: ComparisonVerdict;
    verdict_reasons: string[];
  };
  context: { champion: ComparisonArmContext; challenger: ComparisonArmContext };
  deployments: {
    champion: {
      deployment_id: string;
      mode: string | null;
      status: string | null;
      capital: number | null;
      config: unknown;
    } | null;
    challenger: {
      deployment_id: string;
      mode: string | null;
      status: string | null;
      capital: number | null;
      config: unknown;
    } | null;
  };
  parity: {
    identical_capital: boolean;
    identical_config: boolean;
    venue_shared: boolean;
    mismatches: string[];
    note: string;
  };
  definition_diff: {
    changes: { parameter: string; champion: unknown; challenger: unknown }[];
    identical: boolean;
  };
  timeline: {
    version: number;
    role: "CHAMPION" | "CHALLENGER" | null;
    forward_observations: number;
    created_at: string | null;
  }[];
  advisory_only: boolean;
  applies_changes: boolean;
  promotes: boolean;
}

export const compareChampionChallenger = (opts: {
  strategyId: string;
  championVersion?: number;
  challengerVersion?: number;
  refresh?: boolean;
}) => {
  const q = new URLSearchParams({ strategy_id: opts.strategyId });
  if (opts.championVersion != null) q.set("champion_version", String(opts.championVersion));
  if (opts.challengerVersion != null) q.set("challenger_version", String(opts.challengerVersion));
  if (opts.refresh) q.set("refresh", "true");
  return v1<ChampionComparison>(`/paper/champions/compare?${q.toString()}`);
};

// ─── Monitoring ───────────────────────────────────────────────────────────────
//
// One deployment, everything its screen needs. `overview` is deliberately **one
// request rather than six**: the six views are of one state, and a screen that
// fetches P&L, then positions, then orders can render a position from before a
// fill next to a P&L from after it — and the two disagree for as long as the
// operator is looking at them.
//
// Read-only throughout. Nothing here can start, stop or alter a deployment: a
// monitoring bug must be able to misreport and never to mis-trade.

/** The five causal stages, in the order the chain runs. */
export type TimelineStage = "signal" | "risk" | "order" | "fill" | "position";

export interface TimelineEvent {
  ts: string;
  stage: TimelineStage;
  symbol: string | null;
  summary: string;
  /** A word, not a status code: `observed`, `approved`, `rejected`, `placed`,
   *  `filled`, `recorded`. The wording lives server-side so the two cannot drift. */
  outcome: string;
  /** Present when the step was a *decision*, not a fact. */
  reason: string | null;
  order_id: string | null;
  detail: Record<string, unknown>;
}

/** The monitoring header. `trading` is the field to read first and the one that
 *  is easy to get wrong: a deployment can be `RUNNING` and still place no orders.
 *  When `trading` is false, `not_trading_because` says why. */
export interface MonitorStatus {
  deployment_id: string;
  status: DeploymentStatus | string;
  mode: string;
  strategy_id: string;
  strategy_version: number;
  capital: number;
  symbols: string[];
  exchange: string;
  timeframe: string;
  started_at: string | null;
  stopped_at: string | null;
  stop_reason: string | null;
  created_at: string;
  trading: boolean;
  not_trading_because: string | null;
  diagnostic_state?: string;
  runner_state?: string | null;
  skipped_reason?: string | null;
  last_tick_time?: string | null;
  last_tick_age_seconds?: number | null;
  last_strategy_evaluation?: string | null;
  last_signal?: {
    symbol?: string;
    rule?: string;
    reason?: string;
    side?: string;
    at?: string;
  } | null;
  last_risk_decision?: {
    symbol?: string;
    side?: string;
    quantity?: number;
    approved?: boolean;
    reason?: string;
    rule?: string | null;
    at?: string;
  } | null;
  last_order?: {
    symbol?: string;
    side?: string;
    quantity?: number;
    status?: string;
    order_id?: string;
    reason?: string;
  } | null;
  last_fill?: {
    symbol?: string;
    side?: string;
    quantity?: number;
    status?: string;
    order_id?: string;
    reason?: string;
  } | null;
  skipped_evaluations_count?: number;
  skipped_fills_count?: number;
  trade_count?: number;
  open_trades_count?: number;
  today_pnl?: number | null;
  cumulative_pnl?: number | null;
  equity?: number | null;
  /** Set when the strategy version's rules could not be resolved. A runner that
   *  refuses to trade on a guessed default, rather than silently using
   *  `EntryRules()` defaults and mis-attributing the orders. */
  blocked_reason: string | null;
  in_market_hours: boolean;
  runner_running: boolean;
  loop_attached: boolean;
  last_pass: Record<string, unknown> | null;
}

export interface ForwardEvidenceCounts {
  total_genuine_forward: number;
  today_genuine_forward: number;
  last_7d_genuine_forward: number;
  by_class: {
    IN_SAMPLE: { total: number; today: number; last_7d: number };
    PAPER_FORWARD: { total: number; today: number; last_7d: number };
    LIVE_FORWARD: { total: number; today: number; last_7d: number };
  };
  by_strategy: Array<{
    strategy_id: string;
    strategy_version: number | null;
    total_genuine_forward: number;
    today_genuine_forward: number;
    last_7d_genuine_forward: number;
    paper_forward: number;
    live_forward: number;
    in_sample: number;
    total: number;
  }>;
  filtered_strategy_id?: string | null;
  filtered_strategy_version?: number | null;
  as_of: string;
}

/** Lifetime and *today's* P&L, kept as separate figures.
 *
 *  `today_pnl` is `null` — never `0` — when the log holds no fill before the
 *  session boundary. There is no earlier equity to subtract, so `0` would
 *  report a first day's gain as nothing. The two mean different things and the
 *  UI must be able to tell them apart.
 *
 *  Written out rather than `extends PaperSnapshot`: this read *re-defines*
 *  `total_pnl` as `null`-able, because it measures it against the allocated
 *  capital, and a deployment whose capital is unknown has no total P&L rather
 *  than one of zero. An inheritance that silently narrowed that back to
 *  `number` would be the type system telling a lie. */
export interface MonitorPnl {
  deployment_id: string;
  capital: number;
  initial_cash: number;
  cash: number;
  equity: number;
  market_value: number;
  realized_pnl: number;
  unrealized_pnl: number;
  commission_paid: number;
  net_realized_pnl: number;
  gross_exposure: number;
  net_exposure: number;
  positions: PaperPosition[];
  complete: boolean;
  unpriced_symbols: string[];
  today_pnl: number | null;
  today_pct: number | null;
  today_since: string | null;
  /** Against the *allocated capital*, which is what a deployment is judged on —
   *  not against the initial cash the ledger folded to. */
  total_pnl: number | null;
  total_pct: number | null;
  exposure_pct: number | null;
}

export interface MonitorOrder {
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  order_type: string;
  limit_price: number | null;
  status: string;
  /** The row's own column name. The event payload calls the same thing
   *  `filled_qty`; renaming it here would make this read disagree with the row. */
  filled_quantity: number;
  avg_fill_price: number | null;
  created_at: string;
  updated_at: string | null;
  strategy_id: string | null;
  strategy_version: number | null;
  signal_reason: string | null;
  events: number;
}

export interface MonitorFill {
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  commission: number | null;
  ts: string;
  value: number;
}

export interface MonitorSignal {
  order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  rule: string | null;
  reason: string | null;
  price: number | null;
  strategy_id: string | null;
  strategy_version: number | null;
  ts: string;
}

export interface MonitorTrade {
  [k: string]: unknown;
}

export interface MonitorTrades {
  trades: MonitorTrade[];
  total: number;
  /** An open trade has no realised P&L, so it is reported separately: a win
   *  rate computed from a mixed list is dragged toward zero by rows that have
   *  not resolved yet. */
  open: MonitorTrade[];
  open_count: number;
  all_count: number;
}

export interface MonitorRisk {
  kill_switch: boolean;
  execution_mode: string;
  limits: Record<string, unknown>;
  changed_at: string | null;
  changed_by: string | null;
  reason: string | null;
  /** The deployment's own numbers, so a limit can be read against the figure it
   *  constrains rather than in the abstract. */
  observed: {
    open_positions: number;
    gross_exposure: number;
    net_exposure: number;
    equity: number;
    cash: number;
    unpriced_symbols: string[];
  };
}

/** The whole monitoring screen, in one read. */
export interface MonitorOverview {
  status: MonitorStatus;
  pnl: MonitorPnl;
  positions: PaperPosition[];
  orders: MonitorOrder[];
  fills: MonitorFill[];
  signals: MonitorSignal[];
  trades: MonitorTrades;
  risk: MonitorRisk;
  timeline: TimelineEvent[];
}

export const monitorOverview = (deploymentId: string) =>
  v1<MonitorOverview>(`/monitor/deployments/${encodeURIComponent(deploymentId)}`);

export const monitorStatus = (deploymentId: string) =>
  v1<MonitorStatus>(`/monitor/deployments/${encodeURIComponent(deploymentId)}/status`);

export const monitorPnl = (deploymentId: string) =>
  v1<MonitorPnl>(`/monitor/deployments/${encodeURIComponent(deploymentId)}/pnl`);

export const monitorTimeline = (deploymentId: string, limit = 300) =>
  v1<{ events: TimelineEvent[]; total: number; stages: TimelineStage[] }>(
    `/monitor/deployments/${encodeURIComponent(deploymentId)}/timeline?limit=${limit}`,
  );

export const monitorSignals = (deploymentId: string, limit = 200) =>
  v1<{ signals: MonitorSignal[]; total: number }>(
    `/monitor/deployments/${encodeURIComponent(deploymentId)}/signals?limit=${limit}`,
  );

export const monitorTrades = (deploymentId: string, limit = 200) =>
  v1<MonitorTrades>(
    `/monitor/deployments/${encodeURIComponent(deploymentId)}/trades?limit=${limit}`,
  );

export const monitorRisk = (deploymentId: string) =>
  v1<MonitorRisk>(`/monitor/deployments/${encodeURIComponent(deploymentId)}/risk`);


// ---------------------------------------------------------------------------
// learning — the adaptive intelligence engine (read-only)
// ---------------------------------------------------------------------------

/**
 * The learning engine produces findings, never changes.
 *
 * Every type here is read-only at the transport level: there is no POST, PUT,
 * PATCH or DELETE against `/learning`. That mirrors the backend, where the same
 * guarantee is enforced by an empty method set on the router.
 *
 * The one thing to get right when rendering these payloads: a metric can be
 * **absent** rather than zero. `status` is the field to branch on, not the
 * value — `delta: null` with `status: "insufficient"` means "not measured",
 * and rendering it as 0.00 states a finding the data does not contain.
 */

export interface LearningMissing {
  [feature: string]: string;
}

export interface LearningStatus {
  available: boolean;
  reason?: string;
  counts: Record<string, number>;
  trades_available: number;
  /** How many of those can carry a claim. A different question from whether
   *  there is enough to analyse: a book of four hundred backfilled rows clears
   *  the sample floor and holds no evidence at all. */
  forward_available: number;
  sufficient_for_analysis: boolean;
  /** Whether the **forward** book clears the floor. Render this, not
   *  `sufficient_for_analysis`, next to anything that reads as a finding. */
  sufficient_for_a_claim: boolean;
  missing_features: LearningMissing;
  note: string;
}

export interface LearningDatasetSummary {
  generated_at: string | null;
  /** Every row, closed or not. "Observations" and "trades" are the same count;
   *  both names are on the payload because both are used in prose. */
  observations: number;
  trades: number;
  closed_trades: number;
  open_trades: number;
  /** Rows recorded before their outcome was known. **These are the evidence.**
   *  The number to read first, and the one a finding depends on. */
  forward_observations: number;
  /** Rows measured on the history a rule was selected from — backtests and
   *  backfills. A record, not a test. A book can hold thousands and no
   *  forward row, and this pair is what makes that visible. */
  in_sample_observations: number;
  /** When the most recent forward observation closed. `null` when none has. */
  latest_forward_ts: string | null;
  sources: Record<string, number>;
  /** `{forward, in_sample}` — always both keys, including at zero, so "0
   *  forward" is stated rather than inferred from an absent field. */
  evidence_grades: Record<string, number>;
  /** The four-value provenance breakdown: BACKTEST, IN_SAMPLE, PAPER_FORWARD,
   *  LIVE_FORWARD. The drift comparison needs these; a finding needs the grade. */
  evidence_classes: Record<string, number>;
  /** How many closed trades actually carry each outcome column. A column can be
   *  absent rather than empty — the paper ledger records returns and no size, so
   *  `net_pnl` is 0 there while `return_pct` is the row count. */
  metric_coverage: Record<string, number>;
  paper_ledger: Record<string, unknown>;
  strategies: string[];
  symbols: number;
  date_range: { first: string | null; last: string | null };
  net_pnl_total: number | null;
  missing_features: LearningMissing;
  warnings: string[];
}

/**
 * The per-bucket statistics, as `atr.research.learning_stats.summarise` emits
 * them. They are nested under `stats` in the payload rather than spread onto
 * the bucket, so the bucket's own fields and the computed ones stay separable.
 */
export interface LearningBucketStats {
  n?: number;
  wins?: number;
  win_rate?: number | null;
  win_rate_ci?: number[] | null;
  mean?: number | null;
  mean_ci?: number[] | null;
  median?: number | null;
  trimmed_mean?: number | null;
  stdev?: number | null;
  without_best?: number | null;
  profit_factor?: number | null;
  payoff_ratio?: number | null;
  total?: number | null;
  best?: number | null;
  worst?: number | null;
  small_sample?: boolean;
}

export interface LearningBucket {
  label: string;
  n: number;
  /** Which axis this bucket belongs to. Present on `notable`, where entries
   *  from every axis are flattened into one ranked list. */
  axis?: string | null;
  stats?: LearningBucketStats | null;
  /** The difference between this bucket's mean and the baseline mean, **in the
   *  metric's own units** — not a ratio, so it must not be rendered as a
   *  percentage when the metric is money. */
  lift: number | null;
  significance: string;
  suppressed: boolean;
  note?: string | null;
  p_value?: number | null;
  p_adjusted?: number | null;
  comparisons?: number | null;
}

export interface LearningBreakdown {
  axis: string;
  label: string;
  metric: string;
  rows_with_value: number;
  rows_scanned: number;
  coverage: number | null;
  baseline: Record<string, unknown>;
  buckets: LearningBucket[];
}

export interface LearningAnalysis {
  strategy: string;
  n: number;
  metric: string;
  /** Set when the metric was resolved rather than requested — the book records
   *  returns and no rupees, so the analysis ran on `return_pct`. */
  metric_note?: string | null;
  overall: Record<string, unknown>;
  breakdowns: LearningBreakdown[];
  notable: LearningBucket[];
  caveats: string[];
}

export interface LearningObservation {
  kind: string;
  severity: string;
  statement: string;
  evidence: string;
  confidence: string;
  sample_size: number;
  advisory: boolean;
}

export interface LearningReport {
  as_of: string;
  window_days: number;
  headline: string;
  trades_today: number;
  /** Which outcome column the figures are expressed in. */
  metric: string;
  metric_note?: string | null;
  /** `sum` for a currency metric, `mean` for a percentage one. Summing the
   *  returns of an equal-weight basket reports the basket once per pick. */
  aggregate: string;
  /** Today's total in `metric` units. */
  metric_total: number | null;
  /** Today's net rupees — `null` whenever the metric is not `net_pnl`. */
  net_pnl_today: number | null;
  expectation: Record<string, unknown>;
  deviation: Record<string, unknown>;
  by_strategy: Record<string, unknown>[];
  strongest: Record<string, unknown>[];
  weakest: Record<string, unknown>[];
  regime: Record<string, unknown>;
  unusual: string[];
  execution: Record<string, unknown>;
  drift: Record<string, unknown>;
  /** The evidence **grades** this report drew on. Both by default: the report's
   *  first job is to say what happened, and a book of in-sample rows still had a
   *  day. What is gated is the *claim*, which `claimable` decides. */
  scope: string[];
  /** Closed forward observations in the book — the count that decides whether
   *  anything here is claimable. */
  forward_observations: number;
  /** Closed in-sample observations in the book. Reported beside the forward
   *  count because the two together say how much of this book is evidence. */
  in_sample_observations: number;
  /** The grades of the trades that closed **today**, by count. A day whose
   *  trades are all in-sample produced a figure and no evidence. */
  today_grades: Record<string, number>;
  /** Whether the book holds enough *forward* observations to support a claim.
   *  `false` means the figures are a record and not a finding. */
  claimable: boolean;
  observations: LearningObservation[];
  limitations: string[];
  advisory: boolean;
  applies_changes: boolean;
}

export interface DriftMetric {
  metric: string;
  label: string;
  higher_is_better: boolean;
  reference: string;
  comparison: string;
  /** ok | insufficient | absent | not_comparable — branch on this, not on delta. */
  status: string;
  reference_value: number | null;
  comparison_value: number | null;
  reference_n: number;
  comparison_n: number;
  delta: number | null;
  relative: number | null;
  /** improved | deteriorated | changed | unchanged | unknown */
  direction: string;
  /** within noise | modest | material | large | unknown */
  magnitude: string;
  reason: string | null;
  p_value: number | null;
  significant: boolean;
}

export interface DriftFinding {
  kind: string;
  severity: string;
  statement: string;
  evidence?: string;
  metric?: string;
  sample_size?: number;
  confidence?: string;
}

export interface DriftPair {
  reference: string;
  comparison: string;
  reference_n: number;
  comparison_n: number;
  status: string;
  metrics: DriftMetric[];
  distribution: Record<string, unknown>;
  regime: Record<string, unknown>;
  findings: DriftFinding[];
  limitations: string[];
  deteriorated: string[];
  improved: string[];
}

export interface LearningDrift {
  strategy: string;
  available_sources: Record<string, number>;
  headline: string;
  findings: DriftFinding[];
  limitations: string[];
  pairs: DriftPair[];
  advisory: boolean;
  applies_changes: boolean;
}

export interface StoredLearningObservation {
  observation_id: string;
  strategy_id: string;
  strategy_version?: number | null;
  date: string;
  created_at: string;
  metric: string;
  condition_bucket: string;
  sample_size: number;
  statistical_result: Record<string, any>;
  evidence_class: string;
  confidence?: number | null;
  source_trades?: string[];
}

export interface BacktestVsForwardComparison {
  strategy: string;
  strategy_version?: number | null;
  backtest_n: number;
  forward_n: number;
  comparison: DriftPair;
  findings: DriftFinding[];
  limitations: string[];
  distribution: Record<string, unknown>;
  regime: Record<string, unknown>;
}

export interface DailyLearningCycleReport {
  cycle_id: string;
  execution_date: string;
  started_at: string;
  completed_at: string;
  runtime_seconds: number;
  status: "SUCCESS" | "NO_DATA" | "INSUFFICIENT_SAMPLE" | "PARTIAL" | "ERROR";
  strategies_analyzed: string[];
  trades_processed: number;
  new_forward_trades_count: number;
  observations_generated: number;
  hypotheses_generated: number;
  candidates_generated: number;
  recommendations_generated: number;
  recommendation_ids: string[];
  drift_detected: Record<string, boolean>;
  insufficient_data_strategies: string[];
  notes: string[];
  errors: string[];
}

export interface LearningOverview {
  generated_at: string | null;
  empty: boolean;
  limited: boolean;
  limitations: string[];
  dataset: LearningDatasetSummary;
  report: LearningReport;
  analysis: LearningAnalysis;
  drift: LearningDrift;
  backtest_vs_forward?: BacktestVsForwardComparison;
  forward_evidence_counts?: ForwardEvidenceCounts;
  observations?: StoredLearningObservation[];
  latest_cycle?: DailyLearningCycleReport | null;
  missing_features: LearningMissing;
  advisory: boolean;
  applies_changes: boolean;
}

export interface LearningAxes {
  available: Record<string, { label: string; max_values: number | null }>;
  default: string[];
  unavailable: LearningMissing;
}

export const learningStatus = () => v1<LearningStatus>("/learning/status");

export const learningDataset = () => v1<LearningDatasetSummary>("/learning/dataset");

export const learningOverview = (windowDays = 90) =>
  v1<LearningOverview>(`/learning/overview?window_days=${windowDays}`);

export const learningBacktestVsForward = (strategy?: string) =>
  v1<BacktestVsForwardComparison>(
    `/learning/backtest-vs-forward${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ""}`
  );

export const getForwardEvidenceCounts = (strategyId?: string, version?: number) => {
  const q = new URLSearchParams();
  if (strategyId) q.set("strategy", strategyId);
  if (version !== undefined && version !== null) q.set("strategy_version", String(version));
  const query = q.toString() ? `?${q.toString()}` : "";
  return v1<ForwardEvidenceCounts>(`/learning/evidence-counts${query}`);
};

export const learningObservationsList = (limit = 100) =>
  v1<{ total: number; observations: StoredLearningObservation[] }>(
    `/learning/observations?limit=${limit}`
  );

export const learningReport = (windowDays = 90) =>
  v1<LearningReport>(`/learning/report?window_days=${windowDays}`);

export function learningPerformance(
  opts: {
    strategy?: string;
    roles?: string;
    axes?: string;
    metric?: string;
    minSample?: number;
  } = {},
): Promise<LearningAnalysis> {
  const q = new URLSearchParams();
  if (opts.strategy) q.set("strategy", opts.strategy);
  if (opts.roles) q.set("roles", opts.roles);
  if (opts.axes) q.set("axes", opts.axes);
  if (opts.metric) q.set("metric", opts.metric);
  if (opts.minSample != null) q.set("min_sample", String(opts.minSample));
  const suffix = q.toString();
  return v1<LearningAnalysis>(`/learning/performance${suffix ? "?" + suffix : ""}`);
}

export function learningDrift(
  opts: { strategy?: string; reference?: string; roles?: string } = {},
): Promise<LearningDrift> {
  const q = new URLSearchParams();
  if (opts.strategy) q.set("strategy", opts.strategy);
  if (opts.reference) q.set("reference", opts.reference);
  if (opts.roles) q.set("roles", opts.roles);
  const suffix = q.toString();
  return v1<LearningDrift>(`/learning/drift${suffix ? "?" + suffix : ""}`);
}

export const learningAxes = () => v1<LearningAxes>("/learning/axes");

// ─── Forward-learning readiness ───────────────────────────────────────────
// Whether trustworthy evidence is accumulating, per strategy. States are
// research labels (NOT READY → MINIMUM SAMPLE → ANALYSIS READY →
// OPTIMIZATION ELIGIBLE); the payload carries `advisory_only` and
// `applies_changes: false` so no client can render them as actions taken.

export type ReadinessState =
  | "NOT READY"
  | "MINIMUM SAMPLE"
  | "ANALYSIS READY"
  | "OPTIMIZATION ELIGIBLE";

export type EvidenceStatus = "INSUFFICIENT" | "SMALL_SAMPLE" | "ANALYSIS_READY";

export interface ReadinessGate {
  required: number | null;
  have: number;
  status: EvidenceStatus;
}

export interface ReadinessScoreBand {
  band: string;
  forward_trades: number;
  required: number | null;
  status: EvidenceStatus;
}

export interface ReadinessWeek {
  week: string;
  trades: number;
  cumulative: number;
}

export interface ReadinessQualityIssue {
  code: string;
  severity: "blocking" | "watch";
  count: number;
  sample_refs: string[];
  explanation: string;
}

export interface ReadinessStrategy {
  strategy_id: string;
  strategy_name: string;
  versions_seen: (number | null)[];
  current_version: number | null;
  deployment: { mode: string | null; status: string | null } | null;
  forward_trades: number;
  open_forward_trades: number;
  state: ReadinessState;
  state_reasons: string[];
  next_gate: ReadinessGate;
  summary: {
    n: number;
    n_with_metric: number;
    metric: string;
    stats: LearningBucketStats;
    max_drawdown: number | null;
    score_bands: ReadinessScoreBand[];
    score_coverage: { with_score: number; scanned: number };
    regime_distribution: Record<string, number>;
    sector_distribution: Record<string, number>;
    recent_vs_historical: {
      window_days: number;
      recent_n: number;
      recent_mean: number | null;
      historical_n: number;
      historical_mean: number | null;
    };
  };
  metric_note: string | null;
  progression: ReadinessWeek[];
  context_observations_recorded: number;
  forward_observations_recorded: number;
  optimization_ready: boolean;
  missing_features: string[];
  last_trade: string | null;
  last_cycle: {
    cycle_id: string | null;
    execution_date: string | null;
    status: string | null;
    strategy_included: boolean;
  } | null;
  quality_issues: ReadinessQualityIssue[];
}

export interface LearningReadiness {
  generated_at: string | null;
  gates: number[];
  forward_trades_unique: number;
  duplicates_dropped: number;
  strategies: ReadinessStrategy[];
  quality_issues: ReadinessQualityIssue[];
  limited: boolean;
  limitations: string[];
  advisory_only: boolean;
  applies_changes: boolean;
}

export const learningReadiness = (strategyId?: string) =>
  v1<LearningReadiness>(
    `/learning/readiness${strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : ""}`
  );

// ─── Strategy Optimization ───────────────────────────────────────────────────

export interface AdaptiveParameterSpec {
  name: string;
  current: number;
  minimum: number;
  maximum: number;
  step: number;
  adaptive: boolean;
  block: string;
  description?: string;
}

export interface OptimizationMetrics {
  cagr_pct: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  sortino: number;
  profit_factor: number;
  win_rate_pct: number;
  num_trades: number;
  avg_trade: number;
  median_return?: number | null;
  trimmed_mean?: number | null;
  skew?: number | null;
  kurtosis?: number | null;
}

export interface WalkForwardMetrics {
  in_sample_sharpe: number;
  out_of_sample_sharpe: number;
  in_sample_profit_factor: number;
  out_of_sample_profit_factor: number;
  out_of_sample_cagr_pct: number;
  out_of_sample_max_dd_pct: number;
  out_of_sample_trades: number;
  efficiency_ratio: number;
  passed: boolean;
  verdict: string;
}

export interface RobustnessResults {
  parameter: string;
  tested_values: number[];
  metrics_by_value: Record<string, { sharpe: number; profit_factor: number; cagr_pct: number; max_drawdown_pct: number; num_trades: number }>;
  is_stable: boolean;
  is_isolated_spike: boolean;
  stable_range: [number, number];
  sensitivity_verdict: string;
}

export interface OptimizationRecommendation {
  recommendation_id: string;
  strategy_id: string;
  source_strategy_version: number;
  target_strategy_version?: number | null;
  parameter: string;
  current_value: number;
  proposed_value: number;
  reason: string;
  source_observations?: Record<string, any>;
  sample_size: number;
  baseline_metrics: OptimizationMetrics;
  candidate_metrics: OptimizationMetrics;
  walk_forward_metrics: WalkForwardMetrics;
  robustness_results: RobustnessResults;
  confidence: "high" | "medium" | "low" | "insufficient";
  status: "PROPOSED" | "VALIDATING" | "RECOMMENDED" | "REJECTED" | "APPROVED" | "APPLIED";
  rejection_reason?: string | null;
  created_at: string;
  updated_at?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export const getAdaptiveParameters = (strategyId: string, version?: number) =>
  v1<{ strategy_id: string; version: number; adaptive_parameters: Record<string, AdaptiveParameterSpec>; count: number }>(
    `/optimization/parameters/${encodeURIComponent(strategyId)}${version != null ? `?version=${version}` : ""}`
  );

export const generateOptimizationCandidates = (strategyId: string, version?: number, minSampleSize = 10) =>
  v1<{ strategy_id: string; version: number | null; candidates: any[]; count: number }>(
    `/optimization/candidates/${encodeURIComponent(strategyId)}`,
    {
      method: "POST",
      body: JSON.stringify({ version, min_sample_size: minSampleSize }),
    }
  );

export const runOptimizationCycle = (strategyId: string, version?: number, minSampleSize = 10) =>
  v1<{ strategy_id: string; recommendations: OptimizationRecommendation[]; count: number }>(
    `/optimization/run/${encodeURIComponent(strategyId)}`,
    {
      method: "POST",
      body: JSON.stringify({ version, min_sample_size: minSampleSize }),
    }
  );

export const listOptimizationRecommendations = (strategyId: string, status?: string) =>
  v1<{ strategy_id: string; recommendations: OptimizationRecommendation[]; count: number }>(
    `/optimization/recommendations/${encodeURIComponent(strategyId)}${status ? `?status=${encodeURIComponent(status)}` : ""}`
  );

export const getOptimizationRecommendation = (recommendationId: string) =>
  v1<OptimizationRecommendation>(`/optimization/recommendation/${encodeURIComponent(recommendationId)}`);

export const approveOptimizationRecommendation = (recommendationId: string) =>
  v1<OptimizationRecommendation>(`/optimization/recommendation/${encodeURIComponent(recommendationId)}/approve`, {
    method: "POST",
  });

export const rejectOptimizationRecommendation = (recommendationId: string, reason?: string) =>
  v1<OptimizationRecommendation>(`/optimization/recommendation/${encodeURIComponent(recommendationId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const applyOptimizationRecommendation = (recommendationId: string) =>
  v1<{ recommendation: OptimizationRecommendation; new_version: { version: number; strategy_id: string } }>(
    `/optimization/recommendation/${encodeURIComponent(recommendationId)}/apply`,
    {
      method: "POST",
    }
  );

export const getLatestLearningCycle = () =>
  v1<{ latest_cycle: DailyLearningCycleReport | null }>("/learning/cycle/latest");

export const getLearningCycleHistory = (limit = 10) =>
  v1<{ history: DailyLearningCycleReport[]; count: number }>(`/learning/cycle/history?limit=${limit}`);

export const triggerDailyLearningCycle = (params?: { strategy_id?: string; min_sample_size?: number }) =>
  v1<DailyLearningCycleReport>("/optimization/cycle/run", {
    method: "POST",
    body: JSON.stringify(params ?? {}),
  });

export interface MetricComparisonRow {
  metric: string;
  label: string;
  unit: string;
  current: number | null;
  candidate: number | null;
  difference: number | null;
  higher_is_better: boolean;
}

export interface CurvePoint {
  ts: string;
  value: number;
}

export interface DistributionBucket {
  label: string;
  count: number;
}

export interface RegimePerformanceItem {
  regime: string;
  trades: number;
  win_rate_pct: number;
  mean_return_pct: number;
}

export interface MonthlyReturnItem {
  year: number;
  month: number;
  return_pct: number;
}

export interface ExperimentResults {
  metrics_table: MetricComparisonRow[];
  baseline_metrics: OptimizationMetrics;
  candidate_metrics: OptimizationMetrics;
  walk_forward_metrics: WalkForwardMetrics;
  robustness_results: RobustnessResults;
  equity_curves: {
    baseline: CurvePoint[];
    candidate: CurvePoint[];
  };
  drawdown_curves: {
    baseline: CurvePoint[];
    candidate: CurvePoint[];
  };
  monthly_returns: {
    baseline: MonthlyReturnItem[];
    candidate: MonthlyReturnItem[];
  };
  return_distributions: {
    baseline: {
      buckets: DistributionBucket[];
      mean: number;
      median: number;
      std: number;
      skew: number;
      kurtosis: number;
    };
    candidate: {
      buckets: DistributionBucket[];
      mean: number;
      median: number;
      std: number;
      skew: number;
      kurtosis: number;
    };
  };
  regime_performance: {
    baseline: RegimePerformanceItem[];
    candidate: RegimePerformanceItem[];
  };
}

export interface ExperimentExplanation {
  what_changed: string;
  why_proposed: string;
  what_experiment_found: string;
}

export interface StrategyExperiment {
  experiment_id: string;
  strategy_id: string;
  source_version: number;
  target_version?: number | null;
  recommendation_id?: string | null;
  creator_user_id: string;
  name: string;
  reason: string;
  parameter_changes: Record<string, any>;
  baseline_definition: Record<string, any>;
  candidate_definition: Record<string, any>;
  status: "CREATED" | "RUNNING" | "COMPLETED" | "FAILED" | "REJECTED" | "APPROVED" | "APPLIED";
  rejection_reason?: string | null;
  results?: ExperimentResults | null;
  explanation?: ExperimentExplanation | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
}

export const listExperiments = (strategyId?: string, status?: string) => {
  const q = new URLSearchParams();
  if (strategyId) q.set("strategy_id", strategyId);
  if (status) q.set("status", status);
  const qs = q.toString() ? `?${q.toString()}` : "";
  return v1<{ experiments: StrategyExperiment[]; count: number }>(`/experiments${qs}`);
};

export const getExperiment = (experimentId: string) =>
  v1<StrategyExperiment>(`/experiments/${encodeURIComponent(experimentId)}`);

export const createExperiment = (body: {
  strategy_id: string;
  source_version?: number;
  parameter_changes?: Record<string, any>;
  name?: string;
  reason?: string;
  recommendation_id?: string;
}) =>
  v1<StrategyExperiment>("/experiments", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const runExperiment = (experimentId: string) =>
  v1<StrategyExperiment>(`/experiments/${encodeURIComponent(experimentId)}/run`, {
    method: "POST",
  });

export const approveExperiment = (experimentId: string) =>
  v1<StrategyExperiment>(`/experiments/${encodeURIComponent(experimentId)}/approve`, {
    method: "POST",
  });

export const rejectExperiment = (experimentId: string, reason?: string) =>
  v1<StrategyExperiment>(`/experiments/${encodeURIComponent(experimentId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });

export const applyExperiment = (experimentId: string) =>
  v1<{ experiment: StrategyExperiment; new_version: { version: number; strategy_id: string } }>(
    `/experiments/${encodeURIComponent(experimentId)}/apply`,
    {
      method: "POST",
    }
  );




// ─── Market Intelligence ──────────────────────────────────────────────────────

export interface BenchmarkProvenance {
  symbol: string;
  display_name: string;
  kind: "BENCHMARK_ACTUAL_INDEX" | "BENCHMARK_ETF_PROXY";
  tracking_target: string;
  is_proxy: boolean;
  notes: string;
}

export interface RawMarketMeasurements {
  nifty_trend_pct: number | null;
  breadth_above_ema50_pct: number | null;
  breadth_above_ema20_pct: number | null;
  breadth_above_sma200_pct: number | null;
  advancing_stocks: number;
  declining_stocks: number;
  advance_decline_ratio: number | null;
  atr_pct: number | null;
  volatility_ratio: number | null;
  sector_participation_pct: number | null;
  benchmark_provenance: BenchmarkProvenance;
  regime_model_version: string;
  as_of: string;
}

export interface MarketRegimeClassification {
  regime: "BULLISH_TREND" | "BEARISH_TREND" | "SIDEWAYS" | "HIGH_VOLATILITY" | "LOW_VOLATILITY";
  label: string;
  confidence: number;
  raw_measurements: RawMarketMeasurements;
  regime_model_version: string;
  explanation: string;
  as_of: string;
}

export interface MarketSummary {
  as_of: string;
  nifty_close: number;
  nifty_change_1d_pct: number;
  nifty_trend_pct: number;
  nifty_above_ema20: boolean;
  nifty_above_ema50: boolean;
  nifty_1m_return_pct: number;
  benchmark_provenance: BenchmarkProvenance;
  regime: MarketRegimeClassification;
  raw_measurements: RawMarketMeasurements;
  total_stocks_analyzed: number;
  advancing_stocks: number;
  declining_stocks: number;
  unchanged_stocks: number;
  advance_decline_ratio: number;
  breadth_above_ema20_pct: number;
  breadth_above_ema50_pct: number;
  breadth_above_sma200_pct: number;
  highs_52w_count: number;
  lows_52w_count: number;
  market_volatility_atr_pct: number;
  volatility_ratio: number;
  market_trend_strength: number;
  sector_participation_pct: number;
  strongest_sectors: string[];
  weakest_sectors: string[];
  top_breakouts: Record<string, unknown>[];
  top_relative_strength_stocks: Record<string, unknown>[];
  universe_provenance: string;
  survivorship_safeguard: string;
  /** ~6 months of the benchmark with its 50/200-day averages, for the chart. */
  benchmark_series?: { d: string; c: number; s50: number | null; s200: number | null }[];
  /** Share of stocks above their 50-day average, per day, last ~30 sessions. */
  breadth_history?: { d: string; pct: number }[];
  /** Latest trading day the stock data covers; can lag the benchmark's. */
  stocks_as_of?: string | null;
  nifty_52w_high?: number | null;
  nifty_52w_low?: number | null;
  sectors_turning_up?: string[];
  sectors_fading?: string[];
}

// ─── Insights: the day's read ────────────────────────────────────────────────

export interface InsightIdea {
  symbol: string;
  setup: "strong_rs" | "oversold" | "resting_leader" | string;
  setup_label: string;
  evidence: string;
  risk: string;
  market_note: string;
  price: number;
  change_1d_pct: number;
  vs_nifty_20d_pct: number;
  move_20d_pct: number;
  from_high_pct: number;
  sector: string | null;
  stop_pct: number;
  stop_price: number;
  on_watchlist: boolean;
  is_new: boolean;
}

export interface HoldingFlag {
  kind: "protect" | "slipped" | "quiet" | "lagging";
  title: string;
  detail: string;
  level: number | null;
  note: string;
}

export interface HoldingRow {
  symbol: string;
  qty: number | null;
  avg_price: number | null;
  last: number | null;
  pnl_pct: number | null;
  as_of: string | null;
  flags: HoldingFlag[];
}

export interface InsightsDigest {
  as_of: string;
  data_as_of: string | null;
  stale: boolean;
  stale_days: number | null;
  market: {
    regime: string;
    word: string;
    tone: "good" | "bad" | "warn" | "flat" | string;
    headline: string;
    context: string;
    breadth_pct: number;
    nifty_close: number | null;
    nifty_change_1d_pct: number | null;
    vs_50d_pct: number | null;
    vs_200d_pct: number | null;
  };
  changes: string[];
  ideas: InsightIdea[];
  holdings: { source: "live" | "snapshot" | "none"; as_of: string | null; items: HoldingRow[] };
  disclaimer: string;
  last_sent: string | null;
}

export interface InsightsSettings {
  enabled: boolean;
  market_changes: boolean;
  buy_ideas: boolean;
  holdings_watch: boolean;
  max_buy_ideas: number;
  send_after: string;
}

export const getInsights = (fresh = false) =>
  v1<InsightsDigest>(`/insights/today${fresh ? "?fresh=true" : ""}`);
export const sendInsights = () =>
  v1<{ sent: boolean; channel: string | null }>("/insights/send", { method: "POST" });
export const getInsightSettings = () => v1<InsightsSettings>("/insights/settings");
export const saveInsightSettings = (body: InsightsSettings) =>
  v1<InsightsSettings>("/insights/settings", { method: "PUT", body: JSON.stringify(body) });

export interface SectorMetrics {
  sector: string;
  stock_count: number;
  return_1d_pct: number;
  return_1w_pct: number;
  return_1m_pct: number;
  relative_strength_1d: number;
  relative_strength_1m: number;
  advancing_count: number;
  declining_count: number;
  advance_decline_ratio: number;
  volume_multiple: number;
  breakout_count: number;
  above_ema20_count: number;
  above_ema20_pct: number;
  above_ema50_count: number;
  above_ema50_pct: number;
  trend: "BULLISH" | "BEARISH" | "SIDEWAYS";
  top_stocks: Record<string, unknown>[];
}

export interface StockContext {
  symbol: string;
  close: number;
  change_1d_pct: number;
  trend_pct: number;
  relative_strength_nifty_20d: number;
  relative_volume: number;
  atr_pct: number;
  from_52w_high_pct: number;
  from_52w_low_pct: number;
  gap_pct: number;
  above_ema20: boolean;
  above_ema50: boolean;
  is_breakout: boolean;
  sector: string | null;
  sector_relative_strength_1m: number | null;
  market_breadth_pct: number | null;
  benchmark_provenance: BenchmarkProvenance;
  regime_model_version: string;
  as_of: string;
}

export interface MarketContextBucket {
  label: string;
  n: number;
  n_forward: number;
  n_in_sample: number;
  suppressed: boolean;
  mean: number | null;
  median: number | null;
  ci_low: number | null;
  ci_high: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  evidence_note: "forward" | "insufficient_forward_observations" | "in_sample_only";
}

export interface MarketContextPerformance {
  strategy: string;
  condition_axis: string;
  condition_value: string | null;
  metric: string;
  metric_note: string | null;
  min_sample_floor: number;
  total_rows_scanned: number;
  rows_with_value: number;
  baseline: { n: number; mean: number | null; median: number | null; win_rate: number | null };
  buckets: MarketContextBucket[];
  all_buckets: MarketContextBucket[];
  requested: MarketContextBucket | null;
  caveats: string[];
}

export const getMarketIntelSummary = (forceRefresh = false) =>
  v1<{ ok: boolean; data: MarketSummary }>(
    `/market-intel/summary${forceRefresh ? "?force_refresh=true" : ""}`
  );

export type SectorSortKey =
  | "relative_strength_1d"
  | "relative_strength_1m"
  | "return_1d_pct"
  | "return_1w_pct"
  | "return_1m_pct"
  | "volume_multiple"
  | "advance_decline_ratio"
  | "above_ema50_pct"
  | "breakout_count";

export const getMarketIntelSectors = (
  sortBy: SectorSortKey = "relative_strength_1d",
  descending = true,
  forceRefresh = false
) => {
  const params = new URLSearchParams({
    sort_by: sortBy,
    descending: String(descending),
    ...(forceRefresh ? { force_refresh: "true" } : {}),
  });
  return v1<{ ok: boolean; sort_by: string; count: number; sectors: SectorMetrics[] }>(
    `/market-intel/sectors?${params}`
  );
};


// ---------------------------------------------------------------------------
// Portfolio Control Center & Risk Policy
// ---------------------------------------------------------------------------

export interface PortfolioPolicyConfig {
  max_total_exposure: number | null;
  max_daily_loss: number | null;
  max_capital_per_strategy: number | null;
  max_open_positions: number | null;
  max_stock_exposure: number | null;
  max_sector_exposure_pct: number | null;
  max_correlated_exposure_pct: number | null;
  conflict_mode: "reject" | "priority" | "net" | string;
  strategy_priorities: Record<string, number>;
  correlation_groups: string[][];
  warn_at_pct_of_limit: number;
}

export interface PortfolioControlCenterData {
  generated_at: string;
  policy_configured: boolean;
  policy: PortfolioPolicyConfig;
  capital: {
    total: number;
    deployed: number;
    used: number;
    available: number;
    cash: number;
    cash_utilization: number | null;
  };
  exposure: {
    gross: number;
    long: number;
    short: number;
    net: number;
  };
  today: {
    realized: number;
    unrealized: number;
    total: number;
  };
  drawdown: {
    value: number | null;
    method: string;
  };
  open_positions: Array<{
    symbol: string;
    qty: number;
    value: number;
    sector: string;
    deployments: string[];
    pct_of_capital: number | null;
  }>;
  sectors: Record<string, { value: number; pct: number | null }>;
  strategies: Array<{
    strategy_id: string;
    strategy_name: string;
    allocated: number;
    used: number;
    available: number;
    exposure: number;
    realized: number;
    unrealized: number;
    total_pnl: number;
    return_pct: number | null;
    drawdown: number | null;
    trades_closed: number;
    trades_open: number;
    contribution: number | null;
    deployment_id: string;
    deployment_status: string;
  }>;
  concentrations: {
    stocks: Array<{ name: string; value: number; pct: number | null }>;
    sectors: Array<{ name: string; value: number; pct: number | null }>;
    strategies: Array<{ name: string; value: number; pct: number | null }>;
    warnings: string[];
  };
  conflicts: Array<{
    symbol: string;
    net_qty: number;
    long: Array<{ deployment_id: string; strategy_id: string; qty: number }>;
    short: Array<{ deployment_id: string; strategy_id: string; qty: number }>;
  }>;
  limits: Array<{
    key: string;
    label: string;
    configured: any;
    current: any;
    unit: string;
    utilization: number | null;
  }>;
  unpriced_symbols: string[];
  limitations: string[];
  advisory_only: boolean;
  applies_changes: boolean;
}

export const getPortfolioControlCenter = () =>
  v1<PortfolioControlCenterData>("/portfolio/control-center");

export const getPortfolioPolicy = () =>
  v1<{ configured: boolean; policy: PortfolioPolicyConfig }>("/portfolio/policy");

export const updatePortfolioPolicy = (payload: Partial<PortfolioPolicyConfig> & { reason?: string }) =>
  v1<{ status: string; policy: PortfolioPolicyConfig }>("/portfolio/policy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export type StockSortKey =
  | "relative_strength_nifty_20d"
  | "relative_volume"
  | "from_52w_high_pct"
  | "atr_pct"
  | "trend_pct"
  | "change_1d_pct";

export const getMarketIntelStocks = (opts?: {
  sortBy?: StockSortKey;
  descending?: boolean;
  breakoutsOnly?: boolean;
  aboveEma50Only?: boolean;
  sector?: string;
  limit?: number;
  forceRefresh?: boolean;
}) => {
  const {
    sortBy = "relative_strength_nifty_20d",
    descending = true,
    breakoutsOnly = false,
    aboveEma50Only = false,
    sector,
    limit = 50,
    forceRefresh = false,
  } = opts ?? {};
  const params = new URLSearchParams({
    sort_by: sortBy,
    descending: String(descending),
    breakouts_only: String(breakoutsOnly),
    above_ema50_only: String(aboveEma50Only),
    limit: String(limit),
    ...(sector ? { sector } : {}),
    ...(forceRefresh ? { force_refresh: "true" } : {}),
  });
  return v1<{ ok: boolean; count: number; stocks: StockContext[] }>(
    `/market-intel/stocks?${params}`
  );
};

export const getMarketIntelStock = (symbol: string, forceRefresh = false) =>
  v1<{ ok: boolean; data: StockContext }>(
    `/market-intel/stock/${encodeURIComponent(symbol)}${forceRefresh ? "?force_refresh=true" : ""}`
  );

export type MarketContextAxis =
  | "market_regime"
  | "volatility_regime"
  | "nifty_trend_bucket"
  | "breadth_bucket"
  | "sector_strength_bucket"
  | "stock_rs_bucket";

export const getStrategyMarketContext = (opts: {
  conditionAxis: MarketContextAxis;
  strategy?: string;
  conditionValue?: string;
  grades?: string;
  metric?: string;
  minSample?: number;
}) => {
  const params = new URLSearchParams({
    condition_axis: opts.conditionAxis,
    ...(opts.strategy ? { strategy: opts.strategy } : {}),
    ...(opts.conditionValue ? { condition_value: opts.conditionValue } : {}),
    ...(opts.grades ? { grades: opts.grades } : {}),
    ...(opts.metric ? { metric: opts.metric } : {}),
    ...(opts.minSample !== undefined ? { min_sample: String(opts.minSample) } : {}),
  });
  return v1<{ ok: boolean } & MarketContextPerformance>(
    `/market-intel/strategy-context?${params}`
  );
};

// ─── Signal Context Engine ────────────────────────────────────────────────────
// Read-only projection of the contexts the engine recorded against signals. The
// whole surface is descriptive: a context score is the share of predefined
// conditions that were met at signal time, never a probability of profit.

export interface SignalContextCriterionSpec {
  key: string;
  label: string;
  weight: number;
  description: string;
}

export interface SignalContextModel {
  version: string;
  description: string;
  criteria: SignalContextCriterionSpec[];
  strong_min_score: number;
  neutral_min_score: number;
  bullish_trend_min_pct: number;
  bearish_trend_max_pct: number;
  breadth_strong_min_pct: number;
  volatility_elevated_ratio: number;
  sector_strong_rs_pct: number;
  stock_strong_rs_pct: number;
  rvol_confirmation_min: number;
}

export interface SignalScoreCriterion {
  key: string;
  label: string;
  weight: number;
  met: boolean;
  points_awarded: number;
  value: string | null;
  reason: string | null;
}

export interface SignalMarketSnapshot {
  regime: string | null;
  nifty_trend_pct: number | null;
  breadth_above_ema50_pct: number | null;
  breadth_above_ema20_pct: number | null;
  volatility_atr_pct: number | null;
  volatility_ratio: number | null;
  advance_decline_ratio: number | null;
  benchmark_symbol: string;
  benchmark_is_proxy: boolean;
  regime_model_version: string;
  as_of: string;
}

export interface SignalSectorSnapshot {
  sector: string | null;
  return_1d_pct: number | null;
  return_1m_pct: number | null;
  relative_strength_1m: number | null;
  breadth_above_ema50_pct: number | null;
  volume_multiple: number | null;
  trend: string | null;
  as_of: string;
}

export interface SignalStockSnapshot {
  relative_strength_nifty_20d: number | null;
  relative_volume: number | null;
  atr_pct: number | null;
  trend_pct: number | null;
  above_sma50: boolean | null;
  from_52w_high_pct: number | null;
  from_52w_low_pct: number | null;
  gap_pct: number | null;
  close: number | null;
  as_of: string;
}

export type SignalContextClass =
  | "STRONG_CONTEXT"
  | "NEUTRAL_CONTEXT"
  | "WEAK_CONTEXT"
  | "INSUFFICIENT_DATA";

export type SignalSource = "LIVE" | "PAPER" | "BACKTEST";

export interface SignalContextRecord {
  id: number;
  user_id: string;
  signal_id: string;
  strategy_id: string | null;
  strategy_version: number | null;
  symbol: string;
  action: string;
  signal_source: SignalSource;
  signal_ts: string;
  context_model_version: string;
  context_class: SignalContextClass;
  context_score: number;
  max_possible_score: number;
  has_insufficient_data: boolean;
  run_id: string | null;
  trade_id: number | null;
  order_id: number | null;
  market_context: SignalMarketSnapshot;
  sector_context: SignalSectorSnapshot | null;
  stock_context: SignalStockSnapshot;
  score_breakdown: SignalScoreCriterion[];
  missing_fields: string[];
  benchmark_provenance: Record<string, unknown>;
  created_at: string;
}

export interface SignalContextList {
  items: SignalContextRecord[];
  total: number;
  limit: number;
  offset: number;
  model_version: string;
}

export type ContextDimension =
  | "context_class"
  | "score_band"
  | "regime"
  | "sector_rs"
  | "stock_rs"
  | "breadth"
  | "volatility";

export type ContextEvidenceNote =
  | "forward"
  | "insufficient_forward_observations"
  | "in_sample_only"
  | "no_resolvable_outcomes";

export interface SignalContextBucket {
  key: string;
  n: number;
  n_forward: number;
  n_in_sample: number;
  mean_return: number | null;
  median_return: number | null;
  win_rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
  profit_factor: number | null;
  suppressed: boolean;
  evidence_note: ContextEvidenceNote;
}

export interface SignalContextAnalytics {
  dimension: ContextDimension;
  model_version: string;
  min_forward_n: number;
  generated_at: string;
  buckets: SignalContextBucket[];
}

export const getSignalContextModel = () =>
  v1<{ ok: boolean; data: SignalContextModel }>("/signal-context/model");

export const getSignalContexts = (opts?: {
  limit?: number;
  offset?: number;
  source?: SignalSource;
  strategyId?: string;
  contextClass?: SignalContextClass;
}) => {
  const { limit = 50, offset = 0, source, strategyId, contextClass } = opts ?? {};
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    ...(source ? { source } : {}),
    ...(strategyId ? { strategy_id: strategyId } : {}),
    ...(contextClass ? { context_class: contextClass } : {}),
  });
  return v1<{ ok: boolean; data: SignalContextList }>(
    `/signal-context/signals?${params}`
  );
};

export const getSignalContext = (signalId: string) =>
  v1<{ ok: boolean; data: SignalContextRecord }>(
    `/signal-context/signals/${encodeURIComponent(signalId)}`
  );

export const getSignalContextAnalytics = (opts: {
  dimension: ContextDimension;
  strategyId?: string;
  source?: SignalSource;
}) => {
  const params = new URLSearchParams({
    dimension: opts.dimension,
    ...(opts.strategyId ? { strategy_id: opts.strategyId } : {}),
    ...(opts.source ? { source: opts.source } : {}),
  });
  return v1<{ ok: boolean; data: SignalContextAnalytics }>(
    `/signal-context/analytics?${params}`
  );
};

// ─── Context effectiveness ────────────────────────────────────────────────
// Whether the score and its features accompany different forward outcomes.
// Every bucket is measured against its complement with a Bonferroni correction
// across the declared axes; in-sample (backtest) buckets are descriptive only
// and never combined with forward evidence.

export type EffectivenessMetric = "return_pct" | "net_pnl";

export interface EffectivenessStats {
  n: number;
  wins: number;
  win_rate: number | null;
  win_rate_ci: [number, number] | null;
  mean: number | null;
  mean_ci: [number, number] | null;
  median: number | null;
  trimmed_mean: number | null;
  stdev: number | null;
  without_best: number | null;
  profit_factor: number | null;
  payoff_ratio: number | null;
  total: number | null;
  best: number | null;
  worst: number | null;
  small_sample: boolean;
}

export interface EffectivenessBucket {
  label: string;
  n: number;
  sample_size: number;
  stats: EffectivenessStats;
  win_rate: number | null;
  win_rate_ci: [number, number] | null;
  mean: number | null;
  mean_ci: [number, number] | null;
  median: number | null;
  profit_factor: number | null;
  is_meaningful: boolean;
  sample_adequacy: string;
  /** Bucket mean minus complement mean, in the metric's own unit. */
  lift: number | null;
  p_value: number | null;
  p_adjusted: number | null;
  significance:
    | "strong"
    | "moderate"
    | "weak"
    | "not_significant"
    | "not_tested"
    | "insufficient_sample"
    | "in_sample_not_a_claim";
  comparisons: number;
  suppressed: boolean;
  note: string | null;
  max_drawdown: number | null;
  n_forward?: number;
  evidence_class_forward?: string;
  /** Present on in-sample buckets only; forward buckets carry the class above. */
  evidence_class?: string;
}

export interface EffectivenessAxis {
  axis: string;
  label: string;
  max_values?: number | null;
  coverage: { with_value: number; scanned: number };
  excluded?: { axis_missing: number; outcome_missing: number };
  buckets: EffectivenessBucket[];
}

export interface EffectivenessScoreVerdict {
  bands: string[];
  means: Record<string, number | null>;
  monotonic_high_is_better: boolean;
  high_band: EffectivenessBucket | null;
  low_band: EffectivenessBucket | null;
  may_claim: boolean;
  statement: string;
}

export interface SignalContextEffectiveness {
  metric: string;
  min_sample: number;
  bonferroni_comparisons: number;
  model_note: string;
  forward_n: number;
  forward_with_metric: number;
  in_sample_n: number;
  score_verdict: EffectivenessScoreVerdict;
  axes: EffectivenessAxis[];
  in_sample_axes: EffectivenessAxis[];
  caveats: string[];
  generated_at: string;
  model_version: string;
}

export const getSignalContextEffectiveness = (opts?: {
  strategyId?: string;
  source?: SignalSource;
  metric?: EffectivenessMetric;
  minSample?: number;
}) => {
  const params = new URLSearchParams({
    ...(opts?.strategyId ? { strategy_id: opts.strategyId } : {}),
    ...(opts?.source ? { source: opts.source } : {}),
    ...(opts?.metric ? { metric: opts.metric } : {}),
    ...(opts?.minSample != null ? { min_sample: String(opts.minSample) } : {}),
  });
  return v1<{ ok: boolean; data: SignalContextEffectiveness }>(
    `/signal-context/effectiveness?${params}`
  );
};

// ─── Post-trade analytics & trade attribution ────────────────────────────────
//
// READ-ONLY, and there is deliberately no POST/PATCH/DELETE below. The backend
// exposes attribution as a measurement: a trade's excursion, its slippage and
// the reasons it was classified a certain way. Nothing here changes a strategy,
// promotes a version or places an order.
//
// Two vocabulary points the types encode rather than comment on:
//
// * `evidence_grade` is `"forward"` or `"in_sample"`. `"forward"` means the
//   trade was recorded before its outcome was known — a deployment actually
//   running. `"in_sample"` means it was measured on the history the rule was
//   chosen on. A backtest is in-sample by definition. Never pool the two.
// * `evidence_class` says *why* the grade is what it is: BACKTEST, IN_SAMPLE,
//   PAPER_FORWARD or LIVE_FORWARD. An absent class grades in-sample.

export type EvidenceGrade = "forward" | "in_sample";
export type EvidenceClass = "BACKTEST" | "IN_SAMPLE" | "PAPER_FORWARD" | "LIVE_FORWARD";
export type TradeSource = "BACKTEST" | "PAPER" | "LIVE";

/** The filters every analytics read accepts. */
export interface AnalyticsFilters {
  strategyId?: string;
  strategyVersion?: number;
  symbol?: string;
  source?: TradeSource;
  provenance?: EvidenceGrade;
  marketRegime?: string;
  sector?: string;
  start?: string;
  end?: string;
}

function analyticsParams(f: AnalyticsFilters = {}): URLSearchParams {
  const params = new URLSearchParams();
  if (f.strategyId) params.set("strategy_id", f.strategyId);
  if (f.strategyVersion != null) params.set("strategy_version", String(f.strategyVersion));
  if (f.symbol) params.set("symbol", f.symbol);
  if (f.source) params.set("source", f.source);
  if (f.provenance) params.set("provenance", f.provenance);
  if (f.marketRegime) params.set("market_regime", f.marketRegime);
  if (f.sector) params.set("sector", f.sector);
  if (f.start) params.set("start", f.start);
  if (f.end) params.set("end", f.end);
  return params;
}

/**
 * How many rows of each grade went into a figure.
 *
 * Always carried beside an aggregate so a number computed over a mixed book is
 * never read as a forward finding. `class_unrecorded` counts rows with no class
 * rather than guessing one — a row whose grade is `forward` and whose class
 * defaulted to `IN_SAMPLE` would print two contradicting facts.
 */
export interface EvidenceCounts {
  n: number;
  forward_n: number;
  in_sample_n: number;
  by_grade: Record<string, number>;
  by_class: Record<string, number>;
  class_unrecorded?: number;
  all_forward: boolean;
}

export interface AnalyticsOverview {
  n_trades: number;
  net_pnl: number | null;
  gross_pnl: number | null;
  net_return_pct_mean: number | null;
  net_return_pct_median: number | null;
  wins: number;
  losses: number;
  flat: number;
  win_rate: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  expectancy_return_pct: number | null;
  average_win: number | null;
  average_loss: number | null;
  largest_win: number | null;
  largest_loss: number | null;
  total_costs: number | null;
  costs_per_trade: number | null;
  total_slippage_bps_mean: number | null;
  total_slippage_bps_sum: number | null;
  median_holding_sec: number | null;
  mean_holding_sec: number | null;
  counts: EvidenceCounts;
  // The counterfactual that belongs beside the headline: one trade carrying a
  // small book is the most common way a sample lies about itself.
  best_trade_share_of_gross_profit: number | null;
  net_without_best: number | null;
}

/** One bucket of a cut, with its own counts and suppression flag. */
export interface AnalyticsBucket {
  key: string;
  n: number;
  net_pnl: number | null;
  net_return_pct_mean: number | null;
  win_rate: number | null;
  expectancy: number | null;
  profit_factor: number | null;
  mean_holding_sec: number | null;
  total_costs: number | null;
  suppressed: boolean;
  counts: EvidenceCounts;
}

/** The six attributable branches. Lists of buckets, never a P&L decomposition. */
export interface AnalyticsBranches {
  signal: AnalyticsBucket[];
  context: AnalyticsBucket[];
  sizing: AnalyticsBucket[];
  risk: AnalyticsBucket[];
  execution: AnalyticsBucket[];
  exit: AnalyticsBucket[];
  note?: string;
}

export interface AttributionCoverage {
  attributed: number;
  closed_trades: number | null;
  complete: boolean | null;
  unattributed?: number;
}

export interface AnalyticsSummary extends AnalyticsOverview {
  attribution_coverage?: AttributionCoverage;
}

/** One row of the attributed-trade list. */
export interface AttributedTradeRow {
  trade_id: string;
  symbol: string | null;
  side: string | null;
  source: TradeSource | null;
  evidence_grade: EvidenceGrade | null;
  evidence_class: EvidenceClass | null;
  simulated: boolean | null;
  holding_sec: number | null;
  exit_reason: string | null;
  net_pnl: number | null;
  net_return_pct: number | null;
  gross_pnl: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  mfe_over_risk: number | null;
  realized_over_risk: number | null;
  capture_efficiency_pct: number | null;
  entry_slippage_bps: number | null;
  exit_slippage_bps: number | null;
  total_slippage_bps: number | null;
  entry_ts?: string | null;
  exit_ts?: string | null;
  reason_codes?: string[];
  entry_quality?: string | null;
  execution_quality?: string | null;
  context_class?: string | null;
  market_regime?: string | null;
  sector?: string | null;
  sizing_method?: string | null;
}

export interface AttributedTradeList {
  items: AttributedTradeRow[];
  total: number;
  limit: number;
  offset: number;
  filters: Record<string, unknown>;
}

/**
 * The full attribution tree for one trade.
 *
 * `attribution` holds the nine named branches — signal, context, entry, sizing,
 * risk, execution, exit, outcome — plus the excursion set. The stored row is
 * returned as-is so the panel renders what was recorded rather than a re-derived
 * version.
 */
export interface TradeAttributionDetail {
  trade_id: string;
  user_id?: string;
  symbol: string;
  side: string;
  source: TradeSource;
  evidence_grade: EvidenceGrade | null;
  evidence_class: EvidenceClass | null;
  simulated: boolean;
  attribution: {
    signal?: Record<string, unknown>;
    context?: Record<string, unknown>;
    entry?: Record<string, unknown>;
    sizing?: Record<string, unknown>;
    risk?: Record<string, unknown>;
    execution?: Record<string, unknown>;
    exit?: Record<string, unknown>;
    outcome?: Record<string, unknown>;
    excursions?: Record<string, unknown>;
    reason_codes?: string[];
    reason_detail?: Record<string, unknown>;
    missing_fields?: Record<string, string>;
    thresholds?: Record<string, number>;
  };
  reason_codes?: string[];
  missing_fields?: Record<string, string>;
  input_fingerprint?: string;
  computed_at?: string;
  updated_at?: string;
}

export interface MaeMfePoint {
  trade_id: string | null;
  symbol: string | null;
  net_pnl: number | null;
  return_pct: number | null;
  mfe_pct: number | null;
  mae_pct: number | null;
  mfe_over_risk: number | null;
  mae_over_risk: number | null;
  realized_over_risk: number | null;
  capped_by_stop: boolean | null;
  evidence_grade: EvidenceGrade | null;
}

export interface MaeMfeDistribution {
  mean: number | null;
  median: number | null;
  p25?: number | null;
  p75?: number | null;
  measured: number;
  buckets: { key: string; n: number }[];
}

export interface MaeMfeAnalytics {
  n: number;
  counts: EvidenceCounts;
  min_sample: number;
  mfe_over_risk: MaeMfeDistribution;
  mae_over_risk: MaeMfeDistribution;
  realized_over_risk: { mean: number | null; median: number | null; measured: number };
  points?: MaeMfePoint[];
  methodology: Record<string, string>;
}

export interface ExecutionDistribution {
  delays?: Record<string, number | null>;
  slippage?: {
    mean_bps?: number | null;
    median_bps?: number | null;
    measured?: number;
    unmeasurable?: number;
    total_amount?: number | null;
  };
  costs?: Record<string, number | null>;
  partial_fills?: Record<string, number>;
  by_symbol?: AnalyticsBucket[];
  by_time_of_day?: AnalyticsBucket[];
  note?: string;
}

export interface StrategyComparison {
  key: string;
  n: number;
  summary: AnalyticsOverview;
  branches: AnalyticsBranches;
}

// ─── reads ────────────────────────────────────────────────────────────────────

export const getAnalyticsSummary = (f?: AnalyticsFilters) =>
  v1<{ ok: boolean; data: AnalyticsSummary }>(
    `/analytics/summary?${analyticsParams(f)}`
  );

export const getAttributedTrades = (
  f?: AnalyticsFilters & { limit?: number; offset?: number; reasonCode?: string }
) => {
  const params = analyticsParams(f);
  if (f?.limit != null) params.set("limit", String(f.limit));
  if (f?.offset != null) params.set("offset", String(f.offset));
  if (f?.reasonCode) params.set("reason_code", f.reasonCode);
  return v1<{ ok: boolean; data: AttributedTradeList }>(`/analytics/trades?${params}`);
};

export const getTradeAttribution = (tradeId: string) =>
  v1<{ ok: boolean; data: TradeAttributionDetail }>(
    `/analytics/trades/${encodeURIComponent(tradeId)}/attribution`
  );

export const getAnalyticsByStrategy = (f?: AnalyticsFilters) =>
  v1<{ ok: boolean; data: { strategies: StrategyComparison[]; note: string } }>(
    `/analytics/performance/by-strategy?${analyticsParams(f)}`
  );

export const getAnalyticsByRegime = (f?: AnalyticsFilters) =>
  v1<{
    ok: boolean;
    data: { buckets: AnalyticsBucket[]; dimension: string };
  }>(`/analytics/performance/by-regime?${analyticsParams(f)}`);

export const getAnalyticsByContext = (f?: AnalyticsFilters) =>
  v1<{
    ok: boolean;
    data: {
      by_class: AnalyticsBucket[];
      by_score_band: AnalyticsBucket[];
      by_score_band_note: string;
    };
  }>(`/analytics/performance/by-context?${analyticsParams(f)}`);

export const getAnalyticsBySizing = (f?: AnalyticsFilters) =>
  v1<{
    ok: boolean;
    data: { by_method: AnalyticsBucket[]; by_cap: AnalyticsBucket[] };
  }>(`/analytics/performance/by-sizing?${analyticsParams(f)}`);

export const getAnalyticsByExecution = (f?: AnalyticsFilters) =>
  v1<{
    ok: boolean;
    data: {
      by_quality: AnalyticsBucket[];
      by_entry_quality: AnalyticsBucket[];
      by_slippage_bucket: AnalyticsBucket[];
      distribution: ExecutionDistribution;
    };
  }>(`/analytics/performance/by-execution?${analyticsParams(f)}`);

export const getAnalyticsMaeMfe = (
  f?: AnalyticsFilters & { includePoints?: boolean }
) => {
  const params = analyticsParams(f);
  if (f?.includePoints === false) params.set("include_points", "false");
  return v1<{ ok: boolean; data: MaeMfeAnalytics }>(`/analytics/mae-mfe?${params}`);
};

