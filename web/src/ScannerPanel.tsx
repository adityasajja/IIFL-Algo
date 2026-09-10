import { useEffect, useState } from "react";
import { API_URL, getScan, type ScanRow } from "./api";

type SortKey = "score" | "ret_1m" | "vs_high" | "rsi" | "vol_x" | "last";
type Mode = "watchlist" | "all";

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "score", label: "Score" },
  { key: "last", label: "Last" },
  { key: "ret_1m", label: "1M %" },
  { key: "vs_high", label: "vsHigh %" },
  { key: "rsi", label: "RSI" },
  { key: "vol_x", label: "Vol ×" },
];

interface ScanAllResponse {
  as_of: string;
  universe: number;
  scored: number;
  breadth_up: number;
  rows: ScanRow[];
}

export default function ScannerPanel() {
  const [mode, setMode] = useState<Mode>("all");
  const [rows, setRows] = useState<ScanRow[] | null>(null);
  const [asOf, setAsOf] = useState("");
  const [breadth, setBreadth] = useState("");
  const [errors, setErrors] = useState<{ symbol: string; error: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [universe, setUniverse] = useState("");
  const [nameFilter, setNameFilter] = useState("");
  const [breakoutsOnly, setBreakoutsOnly] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("score");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  async function load(nextMode: Mode = mode) {
    setBusy(true);
    setError(null);
    try {
      if (nextMode === "all") {
        const res = await fetch(`${API_URL}/scan-all`).then((r) => {
          if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
          return r.json() as Promise<ScanAllResponse>;
        });
        setRows(res.rows);
        setAsOf(res.as_of);
        setBreadth(`${res.breadth_up} UP / ${res.scored - res.breadth_up} down of ${res.scored} scored (${res.universe} cached)`);
        setErrors([]);
      } else {
        const res = await getScan(universe.trim() || undefined);
        setRows(res.rows);
        setAsOf(res.as_of);
        setBreadth(`${res.rows.length} names · live IIFL dailies`);
        setErrors(res.errors);
      }
    } catch (e) {
      setRows(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load("all");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSort(k: SortKey) {
    if (k === sortKey) {
      setSortDir((d) => (d === 1 ? -1 : 1));
    } else {
      setSortKey(k);
      setSortDir(-1);
    }
  }

  function switchMode(m: Mode) {
    setMode(m);
    setRows(null);
    void load(m);
  }

  const visible = rows
    ? [...rows]
        .filter(
          (r) =>
            (!breakoutsOnly || r.breakout) &&
            (nameFilter.trim() === "" ||
              r.symbol.includes(nameFilter.trim().toUpperCase())),
        )
        .sort((a, b) => (a[sortKey] - b[sortKey]) * sortDir)
        .slice(0, 200)
    : null;

  return (
    <section className="panel">
      <div className="row" style={{ marginTop: 0 }}>
        <button
          className={mode === "all" ? "primary" : "ghost"}
          onClick={() => switchMode("all")}
        >
          All NSE (cached)
        </button>
        <button
          className={mode === "watchlist" ? "primary" : "ghost"}
          onClick={() => switchMode("watchlist")}
        >
          Watchlist (live)
        </button>
        <button className="ghost" disabled={busy} onClick={() => void load()}>
          {busy ? "Scanning…" : "Re-scan"}
        </button>
      </div>
      {mode === "watchlist" && (
        <div className="row">
          <input
            placeholder="Universe override: RELIANCE-EQ,INFY-EQ (blank = 19-name default)"
            value={universe}
            onChange={(e) => setUniverse(e.target.value)}
            style={{ flex: 1, minWidth: 220 }}
          />
        </div>
      )}
      <div className="row">
        <input
          placeholder="Filter by symbol…"
          value={nameFilter}
          onChange={(e) => setNameFilter(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <label className="field check">
          <input
            type="checkbox"
            checked={breakoutsOnly}
            onChange={(e) => setBreakoutsOnly(e.target.checked)}
          />
          Breakouts only
        </label>
      </div>
      <p className="hint">
        {busy
          ? mode === "all"
            ? "Scoring cached histories…"
            : "Fetching live IIFL dailies — about a minute."
          : rows
            ? `As of ${asOf} · ${breadth} · showing ${visible?.length} · click a header to sort`
            : mode === "all"
              ? "Full market off the local cache — refresh nightly with `atr history sync`."
              : "Momentum scan over liquid NSE names."}
      </p>
      {error && <div className="error">{error}</div>}
      {visible && (
        <div style={{ overflowX: "auto" }}>
          <table className="metrics scan">
            <thead>
              <tr>
                <th>Symbol</th>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    onClick={() => onSort(c.key)}
                    className="sortable"
                    title="sort"
                  >
                    {c.label}
                    {sortKey === c.key ? (sortDir === -1 ? " ▼" : " ▲") : ""}
                  </th>
                ))}
                <th>Trend</th>
                <th>Signal</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={r.symbol}>
                  <td>
                    <strong>{r.symbol.replace("-EQ", "")}</strong>
                  </td>
                  <td className="num">{r.score.toFixed(1)}</td>
                  <td className="num">{r.last.toLocaleString("en-IN")}</td>
                  <td className={`num ${r.ret_1m >= 0 ? "pos" : "neg"}`}>
                    {r.ret_1m.toFixed(1)}
                  </td>
                  <td className={`num ${r.vs_high > -2 ? "pos" : "neg"}`}>
                    {r.vs_high.toFixed(1)}
                  </td>
                  <td
                    className={`num ${r.rsi < 30 ? "pos" : r.rsi > 70 ? "neg" : ""}`}
                  >
                    {r.rsi.toFixed(0)}
                  </td>
                  <td className="num">{r.vol_x.toFixed(1)}</td>
                  <td>
                    <span className={`pill pill-${r.trend.toLowerCase()}`}>
                      {r.trend}
                    </span>
                  </td>
                  <td>
                    {r.breakout && <span className="pill pill-up">BREAKOUT</span>}{" "}
                    {r.gold_cross_5d && (
                      <span className="pill pill-gold">GOLD CROSS</span>
                    )}{" "}
                    {r.rsi < 30 && (
                      <span className="pill pill-gold">OVERSOLD</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {errors.length > 0 && (
        <p className="hint">Skipped: {errors.map((e) => e.symbol).join(", ")}</p>
      )}
    </section>
  );
}
