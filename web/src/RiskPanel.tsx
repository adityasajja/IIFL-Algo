import { AlertOctagon, Gauge, IndianRupee, ShieldCheck, ShieldOff, Target } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { getRiskStatus, setKillSwitch, type RiskStatus } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, Callout, fmtMoney, fmtNum } from "./components/ui/stat";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

/**
 * Risk — the "improve / maintain" half of the loop.
 *
 * Reads the *live* limits the engine enforces rather than a copy of the config,
 * so this panel cannot quietly disagree with execution. The kill switch is the
 * single most important control here; it is deliberately the largest element.
 */
export default function RiskPanel() {
  const { toast } = useToast();
  const [risk, setRisk] = useState<RiskStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [armed, setArmed] = useState<"kill" | "clear" | null>(null);
  const [btn, setBtn] = useState<ButtonState>("idle");

  const load = useCallback(async () => {
    try {
      setRisk(await getRiskStatus());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15_000);
    return () => clearInterval(t);
  }, [load]);

  const toggle = async (engage: boolean) => {
    setBtn("loading");
    try {
      await setKillSwitch(engage);
      await load();
      setBtn("success");
      toast({
        title: engage ? "Kill switch engaged" : "Kill switch cleared",
        description: engage
          ? "All new orders are now blocked."
          : "Order placement has resumed.",
        status: engage ? "neutral" : "success",
      });
      setTimeout(() => setBtn("idle"), 1200);
    } catch (e) {
      setBtn("idle");
      toast({
        title: "Could not change the kill switch",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setArmed(null);
    }
  };

  if (error && !risk) {
    return (
      <Card>
        <CardHeader title="Risk" sub="Live limits and the kill switch" />
        <div className="p-4">
          <ErrorBox>{error}</ErrorBox>
        </div>
      </Card>
    );
  }

  const on = risk?.kill_switch ?? false;
  const lim = risk?.limits;
  const m = risk?.margin ?? {};
  const riskPerTrade = lim ? (lim.capital * lim.risk_per_trade_pct) / 100 : 0;

  return (
    <div className="space-y-4">
      <div
        className={cn(
          "rounded-xl border p-4",
          on
            ? "border-destructive/50 bg-destructive/[0.07]"
            : "border-emerald-500/40 bg-emerald-500/[0.06]",
        )}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span
              className={cn(
                "grid size-10 place-items-center rounded-lg",
                on
                  ? "bg-destructive/15 text-destructive"
                  : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
              )}
            >
              {on ? <ShieldOff size={18} /> : <ShieldCheck size={18} />}
            </span>
            <div>
              <div className="text-sm font-semibold tracking-tight">
                {on ? "Kill switch engaged" : "Order placement open"}
              </div>
              <div className="text-xs text-muted-foreground">
                {on
                  ? "Every new order is rejected until you clear this."
                  : risk?.live_orders_allowed
                    ? `Environment is ${risk?.env} — orders will reach the broker.`
                    : `Environment is ${risk?.env} — orders are refused regardless.`}
              </div>
            </div>
          </div>

          {on ? (
            armed === "clear" ? (
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">Resume live order flow?</span>
                <Button size="sm" variant="outline" onClick={() => setArmed(null)}>
                  Cancel
                </Button>
                <StatefulButton
                  size="sm"
                  state={btn}
                  onClick={() => void toggle(false)}
                >
                  Confirm clear
                </StatefulButton>
              </div>
            ) : (
              <Button size="sm" variant="outline" onClick={() => setArmed("clear")}>
                Clear switch
              </Button>
            )
          ) : armed === "kill" ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Block all new orders?</span>
              <Button size="sm" variant="outline" onClick={() => setArmed(null)}>
                Cancel
              </Button>
              <StatefulButton size="sm" state={btn} onClick={() => void toggle(true)}>
                Confirm halt
              </StatefulButton>
            </div>
          ) : (
            <Button
              size="sm"
              variant="outline"
              className="border-destructive/50 text-destructive hover:bg-destructive/10"
              onClick={() => setArmed("kill")}
            >
              <AlertOctagon className="mr-1.5 h-3.5 w-3.5" />
              Engage kill switch
            </Button>
          )}
        </div>
      </div>

      {!risk?.live_orders_allowed && !on && (
        <Callout tone="warn">
          This environment (<span className="font-mono">{risk?.env}</span>) does not place live
          orders. Nothing here can move real money until the environment is switched to paper or
          live — the kill switch is a second, independent block on top of that.
        </Callout>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi
          icon={<IndianRupee className="h-3.5 w-3.5" />}
          label="Trading capital"
          value={lim ? fmtMoney(lim.capital) : "—"}
          hint="Base for every sizing calculation"
        />
        <Kpi
          icon={<Target className="h-3.5 w-3.5" />}
          label="Risk per trade"
          value={lim ? `${fmtNum(lim.risk_per_trade_pct, 1)}%` : "—"}
          hint={riskPerTrade ? `${fmtMoney(riskPerTrade)} at risk` : undefined}
        />
        <Kpi
          icon={<Gauge className="h-3.5 w-3.5" />}
          label="Max concurrent"
          value={lim ? String(lim.max_active) : "—"}
          hint="Open signals allowed at once"
        />
        <Kpi
          icon={<IndianRupee className="h-3.5 w-3.5" />}
          label="Available margin"
          value={
            m.availableMargin !== undefined
              ? fmtMoney(m.availableMargin)
              : m.openingCashLimit !== undefined
                ? fmtMoney(m.openingCashLimit)
                : "—"
          }
          hint={risk?.margin_error ? "broker unreachable" : "reported by IIFL"}
        />
      </div>

      <Card>
        <CardHeader
          title="Enforced limits"
          sub="Read from the same settings the engine uses — not a copy"
          action={lim ? <Badge tone="flat">{lim.product}</Badge> : null}
        />
        <div className="grid gap-x-6 gap-y-3 p-5 pt-4 sm:grid-cols-2 lg:grid-cols-3">
          {lim && (
            <>
              <Row label="Stop method" value={lim.stop_method === "atr" ? "ATR-based" : "Fixed %"} />
              <Row
                label="Stop distance"
                value={lim.stop_method === "atr" ? `${fmtNum(lim.stop_atr_mult, 1)} × ATR` : `${fmtNum(lim.stop_pct, 2)}%`}
              />
              <Row label="Reward : risk" value={`${fmtNum(lim.rr_ratio, 1)} : 1`} />
              <Row
                label="Worst case per trade"
                value={fmtMoney(riskPerTrade)}
                hint="capital × risk per trade"
              />
              <Row
                label="Worst case if all fill"
                value={fmtMoney(riskPerTrade * lim.max_active)}
                hint={`${lim.max_active} concurrent × per-trade risk`}
              />
              <Row label="Product" value={lim.product === "CNC" ? "CNC (delivery)" : `${lim.product} (intraday)`} />
            </>
          )}
        </div>
        <div className="px-5 pb-4">
          <Hint>
            The two "worst case" figures assume every position hits its stop at the same time and
            none are closed early — the pessimistic floor, not a forecast.
          </Hint>
        </div>
      </Card>

      {Object.keys(m).length > 0 && (
        <Card>
          <CardHeader title="Broker margin" sub="As reported by IIFL at the last refresh" />
          <div className="grid gap-x-6 gap-y-3 p-5 pt-4 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(m).map(([k, v]) => (
              <Row key={k} label={humanize(k)} value={fmtMoney(v)} />
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function Kpi({
  icon,
  label,
  value,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl bg-muted/40 p-3.5">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="mt-1 text-xl font-medium tabular-nums">{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/50 pb-2">
      <span className="text-[12.5px] text-muted-foreground">
        {label}
        {hint ? <span className="ml-1.5 text-[11px] opacity-70">({hint})</span> : null}
      </span>
      <span className="text-[13px] font-medium tabular-nums">{value}</span>
    </div>
  );
}

const humanize = (k: string) =>
  k.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase()).trim();
