import { cn } from "./lib/utils";

function Tile({
  onClick,
  title,
  children,
}: {
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="flex min-h-36 flex-col justify-between rounded-2xl border border-border bg-card p-4 text-left transition-colors hover:bg-muted/40"
    >
      {children}
    </button>
  );
}

/**
 * How many strategies run on paper right now.
 */
export function StrategyTiles({ running, onOpenPaper }: { running: number; onOpenPaper: () => void }) {
  return (
    <Tile onClick={onOpenPaper} title="Practice trades. No money at risk. Click to open Paper.">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <i className={cn("size-2 rounded-full", running > 0 ? "animate-pulse bg-emerald-500" : "bg-muted-foreground/40")} />
        On paper
      </div>
      <div className="text-5xl font-semibold tabular-nums tracking-tight">{running}</div>
    </Tile>
  );
}
