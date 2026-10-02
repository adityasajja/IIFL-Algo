import { cn } from "./lib/utils";

/**
 * Compact inline summary — how many strategies are running on paper right now.
 * Renders as a slim pill/banner above the strategy list rather than a tall
 * standalone tile, which looked orphaned when only one stat was shown.
 */
 export function StrategyTiles({ running, onOpenPaper }: { running: number; onOpenPaper: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpenPaper}
      title="Practice trades. No money at risk. Click to open Paper."
      className="flex w-full items-center justify-between rounded-lg border border-border/60 bg-card px-4 py-3 text-left transition-colors hover:bg-muted/40"
    >
      <div className="flex items-center gap-2.5 text-sm text-muted-foreground">
      <i className={cn("size-2 shrink-0 rounded-full", running > 0 ? "animate-pulse bg-gain" : "bg-muted-foreground/30")} />
        <span>
        <span className="font-semibold tabular-nums text-foreground">{running}</span>
        {" "}
          {running === 1 ? "strategy" : "strategies"} running
        </span>
      </div>
      <span className="text-xs text-muted-foreground">Paper →</span>
    </button>
  );
}
