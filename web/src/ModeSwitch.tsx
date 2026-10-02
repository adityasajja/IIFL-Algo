import { FlaskConical, Radio } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getExecutionMode,
  setExecutionMode,
  type ExecutionMode,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader } from "./components/ui/card";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Callout } from "./components/ui/stat";
import { Switch } from "./components/motion/switch";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";
import { setVisibleInterval } from "./lib/visibleInterval";

/**
 * Execution mode — paper or live — without its own tab.
 *
 * Going live requires a written reason. That friction is the point: the
 * transition that can lose money should cost a sentence, and that sentence is
 * what appears in the audit trail below.
 */
export function useExecutionMode() {
  const { toast } = useToast();
  const [mode, setMode] = useState<ExecutionMode | null>(null);
  const [reason, setReason] = useState("");
  const [confirmLive, setConfirmLive] = useState(false);
  const [busy, setBusy] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setMode(await getExecutionMode());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setVisibleInterval(() => void load(), 20_000);
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
        description: "Orders will now reach the broker.",
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
  const beginFlip = (next: boolean) => {
    if (next) setConfirmLive(true);
    else void goPaper();
  };

  return {
    mode, isLive, reason, setReason, confirmLive, setConfirmLive,
    busy, error, goLive, beginFlip,
  };
}

export type ExecutionModeCtl = ReturnType<typeof useExecutionMode>;

/** Compact pill for the Trading header row: status at a glance, switch in place. */
export function ModePill({ ex }: { ex: ExecutionModeCtl }) {
  return (
    <div className="flex shrink-0 items-center gap-2.5 rounded-full border border-border/70 bg-card/60 py-1 pl-3 pr-1.5">
      <span className={cn("grid size-5 place-items-center", ex.isLive ? "text-destructive" : "text-gain")}>
        {ex.isLive ? <Radio className="size-4" /> : <FlaskConical className="size-4" />}
      </span>
      <span className="text-sm font-medium text-foreground">
        {ex.mode ? (ex.isLive ? "Live" : "Paper") : "…"}
      </span>
      <Switch
        checked={ex.isLive}
        disabled={ex.busy !== "idle" || !ex.mode}
        ariaLabel="Toggle live execution"
        onCheckedChange={ex.beginFlip}
      />
    </div>
  );
}

/** Confirm-live card plus the collapsible audit trail, under the header row. */
export function ModeDetails({ ex }: { ex: ExecutionModeCtl }) {
  return (
    <>
      {ex.error ? (
        <div className="rounded-lg border border-destructive/40 bg-destructive/[0.07] px-3 py-2 text-body font-normal text-destructive">
          {ex.error}
        </div>
      ) : null}

      {ex.confirmLive && (
        <Card className="border-destructive/45">
          <CardHeader
            title="Confirm live execution"
            sub="This is the step where the system starts spending real money. Write down why."
          />
          <div className="space-y-3 px-5 pb-5 pt-3">
            <Callout tone="bad" title="What changes the moment you switch">
              The trade queue's <strong>Execute</strong> button and{" "}
              <code className="rounded-md bg-black/10 px-1 text-caption dark:bg-white/10">POST /orders</code>{" "}
              will transmit to your broker. Fills, rejections and slippage are real. The stop-loss
              orders attached to each signal are real too.
            </Callout>

            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Reason (required — recorded in the audit trail)
              </label>
              <textarea
                value={ex.reason}
                onChange={(e) => ex.setReason(e.target.value)}
                rows={2}
                placeholder="e.g. Paper-traded this rule for 3 weeks, 41 signals, expectancy within 15% of the walk-forward estimate."
                className="w-full resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>

            <div className="flex gap-2">
              <StatefulButton
                state={ex.busy}
                onClick={() => void ex.goLive()}
                disabled={ex.reason.trim().length === 0}
                className="h-8 px-3 text-xs"
              >
                {ex.reason.trim().length === 0 ? "Reason required" : "Enable live execution"}
              </StatefulButton>
              <Button
                size="xs"
                variant="ghost"
                onClick={() => {
                  ex.setConfirmLive(false);
                  ex.setReason("");
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        </Card>
      )}

    </>
  );
}
