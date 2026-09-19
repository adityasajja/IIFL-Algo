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
import { Input } from "./components/ui/input";
import { Select } from "./components/ui/select";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/ui/switch";
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
            ? "Actionable signals dispatched to Telegram and logged below."
            : "All monitored stocks healthy; no sell/buy triggers met right now.",
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
            ? "Add TELEGRAM_BOT_TOKEN + CHAT_ID to .env to receive notifications."
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
      {/* 1. INTELLIGENT TRIGGER ENGINE BANNER & CONTROL BAR            */}
      {/* ------------------------------------------------------------- */}
      <Card className="border-border bg-gradient-to-r from-[#131722] via-[#1a1f2c] to-[#131722] p-5 shadow-xl">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 text-foreground shadow-lg shadow-indigo-500/20">
              <Sparkles size={22} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-foreground tracking-wide">
                  Smart alerts
                </h2>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider",
                    intelConfig?.enabled
                      ? "bg-emerald-500/15 text-emerald-400 border border-emerald-500/30"
                      : "bg-zinc-500/20 text-zinc-400 border border-zinc-500/30"
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      intelConfig?.enabled ? "bg-emerald-400 animate-pulse" : "bg-zinc-500"
                    )}
                  />
                  {intelConfig?.enabled ? "On" : "Paused"}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                Watches your stocks and messages you on Telegram when something needs attention.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void testTelegram()}
              className="border border-border bg-card text-xs hover:bg-muted"
            >
              <Send size={13} className="mr-1.5 text-blue-400" />
              Send test message
            </Button>
            <Button
              size="sm"
              disabled={evaluating}
              onClick={() => void handleEvaluateNow()}
              className="bg-indigo-600 text-foreground hover:bg-indigo-500 font-semibold shadow-md shadow-indigo-600/20 text-xs h-9 px-3.5"
            >
              {evaluating ? (
                <>
                  <RotateCw size={13} className="mr-1.5 animate-spin" />
                  Evaluating Market…
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
            <div className="flex items-center justify-between rounded-xl bg-card/70 p-3 border border-border/60">
              <div>
                <div className="text-xs font-semibold text-foreground">Run automatically</div>
                <div className="text-[11px] text-muted-foreground">During market hours</div>
              </div>
              <Switch
                checked={intelConfig.enabled}
                onCheckedChange={(checked) => void updateConfigField("enabled", checked)}
              />
            </div>

            {/* Universe Selector */}
            <div className="flex flex-col gap-1.5 rounded-xl bg-card/70 p-3 border border-border/60">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground">
                <span className="flex items-center gap-1.5">
                  <Layers size={13} className="text-indigo-400" /> Watch
                </span>
              </div>
              <div className="flex gap-1 pt-1">
                {(["both", "holdings", "watchlist"] as const).map((u) => (
                  <button
                    key={u}
                    type="button"
                    onClick={() => void updateConfigField("universe", u)}
                    className={cn(
                      "flex-1 rounded-lg py-1 text-[11px] font-semibold capitalize transition-colors",
                      intelConfig.universe === u
                        ? "bg-indigo-600 text-foreground shadow-sm"
                        : "bg-background text-muted-foreground hover:text-foreground"
                    )}
                  >
                    {u === "both" ? "Both" : u}
                  </button>
                ))}
              </div>
            </div>

            {/* Check Frequency */}
            <div className="flex flex-col gap-1.5 rounded-xl bg-card/70 p-3 border border-border/60">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground">
                <span className="flex items-center gap-1.5">
                  <Clock size={13} className="text-emerald-400" /> Check every
                </span>
                <span className="text-[11px] text-muted-foreground">Every {intelConfig.interval_min}m</span>
              </div>
              <div className="flex gap-1 pt-1">
                {[1, 3, 5, 15].map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => void updateConfigField("interval_min", m)}
                    className={cn(
                      "flex-1 rounded-lg py-1 text-[11px] font-semibold transition-colors",
                      intelConfig.interval_min === m
                        ? "bg-emerald-600 text-foreground shadow-sm"
                        : "bg-background text-muted-foreground hover:text-foreground"
                    )}
                  >
                    {m}m
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* ------------------------------------------------------------- */}
      {/* 2. QUANTITATIVE RULES: SELL (EXITS) & BUY (ENTRIES)           */}
      {/* ------------------------------------------------------------- */}
      {intelConfig && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* SELL (EXIT) TRIGGERS */}
          <Card className="border-border bg-background p-4">
            <div className="flex items-center gap-2 border-b border-border pb-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-rose-500/20 text-rose-400">
                <TrendingDown size={16} />
              </div>
              <div>
                <h3 className="text-sm font-bold text-foreground">When to sell</h3>
                <p className="text-[11px] text-muted-foreground">Warnings that it may be time to exit</p>
              </div>
            </div>

            <div className="mt-3 divide-y divide-[#2a2e39]/60">
              {/* Trend Breakdown (SMA 20) */}
              <div className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-xs font-semibold text-foreground">Falls below its 20-day average</div>
                  <div className="text-[11px] text-muted-foreground">The trend may be turning down</div>
                </div>
                <Switch
                  checked={intelConfig.sell_sma_breakdown}
                  onCheckedChange={(c) => void updateConfigField("sell_sma_breakdown", c)}
                />
              </div>

              {/* RSI Overbought Reversal */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground">Looks overbought</div>
                  <div className="text-[11px] text-muted-foreground">Price has run up hard and may pull back</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">RSI ≥</span>
                  <input
                    type="number"
                    value={intelConfig.sell_rsi_threshold}
                    onChange={(e) => void updateConfigField("sell_rsi_threshold", Number(e.target.value) || 75)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
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
                  <div className="text-xs font-semibold text-foreground">Drops from its recent high</div>
                  <div className="text-[11px] text-muted-foreground">Falls this much from its 20-day high</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">Drop %</span>
                  <input
                    type="number"
                    value={intelConfig.sell_trailing_stop_pct}
                    onChange={(e) => void updateConfigField("sell_trailing_stop_pct", Number(e.target.value) || 3)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
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
                  <div className="text-xs font-semibold text-foreground">Profit target reached</div>
                  <div className="text-[11px] text-muted-foreground">A holding is up this much</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">+%</span>
                  <input
                    type="number"
                    value={intelConfig.sell_take_profit_pct}
                    onChange={(e) => void updateConfigField("sell_take_profit_pct", Number(e.target.value) || 8)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
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
                  <div className="text-xs font-semibold text-foreground">Loss limit reached</div>
                  <div className="text-[11px] text-muted-foreground">A holding is down this much</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">-%</span>
                  <input
                    type="number"
                    value={intelConfig.sell_stop_loss_pct}
                    onChange={(e) => void updateConfigField("sell_stop_loss_pct", Number(e.target.value) || 4)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <Switch
                    checked={intelConfig.sell_stop_loss_enabled}
                    onCheckedChange={(c) => void updateConfigField("sell_stop_loss_enabled", c)}
                  />
                </div>
              </div>
            </div>
          </Card>

          {/* BUY (ENTRY) TRIGGERS */}
          <Card className="border-border bg-background p-4">
            <div className="flex items-center gap-2 border-b border-border pb-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/20 text-emerald-400">
                <TrendingUp size={16} />
              </div>
              <div>
                <h3 className="text-sm font-bold text-foreground">When to buy</h3>
                <p className="text-[11px] text-muted-foreground">Signs of a possible entry</p>
              </div>
            </div>

            <div className="mt-3 divide-y divide-[#2a2e39]/60">
              {/* Golden Cross */}
              <div className="flex items-center justify-between py-2.5">
                <div>
                  <div className="text-xs font-semibold text-foreground">Short-term average crosses above long-term</div>
                  <div className="text-[11px] text-muted-foreground">The 20-day average just moved above the 50-day</div>
                </div>
                <Switch
                  checked={intelConfig.buy_golden_cross}
                  onCheckedChange={(c) => void updateConfigField("buy_golden_cross", c)}
                />
              </div>

              {/* Oversold Dip Bounce */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground">Looks oversold</div>
                  <div className="text-[11px] text-muted-foreground">Price has dropped hard and may bounce</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">RSI ≤</span>
                  <input
                    type="number"
                    value={intelConfig.buy_rsi_threshold}
                    onChange={(e) => void updateConfigField("buy_rsi_threshold", Number(e.target.value) || 32)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
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
                  <div className="text-xs font-semibold text-foreground">Breakout on heavy trading</div>
                  <div className="text-[11px] text-muted-foreground">Hits a 20-day high on unusually heavy volume</div>
                </div>
                <Switch
                  checked={intelConfig.buy_breakout_vol}
                  onCheckedChange={(c) => void updateConfigField("buy_breakout_vol", c)}
                />
              </div>

              {/* Alert Cooldown */}
              <div className="flex items-center justify-between py-2.5">
                <div className="pr-3">
                  <div className="text-xs font-semibold text-foreground">Don't repeat within</div>
                  <div className="text-[11px] text-muted-foreground">Avoids repeat messages about the same stock</div>
                </div>
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    value={intelConfig.cooldown_min}
                    onChange={(e) => void updateConfigField("cooldown_min", Number(e.target.value) || 45)}
                    className="h-7 w-14 rounded-lg border border-border bg-card px-2 text-center text-xs text-foreground"
                  />
                  <span className="text-[11px] text-muted-foreground">min</span>
                </div>
              </div>
            </div>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------------------- */}
      {/* 3. RECENT INTELLIGENT SIGNALS & DISPATCH HISTORY             */}
      {/* ------------------------------------------------------------- */}
      <Card className="border-border bg-background p-4">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <Activity size={16} className="text-indigo-400" />
            <h3 className="text-sm font-bold text-foreground">Recent alerts</h3>
          </div>
          <span className="text-[11px] text-muted-foreground">
            Last evaluated: {intelStatus?.last_run ? <RelativeTime value={intelStatus.last_run} /> : "Ready"}
          </span>
        </div>

        <div className="mt-3 divide-y divide-[#2a2e39]/50">
          {intelStatus?.recent_signals && intelStatus.recent_signals.length > 0 ? (
            intelStatus.recent_signals.slice(-10).reverse().map((sig, idx) => {
              const isBuy = sig.action === "BUY";
              return (
                <div key={idx} className="flex items-start justify-between py-3 gap-3">
                  <div className="flex items-start gap-3">
                    <span
                      className={cn(
                        "mt-0.5 inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-bold",
                        isBuy
                          ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                          : "bg-rose-500/20 text-rose-400 border border-rose-500/30"
                      )}
                    >
                      {isBuy ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
                      {sig.action}
                    </span>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-bold text-foreground">{sig.symbol.replace("-EQ", "")}</span>
                        <span className="text-[11px] text-muted-foreground">₹{sig.price.toLocaleString("en-IN")}</span>
                        <span className={cn("text-[11px] font-semibold", sig.day_chg_pct >= 0 ? "text-emerald-400" : "text-rose-400")}>
                          {sig.day_chg_pct >= 0 ? `+${sig.day_chg_pct}%` : `${sig.day_chg_pct}%`}
                        </span>
                        <span className="rounded bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {sig.metric}
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-foreground">{sig.reason}</p>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => openChart(sig.symbol)}
                      className="h-7 text-xs text-indigo-400 hover:bg-card"
                    >
                      Chart
                    </Button>
                    <span className="text-[11px] text-muted-foreground">
                      <RelativeTime value={sig.ts} lead="relative" absolute={false} />
                    </span>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="py-6 text-center text-xs text-muted-foreground">
              No alerts yet.
            </div>
          )}
        </div>
      </Card>

      {/* ------------------------------------------------------------- */}
      {/* 4. CUSTOM MANUAL ALERTS (OPTIONAL SPECIFIC PRICE/INDICATOR)   */}
      {/* ------------------------------------------------------------- */}
      <Card className="border-border bg-background p-4">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-muted-foreground" />
            <h3 className="text-sm font-bold text-foreground">Your own price alerts</h3>
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
            <Button onClick={() => void createManual()} className="h-10 w-full text-xs font-semibold">
              Add Alert
            </Button>
          </div>
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-400">
            {error}
          </div>
        )}

        {rules.length > 0 && (
          <div className="mt-4 divide-y divide-[#2a2e39]/60 border-t border-border pt-2">
            {rules.map((r) => (
              <div key={r.id} className="flex items-center justify-between py-2 text-xs">
                <div className="flex items-center gap-2">
                  <strong className="text-foreground">{r.symbol}</strong>
                  <span className="text-muted-foreground">· {r.kind.replace(/_/g, " ")}</span>
                  {THRESHOLD_KINDS.has(r.kind) && <span className="tabular-nums text-indigo-400">@ {r.threshold}</span>}
                </div>
                <div className="flex items-center gap-2">
                  <Switch checked={r.armed} onCheckedChange={() => void toggle(r)} />
                  <Button size="sm" variant="ghost" onClick={() => openChart(r.symbol)} className="h-7 text-xs">
                    Chart
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => void remove(r.id)} className="h-7 text-xs text-rose-400 hover:text-rose-300">
                    Delete
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}

        {events.length > 0 && (
          <div className="mt-4 border-t border-border pt-3">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">
              Alert history ({events.length})
            </div>
            <div className="max-h-40 overflow-y-auto divide-y divide-[#2a2e39]/40">
              {events.slice(0, 10).map((e) => (
                <div key={e.id} className="flex items-center justify-between py-1.5 text-xs">
                  <div>
                    <span className="font-semibold text-foreground">{e.rule}</span>
                    <span className="ml-2 text-muted-foreground">{e.message}</span>
                  </div>
                  <span className="text-[10px] text-muted-foreground">
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

