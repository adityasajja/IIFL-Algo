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
  Loader2,
  RefreshCw,
  Settings,
  TrendingDown,
  TrendingUp,
  X,
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
import { Input } from "./components/ui/input";
import { cn } from "./lib/utils";

// ─── Helpers ──────────────────────────────────────────────────────────────────

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
  if (diff < 60) return `${Math.round(diff)}s left`;
  return `${Math.round(diff / 60)}m left`;
}

// ─── Signal card (Pending) ────────────────────────────────────────────────────

function PendingCard({
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
    try {
      await onSkip(sig.id);
    } finally {
      setSkipping(false);
    }
  };

  return (
    <Card
      className={cn(
        "relative overflow-hidden border-l-4 p-5 space-y-4",
        isBuy ? "border-l-emerald-500" : "border-l-destructive",
      )}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span
            className={cn(
              "flex h-9 w-9 items-center justify-center rounded-full text-sm font-bold",
              isBuy
                ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                : "bg-destructive/15 text-destructive",
            )}
          >
            {isBuy ? <TrendingUp className="h-4 w-4" /> : <TrendingDown className="h-4 w-4" />}
          </span>
          <div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => onOpenChart?.(sig.symbol)}
                className="text-base font-bold hover:underline cursor-pointer text-left"
                title="Open chart"
              >
                {sig.symbol.replace("-EQ", "")}
              </button>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[10px] font-bold uppercase",
                  isBuy
                    ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                    : "bg-destructive/10 text-destructive",
                )}
              >
                {sig.action}
              </span>
              <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
                {sig.setup}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">{sig.reason}</p>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-xs text-muted-foreground">{timeAgo(sig.created_at)}</div>
          {sig.expires_at && (
            <div className="text-[11px] text-amber-500">{timeLeft(sig.expires_at)}</div>
          )}
        </div>
      </div>

      {/* Price grid */}
      <div className="grid grid-cols-3 gap-3 rounded-lg bg-muted/30 p-3 text-center">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Entry</div>
          <div className="mt-1 text-base font-bold tabular-nums">₹{fmt(sig.entry_price)}</div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-destructive/80">Stop Loss</div>
          <div className="mt-1 text-base font-bold tabular-nums text-destructive">₹{fmt(sig.stop_loss)}</div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">Target</div>
          <div className="mt-1 text-base font-bold tabular-nums text-emerald-600 dark:text-emerald-400">₹{fmt(sig.target)}</div>
        </div>
      </div>

      {/* Academic Paper & Quant Thesis Badge */}
      {sig.paper_citation && (
        <div className="rounded-lg border border-primary/20 bg-primary/5 p-2.5 text-xs">
          <div className="flex items-center gap-1.5 font-semibold text-primary">
            <BookOpen className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">{sig.paper_citation}</span>
            {sig.expected_value !== null && sig.expected_value !== undefined && (
              <span className="ml-auto rounded bg-primary/15 px-1.5 py-0.5 text-[10px] font-bold text-primary">
                EV +{sig.expected_value}R
              </span>
            )}
          </div>
          {sig.thesis && <p className="mt-1 text-muted-foreground leading-relaxed">{sig.thesis}</p>}
        </div>
      )}

      {/* Meta row */}
      <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
        <span><span className="font-semibold text-foreground">{sig.quantity}</span> shares</span>
        <span>Risk <span className="font-semibold text-destructive">₹{fmt(sig.risk_amount, 0)}</span></span>
        <span>R:R <span className="font-semibold text-foreground">{sig.rr_ratio.toFixed(1)}:1</span></span>
        {sig.confidence_score !== null && sig.confidence_score !== undefined && (
          <span>Confidence <span className="font-semibold text-primary">{Math.round(sig.confidence_score * 100)}%</span></span>
        )}
        {sig.rsi !== null && <span>RSI <span className="font-semibold">{sig.rsi.toFixed(0)}</span></span>}
        {sig.vol_x !== null && <span>Vol <span className="font-semibold">{sig.vol_x.toFixed(1)}×</span></span>}
      </div>

      {err && <ErrorBox>{err}</ErrorBox>}

      {/* Action buttons */}
      <div className="flex items-center gap-2">
        <Button
          onClick={() => void handleExecute()}
          disabled={executing || skipping}
          className={cn(
            "flex-1 gap-2 font-bold",
            isBuy
              ? "bg-emerald-600 hover:bg-emerald-700 text-white"
              : "bg-destructive hover:bg-destructive/90 text-white",
          )}
        >
          {executing ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Check className="h-4 w-4" />
          )}
          {executing ? "Placing orders…" : `Execute ${sig.action}`}
        </Button>
        <Button
          variant="outline"
          onClick={() => void handleSkip()}
          disabled={executing || skipping}
          className="gap-1.5 text-muted-foreground"
        >
          {skipping ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
          Skip
        </Button>
      </div>
    </Card>
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
        <select
          value={settings.stop_method}
          onChange={(e) => onChange({ stop_method: e.target.value as "atr" | "pct" })}
          className="w-full rounded-md border border-border/60 bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="atr">ATR-based</option>
          <option value="pct">Fixed %</option>
        </select>
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
        <select
          value={settings.product}
          onChange={(e) => onChange({ product: e.target.value })}
          className="w-full rounded-md border border-border/60 bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="CNC">CNC (Delivery)</option>
          <option value="MIS">MIS (Intraday)</option>
        </select>
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

  const refresh = useCallback(async () => {
    try {
      const [sq, ss, ls] = await Promise.all([
        getTradeSignals(),
        getTradeSignalSettings(),
        getSelfLearningStatus().catch(() => null),
      ]);
      setSignals(sq.signals);
      setSettings(ss);
      setLocalSettings(ss);
      if (ls) setLearningStatus(ls);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 10_000);
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
    try {
      const saved = await saveTradeSignalSettings(localSettings);
      setSettings(saved);
    } finally {
      setSavingSettings(false);
    }
  };

  const pending  = signals.filter((s) => s.status === "PENDING");
  const active   = signals.filter((s) => s.status === "ACTIVE");
  const history  = signals.filter((s) => s.status === "DONE" || s.status === "SKIPPED");

  const maxRisk = settings ? ((settings.capital * settings.risk_per_trade_pct) / 100) : 0;

  return (
    <div className="space-y-5">

      {/* ── Top bar ─────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Capital summary pills */}
        {settings && (
          <div className="flex items-center gap-2 text-xs">
            <span className="rounded-full border border-border/60 bg-muted/30 px-3 py-1 font-medium">
              Capital ₹{(settings.capital / 1e5).toFixed(1)}L
            </span>
            <span className="rounded-full border border-border/60 bg-muted/30 px-3 py-1 font-medium">
              Risk ₹{fmt(maxRisk, 0)}/trade
            </span>
            <span className="rounded-full border border-border/60 bg-muted/30 px-3 py-1 font-medium">
              {settings.stop_method === "atr" ? `SL ${settings.stop_atr_mult}×ATR` : `SL ${settings.stop_pct}%`}
            </span>
            <span className="rounded-full border border-border/60 bg-muted/30 px-3 py-1 font-medium">
              R:R {settings.rr_ratio}:1
            </span>
          </div>
        )}
        <div className="ml-auto flex items-center gap-2">
          {learningStatus && (
            <div className="hidden sm:flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs">
              <BrainCircuit className="h-3.5 w-3.5 text-primary" />
              <span className="font-semibold text-primary">
                {learningStatus.market_regime.regime.replace("_", " ")}
              </span>
              <span className="text-muted-foreground">
                ({learningStatus.market_regime.breadth_pct}% breadth)
              </span>
            </div>
          )}
          <Button
            variant="outline"
            onClick={() => void handleTrain()}
            disabled={training}
            className="gap-1.5 border-primary/40 text-primary hover:bg-primary/10"
            title="Retrain AI quant models across 2,654+ stock history"
          >
            {training ? <Loader2 className="h-4 w-4 animate-spin" /> : <BrainCircuit className="h-4 w-4" />}
            {training ? "Training AI…" : "Retrain Models"}
          </Button>
          <Button
            onClick={() => void handleScan()}
            disabled={scanning}
            className="gap-2"
          >
            {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            {scanning ? "Scanning…" : "Scan now"}
          </Button>
          <Button
            variant="outline"
            onClick={() => setShowSettings((v) => !v)}
            className="gap-1.5"
          >
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
                {savingSettings ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                Save
              </Button>
            </div>
          </div>
          <SettingsPanel
            settings={localSettings}
            onChange={(patch) => setLocalSettings((s) => s ? { ...s, ...patch } : s)}
          />
          <p className="text-xs text-muted-foreground">
            Stop-loss and target are calculated automatically at scan time.
            Changes only affect new signals.
          </p>
        </Card>
      )}

      {/* ── Pending signals ─────────────────────────────────────────────── */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Pending signals</span>
          {pending.length > 0 && (
            <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full bg-primary px-1.5 text-[11px] font-bold text-primary-foreground">
              {pending.length}
            </span>
          )}
        </div>
        {pending.length === 0 ? (
          <Hint>
            No pending signals. Click <strong>Scan now</strong> to look for setups, or the Intelligent Monitor will surface them automatically.
          </Hint>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {pending.map((s) => (
              <PendingCard
                key={s.id}
                sig={s}
                onExecute={handleExecute}
                onSkip={handleSkip}
                onOpenChart={onOpenChart}
              />
            ))}
          </div>
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
