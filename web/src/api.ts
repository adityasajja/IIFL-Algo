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

export const getRiskStatus = () => req<Record<string, unknown>>("/risk/status");

export const setKillSwitch = (engaged: boolean) =>
  req<{ kill_switch: boolean }>(`/risk/kill-switch?engaged=${engaged}`, {
    method: "POST",
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

export interface StrategyInfo {
  name: string;
  tunable: string[];
  warmup_bars: number | null;
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
  req<{ strategies: StrategyInfo[] }>("/strategies");

export const runResearch = (body: ResearchRequest) =>
  req<ResearchResponse>("/research", { method: "POST", body: JSON.stringify(body) });

// ---------------------------------------------------------------------------
// Signals — the live buy/sell rules
// ---------------------------------------------------------------------------

export interface SignalConfig {
  entries: Record<string, number | null>;
  exits: Record<string, number | null>;
  universe: string[];
  exchange: string;
}

export interface SignalRow {
  symbol: string;
  action: "BUY" | "SELL";
  rule: string;
  reason: string;
  price: number;
  detail: Record<string, number | string | null>;
  validated: boolean;
  ts: string;
}

export interface SignalScanResponse {
  buys: SignalRow[];
  sells: SignalRow[];
  errors: string[];
}

export const getSignalsConfig = () =>
  req<{ config: SignalConfig; search_grid: Record<string, number[]> }>("/signals/config");

export const saveSignalsConfig = (body: {
  entries: Record<string, number | null>;
  exits: Record<string, number | null>;
  universe: string[];
  exchange: string;
}) => req<SignalConfig>("/signals/config", { method: "PUT", body: JSON.stringify(body) });

export const runSignalsScan = (body: {
  symbols?: string[];
  include_holdings?: boolean;
  include_entries?: boolean;
}) => req<SignalScanResponse>("/signals/scan", { method: "POST", body: JSON.stringify(body) });

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
