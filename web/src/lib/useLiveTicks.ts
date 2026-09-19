import { useEffect, useRef, useState, useCallback } from "react";
import { API_URL } from "../api";

export interface DepthLevel {
  price: number;
  quantity: number;
  orders: number;
  type: number;
}

export interface LiveTick {
  symbol: string;
  exchange: string;
  ltp: number;
  last_qty: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  close: number;
  chg: number;
  chg_pct: number;
  best_bid?: number | null;
  best_ask?: number | null;
  ts: string;
  epoch?: number; // Unix timestamp for precise candle alignment
  flash?: "up" | "down" | null;
  depth?: DepthLevel[] | null;
}

interface WsMessage {
  type: "status" | "tick";
  bridge_connected?: boolean;
  server_time?: string;
  data?: LiveTick;
}

// Global state shared across hook invocations
const activeSubscriptions = new Set<string>();
const tickListeners = new Set<(tick: LiveTick) => void>();
const statusListeners = new Set<(connected: boolean, bridgeActive: boolean) => void>();
let globalTicks: Record<string, LiveTick> = {};
let wsInstance: WebSocket | null = null;
let reconnectTimer: any = null;
let isConnected = false;
let isBridgeActive = false;

function getWsUrl(): string {
  if (API_URL.startsWith("http")) {
    const base = API_URL.replace(/^http/, "ws");
    return `${base}/ws/ticks`;
  }
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    return `${proto}//${host}/ws/ticks`;
  }
  return "ws://127.0.0.1:8000/ws/ticks";
}

function sendSubscription(symbols: string[]) {
  if (wsInstance && wsInstance.readyState === WebSocket.OPEN && symbols.length > 0) {
    wsInstance.send(
      JSON.stringify({
        action: "subscribe",
        symbols,
        exchange: "NSEEQ",
      })
    );
  }
}

function initWebSocket() {
  if (wsInstance && (wsInstance.readyState === WebSocket.OPEN || wsInstance.readyState === WebSocket.CONNECTING)) {
    return;
  }

  const url = getWsUrl();
  try {
    const ws = new WebSocket(url);
    wsInstance = ws;

    ws.onopen = () => {
      isConnected = true;
      statusListeners.forEach((fn) => fn(isConnected, isBridgeActive));
      const symList = Array.from(activeSubscriptions);
      if (symList.length > 0) {
        sendSubscription(symList);
      }
    };

    ws.onmessage = (ev) => {
      try {
        const msg: WsMessage = JSON.parse(ev.data);
        if (msg.type === "status") {
          isBridgeActive = Boolean(msg.bridge_connected);
          statusListeners.forEach((fn) => fn(isConnected, isBridgeActive));
        } else if (msg.type === "tick" && msg.data) {
          const raw = msg.data;
          const prev = globalTicks[raw.symbol];
          let flash: "up" | "down" | null = null;
          if (prev && prev.ltp !== raw.ltp) {
            flash = raw.ltp > prev.ltp ? "up" : "down";
          }
          const enriched: LiveTick = { ...raw, flash };
          globalTicks[raw.symbol] = enriched;
          tickListeners.forEach((listener) => listener(enriched));
        }
      } catch {
        // ignore malformed frame
      }
    };

    ws.onclose = () => {
      isConnected = false;
      isBridgeActive = false;
      wsInstance = null;
      statusListeners.forEach((fn) => fn(isConnected, isBridgeActive));
      scheduleReconnect();
    };

    ws.onerror = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.close();
      }
    };
  } catch {
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    initWebSocket();
  }, 2500);
}

/**
 * React hook to access live streaming ticks for a set of symbols.
 * Automatically handles sub/unsub and maintains price flash transitions.
 */
export function useLiveTicks(symbols: string[] = []) {
  const [ticks, setTicks] = useState<Record<string, LiveTick>>(() => {
    const initial: Record<string, LiveTick> = {};
    for (const s of symbols) {
      if (globalTicks[s]) initial[s] = globalTicks[s];
    }
    return initial;
  });
  const [connected, setConnected] = useState<boolean>(isConnected);
  const [bridgeActive, setBridgeActive] = useState<boolean>(isBridgeActive);

  // Normalize symbols
  const syms = useRef<string[]>([]);
  syms.current = symbols.map((s) => s.trim().toUpperCase()).filter(Boolean);

  useEffect(() => {
    initWebSocket();

    // High-frequency tick buffer batched per animation frame (60-120fps)
    const pendingTicks: Record<string, LiveTick> = {};
    let rafId: number | null = null;

    const flushTicks = () => {
      rafId = null;
      if (Object.keys(pendingTicks).length === 0) return;
      setTicks((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const [sym, tick] of Object.entries(pendingTicks)) {
          if (next[sym] !== tick) {
            next[sym] = tick;
            changed = true;
          }
        }
        return changed ? next : prev;
      });
      // Clear pending
      for (const k of Object.keys(pendingTicks)) {
        delete pendingTicks[k];
      }
    };

    const onTick = (tick: LiveTick) => {
      if (syms.current.includes(tick.symbol)) {
        pendingTicks[tick.symbol] = tick;
        if (rafId === null) {
          rafId = requestAnimationFrame(flushTicks);
        }
      }
    };

    const onStatus = (conn: boolean, bridge: boolean) => {
      setConnected(conn);
      setBridgeActive(bridge);
    };

    tickListeners.add(onTick);
    statusListeners.add(onStatus);

    // Register active subscriptions
    const newSyms: string[] = [];
    for (const s of syms.current) {
      if (!activeSubscriptions.has(s)) {
        activeSubscriptions.add(s);
        newSyms.push(s);
      }
    }
    if (newSyms.length > 0) {
      sendSubscription(newSyms);
    }

    return () => {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
      }
      tickListeners.delete(onTick);
      statusListeners.delete(onStatus);
    };
  }, [symbols.join(",")]);

  return {
    ticks,
    connected,
    bridgeActive,
    getTick: useCallback(
      (sym: string) => {
        const key = sym.toUpperCase();
        // Only serve from globalTicks if this hook is actively subscribed to the symbol.
        // Avoids leaking a stale cross-session tick for a symbol we're no longer tracking.
        return ticks[key] ?? (syms.current.includes(key) ? globalTicks[key] : undefined);
      },
      [ticks]
    ),
  };
}
