import { useEffect, useState, type ReactNode } from "react";
import { API_URL, getScan, type ScanRow } from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { cn } from "./lib/utils";

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

function Pill({ tone, children }: { tone: "up" | "down" | "gold" | "flat"; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold",
        tone === "up" && "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
        tone === "down" && "bg-destructive/10 text-destructive",
        tone === "gold" && "bg-amber-500/10 text-amber-600 dark:text-amber-400",
        tone === "flat" && "bg-primary/[0.07] text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

export default function ScannerPanel({ onOpenChart }: { onOpenChart?: (tab: string) => void }) {
  const [mode, setMode] = useState<Mode>("all");
  const [rows, setRows] = useState<ScanRow[] | null>(null);
  const [asOf, setAsOf] = useState("");
  const [breadth, setBreadth] = useState("");
  const [errors, setErrors] = useState<{ symbol: string; error: string }[]>([]);
  const [scanState, setScanState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [universe, setUniverse] = useState("");
  const [nameFilter, setNameFilter] = useState("");
  const [breakoutsOnly, setBreakoutsOnly] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("score");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  async function load(nextMode: Mode = mode) {
    setScanState("loading");
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
      setScanState("success");
    } catch (e) {
      setRows(null);
      setError(e instanceof Error ? e.message : String(e));
      setScanState("error");
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

  function openChart(sym: string) {
    try {
      sessionStorage.setItem("atr.chartSymbol", sym);
    } catch {
      /* ignore */
    }
    onOpenChart?.("charts");
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
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-2.5">
        <Tabs value={mode} onValueChange={(v) => switchMode(v as Mode)} variant="segment">
          <TabsList>
            <TabsTrigger value="all">All NSE (cached)</TabsTrigger>
            <TabsTrigger value="watchlist">Watchlist (live)</TabsTrigger>
          </TabsList>
        </Tabs>
        <StatefulButton
          state={scanState}
          variant="secondary"
          onClick={() => void load()}
          loadingText="Scanning…"
          successText="Scanned"
          errorText="Failed — retry"
        >
          Re-scan
        </StatefulButton>
      </div>

      {mode === "watchlist" && (
        <div className="mt-3.5 max-w-xl">
          <Input
            value={universe}
            onChange={setUniverse}
            placeholder="Universe override: RELIANCE-EQ,INFY-EQ (blank = 19-name default)"
          />
        </div>
      )}

      <div className="mt-3.5 flex flex-wrap items-center gap-3.5">
        <div className="w-55 max-w-full">
          <Input value={nameFilter} onChange={setNameFilter} placeholder="Filter by symbol…" />
        </div>
        <Switch checked={breakoutsOnly} onCheckedChange={setBreakoutsOnly} label="Breakouts only" />
      </div>

      <Hint className="mt-3">
        {scanState === "loading"
          ? mode === "all"
            ? "Scoring cached histories…"
            : "Fetching live IIFL dailies — about a minute."
          : rows
            ? `As of ${asOf} · ${breadth} · showing ${visible?.length} · click a header to sort`
            : mode === "all"
              ? "Full market off the local cache — refresh nightly with `atr history sync`."
              : "Momentum scan over liquid NSE names."}
      </Hint>

      {error && (
        <div className="mt-3">
          <ErrorBox>{error}</ErrorBox>
        </div>
      )}

      {visible && (
        <div className="mt-3 overflow-x-auto rounded-xl border border-border">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left">
                <th className="px-3 py-2 font-semibold">Symbol</th>
                {COLUMNS.map((c) => (
                  <th
                    key={c.key}
                    onClick={() => onSort(c.key)}
                    title="sort"
                    className="cursor-pointer select-none px-3 py-2 text-right font-semibold hover:text-foreground"
                  >
                    {c.label}
                    {sortKey === c.key ? (sortDir === -1 ? " ▼" : " ▲") : ""}
                  </th>
                ))}
                <th className="px-3 py-2 font-semibold">Trend</th>
                <th className="px-3 py-2 font-semibold">Signal</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={r.symbol} className="border-b border-border/60 transition-colors last:border-0 hover:bg-primary/[0.03]">
                  <td className="px-3 py-1.5">
                    <strong className="font-semibold">{r.symbol.replace("-EQ", "")}</strong>
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{r.score.toFixed(1)}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{r.last.toLocaleString("en-IN")}</td>
                  <td className={cn("px-3 py-1.5 text-right tabular-nums", r.ret_1m >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>
                    {r.ret_1m.toFixed(1)}
                  </td>
                  <td className={cn("px-3 py-1.5 text-right tabular-nums", r.vs_high > -2 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>
                    {r.vs_high.toFixed(1)}
                  </td>
                  <td className={cn("px-3 py-1.5 text-right tabular-nums", r.rsi < 30 ? "text-emerald-600 dark:text-emerald-400" : r.rsi > 70 ? "text-destructive" : "")}>
                    {r.rsi.toFixed(0)}
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{r.vol_x.toFixed(1)}</td>
                  <td className="px-3 py-1.5">
                    <Pill tone={r.trend === "UP" ? "up" : r.trend === "DOWN" ? "down" : "flat"}>{r.trend}</Pill>
                  </td>
                  <td className="px-3 py-1.5">
                    <span className="inline-flex flex-wrap gap-1">
                      {r.breakout && <Pill tone="up">BREAKOUT</Pill>}
                      {r.gold_cross_5d && <Pill tone="gold">GOLD CROSS</Pill>}
                      {r.rsi < 30 && <Pill tone="gold">OVERSOLD</Pill>}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    <Button size="sm" variant="ghost" onClick={() => openChart(r.symbol)} title="Open chart">
                      Chart
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {errors.length > 0 && (
        <Hint className="mt-2.5">Skipped: {errors.map((e) => e.symbol).join(", ")}</Hint>
      )}
    </Card>
  );
}
