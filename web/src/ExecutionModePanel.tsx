import { FlaskConical, History, Radio } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getAudit,
  getExecutionMode,
  setExecutionMode,
  type AuditEntry,
  type ExecutionMode,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, Callout } from "./components/ui/stat";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

/**
 * Execution mode — paper or live.
 *
 * The distinction this panel exists to make: in *paper* mode the system still
 * scans, still generates signals, and still exercises everything up to the
 * broker boundary — but no order is transmitted. Read-only data stays fully
 * live in both modes, because you need to see the market to decide anything.
 *
 * Going live requires a written reason. That friction is the point: the
 * transition that can lose money should cost a sentence, and that sentence is
 * what appears in the audit trail below.
 */
export default function ExecutionModePanel() {
  const { toast } = useToast();
  const [mode, setMode] = useState<ExecutionMode | null>(null);
  const [reason, setReason] = useState("");
  const [confirmLive, setConfirmLive] = useState(false);
  const [busy, setBusy] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);

  const load = useCallback(async () => {
    try {
      const [m, a] = await Promise.all([getExecutionMode(), getAudit(60)]);
      setMode(m);
      setAudit(a.entries);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 20_000);
    return () => clearInterval(t);
  }, [load]);

  async function goLive() {
    setBusy("loading");
    try {
      const next = await setExecutionMode("live", reason);
      setMode(next);
      setReason("");
      setConfirmLive(false);
      await load();
      toast({
        title: "Live execution enabled",
        description: "Orders will now reach the broker. The reason is on the audit trail.",
        status: "neutral",
      });
    } catch (e) {
      toast({
        title: "Could not switch to live",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setBusy("idle");
    }
  }

  async function goPaper() {
    setBusy("loading");
    try {
      const next = await setExecutionMode("paper", "returned to paper mode");
      setMode(next);
      await load();
      toast({
        title: "Paper mode",
        description: "Signals still generate, but no order will be transmitted.",
        status: "success",
      });
    } catch (e) {
      toast({
        title: "Could not switch to paper",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setBusy("idle");
    }
  }

  const isLive = mode?.mode === "live";

  return (
    <div className="space-y-4">
      {error ? <ErrorBox>{error}</ErrorBox> : null}

      {/* ── The mode itself ─────────────────────────────────────────────── */}
      <Card
        className={cn(
          "overflow-hidden",
          isLive ? "border-destructive/45" : "border-emerald-500/35",
        )}
      >
        <div
          className={cn(
            "flex flex-wrap items-center justify-between gap-5 px-5 py-5",
            isLive ? "bg-destructive/[0.06]" : "bg-emerald-500/[0.05]",
          )}
        >
          <div className="flex min-w-0 items-center gap-4">
            <div
              className={cn(
                "grid size-12 shrink-0 place-items-center rounded-2xl",
                isLive
                  ? "bg-destructive/15 text-destructive"
                  : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
              )}
            >
              {isLive ? <Radio className="size-6" /> : <FlaskConical className="size-6" />}
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-lg font-semibold tracking-tight text-foreground">
                  {isLive ? "Live execution" : "Paper mode"}
                </span>
                <Badge tone={isLive ? "bad" : "good"}>{isLive ? "REAL MONEY" : "SAFE"}</Badge>
              </div>
              <p className="mt-0.5 text-[13px] text-muted-foreground">
                {isLive
                  ? "Approved signals are transmitted to IIFL and fill in the real market."
                  : "Everything runs, nothing is sent. Signals are generated and queued but never transmitted."}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 flex-col items-end gap-2">
            <Switch
              checked={isLive}
              disabled={busy !== "idle" || !mode}
              ariaLabel="Toggle live execution"
              onCheckedChange={(next) => {
                if (next) setConfirmLive(true);
                else void goPaper();
              }}
            />
            <span className="text-[11px] text-muted-foreground">
              {isLive ? "Switch off to return to paper" : "Switch on to go live"}
            </span>
          </div>
        </div>

        {mode?.changed_at ? (
          <div className="border-t border-border/60 px-5 py-3 text-xs text-muted-foreground">
            Last changed{" "}
            <span className="text-foreground">{mode.changed_at.replace("T", " ").slice(0, 19)} UTC</span>{" "}
            by <span className="text-foreground">{mode.changed_by ?? "unknown"}</span>
            {mode.reason ? <> — “{mode.reason}”</> : null}
          </div>
        ) : null}
      </Card>

      {/* ── Confirm going live ──────────────────────────────────────────── */}
      {confirmLive && (
        <Card className="border-destructive/45">
          <CardHeader
            title="Confirm live execution"
            sub="This is the step where the system starts spending real money. Write down why."
          />
          <div className="space-y-3 px-5 pb-5 pt-3">
            <Callout tone="bad" title="What changes the moment you switch">
              The trade queue's <strong>Execute</strong> button and{" "}
              <code className="rounded bg-black/10 px-1 text-[11px] dark:bg-white/10">POST /orders</code>{" "}
              will transmit to your broker. Fills, rejections and slippage are real. The stop-loss
              orders attached to each signal are real too.
            </Callout>

            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Reason (required — recorded in the audit trail)
              </label>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={2}
                placeholder="e.g. Paper-traded this rule for 3 weeks, 41 signals, expectancy within 15% of the walk-forward estimate."
                className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <div className="flex gap-2">
              <StatefulButton
                state={busy}
                onClick={() => void goLive()}
                disabled={reason.trim().length === 0}
                className="h-8 px-3 text-xs"
              >
                {reason.trim().length === 0 ? "Reason required" : "Enable live execution"}
              </StatefulButton>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setConfirmLive(false);
                  setReason("");
                }}
                className="h-8 text-xs"
              >
                Cancel
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* ── What paper mode does and does not do ────────────────────────── */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader title="Still live in paper mode" sub="No feature is lost by staying safe" />
          <ul className="space-y-2 px-5 pb-5 pt-3 text-[13px] text-muted-foreground">
            {[
              "Market data, quotes and historical candles",
              "The scanner across the full NSE universe",
              "Signal generation and the trade queue",
              "Positions, holdings and margin (read-only)",
              "Walk-forward validation and the measured results table",
            ].map((t) => (
              <li key={t} className="flex gap-2">
                <span className="text-emerald-500">✓</span>
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <CardHeader title="Blocked in paper mode" sub="Enforced at the order path, not just here" />
          <ul className="space-y-2 px-5 pb-5 pt-3 text-[13px] text-muted-foreground">
            {[
              "Executing a queued signal",
              "Manual order placement",
              "Anything that would reach the broker with an order",
            ].map((t) => (
              <li key={t} className="flex gap-2">
                <span className="text-destructive">✕</span>
                <span>{t}</span>
              </li>
            ))}
          </ul>
          <div className="px-5 pb-5">
            <Hint>
              The refusal comes from the server, not the interface. Disabling a button hides a
              capability; refusing an order actually prevents one.
            </Hint>
          </div>
        </Card>
      </div>

      {/* ── Audit trail ─────────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              <History className="size-4" /> Audit trail
            </span>
          }
          sub="Append-only. Every mode change, kill-switch action and order is recorded and cannot be edited."
          action={
            <Badge tone="flat">{audit.length} entries</Badge>
          }
        />
        <div className="px-5 pb-5 pt-3">
          {audit.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
              No actions recorded yet. The trail fills as you change modes, trip the kill switch, or
              place orders.
            </div>
          ) : (
            <div className="max-h-80 overflow-y-auto rounded-lg border border-border/60">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-card text-muted-foreground">
                  <tr className="border-b border-border/60">
                    <th className="px-3 py-2 font-medium">When</th>
                    <th className="px-3 py-2 font-medium">Action</th>
                    <th className="px-3 py-2 font-medium">Subject</th>
                    <th className="px-3 py-2 font-medium">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.map((e, i) => (
                    <tr key={`${e.ts}-${i}`} className="border-b border-border/40 last:border-0">
                      <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                        {e.ts.replace("T", " ").slice(0, 19)}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={cn(
                            "rounded px-1.5 py-0.5 font-medium",
                            e.action.startsWith("kill_switch")
                              ? "bg-destructive/10 text-destructive"
                              : e.action.startsWith("execution_mode")
                                ? "bg-amber-500/10 text-amber-700 dark:text-amber-300"
                                : "bg-muted text-muted-foreground",
                          )}
                        >
                          {e.action}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-foreground">{e.subject}</td>
                      <td className="px-3 py-2 text-muted-foreground">{e.detail ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      <Callout tone="info" title="Why paper mode is the default">
        Every strategy in this system currently <strong>fails</strong> out-of-sample validation — see
        Evidence → Measured results. Running those rules against real money before one of them clears
        its hurdle would invert the entire purpose of the validation work.
      </Callout>
    </div>
  );
}
