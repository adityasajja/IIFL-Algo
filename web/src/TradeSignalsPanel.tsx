/**
 * TradeSignalsPanel — Semi-automatic trading command centre.
 *
 * Layout:
 *   Settings bar  (capital, risk %, stop method)
 *   ─────────────────────────────────────────────
 *   Pending signals  (big cards — Execute / Skip)
 *   ─────────────────────────────────────────────
 *   Active positions (signals that are in-flight)
 *   ─────────────────────────────────────────────
 *   History          (done / skipped, collapsed)
 */
import {
  AlertTriangle,
  BookOpen,
  BrainCircuit,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  Settings,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  executeTradeSignal,
  getSelfLearningStatus,
  getTradeSignalSettings,
  getTradeSignals,
  saveTradeSignalSettings,
  scanTradeSignals,
  skipTradeSignal,
  trainSelfLearningModel,
  type SelfLearningStatus,
  type TradeSignal,
  type TradeSignalSettings,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox, Hint } from "./components/ui/card";
import { ButtonLoader } from "./components/ui/loading";
import { Input } from "./components/ui/input";
import { Select } from "./components/ui/select";
import { humanizeSentence } from "./lib/format";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** `BEAR_TREND` -> "Falling market". The model's regime names are internal. */
function marketWord(regime: string): string {
  const r = regime.toUpperCase();
  if (r.includes("BEAR")) return "Falling market";
  if (r.includes("BULL")) return "Rising market";
  if (r.includes("SIDE") || r.includes("RANGE")) return "Sideways market";
  return `${humanizeSentence(regime)} market`;
}

function fmt(n: number, decimals = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

function timeLeft(iso: string | null): string {
  if (!iso) return "";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  if (diff <= 0) return "Expired";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))}m left`;
  return `${Math.round(diff / 3600)}h left`;
}

// ─── The trade plan on one bar: stop, entry, target ───────────────────────────

/** Risk (red) and reward (green) either side of the entry, in proportion. */
function PlanBar({ sig }: { sig: TradeSignal }) {
  const prices = [sig.stop_loss, sig.entry_price, sig.target];
  const lo = Math.min(...prices);
  const span = Math.max(...prices) - lo || 1;
  const x = (p: number) => ((p - lo) / span) * 100;
  const seg = (a: number, b: number) => ({ left: `${x(Math.min(a, b))}%`, width: `${Math.abs(x(a) - x(b))}%` });
  const labels = [
    { k: "Stop", p: sig.stop_loss, cls: "text-destructive" },
    { k: "Entry", p: sig.entry_price, cls: "text-foreground" },
    { k: "Target", p: sig.target, cls: "text-emerald-500" },
  ].sort((a, b) => a.p - b.p);

  return (
    <div>
      <div className="relative h-2 rounded-full bg-muted">
        <div className="absolute inset-y-0 rounded-full bg-rose-500/60" style={seg(sig.stop_loss, sig.entry_price)} />
        <div className="absolute inset-y-0 rounded-full bg-emerald-500/60" style={seg(sig.entry_price, sig.target)} />
        <div
          className="absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background bg-foreground"
          style={{ left: `${x(sig.entry_price)}%` }}
        />
      </div>
      <div className="mt-1.5 flex justify-between text-[11px] tabular-nums">
        {labels.map((l) => (
          <span key={l.k} className={l.cls}>
            {l.k} ₹{fmt(l.p)}
          </span>
        ))}
      </div>
    </div>
  );
}

// ─── Pending signal row ───────────────────────────────────────────────────────

function PendingRow({
  sig,
  onExecute,
  onSkip,
  onOpenChart,
}: {
  sig: TradeSignal;
  onExecute: (id: string) => Promise<void>;
  onSkip: (id: string) => Promise<void>;
  onOpenChart?: (symbol: string) => void;
}) {
  const [executing, setExecuting] = useState(false);
  const [skipping, setSkipping] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const isBuy = sig.action === "BUY";

  const handleExecute = async () => {
    setExecuting(true);
    setErr(null);
    try {
      await onExecute(sig.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setExecuting(false);
    }
  };

  const handleSkip = async () => {
    setSkipping(true);
    setErr(null);
    try {
      await onSkip(sig.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSkipping(false);
    }
  };

  const hasDetails = !!(sig.thesis || sig.paper_citation || sig.rsi !== null || sig.vol_x !== null);

  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0 flex-1 basis-56">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-[11px] font-semibold",
                isBuy ? "bg-emerald-500/10 text-emerald-500" : "bg-destructive/10 text-destructive",
              )}
            >
              {isBuy ? "Buy" : "Sell"}
            </span>
            <button
              type="button"
              onClick={() => onOpenChart?.(sig.symbol)}
              className="text-base font-semibold hover:underline"
              title="Open chart"
            >
              {sig.symbol.replace("-EQ", "")}
            </button>
            <span className="text-xs text-muted-foreground">{sig.setup}</span>
          </div>
          <div className="mt-1 truncate text-xs text-muted-foreground" title={sig.reason}>
            {sig.reason}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={() => void handleSkip()} disabled={executing || skipping} className="text-muted-foreground">
            {skipping ? <ButtonLoader /> : null}
            Skip
          </Button>
          <Button
            size="sm"
            onClick={() => void handleExecute()}
            disabled={executing || skipping}
            title="Places the entry, stop-loss and target orders together"
            className={cn("gap-1.5 text-white", isBuy ? "bg-emerald-600 hover:bg-emerald-700" : "bg-destructive hover:bg-destructive/90")}
          >
            {executing ? <ButtonLoader size={14} /> : <Check className="h-3.5 w-3.5" />}
            {executing ? "Placing…" : isBuy ? "Buy" : "Sell"}
          </Button>
        </div>
      </div>

      <div className="mt-3">
        <PlanBar sig={sig} />
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>
          <span className="font-medium text-foreground">{sig.quantity}</span> shares
        </span>
        <span>
          Risking <span className="font-medium text-destructive">₹{fmt(sig.risk_amount, 0)}</span>
        </span>
        <span>
          Reward is <span className="font-medium text-emerald-500">{sig.rr_ratio.toFixed(1)}x</span> the risk
        </span>
        <span className="ml-auto flex items-center gap-3">
          <span>{timeAgo(sig.created_at)}</span>
          {sig.expires_at ? <span className="text-amber-500">{timeLeft(sig.expires_at)}</span> : null}
          {hasDetails ? (
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-label={open ? "Hide details" : "Show details"}
              className="grid size-6 place-items-center rounded-full hover:bg-muted hover:text-foreground"
            >
              <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
            </button>
          ) : null}
        </span>
      </div>

      {open && (
        <div className="mt-3 space-y-2 rounded-xl bg-muted/30 p-3 text-xs">
          {sig.paper_citation ? (
            <div className="flex items-center gap-1.5 font-medium text-primary">
              <BookOpen className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{sig.paper_citation}</span>
            </div>
          ) : null}
          {sig.thesis ? <p className="leading-relaxed text-muted-foreground">{sig.thesis}</p> : null}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            {sig.confidence_score !== null && sig.confidence_score !== undefined && (
              <span>Confidence <span className="font-medium text-foreground">{Math.round(sig.confidence_score * 100)}%</span></span>
            )}
            {sig.rsi !== null && <span>RSI <span className="font-medium text-foreground">{sig.rsi.toFixed(0)}</span></span>}
            {sig.vol_x !== null && <span>Volume <span className="font-medium text-foreground">{sig.vol_x.toFixed(1)}x</span></span>}
          </div>
        </div>
      )}

      {err && <div className="mt-3"><ErrorBox>{err}</ErrorBox></div>}
    </div>
  );
}

// ─── Active signal row ────────────────────────────────────────────────────────

function ActiveRow({ sig }: { sig: TradeSignal }) {
  const isBuy = sig.action === "BUY";
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/60 bg-muted/10 px-4 py-3">
      <span className={cn(
        "h-2 w-2 rounded-full",
        isBuy ? "bg-emerald-500" : "bg-destructive",
      )} />
      <span className="font-semibold">{sig.symbol.replace("-EQ", "")}</span>
      <span className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-bold",
        isBuy ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" : "bg-destructive/10 text-destructive",
      )}>
        {sig.action}
      </span>
      <span className="text-xs text-muted-foreground">{sig.setup}</span>
      <span className="ml-auto flex items-center gap-4 text-xs tabular-nums">
        <span>Entry <strong>₹{fmt(sig.entry_price)}</strong></span>
        <span className="text-destructive">SL ₹{fmt(sig.stop_loss)}</span>
        <span className="text-emerald-600 dark:text-emerald-400">Target ₹{fmt(sig.target)}</span>
        <span className="text-muted-foreground">{sig.quantity} shares</span>
      </span>
    </div>
  );
}

// ─── Settings panel ───────────────────────────────────────────────────────────

function SettingsPanel({
  settings,
  onChange,
}: {
  settings: TradeSignalSettings;
  onChange: (s: Partial<TradeSignalSettings>) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Capital (₹)
        </label>
        <Input
          type="number"
          value={String(settings.capital)}
          onChange={(v) => onChange({ capital: Number(v) })}
          className="tabular-nums"
        />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Risk / trade %
        </label>
        <Input
          type="number"
          value={String(settings.risk_per_trade_pct)}
          step={0.1}
          onChange={(v) => onChange({ risk_per_trade_pct: Number(v) })}
          className="tabular-nums"
        />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Stop method
        </label>
        <Select
          value={settings.stop_method}
          onChange={(v) => onChange({ stop_method: v as "atr" | "pct" })}
          options={[
            { value: "atr", label: "ATR-based" },
            { value: "pct", label: "Fixed %" },
          ]}
          className="w-full"
        />
      </div>
      {settings.stop_method === "atr" ? (
        <div>
          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            ATR multiplier
          </label>
          <Input
            type="number"
            value={String(settings.stop_atr_mult)}
            step={0.1}
            onChange={(v) => onChange({ stop_atr_mult: Number(v) })}
            className="tabular-nums"
          />
        </div>
      ) : (
        <div>
          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Stop loss %
          </label>
          <Input
            type="number"
            value={String(settings.stop_pct)}
            step={0.1}
            onChange={(v) => onChange({ stop_pct: Number(v) })}
            className="tabular-nums"
          />
        </div>
      )}
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          R:R ratio
        </label>
        <Input
          type="number"
          value={String(settings.rr_ratio)}
          step={0.1}
          onChange={(v) => onChange({ rr_ratio: Number(v) })}
          className="tabular-nums"
        />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Max active trades
        </label>
        <Input
          type="number"
          value={String(settings.max_active)}
          onChange={(v) => onChange({ max_active: Number(v) })}
          className="tabular-nums"
        />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
          Product
        </label>
        <Select
          value={settings.product}
          onChange={(v) => onChange({ product: v })}
          options={[
            { value: "CNC", label: "CNC (Delivery)" },
            { value: "MIS", label: "MIS (Intraday)" },
          ]}
          className="w-full"
        />
      </div>
    </div>
  );
}

// ─── Main panel ───────────────────────────────────────────────────────────────

export default function TradeSignalsPanel({
  onOpenChart,
}: {
  onOpenChart?: (symbol: string) => void;
} = {}) {
  const [signals, setSignals] = useState<TradeSignal[]>([]);
  const [settings, setSettings] = useState<TradeSignalSettings | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastScan, setLastScan] = useState<string | null>(null);
  const [localSettings, setLocalSettings] = useState<TradeSignalSettings | null>(null);
  const [learningStatus, setLearningStatus] = useState<SelfLearningStatus | null>(null);
  const [training, setTraining] = useState(false);
  const [sideFilter, setSideFilter] = useState<"ALL" | "BUY" | "SELL">("ALL");
  const [showAllPending, setShowAllPending] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [sq, ss, ls] = await Promise.all([
        getTradeSignals(),
        getTradeSignalSettings(),
        getSelfLearningStatus().catch(() => null),
      ]);
      setSignals(sq.signals);
      // Seed the edit copy only when it is untouched; the poll must not wipe
      // whatever the user is mid-way through typing into the settings form.
      setSettings(ss);
      setLocalSettings((cur) => (cur === null ? ss : cur));
      if (ls) setLearningStatus(ls);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setVisibleInterval(() => void refresh(), 10_000);
    return () => clearInterval(t);
  }, [refresh]);

  const handleTrain = async () => {
    setTraining(true);
    setError(null);
    try {
      await trainSelfLearningModel();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTraining(false);
    }
  };

  const handleScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const res = await scanTradeSignals();
      setLastScan(`Scanned ${res.scanned} symbols — ${res.new_signals} new signal${res.new_signals !== 1 ? "s" : ""}`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  };

  const handleExecute = async (id: string) => {
    await executeTradeSignal(id);
    await refresh();
  };

  const handleSkip = async (id: string) => {
    await skipTradeSignal(id);
    await refresh();
  };

  const handleSaveSettings = async () => {
    if (!localSettings) return;
    setSavingSettings(true);
    setError(null);
    try {
      const saved = await saveTradeSignalSettings(localSettings);
      setSettings(saved);
      setLocalSettings(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingSettings(false);
    }
  };

  const pending  = signals.filter((s) => s.status === "PENDING");
  const buyCount = pending.filter((s) => s.action === "BUY").length;
  const sellCount = pending.length - buyCount;
  const filteredPending = sideFilter === "ALL" ? pending : pending.filter((s) => s.action === sideFilter);
  const visiblePending = showAllPending ? filteredPending : filteredPending.slice(0, 8);
  const active   = signals.filter((s) => s.status === "ACTIVE");
  const history  = signals.filter((s) => s.status === "DONE" || s.status === "SKIPPED");

  const maxRisk = settings ? ((settings.capital * settings.risk_per_trade_pct) / 100) : 0;

  return (
    <div className="space-y-5">

      {/* ── Top bar ─────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3">
        {settings && (
          <button
            type="button"
            onClick={() => setShowSettings((v) => !v)}
            title="Change position sizing and risk"
            className="text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            ₹{(settings.capital / 1e5).toFixed(1)}L capital · risking ₹{fmt(maxRisk, 0)} a trade
          </button>
        )}
        <div className="ml-auto flex items-center gap-2">
          {learningStatus && (
            <span
              title={`${learningStatus.market_regime.breadth_pct}% of stocks are above their average`}
              className="hidden items-center gap-1.5 rounded-full bg-muted px-3 py-1.5 text-xs text-muted-foreground sm:inline-flex"
            >
              {marketWord(learningStatus.market_regime.regime)}
            </span>
          )}
          <Button onClick={() => void handleScan()} disabled={scanning} className="gap-2">
            {scanning ? <ButtonLoader size={16} /> : <RefreshCw className="h-4 w-4" />}
            {scanning ? "Scanning…" : "Scan now"}
          </Button>
          <Button variant="outline" onClick={() => setShowSettings((v) => !v)} className="gap-1.5" aria-label="Settings">
            <Settings className="h-4 w-4" />
            Settings
          </Button>
        </div>
      </div>

      {lastScan && <div className="text-xs text-muted-foreground">{lastScan}</div>}
      {error && <ErrorBox>{error}</ErrorBox>}

      {/* ── Settings panel ─────────────────────────────────────────────── */}
      {showSettings && localSettings && (
        <Card className="p-5 space-y-4">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold">Position sizing & risk</span>
            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={() => void handleSaveSettings()}
                disabled={savingSettings}
                className="gap-1.5"
              >
                {savingSettings ? <ButtonLoader /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                Save
              </Button>
            </div>
          </div>
          <SettingsPanel
            settings={localSettings}
            onChange={(patch) => setLocalSettings((s) => s ? { ...s, ...patch } : s)}
          />
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
            <span className="text-xs text-muted-foreground">Changes apply to new signals only.</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void handleTrain()}
              disabled={training}
              title="Retrain the signal model on the stock history"
              className="gap-1.5"
            >
              {training ? <ButtonLoader size={14} /> : <BrainCircuit className="h-3.5 w-3.5" />}
              {training ? "Retraining…" : "Retrain model"}
            </Button>
          </div>
        </Card>
      )}

      {/* ── Pending signals ─────────────────────────────────────────────── */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">Waiting for you</span>
            {pending.length > 0 && (
              <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full bg-primary px-1.5 text-[11px] font-bold text-primary-foreground">
                {pending.length}
              </span>
            )}
          </div>
          {buyCount > 0 && sellCount > 0 ? (
            <div className="flex gap-2">
              {([
                ["ALL", `All ${pending.length}`],
                ["BUY", `Buy ${buyCount}`],
                ["SELL", `Sell ${sellCount}`],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSideFilter(key)}
                  aria-pressed={sideFilter === key}
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs transition-colors",
                    sideFilter === key
                      ? "border-primary/40 bg-primary/10 text-foreground"
                      : "border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
        {pending.length === 0 ? (
          <Hint>Nothing waiting. Press Scan now to look for setups.</Hint>
        ) : (
          <>
            <div className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card">
              {visiblePending.map((s) => (
                <PendingRow
                  key={s.id}
                  sig={s}
                  onExecute={handleExecute}
                  onSkip={handleSkip}
                  onOpenChart={onOpenChart}
                />
              ))}
            </div>
            {filteredPending.length > 8 ? (
              <button
                type="button"
                onClick={() => setShowAllPending((v) => !v)}
                className="mx-auto block rounded-full border border-border px-4 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                {showAllPending ? "Show fewer" : `Show ${filteredPending.length - 8} more`}
              </button>
            ) : null}
          </>
        )}
      </div>

      {/* ── Active trades ───────────────────────────────────────────────── */}
      {active.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">Active trades</span>
            <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full bg-emerald-500 px-1.5 text-[11px] font-bold text-white">
              {active.length}
            </span>
            <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
            <span className="text-xs text-muted-foreground">
              Stop-loss and target orders are live in IIFL
            </span>
          </div>
          <div className="space-y-2">
            {active.map((s) => <ActiveRow key={s.id} sig={s} />)}
          </div>
        </div>
      )}

      {/* ── History ─────────────────────────────────────────────────────── */}
      {history.length > 0 && (
        <div className="space-y-2">
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className="flex items-center gap-2 text-sm font-semibold text-muted-foreground hover:text-foreground"
          >
            {showHistory ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            History ({history.length})
          </button>
          {showHistory && (
            <div className="space-y-1">
              {history.slice(0, 30).map((s) => (
                <div
                  key={s.id}
                  className="flex flex-wrap items-center gap-3 rounded-lg border border-border/40 px-3 py-2 text-xs text-muted-foreground"
                >
                  <span
                    className={cn(
                      "rounded-full px-2 py-0.5 font-semibold",
                      s.status === "DONE" ? "bg-emerald-500/10 text-emerald-600" : "bg-muted text-muted-foreground",
                    )}
                  >
                    {s.status}
                  </span>
                  <span className="font-medium text-foreground">{s.symbol.replace("-EQ", "")}</span>
                  <span>{s.action}</span>
                  <span>{s.setup}</span>
                  <span className="ml-auto">{timeAgo(s.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
