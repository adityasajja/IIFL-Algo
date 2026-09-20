import { AlertOctagon, FlaskConical } from "lucide-react";
import { cn } from "../../lib/utils";

/**
 * The environment banner.
 *
 * This exists because "which environment am I in" is the single most
 * consequential fact on the page and it was previously buried as a muted row
 * inside System Status — a row you had to *choose* to read, on the one screen
 * where being wrong costs money. It is now full-width at the very top and
 * cannot be scrolled past or mistaken for decoration.
 *
 * `live` requires BOTH the env to permit it and execution mode to be live:
 * showing "REAL CAPITAL" for an env that cannot trade would be its own lie.
 */
export function EnvironmentBanner({
  env,
  executionMode,
  killSwitch,
  className,
}: {
  env?: string;
  executionMode?: "paper" | "live";
  killSwitch?: boolean;
  className?: string;
}) {
  const envKnown = env !== undefined && env !== null;
  const isLiveEnv = env === "live";
  const isLiveExec = executionMode === "live";
  const live = isLiveEnv && isLiveExec;

  // Kill switch outranks everything: if orders are blocked, that is the fact
  // that matters, regardless of which environment we are nominally in.
  // "unknown" stays loud: it means the backend cannot be reached. "safe" is the everyday
  // paper state and is deliberately quiet, because a bar that always shouts is ignored.
  const state = killSwitch ? "halted" : live ? "live" : !envKnown ? "unknown" : "safe";

  const label = !envKnown
    ? "ENVIRONMENT UNKNOWN"
    : killSwitch
      ? `HALTED — KILL SWITCH ENGAGED (${env})`
      : live
        ? "LIVE — REAL CAPITAL"
        : "Paper mode";

  const detail = !envKnown
    ? "Backend unreachable — do not assume orders are safe."
    : killSwitch
      ? "No new orders will be placed until it is released."
      : live
        ? `Env ${env} · execution live · approved signals reach the broker and fill for real.`
        : "Orders are simulated. Nothing is sent to your broker.";

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex w-full items-center justify-center gap-2.5 border-b text-center",
        state === "safe" ? "gap-2 border-border/50 px-4 py-1 text-muted-foreground" : "px-4 py-1.5",
        state === "live" && "border-red-700 bg-red-600 text-white",
        state === "halted" && "border-amber-600 bg-amber-500 text-black",
        state === "unknown" && "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
        className,
      )}
    >
      {state === "live" ? (
        <AlertOctagon className="h-3.5 w-3.5 shrink-0" />
      ) : state === "halted" ? (
        <AlertOctagon className="h-3.5 w-3.5 shrink-0" />
      ) : state === "unknown" ? (
        <AlertOctagon className="h-3.5 w-3.5 shrink-0" />
      ) : (
        <FlaskConical className="h-3 w-3 shrink-0 opacity-60" />
      )}

      <span
        className={cn(
          state === "safe" ? "text-[11px] font-medium" : "text-[11.5px] font-bold uppercase tracking-[0.14em]",
        )}
      >
        {label}
      </span>

      <span
        className={cn(
          "hidden text-[11px] sm:inline",
          state === "live" ? "text-white/85" : state === "halted" ? "text-black/70" : state === "safe" ? "opacity-70" : "opacity-80",
        )}
      >
        {state === "safe" && <span className="mr-2 opacity-50">·</span>}
        {detail}
      </span>

      {state === "live" && (
        <span className="ml-1 inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-white" />
      )}
    </div>
  );
}
