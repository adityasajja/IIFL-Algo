import { useEffect, useState } from "react";
import { getForwardTracker, getGapPlan, type ForwardTracker, type GapPlan } from "./api";
import { Ring } from "./components/ui/ring";
import { cn } from "./lib/utils";

const signed = (n: number, d = 2) => `${n > 0 ? "+" : ""}${n.toFixed(d)}%`;

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

/** How many of the last few trades won, as a row of dots: green a win, red a loss. */
function Dots({ results, total = 14 }: { results: boolean[]; total?: number }) {
  const cells = Array.from({ length: total }, (_, i) => results[i]);
  return (
    <div className="flex flex-wrap gap-1.5" aria-hidden="true">
      {cells.map((won, i) => (
        <i
          key={i}
          className={cn(
            "size-3 rounded-full",
            won === undefined ? "border border-border" : won ? "bg-emerald-500" : "bg-rose-500",
          )}
        />
      ))}
    </div>
  );
}

/**
 * The strategies that are doing something now, as three tiles you can read at a glance:
 * how many run on paper, how the Monday gap plan is doing, and how far the live test has
 * got. The rules behind each are in the tooltips rather than on the page.
 */
export function StrategyTiles({
  running,
  onOpenPaper,
  onOpenPlans,
}: {
  running: number;
  onOpenPaper: () => void;
  onOpenPlans: () => void;
}) {
  const [gap, setGap] = useState<GapPlan | null>(null);
  const [test, setTest] = useState<ForwardTracker | null>(null);

  useEffect(() => {
    getGapPlan().then(setGap).catch(() => setGap(null));
    getForwardTracker().then(setTest).catch(() => setTest(null));
  }, []);

  const results = gap ? gap.recent.slice(0, 14).map((t) => (t.net_pct ?? 0) > 0) : [];
  const shown = gap && (gap.live.graded > 0 ? gap.live : gap.replay);
  const isLive = !!gap && gap.live.graded > 0;
  const call = gap?.this_week.status;

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <Tile onClick={onOpenPaper} title="Practice trades. No money at risk. Click to open Paper.">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <i className={cn("size-2 rounded-full", running > 0 ? "animate-pulse bg-emerald-500" : "bg-muted-foreground/40")} />
          On paper
        </div>
        <div className="text-5xl font-semibold tabular-nums tracking-tight">{running}</div>
      </Tile>

      {gap && (
        <Tile
          onClick={onOpenPlans}
          title={`After a week the market rose over ${gap.plan.market_min_pct}%, buy stocks that open over ${Math.abs(gap.plan.gap_pct)}% below Friday's close. Sell at +${gap.plan.target_pct}%, stop at −${gap.plan.stop_pct}%, else sell Friday.`}
        >
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="text-muted-foreground">Monday gap plan</span>
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 font-medium",
                call === "trade" ? "bg-emerald-500/10 text-emerald-500" : call === "skip" ? "bg-amber-500/10 text-amber-500" : "bg-muted text-muted-foreground",
              )}
            >
              {call === "trade" ? "Trade" : call === "skip" ? "Skip" : "—"}
            </span>
          </div>
          <div>
            <div
              className={cn(
                "text-3xl font-semibold tabular-nums tracking-tight",
                shown?.avg_net_pct == null ? "text-muted-foreground" : shown.avg_net_pct >= 0 ? "text-emerald-500" : "text-rose-500",
              )}
            >
              {shown?.avg_net_pct == null ? "—" : signed(shown.avg_net_pct)}
            </div>
            <div className="text-[11px] text-muted-foreground">per trade{isLive ? "" : " · replay"}</div>
          </div>
          <Dots results={results} />
        </Tile>
      )}

      {test && (
        <Tile
          onClick={onOpenPlans}
          title={`${test.description}. Each Friday's picks are written down and graded a week later on whether they gained 2% or more.`}
        >
          <div className="text-xs text-muted-foreground">Live test</div>
          <div className="flex items-center gap-4">
            <Ring value={(test.graded / Math.max(test.needed, 1)) * 100} size={72} stroke={9}>
              <span className="text-sm font-semibold tabular-nums">
                {test.graded}
                <span className="text-muted-foreground">/{test.needed}</span>
              </span>
            </Ring>
            {test.hit_rate_pct != null && (
              <span className="text-base font-semibold">{test.hit_rate_pct}%</span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {test.open.length === 0 ? (
              <span className="text-[11px] text-muted-foreground">No picks this week</span>
            ) : (
              test.open.map((s) => (
                <span key={s.symbol} className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium">
                  {s.symbol}
                </span>
              ))
            )}
          </div>
        </Tile>
      )}
    </div>
  );
}
