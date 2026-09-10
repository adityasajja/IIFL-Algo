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
}

export interface BacktestResponse {
  strategy: string;
  metrics: Record<string, number | string | boolean | null>;
  num_fills: number;
  num_trades: number;
  killed: boolean;
  kill_reason: string | null;
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
  min_bars: number;
  universe: string;
  watchlist: string[];
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

export const sendBriefing = () =>
  req<{ sent_on: string; stats: Record<string, number | string> }>("/briefing/send", {
    method: "POST",
  });
