import { useEffect, useState } from "react";
import {
  armAlertRule,
  createAlertRule,
  deleteAlertRule,
  getAlertEvents,
  getAlertRules,
  runAlertCheck,
  sendAlertTest,
  type AlertEvent,
  type AlertRule,
} from "./api";

const KINDS = [
  { v: "price_below", label: "Price falls to/below ₹", needs: true, hint: "exit / falling-stock alert" },
  { v: "price_above", label: "Price rises to/above ₹", needs: true, hint: "entry / breakout alert" },
  { v: "day_drop_pct", label: "Falls % today", needs: true, hint: "e.g. 3 = down 3% on the day" },
  { v: "day_gain_pct", label: "Rises % today", needs: true, hint: "e.g. 3 = up 3% on the day" },
  { v: "rsi_below", label: "RSI at/below", needs: true, hint: "oversold bounce watch" },
  { v: "rsi_above", label: "RSI at/above", needs: true, hint: "overbought warning" },
  { v: "sma_cross_up", label: "Golden cross", needs: false, hint: "SMA20 × SMA50 up, last 5 bars" },
  { v: "sma_cross_down", label: "Death cross", needs: false, hint: "SMA20 × SMA50 down, last 5 bars" },
];

export default function AlertsPanel() {
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("RELIANCE-EQ");
  const [kind, setKind] = useState("price_below");
  const [threshold, setThreshold] = useState("1250");

  async function refresh() {
    try {
      setError(null);
      setRules(await getAlertRules());
      setEvents(await getAlertEvents());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function create() {
    setError(null);
    setNote(null);
    try {
      await createAlertRule({
        symbol: symbol.trim().toUpperCase(),
        kind,
        threshold: Number(threshold) || 0,
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function toggle(r: AlertRule) {
    await armAlertRule(r.id, !r.armed);
    await refresh();
  }

  async function remove(id: string) {
    await deleteAlertRule(id);
    await refresh();
  }

  async function checkNow() {
    setBusy(true);
    setNote(null);
    try {
      const res = await runAlertCheck();
      setNote(
        res.fired.length === 0
          ? `Checked (${res.market_open ? "market open" : "market closed"}) — nothing firing.`
          : `${res.fired.length} alert(s) fired — see feed below.`,
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setNote(null);
    try {
      const res = await sendAlertTest();
      setNote(
        res.sent_on === "none" || res.sent_on === "log"
          ? "No Telegram/SMS configured — test went to the server log. Add TELEGRAM_BOT_TOKEN + CHAT_ID to .env."
          : `Test message sent via ${res.sent_on}. Check your phone.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const kindInfo = KINDS.find((k) => k.v === kind);

  return (
    <section className="panel">
      <div className="row" style={{ marginTop: 0 }}>
        <button className="primary" disabled={busy} onClick={() => void checkNow()}>
          {busy ? "Checking…" : "Check now"}
        </button>
        <button className="ghost" onClick={() => void test()}>
          Send test message
        </button>
        <span className="hint">Notify-only: alerts ping you, nothing auto-trades.</span>
      </div>
      {note && <p className="hint">{note}</p>}
      {error && <div className="error">{error}</div>}

      <h3>New alert</h3>
      <div className="grid">
        <label className="field">
          Symbol
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} />
        </label>
        <label className="field">
          Condition
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => (
              <option key={k.v} value={k.v}>
                {k.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Level {kindInfo?.needs ? "" : "(unused)"}
          <input
            type="number"
            value={threshold}
            disabled={!kindInfo?.needs}
            onChange={(e) => setThreshold(e.target.value)}
          />
        </label>
      </div>
      <div className="row">
        <button className="primary" onClick={() => void create()}>
          Create alert
        </button>
        {kindInfo && <span className="hint">{kindInfo.hint}</span>}
      </div>

      <h3>Rules ({rules.length})</h3>
      {rules.length === 0 && <p className="hint">None yet — create your first above.</p>}
      <table className="metrics">
        <tbody>
          {rules.map((r) => (
            <tr key={r.id}>
              <td>
                <strong>{r.symbol}</strong> · {r.kind.replace(/_/g, " ")}
                {["price_above", "price_below", "day_drop_pct", "day_gain_pct", "rsi_below", "rsi_above"].includes(r.kind) && (
                  <> @ {r.threshold}</>
                )}
              </td>
              <td>
                <span className={`pill ${r.armed ? "pill-up" : ""}`}>
                  {r.armed ? "ARMED" : "OFF"}
                </span>
              </td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <button className="ghost" onClick={() => void toggle(r)}>
                  {r.armed ? "Disarm" : "Arm"}
                </button>{" "}
                <button className="ghost" onClick={() => void remove(r.id)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Recent firings</h3>
      {events.length === 0 && <p className="hint">Nothing fired yet.</p>}
      <table className="metrics">
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>
                <strong>{e.rule}</strong>
                <br />
                <span className="hint">{e.message}</span>
              </td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <span className="pill">{e.channel}</span>
                <br />
                <span className="hint">{new Date(e.ts).toLocaleString("en-IN")}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
