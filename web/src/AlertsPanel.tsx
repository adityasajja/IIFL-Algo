import { useEffect, useState } from "react";
import {
  armAlertRule,
  createAlertRule,
  deleteAlertRule,
  evaluateIntelligentAlertsNow,
  getAlertEvents,
  getAlertRules,
  getIntelligentAlerts,
  runAlertCheck,
  sendAlertTest,
  updateIntelligentAlerts,
  type AlertEvent,
  type AlertRule,
  type IntelligentAlertConfig,
  type IntelligentStatus,
} from "./api";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";
import { Input } from "./components/motion/input";
import { Select } from "./components/ui/select";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/motion/switch";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Bell,
  Clock,
  Layers,
  Play,
  RotateCw,
  Send,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { Chip } from "./components/ui/chip";

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

const THRESHOLD_KINDS = new Set([
  "price_above", "price_below", "day_drop_pct", "day_gain_pct", "rsi_below", "rsi_above",
]);

export default function AlertsPanel({ onOpenChart }: { onOpenChart?: (tab: string) => void }) {
  const { toast } = useToast();

  // Manual rules & events
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [checkState, setCheckState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("RELIANCE-EQ");
  const [kind, setKind] = useState("price_below");
  const [threshold, setThreshold] = useState("1250");

  // Intelligent Monitor State
  const [intelConfig, setIntelConfig] = useState<IntelligentAlertConfig | null>(null);
  const [intelStatus, setIntelStatus] = useState<IntelligentStatus | null>(null);
  const [evaluating, setEvaluating] = useState(false);

  async function refresh() {
    try {
      setError(null);
      const [r, ev, intel] = await Promise.all([
        getAlertRules(),
        getAlertEvents(),
        getIntelligentAlerts(),
      ]);
      setRules(r);
      setEvents(ev);
      setIntelConfig(intel.config);
      setIntelStatus(intel.status);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function openChart(sym: string) {
    try {
      sessionStorage.setItem("atr.chartSymbol", sym);
    } catch {
      /* ignore */
    }
    onOpenChart?.("charts");
  }

  async function updateConfigField<K extends keyof IntelligentAlertConfig>(
    key: K,
    val: IntelligentAlertConfig[K]
  ) {
    if (!intelConfig) return;
    const updated = { ...intelConfig, [key]: val };
    setIntelConfig(updated);
    try {
      const res = await updateIntelligentAlerts({ [key]: val });
      setIntelConfig(res.config);
      setIntelStatus(res.status);
      toast({
        title: "Rule Updated",
        description: `Saved: ${String(key).replace(/_/g, " ")}`,
        status: "success",
      });
    } catch (e) {
      toast({
        title: "Update Failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    }
  }

  async function handleEvaluateNow() {
    setEvaluating(true);
    try {
      const res = await evaluateIntelligentAlertsNow();
      setIntelStatus(res.status);
      toast({
        title: res.count > 0 ? `🚨 ${res.count} Signal(s) Triggered!` : "Evaluation Finished",
        description:
          res.count > 0
            ? "Sent to Telegram"
            : "All clear",
        status: res.count > 0 ? "success" : "info",
      });
      await refresh();
    } catch (e) {
      toast({
        title: "Evaluation Failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setEvaluating(false);
    }
  }

  async function createManual() {
    setError(null);
    try {
      await createAlertRule({
        symbol: symbol.trim().toUpperCase(),
        kind,
        threshold: Number(threshold) || 0,
      });
      toast({
        title: "Alert created",
        description: `${symbol.trim().toUpperCase()} · ${kind.replace(/_/g, " ")}`,
        status: "success",
      });
      await refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      toast({ title: "Could not create alert", description: msg, status: "error" });
    }
  }

  async function toggle(r: AlertRule) {
    try {
      await armAlertRule(r.id, !r.armed);
      await refresh();
    } catch (e) {
      toast({ title: "Toggle failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function remove(id: string) {
    try {
      await deleteAlertRule(id);
      await refresh();
    } catch (e) {
      toast({ title: "Delete failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function checkManualNow() {
    setCheckState("loading");
    try {
      const res = await runAlertCheck();
      setCheckState("success");
      toast({
        title: res.fired.length === 0 ? "Checked — nothing firing" : `${res.fired.length} alert(s) fired`,
        description: res.market_open ? "Market is open" : "Market is closed",
        status: res.fired.length === 0 ? "info" : "success",
      });
      await refresh();
    } catch (e) {
      setCheckState("error");
      toast({ title: "Check failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function testTelegram() {
    try {
      const res = await sendAlertTest();
      toast({
        title:
          res.sent_on === "none" || res.sent_on === "log"
            ? "No Telegram configured"
            : `Test sent via ${res.sent_on}`,
        description:
          res.sent_on === "none" || res.sent_on === "log"
            ? "Telegram not set up"
            : "Check your Telegram channel/chat.",
        status: res.sent_on === "none" || res.sent_on === "log" ? "info" : "success",
      });
    } catch (e) {
      toast({ title: "Test failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  const kindInfo = KINDS.find((k) => k.v === kind);

  return (
    <div className="space-y-4 pb-12">
      {/* ------------------------------------------------------------- */}
      {/* 1. INTELLIGENT TRIGGER ENGINE BANNER & CONTROL BAR */}
      {/* ------------------------------------------------------------- */}
      <Card padding="md">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-md bg-gradient-to-br from-indigo-500 to-purple-600 text-foreground -indigo-500/20">
              <Sparkles size={22} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-semibold text-foreground tracking-wide" title="Watches your stocks and messages you on Telegram when something needs attention.">
                  Smart alerts
                </h2>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-caption font-semibold uppercase tracking-wider",
                    intelConfig?.enabled
                      ? "border border-gain/20 bg-gain/[0.08] text-gain"
                      : "bg-muted text-muted-foreground border border-border"
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      intelConfig?.enabled ? "bg-gain animate-pulse" : "bg-muted-foreground/60"
                    )}
                  />
                  {intelConfig?.enabled ? "On" : "Paused"}
                </span>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="quiet"
              size="xs"
              onClick={() => void testTelegram()}
            >
              <Send size={13} className="mr-1.5 text-blue-400" />
              Send test message
            </Button>
            <Button
              size="xs"
              disabled={evaluating}
              onClick={() => void handleEvaluateNow()}
            >
              {evaluating ? (
                <>
                  <RotateCw size={13} className="mr-1.5 animate-spin" />
                  Checking…
                </>
              ) : (
                <>
                  <Play size={13} className="mr-1.5 fill-current" />
                  Check now
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Engine Config Strip */}
        {intelConfig && (
          <div className="mt-5 grid grid-cols-1 gap-3 border-t border-border/80 pt-4 sm:grid-cols-3">
            {/* Master Switch */}
            <div className="flex items-center justify-between rounded-md bg-card/70 p-3 border border-border/60">
              <div>
                <div className="text-xs font-semibold text-foreground" title="During market hours">Run automatically</div>
              </div>
              <Switch
                checked={intelConfig.enabled}
                onCheckedChange={(checked) => void updateConfigField("enabled", checked)}
              />
            </div>

            {/* Universe Selector */}
            <div className="flex flex-col gap-1.5 rounded-md bg-card/70 p-3 border border-border/60">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground">
                <span className="flex items-center gap-1.5">
                  <Layers size={13} className="text-primary" /> Watch
                </span>
              </div>
              <div className="flex gap-1 pt-1">
                {(["both", "holdings", "watchlist"] as const).map((u) => (
                  <Chip
                    key={u}
                    selected={intelConfig.universe === u}
                    onClick={() => void updateConfigField("universe", u)}
                    className="flex-1 capitalize"
                  >
                    {u === "both" ? "Both" : u}
                  </Chip>
                ))}
              </div>
            </div>

            {/* Check Frequency */}
            <div className="flex flex-col gap-1.5 rounded-md bg-card/70 p-3 border border-border/60">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground">
                <span className="flex items-center gap-1.5">
                  <Clock size={13} className="text-gain" /> Check every
                </span>
                <span className="text-caption text-muted-foreground">Every {intelConfig.interval_min}m</span>
              </div>
              <div className="flex gap-1 pt-1">
                {[1, 3, 5, 15].map((m) => (
                  <Chip
                    key={m}
                    selected={intelConfig.interval_min === m}
                    onClick={() => void updateConfigField("interval_min", m)}
                    className="flex-1"
                  >
                    {m}m
                  </Chip>
                ))}
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------- */}
      {/* 2. QUANTITATIVE RULES: SELL (EXITS) & BUY (ENTRIES) */}
      {/* ------------------------------------------------------------- */}
      {intelConfig && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* SELL (EXIT) TRIGGERS */}
          <Card className="border-border bg-background p-4">
            <div className="flex items-center gap-2 border-b border-border pb-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-md border border-destructive/20 bg-destructive/[0.08] text-destructive">
                <TrendingDown size={16} />
              </div>
              <div>
                <h3 className="text-sm font-semibold text-foreground">When to sell</h3>
                              </div>
            </div>

            <div className="mt-3 divide-y divide-border/60">
              {/* Trend Breakdown (SMA 20) */}
              <div className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-xs font-semibold text-foreground" title="The trend may be turning down">Falls below its 20-day average</div>
                </div>
                <Switch
                  checked={intelConfig.sell_sma_breakdown}
                  onCheckedChange={(c) => void updateConfigField("sell_sma_breakdown", c)}
                />
              </div>

              {/* RSI Overbought Reversal */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="Price has run up hard and may pull back">Looks overbought</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">RSI ≥</span>
                  <input
                    type="number"
                    value={intelConfig.sell_rsi_threshold}
                    onChange={(e) => void updateConfigField("sell_rsi_threshold", Number(e.target.value) || 75)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.sell_rsi_overbought}
                    onCheckedChange={(c) => void updateConfigField("sell_rsi_overbought", c)}
                  />
                </div>
              </div>

              {/* Trailing Stop Drop from Peak */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="Falls this much from its 20-day high">Drops from its recent high</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">Drop %</span>
                  <input
                    type="number"
                    value={intelConfig.sell_trailing_stop_pct}
                    onChange={(e) => void updateConfigField("sell_trailing_stop_pct", Number(e.target.value) || 3)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.sell_trailing_stop_enabled}
                    onCheckedChange={(c) => void updateConfigField("sell_trailing_stop_enabled", c)}
                  />
                </div>
              </div>

              {/* Take-Profit Target */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="A holding is up this much">Profit target reached</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">+%</span>
                  <input
                    type="number"
                    value={intelConfig.sell_take_profit_pct}
                    onChange={(e) => void updateConfigField("sell_take_profit_pct", Number(e.target.value) || 8)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.sell_take_profit_enabled}
                    onCheckedChange={(c) => void updateConfigField("sell_take_profit_enabled", c)}
                  />
                </div>
              </div>

              {/* Stop-Loss Cut */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="A holding is down this much">Loss limit reached</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">-%</span>
                  <input
                    type="number"
                    value={intelConfig.sell_stop_loss_pct}
                    onChange={(e) => void updateConfigField("sell_stop_loss_pct", Number(e.target.value) || 4)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.sell_stop_loss_enabled}
                    onCheckedChange={(c) => void updateConfigField("sell_stop_loss_enabled", c)}
                  />
                </div>
              </div>

              {/* Capital Rotation: a winner that stopped moving */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                <div className="text-xs font-semibold text-foreground" title="Up, then flat for a while — names something else moving now to rotate into instead">Gone quiet after a run-up</div>
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    checked={intelConfig.sell_stall_enabled}
                    onCheckedChange={(c) => void updateConfigField("sell_stall_enabled", c)}
                  />
                </div>
              </div>
              {intelConfig.sell_stall_enabled ? (
                <div className="flex flex-wrap items-center gap-x-5 gap-y-2 pb-2.5 pl-0.5 text-caption text-muted-foreground">
                  <label className="flex items-center gap-1.5">
                    Up at least
                    <input
                      type="number"
                      value={intelConfig.sell_stall_min_gain_pct}
                      onChange={(e) => void updateConfigField("sell_stall_min_gain_pct", Number(e.target.value) || 5)}
                      className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                    />
                    %
                  </label>
                  <label className="flex items-center gap-1.5">
                    flat for
                    <input
                      type="number"
                      value={intelConfig.sell_stall_days}
                      onChange={(e) => void updateConfigField("sell_stall_days", Number(e.target.value) || 10)}
                      className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                    />
                    sessions
                  </label>
                  <label className="flex items-center gap-1.5">
                    (range under
                    <input
                      type="number"
                      step="0.1"
                      value={intelConfig.sell_stall_atr_mult}
                      onChange={(e) => void updateConfigField("sell_stall_atr_mult", Number(e.target.value) || 1.5)}
                      className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                    />
                    x its own ATR)
                  </label>
                </div>
              ) : null}
            </div>
          </Card>

          {/* BUY (ENTRY) TRIGGERS */}
          <Card className="border-border bg-background p-4">
            <div className="flex items-center gap-2 border-b border-border pb-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gain/20 text-gain">
                <TrendingUp size={16} />
              </div>
              <div>
                <h3 className="text-sm font-semibold text-foreground">When to buy</h3>
                              </div>
            </div>

            <div className="mt-3 divide-y divide-border/60">
              {/* Golden Cross */}
              <div className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-xs font-semibold text-foreground" title="The 20-day average just moved above the 50-day">Short-term average crosses above long-term</div>
                </div>
                <Switch
                  checked={intelConfig.buy_golden_cross}
                  onCheckedChange={(c) => void updateConfigField("buy_golden_cross", c)}
                />
              </div>

              {/* Oversold Dip Bounce */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="Price has dropped hard and may bounce">Looks oversold</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-caption text-muted-foreground">RSI ≤</span>
                  <input
                    type="number"
                    value={intelConfig.buy_rsi_threshold}
                    onChange={(e) => void updateConfigField("buy_rsi_threshold", Number(e.target.value) || 32)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.buy_rsi_oversold}
                    onCheckedChange={(c) => void updateConfigField("buy_rsi_oversold", c)}
                  />
                </div>
              </div>

              {/* Breakout with Volume */}
              <div className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-xs font-semibold text-foreground" title="Hits a 20-day high on unusually heavy volume">Breakout on heavy trading</div>
                </div>
                <Switch
                  checked={intelConfig.buy_breakout_vol}
                  onCheckedChange={(c) => void updateConfigField("buy_breakout_vol", c)}
                />
              </div>

              {/* Alert Cooldown */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground" title="Avoids repeat messages about the same stock">Don't repeat within</div>
                </div>
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    value={intelConfig.cooldown_min}
                    onChange={(e) => void updateConfigField("cooldown_min", Number(e.target.value) || 45)}
                    className="h-7 w-14 rounded-xl border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <span className="text-caption text-muted-foreground">min</span>
                </div>
              </div>
            </div>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------------------- */}
      {/* 3. RECENT INTELLIGENT SIGNALS & DISPATCH HISTORY */}
      {/* ------------------------------------------------------------- */}
      <Card className="border-border bg-background p-4">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <Activity size={16} className="text-primary" />
            <h3 className="text-sm font-semibold text-foreground">Recent alerts</h3>
          </div>
          <span className="text-caption text-muted-foreground">
            Last evaluated: {intelStatus?.last_run ? <RelativeTime value={intelStatus.last_run} /> : "Ready"}
          </span>
        </div>

        <div className="mt-3 divide-y divide-border/50">
          {intelStatus?.recent_signals && intelStatus.recent_signals.length > 0 ? (
            intelStatus.recent_signals.slice(-10).reverse().map((sig, idx) => {
              const isBuy = sig.action === "BUY";
              return (
                <div key={idx} className="flex items-start justify-between py-3 gap-3">
                  <div className="flex items-start gap-3">
                    <span
                      className={cn(
                          "mt-0.5 inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-semibold",
                        isBuy
                            ? "border border-gain/20 bg-gain/[0.08] text-gain"
                            : "border border-destructive/20 bg-destructive/[0.08] text-destructive"
                      )}
                    >
                      {isBuy ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
                      {sig.action}
                    </span>
                    <div>
                      <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-foreground">{sig.symbol.replace("-EQ", "")}</span>
                      <span className="text-caption text-muted-foreground">₹{sig.price.toLocaleString("en-IN")}</span>
                      <span className={cn("text-caption font-semibold", sig.day_chg_pct >= 0 ? "text-gain" : "text-loss")}>
                          {sig.day_chg_pct >= 0 ? `+${sig.day_chg_pct}%` : `${sig.day_chg_pct}%`}
                        </span>
                          <span className="rounded-md bg-card px-1.5 py-0.5 text-micro text-muted-foreground">
                          {sig.metric}
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-foreground">{sig.reason}</p>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <Button
                      size="xs"
                      variant="ghost"
                      onClick={() => openChart(sig.symbol)}
                    >
                      Chart
                    </Button>
                      <span className="text-caption text-muted-foreground">
                      <RelativeTime value={sig.ts} lead="relative" absolute={false} />
                    </span>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="py-6 text-center text-xs text-muted-foreground">
              None yet
            </div>
          )}
        </div>
      </Card>

      {/* ------------------------------------------------------------- */}
      {/* 4. CUSTOM MANUAL ALERTS (OPTIONAL SPECIFIC PRICE/INDICATOR) */}
      {/* ------------------------------------------------------------- */}
      <Card className="border-border bg-background p-4">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-muted-foreground" />
            <h3 className="text-sm font-semibold text-foreground">Your own price alerts</h3>
          </div>
          <StatefulButton
            state={checkState}
            onClick={() => void checkManualNow()}
            loadingText="Checking…"
            successText="Checked"
            errorText="Failed"
            className="h-7 text-xs"
          >
            Check now
          </StatefulButton>
        </div>

        <div className="grid gap-3 pt-3 sm:grid-cols-4">
          <Input label="Symbol" value={symbol} onChange={setSymbol} placeholder="RELIANCE-EQ" />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-xs font-medium text-foreground">Condition</label>
            <Select
              size="sm"
              value={kind}
              onChange={setKind}
              options={KINDS.map((k) => ({ value: k.v, label: k.label }))}
            />
          </div>
          <Input
            label={`Level ${kindInfo?.needs ? "" : "(unused)"}`}
            type="number"
            value={threshold}
            disabled={!kindInfo?.needs}
            onChange={setThreshold}
          />
          <div className="flex items-end">
            <Button
              onClick={() => void createManual()}
              className="h-10 w-full text-xs font-semibold"
            >
              Add Alert
            </Button>
          </div>
        </div>

        {error && (
          <div className="mt-3 rounded-md border border-destructive/20 bg-destructive/[0.08] p-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {rules.length > 0 && (
          <div className="mt-4 divide-y divide-border/60 border-t border-border pt-2">
            {rules.map((r) => (
              <div key={r.id} className="flex items-center justify-between py-2 text-xs">
                <div className="flex items-center gap-2">
                  <strong className="text-foreground">{r.symbol}</strong>
                  <span className="text-muted-foreground">· {r.kind.replace(/_/g, " ")}</span>
                  {THRESHOLD_KINDS.has(r.kind) && <span className="tabular-nums text-primary">@ {r.threshold}</span>}
                </div>
                <div className="flex items-center gap-2">
                  <Switch checked={r.armed} onCheckedChange={() => void toggle(r)} />
                  <Button size="xs" variant="ghost" onClick={() => openChart(r.symbol)}>
                    Chart
                  </Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    onClick={() => void remove(r.id)}
                    className="text-loss hover:text-loss"
                  >
                    Delete
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}

        {events.length > 0 && (
          <div className="mt-4 border-t border-border pt-3">
            <div className="text-caption font-semibold uppercase tracking-wider text-muted-foreground mb-2">
              Alert history ({events.length})
            </div>
            <div data-lenis-prevent className="max-h-40 overflow-y-auto divide-y divide-border/40">
              {events.slice(0, 10).map((e) => (
                <div key={e.id} className="flex items-center justify-between py-1.5 text-xs">
                  <div>
                    <span className="font-semibold text-foreground">{e.rule}</span>
                    <span className="ml-2 text-muted-foreground">{e.message}</span>
                  </div>
                  <span className="text-micro text-muted-foreground">
                    <RelativeTime value={e.ts} lead="relative" absolute={false} />
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

