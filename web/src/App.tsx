import { useCallback, useEffect, useState } from "react";
import AlertsPanel from "./AlertsPanel";
import BriefingPanel from "./BriefingPanel";
import ChartsPanel from "./ChartsPanel";
import ScannerPanel from "./ScannerPanel";
import {
  API_URL,
  getHealth,
  getPositions,
  getRiskStatus,
  placeOrder,
  runBacktest,
  setKillSwitch,
  type BacktestResponse,
  type Health,
  type Position,
} from "./api";

type Tab = "backtest" | "scanner" | "charts" | "alerts" | "briefing" | "orders" | "risk";

const STRATEGIES = ["sma_crossover", "opening_range_breakout"];

function fmt(v: number | string | boolean | null): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    return Math.abs(v) >= 1000 || Number.isInteger(v)
      ? v.toLocaleString("en-IN", { maximumFractionDigits: 2 })
      : v.toFixed(4);
  }
  return String(v);
}

export default function App() {
  const [tab, setTab] = useState<Tab>("backtest");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const refreshHealth = useCallback(async () => {
    try {
      setHealthError(null);
      setHealth(await getHealth());
    } catch (e) {
      setHealth(null);
      setHealthError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    const t = setInterval(() => void refreshHealth(), 15000);
    return () => clearInterval(t);
  }, [refreshHealth]);

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <h1>ATR — algo trading dashboard</h1>
          <p>
            Backend: <code>{API_URL}</code> · <a href={`${API_URL}/docs`}>API docs</a>
          </p>
        </div>
        <div className="health">
          <span className={`dot ${health ? "ok" : "bad"}`} />
          {health ? (
            <span>
              {health.status} · {health.env} · session{" "}
              {health.session_active ? "active" : "none"} · db{" "}
              {health.database ? "up" : "off"}
            </span>
          ) : (
            <span>backend unreachable</span>
          )}
          <button className="ghost" onClick={() => void refreshHealth()}>
            Refresh
          </button>
        </div>
      </header>

      {healthError && <div className="error">{healthError}</div>}

      <nav className="tabs">
        {(["backtest", "scanner", "charts", "alerts", "briefing", "orders", "risk"] as Tab[]).map((t) => (
          <button
            key={t}
            className={tab === t ? "active" : ""}
            onClick={() => setTab(t)}
          >
            {t === "backtest"
              ? "Backtest"
              : t === "scanner"
                ? "Scanner"
                : t === "charts"
                  ? "Charts"
                  : t === "alerts"
                    ? "Alerts"
                    : t === "briefing"
                      ? "Briefing"
                      : t === "orders"
                        ? "Positions & Orders"
                        : "Risk"}
          </button>
        ))}
      </nav>

      {tab === "backtest" && <BacktestPanel />}
      {tab === "scanner" && <ScannerPanel />}
      {tab === "charts" && <ChartsPanel />}
      {tab === "alerts" && <AlertsPanel />}
      {tab === "briefing" && <BriefingPanel />}
      {tab === "orders" && <OrdersPanel />}
      {tab === "risk" && <RiskPanel />}
    </div>
  );
}

function BacktestPanel() {
  const [strategy, setStrategy] = useState("sma_crossover");
  const [symbols, setSymbols] = useState("AAPL,MSFT");
  const [cash, setCash] = useState(1000000);
  const [fast, setFast] = useState(20);
  const [slow, setSlow] = useState(50);
  const [slippage, setSlippage] = useState(5);
  const [futures, setFutures] = useState(false);
  const [squareOff, setSquareOff] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResponse | null>(null);

  async function onRun() {
    setBusy(true);
    setError(null);
    try {
      const res = await runBacktest({
        strategy,
        symbols: symbols
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
        initial_cash: cash,
        fast,
        slow,
        slippage_bps: slippage,
        square_off_eod: squareOff,
        futures,
      });
      setResult(res);
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="grid">
        <label className="field">
          Strategy
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {STRATEGIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Symbols (comma separated)
          <input value={symbols} onChange={(e) => setSymbols(e.target.value)} />
        </label>
        <label className="field">
          Initial cash
          <input
            type="number"
            value={cash}
            onChange={(e) => setCash(Number(e.target.value))}
          />
        </label>
        <label className="field">
          Fast
          <input
            type="number"
            value={fast}
            onChange={(e) => setFast(Number(e.target.value))}
          />
        </label>
        <label className="field">
          Slow
          <input
            type="number"
            value={slow}
            onChange={(e) => setSlow(Number(e.target.value))}
          />
        </label>
        <label className="field">
          Slippage (bps)
          <input
            type="number"
            value={slippage}
            onChange={(e) => setSlippage(Number(e.target.value))}
          />
        </label>
        <label className="field check">
          <input
            type="checkbox"
            checked={futures}
            onChange={(e) => setFutures(e.target.checked)}
          />
          Futures margin mode
        </label>
        <label className="field check">
          <input
            type="checkbox"
            checked={squareOff}
            onChange={(e) => setSquareOff(e.target.checked)}
          />
          Square off EOD
        </label>
      </div>
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => void onRun()}>
          {busy ? "Running…" : "Run backtest"}
        </button>
        <span className="hint">Runs on synthetic data, no broker needed.</span>
      </div>
      {error && <div className="error">{error}</div>}
      {result && (
        <>
          <p className="hint">
            {result.strategy} · {result.num_trades} trades · {result.num_fills}{" "}
            fills
            {result.killed ? ` · halted: ${result.kill_reason ?? ""}` : ""}
          </p>
          <table className="metrics">
            <tbody>
              {Object.entries(result.metrics).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{fmt(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

function OrdersPanel() {
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("RELIANCE");
  const [exchange, setExchange] = useState("NSEEQ");
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState("MARKET");
  const [price, setPrice] = useState<string>("");
  const [feedback, setFeedback] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      setPositions(await getPositions());
    } catch (e) {
      setPositions(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function submit() {
    setError(null);
    setFeedback(null);
    try {
      const res = await placeOrder({
        symbol: symbol.trim().toUpperCase(),
        exchange: exchange.trim().toUpperCase(),
        quantity: qty,
        order_type: orderType,
        price: price === "" ? null : Number(price),
      });
      setFeedback(
        `order ${res.order_id} → ${res.status}${res.reject_reason ? ` (${res.reject_reason})` : ""}`,
      );
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel">
      <div className="row" style={{ marginTop: 0 }}>
        <button className="ghost" onClick={() => void load()}>
          Reload positions
        </button>
        <span className="hint">
          Live endpoints need <code>ENV=paper|live</code> + active IIFL session.
        </span>
      </div>
      {positions && (
        <table className="metrics">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Qty</th>
              <th>Avg</th>
              <th>Last</th>
              <th>Unrealised</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={`${p.exchange}:${p.symbol}`}>
                <td>{p.symbol}</td>
                <td>{p.quantity}</td>
                <td>{fmt(p.avg_price)}</td>
                <td>{fmt(p.last_price)}</td>
                <td>{fmt(p.unrealized_pnl)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Place order</h3>
      <div className="grid">
        <label className="field">
          Symbol
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
        </label>
        <label className="field">
          Exchange
          <input value={exchange} onChange={(e) => setExchange(e.target.value)} />
        </label>
        <label className="field">
          Quantity (signed)
          <input
            type="number"
            value={qty}
            onChange={(e) => setQty(Number(e.target.value))}
          />
        </label>
        <label className="field">
          Type
          <select value={orderType} onChange={(e) => setOrderType(e.target.value)}>
            <option>MARKET</option>
            <option>LIMIT</option>
            <option>SL</option>
            <option>SLM</option>
          </select>
        </label>
        <label className="field">
          Price (limit)
          <input value={price} onChange={(e) => setPrice(e.target.value)} />
        </label>
      </div>
      <div className="row">
        <button className="primary" onClick={() => void submit()}>
          Submit
        </button>
        {feedback && <span className="hint">{feedback}</span>}
      </div>
      {error && <div className="error">{error}</div>}
    </section>
  );
}

function RiskPanel() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      setStatus(await getRiskStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function kill(engaged: boolean) {
    setError(null);
    try {
      await setKillSwitch(engaged);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel">
      <div className="row" style={{ marginTop: 0 }}>
        <button className="ghost" onClick={() => void load()}>
          Reload
        </button>
        <button className="danger" onClick={() => void kill(true)}>
          Engage kill switch
        </button>
        <button className="ghost" onClick={() => void kill(false)}>
          Clear
        </button>
      </div>
      {status && (
        <table className="metrics">
          <tbody>
            {Object.entries(status).map(([k, v]) => (
              <tr key={k}>
                <td>{k}</td>
                <td>{fmt(v as number | string | boolean | null)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <div className="error">{error}</div>}
      <p className="hint">
        The kill switch here only flips API state. The backtest/live
        `RiskEngine` still enforces loss, exposure, and square-off limits.
      </p>
    </section>
  );
}
