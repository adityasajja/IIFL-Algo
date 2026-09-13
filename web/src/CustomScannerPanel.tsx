/**
 * Chartink-style condition builder & scanner panel.
 *
 * Features:
 *  - Drag-and-drop-free condition rows (indicator, op, value/indicator)
 *  - AND / OR combinator
 *  - Saved scans (presets + user saves)
 *  - Universe toggle: All NSE (cached) vs Watchlist
 *  - Results table with sort, live ticks, chart nav
 */
import {
  BookmarkCheck,
  ChevronDown,
  Loader2,
  Play,
  Plus,
  Save,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  deleteSavedScan,
  getSavedScans,
  runCustomScan,
  upsertSavedScan,
  type CustomScanRow,
  type SavedScan,
  type ScanCondition,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { useLiveTicks } from "./lib/useLiveTicks";
import { cn } from "./lib/utils";

// ─── Indicator catalogue ─────────────────────────────────────────────────────

interface IndicatorMeta {
  id: string;
  label: string;
  hasPeriod: boolean;
  defaultPeriod?: number;
  category: string;
}

const INDICATORS: IndicatorMeta[] = [
  // Price
  { id: "close",       label: "Close",          hasPeriod: false, category: "Price" },
  { id: "open",        label: "Open",            hasPeriod: false, category: "Price" },
  { id: "high",        label: "High",            hasPeriod: false, category: "Price" },
  { id: "low",         label: "Low",             hasPeriod: false, category: "Price" },
  // Moving Averages
  { id: "sma",         label: "SMA",             hasPeriod: true, defaultPeriod: 20, category: "MA" },
  { id: "ema",         label: "EMA",             hasPeriod: true, defaultPeriod: 20, category: "MA" },
  // Momentum
  { id: "rsi",         label: "RSI",             hasPeriod: true, defaultPeriod: 14, category: "Momentum" },
  // Volatility
  { id: "atr",         label: "ATR",             hasPeriod: true, defaultPeriod: 14, category: "Volatility" },
  { id: "atr_pct",     label: "ATR %",           hasPeriod: true, defaultPeriod: 14, category: "Volatility" },
  { id: "bb_upper",    label: "BB Upper",        hasPeriod: true, defaultPeriod: 20, category: "Volatility" },
  { id: "bb_mid",      label: "BB Mid",          hasPeriod: true, defaultPeriod: 20, category: "Volatility" },
  { id: "bb_lower",    label: "BB Lower",        hasPeriod: true, defaultPeriod: 20, category: "Volatility" },
  // Volume
  { id: "volume",      label: "Volume",          hasPeriod: false, category: "Volume" },
  { id: "vol_x",       label: "Volume ×avg",     hasPeriod: false, category: "Volume" },
  // Returns
  { id: "day_chg_pct", label: "Day Change %",    hasPeriod: false, category: "Returns" },
  { id: "ret_1m",      label: "1-Month Return %",hasPeriod: false, category: "Returns" },
  { id: "vs_high",     label: "vs 52w High %",   hasPeriod: false, category: "Returns" },
];

const IND_BY_ID = Object.fromEntries(INDICATORS.map((i) => [i.id, i]));

const OPERATORS = [
  { id: ">",             label: ">" },
  { id: "<",             label: "<" },
  { id: ">=",            label: "≥" },
  { id: "<=",            label: "≤" },
  { id: "=",             label: "=" },
  { id: "crosses_above", label: "crosses above" },
  { id: "crosses_below", label: "crosses below" },
];

const CROSS_OPS = new Set(["crosses_above", "crosses_below"]);

// ─── Helpers ─────────────────────────────────────────────────────────────────

function indicatorLabel(ind: string, period?: number): string {
  const meta = IND_BY_ID[ind];
  if (!meta) return ind;
  if (meta.hasPeriod && period) return `${meta.label}(${period})`;
  return meta.label;
}

function conditionText(c: ScanCondition): string {
  const lhs = indicatorLabel(c.indicator, c.period);
  const op = OPERATORS.find((o) => o.id === c.op)?.label ?? c.op;
  const rhs =
    c.rhs_type === "indicator"
      ? indicatorLabel(c.rhs_indicator ?? "", c.rhs_period)
      : String(c.rhs_value ?? 0);
  return `${lhs} ${op} ${rhs}`;
}

function mkId(): string {
  return Math.random().toString(36).slice(2, 9);
}

type SortKey = "score" | "last" | "day_chg_pct" | "ret_1m" | "vs_high" | "rsi" | "vol_x";

// ─── Condition row ────────────────────────────────────────────────────────────

interface CondRowState extends ScanCondition {
  _key: string;
}

function ConditionRow({
  cond,
  onChange,
  onRemove,
}: {
  cond: CondRowState;
  onChange: (c: CondRowState) => void;
  onRemove: () => void;
}) {
  const lhsMeta = IND_BY_ID[cond.indicator];
  const isCross = CROSS_OPS.has(cond.op);

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border/50 bg-muted/20 px-3 py-2.5">
      {/* LHS indicator */}
      <select
        value={cond.indicator}
        onChange={(e) => {
          const meta = IND_BY_ID[e.target.value];
          onChange({
            ...cond,
            indicator: e.target.value,
            period: meta?.hasPeriod ? (meta.defaultPeriod ?? 14) : undefined,
          });
        }}
        className="rounded-md border border-border/60 bg-background px-2 py-1 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary"
      >
        {["Price", "MA", "Momentum", "Volatility", "Volume", "Returns"].map((cat) => (
          <optgroup key={cat} label={cat}>
            {INDICATORS.filter((i) => i.category === cat).map((i) => (
              <option key={i.id} value={i.id}>{i.label}</option>
            ))}
          </optgroup>
        ))}
      </select>

      {/* LHS period */}
      {lhsMeta?.hasPeriod && (
        <input
          type="number"
          value={cond.period ?? lhsMeta.defaultPeriod ?? 14}
          min={2}
          max={500}
          onChange={(e) => onChange({ ...cond, period: Number(e.target.value) })}
          className="w-16 rounded-md border border-border/60 bg-background px-2 py-1 text-center text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        />
      )}

      {/* Operator */}
      <select
        value={cond.op}
        onChange={(e) => onChange({ ...cond, op: e.target.value })}
        className="rounded-md border border-border/60 bg-background px-2 py-1 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary"
      >
        {OPERATORS.map((o) => (
          <option key={o.id} value={o.id}>{o.label}</option>
        ))}
      </select>

      {/* RHS type toggle */}
      {!isCross && (
        <button
          type="button"
          onClick={() =>
            onChange({ ...cond, rhs_type: cond.rhs_type === "value" ? "indicator" : "value" })
          }
          className="flex items-center gap-1 rounded-md border border-border/60 bg-background px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {cond.rhs_type === "value" ? "Fixed value" : "Indicator"}
          <ChevronDown className="h-3 w-3" />
        </button>
      )}

      {/* RHS */}
      {!isCross && cond.rhs_type === "value" ? (
        <input
          type="number"
          value={cond.rhs_value ?? 0}
          step="any"
          onChange={(e) => onChange({ ...cond, rhs_value: parseFloat(e.target.value) })}
          className="w-24 rounded-md border border-border/60 bg-background px-2 py-1 text-center text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-primary"
        />
      ) : !isCross ? (
        <>
          <select
            value={cond.rhs_indicator ?? "sma"}
            onChange={(e) => {
              const meta = IND_BY_ID[e.target.value];
              onChange({
                ...cond,
                rhs_indicator: e.target.value,
                rhs_period: meta?.hasPeriod ? (meta.defaultPeriod ?? 20) : undefined,
              });
            }}
            className="rounded-md border border-border/60 bg-background px-2 py-1 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary"
          >
            {INDICATORS.filter((i) => i.category === "MA" || i.category === "Price" || i.category === "Momentum").map((i) => (
              <option key={i.id} value={i.id}>{i.label}</option>
            ))}
          </select>
          {IND_BY_ID[cond.rhs_indicator ?? "sma"]?.hasPeriod && (
            <input
              type="number"
              value={cond.rhs_period ?? 20}
              min={2}
              max={500}
              onChange={(e) => onChange({ ...cond, rhs_period: Number(e.target.value) })}
              className="w-16 rounded-md border border-border/60 bg-background px-2 py-1 text-center text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
          )}
        </>
      ) : (
        /* crosses_above/below — RHS is always an indicator */
        <>
          <select
            value={cond.rhs_indicator ?? "sma"}
            onChange={(e) => {
              const meta = IND_BY_ID[e.target.value];
              onChange({
                ...cond,
                rhs_indicator: e.target.value,
                rhs_period: meta?.hasPeriod ? (meta.defaultPeriod ?? 20) : undefined,
              });
            }}
            className="rounded-md border border-border/60 bg-background px-2 py-1 text-sm font-medium focus:outline-none focus:ring-1 focus:ring-primary"
          >
            {INDICATORS.filter((i) => i.category === "MA" || i.category === "Price" || i.category === "Momentum").map((i) => (
              <option key={i.id} value={i.id}>{i.label}</option>
            ))}
          </select>
          {IND_BY_ID[cond.rhs_indicator ?? "sma"]?.hasPeriod && (
            <input
              type="number"
              value={cond.rhs_period ?? 50}
              min={2}
              max={500}
              onChange={(e) => onChange({ ...cond, rhs_period: Number(e.target.value) })}
              className="w-16 rounded-md border border-border/60 bg-background px-2 py-1 text-center text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
          )}
        </>
      )}

      {/* Remove */}
      <button
        type="button"
        onClick={onRemove}
        className="ml-auto text-muted-foreground/50 hover:text-destructive"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

const DEFAULT_COND = (): CondRowState => ({
  _key: mkId(),
  indicator: "rsi",
  period: 14,
  op: "<",
  rhs_type: "value",
  rhs_value: 35,
});

export default function CustomScannerPanel({
  onOpenChart,
}: {
  onOpenChart?: (tab: string) => void;
}) {
  const [conditions, setConditions] = useState<CondRowState[]>([DEFAULT_COND()]);
  const [combine, setCombine] = useState<"AND" | "OR">("AND");
  const [universe, setUniverse] = useState<"all" | "watchlist">("all");
  const [watchlist, setWatchlist] = useState("");

  const [savedScans, setSavedScans] = useState<SavedScan[]>([]);
  const [saveName, setSaveName] = useState("");
  const [showSaveInput, setShowSaveInput] = useState(false);
  const saveRef = useRef<HTMLInputElement>(null);

  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{ rows: CustomScanRow[]; meta: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [sortKey, setSortKey] = useState<SortKey>("score");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);
  const [nameFilter, setNameFilter] = useState("");

  // Load saved scans on mount
  useEffect(() => {
    void getSavedScans()
      .then((r) => setSavedScans(r.scans))
      .catch(() => {/* backend may be warming up */});
  }, []);

  // Focus save input when shown
  useEffect(() => {
    if (showSaveInput) saveRef.current?.focus();
  }, [showSaveInput]);

  // ── actions ─────────────────────────────────────────────────────────────────

  async function runScan() {
    if (conditions.length === 0) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await runCustomScan({
        conditions: conditions.map(({ _key, ...c }) => c),
        combine,
        universe,
        watchlist: universe === "watchlist"
          ? watchlist.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean)
          : undefined,
      });
      setResult({
        rows: res.rows,
        meta: `${res.matched} match${res.matched !== 1 ? "es" : ""} from ${res.universe_size.toLocaleString()} names · ${res.elapsed_s}s · as of ${res.as_of}`,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  async function saveScan() {
    const name = saveName.trim();
    if (!name) return;
    const id = name.toLowerCase().replace(/\s+/g, "_");
    const scan: SavedScan = {
      id,
      name,
      combine,
      conditions: conditions.map(({ _key, ...c }) => c),
    };
    await upsertSavedScan(id, scan);
    const r = await getSavedScans();
    setSavedScans(r.scans);
    setSaveName("");
    setShowSaveInput(false);
  }

  function loadScan(scan: SavedScan) {
    setConditions(scan.conditions.map((c) => ({ ...c, _key: mkId() })));
    setCombine(scan.combine);
    setResult(null);
    setError(null);
  }

  async function deleteScan(id: string) {
    await deleteSavedScan(id);
    const r = await getSavedScans();
    setSavedScans(r.scans);
  }

  function addCondition() {
    setConditions((cs) => [...cs, DEFAULT_COND()]);
  }

  function updateCondition(key: string, updated: CondRowState) {
    setConditions((cs) => cs.map((c) => (c._key === key ? updated : c)));
  }

  function removeCondition(key: string) {
    setConditions((cs) => cs.filter((c) => c._key !== key));
  }

  function onSort(k: SortKey) {
    if (k === sortKey) setSortDir((d) => (d === 1 ? -1 : 1));
    else { setSortKey(k); setSortDir(-1); }
  }

  // ── results ──────────────────────────────────────────────────────────────────

  const visible = useMemo(() => {
    if (!result) return [];
    return [...result.rows]
      .filter((r) => !nameFilter || r.symbol.includes(nameFilter.trim().toUpperCase()))
      .sort((a, b) => ((a[sortKey] as number) - (b[sortKey] as number)) * sortDir);
  }, [result, nameFilter, sortKey, sortDir]);

  const { getTick } = useLiveTicks(visible.slice(0, 50).map((r) => r.symbol));

  const SORT_COLS: { key: SortKey; label: string }[] = [
    { key: "score",       label: "Score" },
    { key: "last",        label: "Last" },
    { key: "day_chg_pct", label: "Day %" },
    { key: "ret_1m",      label: "1M %" },
    { key: "vs_high",     label: "vs High" },
    { key: "rsi",         label: "RSI" },
    { key: "vol_x",       label: "Vol ×" },
  ];

  return (
    <div className="space-y-4">

      {/* ── Saved scans row ──────────────────────────────────────────────── */}
      {savedScans.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Saved scans
          </span>
          {savedScans.map((s) => (
            <div key={s.id} className="group flex items-center gap-0.5 rounded-full border border-border/50 bg-muted/30 pl-3 pr-1.5 py-1">
              <button
                type="button"
                onClick={() => loadScan(s)}
                className="text-xs font-medium hover:text-primary"
              >
                {s.name}
              </button>
              <button
                type="button"
                onClick={() => void deleteScan(s.id)}
                className="ml-1 hidden h-4 w-4 items-center justify-center rounded-full text-muted-foreground/50 hover:text-destructive group-hover:flex"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      <Card className="p-5 space-y-4">

        {/* ── Condition builder ─────────────────────────────────────────── */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Conditions</span>
            {conditions.length > 1 && (
              <div className="flex rounded-lg border border-border/60 overflow-hidden text-xs font-semibold">
                {(["AND", "OR"] as const).map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setCombine(v)}
                    className={cn(
                      "px-3 py-1 transition-colors",
                      combine === v
                        ? "bg-primary text-primary-foreground"
                        : "bg-background text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {v}
                  </button>
                ))}
              </div>
            )}
          </div>

          {conditions.map((c) => (
            <ConditionRow
              key={c._key}
              cond={c}
              onChange={(updated) => updateCondition(c._key, updated)}
              onRemove={() => removeCondition(c._key)}
            />
          ))}

          <button
            type="button"
            onClick={addCondition}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-primary"
          >
            <Plus className="h-3.5 w-3.5" />
            Add condition
          </button>
        </div>

        {/* ── Universe + run bar ───────────────────────────────────────── */}
        <div className="flex flex-wrap items-center gap-3 border-t border-border/40 pt-3">
          {/* Universe */}
          <div className="flex rounded-lg border border-border/60 overflow-hidden text-xs font-semibold">
            {(["all", "watchlist"] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setUniverse(v)}
                className={cn(
                  "px-3 py-1.5 transition-colors",
                  universe === v
                    ? "bg-primary text-primary-foreground"
                    : "bg-background text-muted-foreground hover:text-foreground",
                )}
              >
                {v === "all" ? "All NSE" : "Watchlist"}
              </button>
            ))}
          </div>

          {universe === "watchlist" && (
            <Input
              value={watchlist}
              onChange={setWatchlist}
              placeholder="RELIANCE-EQ,INFY-EQ,… (blank = default 19)"
              className="max-w-xs"
            />
          )}

          {/* Run */}
          <Button
            onClick={() => void runScan()}
            disabled={running || conditions.length === 0}
            className="gap-2"
          >
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {running ? "Scanning…" : "Run scan"}
          </Button>

          {/* Save */}
          {showSaveInput ? (
            <div className="flex items-center gap-2">
              <input
                ref={saveRef}
                type="text"
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void saveScan()}
                placeholder="Scan name…"
                className="rounded-md border border-border/60 bg-background px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              />
              <Button size="sm" variant="secondary" onClick={() => void saveScan()}>
                <BookmarkCheck className="h-4 w-4" />
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setShowSaveInput(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setShowSaveInput(true)}
              className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              <Save className="h-3.5 w-3.5" />
              Save scan
            </button>
          )}
        </div>

        {/* ── Scan summary ────────────────────────────────────────────── */}
        {result && (
          <div className="text-xs text-muted-foreground border-t border-border/40 pt-2">
            {result.meta}
          </div>
        )}

        {error && <ErrorBox>{error}</ErrorBox>}

        {/* ── Summary description ──────────────────────────────────────── */}
        {conditions.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {conditions.map((c, i) => (
              <span key={c._key} className="flex items-center gap-1.5">
                {i > 0 && (
                  <span className="text-[10px] font-bold uppercase text-primary">{combine}</span>
                )}
                <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs text-primary font-medium">
                  {conditionText(c)}
                </span>
              </span>
            ))}
          </div>
        )}
      </Card>

      {/* ── Results table ────────────────────────────────────────────────── */}
      {visible.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <div className="flex items-center gap-3 border-b border-border/60 px-4 py-3">
            <span className="text-sm font-semibold">{visible.length} matching stocks</span>
            <div className="ml-auto w-44">
              <Input value={nameFilter} onChange={setNameFilter} placeholder="Filter symbol…" />
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-border/60 bg-muted/30 text-left">
                  <th className="px-4 py-2.5 font-semibold">Symbol</th>
                  {SORT_COLS.map((c) => (
                    <th
                      key={c.key}
                      onClick={() => onSort(c.key)}
                      className="cursor-pointer select-none px-3 py-2.5 text-right font-semibold hover:text-foreground text-muted-foreground"
                    >
                      {c.label}
                      {sortKey === c.key ? (sortDir === -1 ? " ▼" : " ▲") : ""}
                    </th>
                  ))}
                  <th className="px-3 py-2.5 font-semibold text-muted-foreground">Trend</th>
                  <th className="px-3 py-2.5 font-semibold text-muted-foreground">Signals</th>
                  <th className="px-3 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => {
                  const tick = getTick(r.symbol);
                  const ltp = tick?.ltp ?? r.last;
                  return (
                    <tr
                      key={r.symbol}
                      className="border-b border-border/40 last:border-0 hover:bg-primary/[0.03] transition-colors"
                    >
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-1.5">
                          <strong className="font-semibold">{r.symbol.replace("-EQ", "")}</strong>
                          {tick && <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{r.score.toFixed(1)}</td>
                      <td className={cn("px-3 py-2 text-right tabular-nums font-medium transition-colors duration-300",
                        tick?.flash === "up" && "text-emerald-500",
                        tick?.flash === "down" && "text-destructive",
                        !tick?.flash && "text-foreground",
                      )}>
                        {ltp.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums",
                        r.day_chg_pct >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
                      )}>
                        {r.day_chg_pct >= 0 ? "+" : ""}{r.day_chg_pct.toFixed(2)}%
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums",
                        r.ret_1m >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
                      )}>
                        {r.ret_1m >= 0 ? "+" : ""}{r.ret_1m.toFixed(1)}%
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums",
                        r.vs_high > -2 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
                      )}>
                        {r.vs_high.toFixed(1)}%
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums",
                        r.rsi < 30 ? "text-emerald-600 dark:text-emerald-400" : r.rsi > 70 ? "text-amber-500" : ""
                      )}>
                        {r.rsi.toFixed(0)}
                      </td>
                      <td className={cn("px-3 py-2 text-right tabular-nums",
                        r.vol_x >= 2 ? "text-amber-500" : ""
                      )}>
                        {r.vol_x.toFixed(1)}×
                      </td>
                      <td className="px-3 py-2">
                        <span className={cn(
                          "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                          r.trend === "UP" ? "bg-emerald-500/10 text-emerald-500" :
                          r.trend === "DOWN" ? "bg-destructive/10 text-destructive" :
                          "bg-muted text-muted-foreground",
                        )}>
                          {r.trend}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <span className="flex flex-wrap gap-1">
                          {r.breakout && <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-500">BREAKOUT</span>}
                          {r.gold_cross_5d && <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-500">GOLDEN ✕</span>}
                          {r.rsi < 30 && <span className="rounded-full bg-blue-500/10 px-2 py-0.5 text-[10px] font-semibold text-blue-500">OVERSOLD</span>}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            try { sessionStorage.setItem("atr.chartSymbol", r.symbol); } catch { /* */ }
                            onOpenChart?.("charts");
                          }}
                        >
                          Chart
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {result && result.rows.length === 0 && (
        <Hint>No stocks matched your conditions. Try relaxing a threshold.</Hint>
      )}
    </div>
  );
}
