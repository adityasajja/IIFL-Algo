import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { getForwardTracker, getGapPlan, type ForwardTracker, type GapPlan, type GapPlanStats } from "./api";
import { Card, CardHeader } from "./components/ui/card";
import { Badge } from "./components/ui/stat";

const pct = (n: number | null, digits = 2) => (n === null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`);

function Result({ label, stats }: { label: string; stats: GapPlanStats }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      {stats.graded === 0 ? (
        <div className="text-sm text-muted-foreground">No graded trades yet</div>
      ) : (
        <div className="text-sm">
          <span className={(stats.avg_net_pct ?? 0) >= 0 ? "font-semibold text-emerald-500" : "font-semibold text-rose-500"}>
            {pct(stats.avg_net_pct)}
          </span>
          <span className="text-muted-foreground"> per trade · {stats.win_rate_pct}% hit target · {stats.graded} trades</span>
        </div>
      )}
    </div>
  );
}

function PlanRow({
  name,
  rule,
  badge,
  children,
  onOpen,
}: {
  name: string;
  rule: string;
  badge: React.ReactNode;
  children: React.ReactNode;
  onOpen: () => void;
}) {
  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{name}</span>
          {badge}
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex items-center gap-0.5 text-xs text-primary hover:underline"
        >
          Results and trades
          <ChevronRight className="size-3.5" />
        </button>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{rule}</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">{children}</div>
    </div>
  );
}

/**
 * Plans that are followed as paper trades outside the deployment engine.
 *
 * They are not built-in strategies (nothing here is versioned or deployed to the paper
 * runner), so they get their own section. Each shows its rule in one line, this week's
 * call, and how it has done, with live weeks kept apart from replayed ones.
 */
export function TrackedPlans({ onOpen }: { onOpen: () => void }) {
  const [gap, setGap] = useState<GapPlan | null>(null);
  const [test, setTest] = useState<ForwardTracker | null>(null);

  useEffect(() => {
    getGapPlan().then(setGap).catch(() => setGap(null));
    getForwardTracker().then(setTest).catch(() => setTest(null));
  }, []);

  if (!gap && !test) return null;

  return (
    <Card>
      <CardHeader
        title="Tracked plans"
        sub="Rules followed as paper trades, so their results can be read with no money at risk. They are not run by the paper trading engine."
      />
      <div className="mt-2 divide-y divide-border/60">
        {gap && (
          <PlanRow
            name="Monday gap plan"
            rule={`After a week the market rose more than ${gap.plan.market_min_pct}%, buy stocks that open over ${Math.abs(gap.plan.gap_pct)}% below Friday's close. Sell at +${gap.plan.target_pct}%, stop at −${gap.plan.stop_pct}%, otherwise sell Friday.`}
            badge={
              <Badge tone={gap.this_week.status === "trade" ? "good" : gap.this_week.status === "skip" ? "warn" : "flat"}>
                {gap.this_week.status === "trade" ? "This week: trade" : gap.this_week.status === "skip" ? "This week: skip" : "No reading"}
              </Badge>
            }
            onOpen={onOpen}
          >
            <Result label={`Live since ${gap.live_from ?? "—"}`} stats={gap.live} />
            <Result label="Replay of recent weeks" stats={gap.replay} />
          </PlanRow>
        )}
        {test && (
          <PlanRow
            name="Oversold and volatile"
            rule={`${test.description}. Each Friday's picks are written down and graded a week later on whether they gained 2% or more.`}
            badge={<Badge tone="flat">{test.graded} of {test.needed} graded</Badge>}
            onOpen={onOpen}
          >
            <div className="min-w-0">
              <div className="text-[11px] text-muted-foreground">Hit rate</div>
              <div className="text-sm">
                {test.hit_rate_pct === null ? (
                  <span className="text-muted-foreground">Too early to say</span>
                ) : (
                  <>
                    <span className="font-semibold">{test.hit_rate_pct}%</span>
                    <span className="text-muted-foreground"> against {test.base_rate_pct}% for any stock</span>
                  </>
                )}
              </div>
            </div>
            <div className="min-w-0">
              <div className="text-[11px] text-muted-foreground">This week's picks</div>
              <div className="text-sm">
                {test.open.length === 0 ? (
                  <span className="text-muted-foreground">None</span>
                ) : (
                  test.open.map((s) => s.symbol).join(", ")
                )}
              </div>
            </div>
          </PlanRow>
        )}
      </div>
    </Card>
  );
}
