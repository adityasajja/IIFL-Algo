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
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

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

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 disabled:opacity-60 [&>option]:bg-card";

export default function AlertsPanel({ onOpenChart }: { onOpenChart?: (tab: string) => void }) {
  const { toast } = useToast();
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [checkState, setCheckState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
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

  function openChart(sym: string) {
    try {
      sessionStorage.setItem("atr.chartSymbol", sym);
    } catch {
      /* ignore */
    }
    onOpenChart?.("charts");
  }

  async function create() {
    setError(null);
    try {
      await createAlertRule({
        symbol: symbol.trim().toUpperCase(),
        kind,
        threshold: Number(threshold) || 0,
      });
      toast({ title: "Alert created", description: `${symbol.trim().toUpperCase()} · ${kind.replace(/_/g, " ")}`, status: "success" });
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

  async function checkNow() {
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

  async function test() {
    try {
      const res = await sendAlertTest();
      toast({
        title: res.sent_on === "none" || res.sent_on === "log" ? "No channel configured" : `Test sent via ${res.sent_on}`,
        description:
          res.sent_on === "none" || res.sent_on === "log"
            ? "Add TELEGRAM_BOT_TOKEN + CHAT_ID to .env."
            : "Check your phone.",
        status: res.sent_on === "none" || res.sent_on === "log" ? "info" : "success",
      });
    } catch (e) {
      toast({ title: "Test failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  const kindInfo = KINDS.find((k) => k.v === kind);

  return (
    <div className="space-y-3.5">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-2.5">
          <StatefulButton
            state={checkState}
            onClick={() => void checkNow()}
            loadingText="Checking…"
            successText="Checked"
            errorText="Failed — retry"
          >
            Check now
          </StatefulButton>
          <Button variant="secondary" onClick={() => void test()}>
            Send test message
          </Button>
          <Hint>Notify-only: alerts ping you, nothing auto-trades.</Hint>
        </div>
        {error && (
          <div className="mt-3">
            <ErrorBox>{error}</ErrorBox>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader title="New alert" sub={kindInfo?.hint} />
        <div className="grid gap-3.5 p-5 sm:grid-cols-3">
          <Input label="Symbol" value={symbol} onChange={setSymbol} placeholder="RELIANCE-EQ" />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Condition</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={selectClass}>
              {KINDS.map((k) => (
                <option key={k.v} value={k.v}>
                  {k.label}
                </option>
              ))}
            </select>
          </div>
          <Input
            label={`Level ${kindInfo?.needs ? "" : "(unused)"}`}
            type="number"
            value={threshold}
            disabled={!kindInfo?.needs}
            onChange={setThreshold}
          />
        </div>
        <div className="px-5 pb-5">
          <Button onClick={() => void create()}>Create alert</Button>
        </div>
      </Card>

      <Card>
        <CardHeader title={`Rules (${rules.length})`} sub={rules.length === 0 ? "None yet — create your first above." : undefined} />
        <div className="divide-y divide-border/60 px-5 pb-2">
          {rules.map((r) => (
            <div key={r.id} className="flex items-center gap-3 py-2.5">
              <div className="min-w-0 flex-1 text-[13px]">
                <strong className="font-semibold">{r.symbol}</strong>
                <span className="text-muted-foreground"> · {r.kind.replace(/_/g, " ")}</span>
                {THRESHOLD_KINDS.has(r.kind) && (
                  <span className="tabular-nums text-muted-foreground"> @ {r.threshold}</span>
                )}
              </div>
              <Switch checked={r.armed} onCheckedChange={() => void toggle(r)} ariaLabel={r.armed ? "Disarm rule" : "Arm rule"} />
              <span className={cn("w-14 text-right text-[11px] font-bold", r.armed ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>
                {r.armed ? "ARMED" : "OFF"}
              </span>
              <Button size="sm" variant="ghost" onClick={() => openChart(r.symbol)}>
                Chart
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void remove(r.id)}>
                Delete
              </Button>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <CardHeader title="Recent firings" sub={events.length === 0 ? "Nothing fired yet." : undefined} />
        <div className="divide-y divide-border/60 px-5 pb-2">
          {events.map((e) => (
            <div key={e.id} className="flex items-start justify-between gap-3 py-2.5">
              <div className="min-w-0 text-[13px]">
                <strong className="font-semibold">{e.rule}</strong>
                <div className="truncate text-xs text-muted-foreground">{e.message}</div>
              </div>
              <div className="shrink-0 text-right">
                <span className="inline-flex items-center rounded-full bg-primary/[0.07] px-2 py-0.5 text-[11px] font-semibold text-muted-foreground">
                  {e.channel}
                </span>
                <div className="mt-1 text-[11px] text-muted-foreground">
                  {new Date(e.ts).toLocaleString("en-IN")}
                </div>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
