import { useEffect, useState } from "react";
import {
  getBriefingConfig,
  previewBriefing,
  saveBriefingConfig,
  sendBriefing,
  type BriefingConfig,
} from "./api";

const SYNC_CMD = `schtasks /create /tn "ATR history sync" /tr "cmd /c cd /d D:\\ALGO && uv run atr history sync" /sc daily /st 16:00 /f`;
const BRIEF_CMD = `schtasks /create /tn "ATR morning brief" /tr "cmd /c cd /d D:\\ALGO && uv run atr brief send" /sc daily /st 08:45 /f`;

export default function BriefingPanel() {
  const [cfg, setCfg] = useState<BriefingConfig | null>(null);
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function refresh() {
    try {
      setError(null);
      const res = await getBriefingConfig();
      setCfg(res.config);
      setLastSent(
        res.last_sent ? `${res.last_sent.sent_at} via ${res.last_sent.channel}` : null,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function set<K extends keyof BriefingConfig>(k: K, v: BriefingConfig[K]) {
    if (cfg) setCfg({ ...cfg, [k]: v });
  }

  async function save() {
    if (!cfg) return;
    try {
      setError(null);
      setNote("Saved.");
      setCfg(await saveBriefingConfig(cfg));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function preview() {
    setBusy(true);
    setNote(null);
    try {
      setError(null);
      await save();
      setMessage((await previewBriefing()).message);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    setBusy(true);
    setNote(null);
    try {
      setError(null);
      await save();
      const res = await sendBriefing();
      setNote(
        res.sent_on === "none"
          ? "Nothing to send with — configure Telegram first."
          : res.sent_on === "disabled"
            ? "Sending is disabled in config (toggle below)."
            : `Sent via ${res.sent_on}. Check Telegram.`,
      );
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!cfg) return <section className="panel">{error ? <div className="error">{error}</div> : "Loading…"}</section>;

  return (
    <section className="panel">
      <div className="grid">
        <label className="field">
          Long ideas
          <input type="number" value={cfg.top_n} onChange={(e) => set("top_n", Number(e.target.value))} />
        </label>
        <label className="field">
          Avoid list
          <input type="number" value={cfg.avoid_n} onChange={(e) => set("avoid_n", Number(e.target.value))} />
        </label>
        <label className="field">
          Min price ₹
          <input type="number" value={cfg.min_price} onChange={(e) => set("min_price", Number(e.target.value))} />
        </label>
        <label className="field">
          Min day value ₹L
          <input type="number" value={cfg.min_day_value_lakh} onChange={(e) => set("min_day_value_lakh", Number(e.target.value))} />
        </label>
        <label className="field">
          Universe
          <select value={cfg.universe} onChange={(e) => set("universe", e.target.value)}>
            <option value="all">All NSE (cached)</option>
            <option value="watchlist">Watchlist only</option>
          </select>
        </label>
        <label className="field check">
          <input type="checkbox" checked={cfg.send_enabled} onChange={(e) => set("send_enabled", e.target.checked)} />
          Sending enabled
        </label>
      </div>
      <div className="row">
        <button className="ghost" onClick={() => void save()}>Save</button>
        <button className="primary" disabled={busy} onClick={() => void preview()}>
          {busy ? "Working…" : "Preview now"}
        </button>
        <button className="primary" disabled={busy} onClick={() => void send()}>
          Send to Telegram
        </button>
        {lastSent && <span className="hint">Last sent: {lastSent}</span>}
      </div>
      {note && <p className="hint">{note}</p>}
      {error && <div className="error">{error}</div>}
      {message && <pre className="brief">{message}</pre>}

      <h3>Daily automation (Windows Task Scheduler, machine must be on)</h3>
      <p className="hint">1 · Refresh data after close (4 PM):</p>
      <code className="cmd">{SYNC_CMD}</code>
      <p className="hint">2 · Telegram the brief before open (8:45 AM):</p>
      <code className="cmd">{BRIEF_CMD}</code>
      <p className="hint">Run each once in an admin terminal. The 8:45 AM brief needs no IIFL login (cache + Telegram only). The 4 PM sync needs that day's session — log in once each morning via <code>atr login</code> and everything downstream works till midnight IST.</p>
    </section>
  );
}
