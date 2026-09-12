import { useEffect, useState } from "react";
import { getRiskStatus, setKillSwitch } from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    return Math.abs(v) >= 1000 || Number.isInteger(v)
      ? v.toLocaleString("en-IN", { maximumFractionDigits: 2 })
      : v.toFixed(4);
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

const ORDER = [
  "kill_switch",
  "regime",
  "daily_loss_pct",
  "daily_loss_limit_pct",
  "exposure_pct",
  "exposure_limit_pct",
  "positions",
  "positions_limit",
];

export default function RiskPanel() {
  const { toast } = useToast();
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [killState, setKillState] = useState<ButtonState>("idle");

  async function load() {
    setError(null);
    try {
      setStatus(await getRiskStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function kill(engaged: boolean) {
    setKillState("loading");
    try {
      await setKillSwitch(engaged);
      await load();
      setKillState("success");
      toast({
        title: engaged ? "Kill switch ENGAGED" : "Kill switch cleared",
        description: engaged ? "No new positions will be opened." : "Engine may open positions again.",
        status: engaged ? "error" : "success",
      });
    } catch (e) {
      setKillState("error");
      toast({ title: "Kill switch failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  const engaged = status?.["kill_switch"] === true;

  return (
    <Card>
      <CardHeader
        title="Risk engine"
        action={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={() => void load()}>
              Reload
            </Button>
            <StatefulButton
              size="sm"
              state={killState}
              disabled={engaged}
              onClick={() => void kill(true)}
              className="border-destructive/50 text-destructive hover:bg-destructive/10"
              loadingText="Engaging…"
              successText="Engaged"
              errorText="Failed"
            >
              Engage kill switch
            </StatefulButton>
            <Button size="sm" variant="ghost" disabled={!engaged} onClick={() => void kill(false)}>
              Clear
            </Button>
          </div>
        }
      />
      <div className="p-5 pt-3">
        {engaged && (
          <div className="mb-3.5">
            <ErrorBox>KILL SWITCH ENGAGED — the engine will refuse to open any new position.</ErrorBox>
          </div>
        )}
        {error && (
          <div className="mb-3.5">
            <ErrorBox>{error}</ErrorBox>
          </div>
        )}
        {status === null && !error && <Hint>Loading risk status…</Hint>}
        {status && (
          <div className="grid gap-3.5 sm:grid-cols-2 xl:grid-cols-4">
            {ORDER.filter((k) => k in status).map((k) => {
              const v = status[k] as boolean | number;
              const good =
                typeof v === "boolean"
                  ? !v
                  : k.endsWith("_limit_pct")
                    ? true
                    : typeof v === "number" && status[`${k}_limit_pct`] !== undefined
                      ? v < (status[`${k}_limit_pct`] as number)
                      : undefined;
              return (
                <div key={k} className="rounded-xl border border-border bg-background/40 p-4">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                    {k.replace(/_/g, " ")}
                  </div>
                  <div className="mt-1 text-xl font-bold tabular-nums">{fmt(v)}</div>
                  {good !== undefined && (
                    <div className="mt-1.5">
                      <span
                        className={cn(
                          "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold",
                          good
                            ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                            : "bg-destructive/10 text-destructive",
                        )}
                      >
                        {good ? "within limits" : "AT LIMIT"}
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <Hint className="mt-3.5">
          The kill switch here only flips API state. The backtest/live <code className="font-mono">RiskEngine</code> still
          enforces loss, exposure, and square-off limits.
        </Hint>
      </div>
    </Card>
  );
}
