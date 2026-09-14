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
  const state = killSwitch ? "halted" : live ? "live" : "safe";

  const label = !envKnown
    ? "ENVIRONMENT UNKNOWN"
    : killSwitch
      ? `HALTED — KILL SWITCH ENGAGED (${env})`
      : live
        ? "LIVE — REAL CAPITAL"
        : env === "dev"
          ? "PAPER / DEV MODE"
          : "PAPER — NO ORDERS TRANSMITTED";

  const detail = !envKnown
    ? "Backend unreachable — do not assume orders are safe."
    : killSwitch
      ? "No new orders will be placed until it is released."
      : live
        ? `Env ${env} · execution live · approved signals reach the broker and fill for real.`
        : `Env ${env} · execution ${executionMode ?? "paper"} · signals generate and queue, nothing is transmitted.`;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex w-full items-center justify-center gap-2.5 border-b px-4 py-1.5 text-center",
        state === "live" && "border-red-700 bg-red-600 text-white",
        state === "halted" && "border-amber-600 bg-amber-500 text-black",
        state === "safe" && "border-slate-400/40 bg-slate-500/15 text-slate-700 dark:text-slate-200",
        className,
      )}
    >
      {state === "live" ? (
        <AlertOctagon className="h-3.5 w-3.5 shrink-0" />
      ) : state === "halted" ? (
        <AlertOctagon className="h-3.5 w-3.5 shrink-0" />
      ) : (
        <FlaskConical className="h-3.5 w-3.5 shrink-0 opacity-80" />
      )}

      <span className="text-[11.5px] font-bold uppercase tracking-[0.14em]">{label}</span>

      <span
        className={cn(
          "hidden text-[11px] sm:inline",
          state === "live" ? "text-white/85" : state === "halted" ? "text-black/70" : "opacity-80",
        )}
      >
        {detail}
      </span>

      {state === "live" && (
        <span className="ml-1 inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-white" />
      )}
    </div>
  );
}
