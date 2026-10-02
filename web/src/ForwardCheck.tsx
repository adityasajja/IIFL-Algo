import { useEffect, useState } from "react";
import { getForwardCheck, type ForwardCheck as Check } from "./api";
import { cn } from "./lib/utils";

const DOT = { holding_up: "bg-gain", weaker: "bg-loss", too_early: "bg-muted-foreground/50", no_backtest: "bg-muted-foreground/50" } as const;

/** One paired bar: the backtest in grey, paper in the accent colour, both on one scale. */
function Pair({ label, test, paper, unit }: { label: string; test: number; paper: number; unit: string }) {
  const top = Math.max(Math.abs(test), Math.abs(paper), 0.0001);
  const bar = (v: number, cls: string) => (
    <div className="h-1.5 rounded-full bg-muted/60">
      <div className={cn("h-full rounded-full", cls)} style={{ width: `${Math.max(2, (Math.max(v, 0) / top) * 100)}%` }} />
    </div>
  );
  const fmt = (v: number) => `${v > 0 && unit === "%" && label !== "Wins" ? "+" : ""}${v.toFixed(1)}${unit}`;
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="grid grid-cols-[3.5rem_1fr_3.5rem] items-center gap-2 text-caption">
        <span className="text-muted-foreground">Test</span>
        {bar(test, "bg-muted-foreground/60")}
        <span className="text-right tabular-nums">{fmt(test)}</span>
        <span className="font-medium">Paper</span>
        {bar(paper, "bg-primary")}
        <span className="text-right font-medium tabular-nums">{fmt(paper)}</span>
      </div>
    </div>
  );
}

/** Is paper trading doing what the backtest said? One sentence, and two bars once there is enough to compare. */
export function ForwardCheck({ strategyId, version }: { strategyId: string; version: number }) {
  const [c, setC] = useState<Check | null>(null);
  useEffect(() => {
    getForwardCheck(strategyId, version).then(setC).catch(() => setC(null));
  }, [strategyId, version]);
  if (!c || c.verdict === "no_backtest") return null; // nothing to say, so say nothing

  const done = Math.min(c.paper.trades, c.min_paper_trades);
  const show = c.verdict === "holding_up" || c.verdict === "weaker";
  return (
    <div className="space-y-3 rounded-lg border border-border/60 p-3.5">
      <div className="flex items-center gap-2 text-body font-medium">
        <i className={cn("size-2 shrink-0 rounded-full", DOT[c.verdict])} />
        {c.message}
      </div>
      {c.verdict === "too_early" && (
        <div className="h-1.5 rounded-full bg-muted/60" role="progressbar" aria-valuemin={0} aria-valuemax={c.min_paper_trades} aria-valuenow={done}>
          <div className="h-full rounded-full bg-primary" style={{ width: `${(done / c.min_paper_trades) * 100}%` }} />
        </div>
      )}
      {show && c.backtest.win_rate_pct != null && c.paper.win_rate_pct != null && c.backtest.avg_trade_pct != null && c.paper.avg_trade_pct != null && (
        <div className="grid gap-4 sm:grid-cols-2">
          <Pair label="Wins" test={c.backtest.win_rate_pct} paper={c.paper.win_rate_pct} unit="%" />
          <Pair label="Average trade" test={c.backtest.avg_trade_pct} paper={c.paper.avg_trade_pct} unit="%" />
        </div>
      )}
    </div>
  );
}
