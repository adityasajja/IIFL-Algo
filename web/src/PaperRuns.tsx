import { useEffect, useState } from "react";
import { getRunnerStatus, listSavedStrategies, monitorPnl, type Deployment, type MonitorPnl } from "./api";
import { Card } from "./components/ui/card";
import { cn } from "./lib/utils";

const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;
const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${inr(Math.abs(n))}`;
const tone = (n: number | null | undefined) =>
  !n ? "text-foreground" : n > 0 ? "text-emerald-500" : "text-rose-500";

type RunnerRow = { deployment_id: string; state?: string; symbols?: number; skipped_reason?: string | null };

/** What the run is doing, in a few words. */
function statusOf(d: Deployment, runner: RunnerRow | undefined): string {
  if (d.status === "PAUSED") return "Paused";
  if (!runner) return "Starting";
  if (runner.state === "WAITING_FOR_MARKET") return "Waiting for the market";
  if (runner.state === "WAITING_FOR_TICKS") return "Waiting for prices";
  if (runner.state === "ERROR") return "Needs attention";
  return `Watching ${runner.symbols ?? "the"} stocks`;
}

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-lg font-semibold tabular-nums tracking-tight", className)}>{value}</div>
    </div>
  );
}

function RunCard({ d, name, pnl, runner, onManage }: {
  d: Deployment;
  name: string;
  pnl: MonitorPnl | undefined;
  runner: RunnerRow | undefined;
  onManage: () => void;
}) {
  const [all, setAll] = useState(false);
  const held = (pnl?.positions ?? []).filter((p) => p.quantity !== 0);
  const shown = all ? held : held.slice(0, 5);
  const total = pnl?.total_pnl ?? 0;
  const today = pnl?.today_pnl ?? 0;

  return (
    <Card>
      <div className="space-y-4 p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="text-base font-semibold tracking-tight">
            {name} <span className="text-sm font-normal text-muted-foreground">v{d.strategy_version}</span>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <i
              className={cn(
                "size-2 rounded-full",
                d.status === "PAUSED" ? "bg-amber-500" : runner?.state === "ERROR" ? "bg-rose-500" : "bg-emerald-500",
              )}
            />
            {statusOf(d, runner)}
            <button type="button" onClick={onManage} className="ml-2 text-primary hover:underline">
              Manage
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat
            label="Profit / loss"
            value={pnl ? `${signed(total)}${pnl.total_pct != null ? ` (${pnl.total_pct.toFixed(2)}%)` : ""}` : "—"}
            className={tone(total)}
          />
          <Stat label="Today" value={pnl ? signed(today) : "—"} className={tone(today)} />
          <Stat label="Holding" value={pnl ? `${held.length} stock${held.length === 1 ? "" : "s"}` : "—"} />
          <Stat label="Practice money" value={inr(d.capital)} />
        </div>

        {pnl && held.length === 0 ? (
          <div className="rounded-xl bg-muted/40 px-3.5 py-3 text-[13px] text-muted-foreground">No trades yet.</div>
        ) : (
          <div className="divide-y divide-border/60 rounded-xl border border-border/60">
            {shown.map((p) => (
              <div key={p.symbol} className="flex items-center justify-between gap-3 px-3.5 py-2 text-[13px]">
                <span className="font-medium">{p.symbol}</span>
                <span className="text-muted-foreground tabular-nums">
                  {p.quantity} @ {p.avg_price.toFixed(2)}
                </span>
                <span className={cn("w-24 text-right tabular-nums", tone(p.unrealized_pnl))}>
                  {p.unrealized_pnl == null ? "—" : signed(p.unrealized_pnl)}
                </span>
              </div>
            ))}
            {held.length > 5 && (
              <button
                type="button"
                onClick={() => setAll((v) => !v)}
                className="w-full px-3.5 py-2 text-left text-xs text-primary hover:underline"
              >
                {all ? "Show fewer" : `Show all ${held.length}`}
              </button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

/**
 * What is running on paper and how it is doing, one card per run. The pipeline views and
 * evidence counters stay one click away, under Details, for when someone needs them.
 */
export function PaperRuns({ deployments, onManage }: { deployments: Deployment[]; onManage: (id: string) => void }) {
  const [names, setNames] = useState<Record<string, string>>({});
  const [pnls, setPnls] = useState<Record<string, MonitorPnl>>({});
  const [runners, setRunners] = useState<Record<string, RunnerRow>>({});

  const active = deployments.filter((d) => d.status === "RUNNING" || d.status === "PAUSED");
  const key = active.map((d) => d.deployment_id).join(",");

  useEffect(() => {
    listSavedStrategies()
      .then((r) => setNames(Object.fromEntries(r.strategies.map((s) => [s.strategy_id, s.name]))))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (document.visibilityState !== "visible") return;
      const results = await Promise.allSettled(active.map((d) => monitorPnl(d.deployment_id)));
      const next: Record<string, MonitorPnl> = {};
      results.forEach((r, i) => {
        if (r.status === "fulfilled") next[active[i].deployment_id] = r.value;
      });
      const status = await getRunnerStatus().catch(() => null);
      if (cancelled) return;
      setPnls((prev) => ({ ...prev, ...next }));
      const rows = Array.isArray(status?.deployments) ? (status?.deployments as unknown as RunnerRow[]) : [];
      setRunners(Object.fromEntries(rows.map((r) => [r.deployment_id, r])));
    };
    void load();
    const timer = setInterval(() => void load(), 10000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (active.length === 0) {
    return (
      <Card>
        <div className="p-5 text-sm text-muted-foreground">Nothing is running on paper. Start a strategy from the Strategies page.</div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {active.map((d) => (
        <RunCard
          key={d.deployment_id}
          d={d}
          name={names[d.strategy_id] ?? "Strategy"}
          pnl={pnls[d.deployment_id]}
          runner={runners[d.deployment_id]}
          onManage={() => onManage(d.deployment_id)}
        />
      ))}
    </div>
  );
}
