import { useEffect, useMemo, useState } from "react";
import { monitorTrades, type MonitorTrade } from "./api";
import { Button } from "./components/ui/button";
import { cn } from "./lib/utils";

const inr = (n: number) => `₹${Math.round(Math.abs(n)).toLocaleString("en-IN")}`;
const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${inr(n)}`;
const price = (n: number) => `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tone = (n: number) => (n > 0 ? "text-gain" : n < 0 ? "text-loss" : "text-foreground");

/** Journal timestamps are naive UTC. */
const day = (ts: unknown) =>
  typeof ts === "string"
    ? new Date(ts.endsWith("Z") ? ts : `${ts}Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" })
    : "—";

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);

const RULE_NAMES: Record<string, string> = {
  take_profit: "Target hit",
  stop_loss: "Stop hit",
  trailing_stop: "Trailing stop",
  trend_exit: "Trend exit",
  breakout: "Breakout",
};

/** "take_profit: up 4.2% against an average of ..." -> a short name and the detail. */
function trigger(raw: unknown): { name: string; detail: string | null } | null {
  const t = text(raw);
  if (!t) return null;
  const [head, ...rest] = t.split(": ");
  const key = head.trim().toLowerCase();
  if (rest.length === 0) return { name: RULE_NAMES[key] ?? t, detail: null };
  return { name: RULE_NAMES[key] ?? head.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()), detail: rest.join(": ") };
}

function Trigger({ raw, empty }: { raw: unknown; empty: string }) {
  const t = trigger(raw);
  if (!t) return <div className="text-caption text-muted-foreground">{empty}</div>;
  return (
    <div className="mt-0.5" title={t.detail ?? undefined}>
      <div className="text-caption font-semibold text-foreground">{t.name}</div>

    </div>
  );
}

/**
 * Every trade of one paper run, newest first: what bought it, what sold it, how long it was held and
 * the profit it realised after costs. Open trades are listed first with no profit, because nothing is
 * realised until they are sold.
 */
export function TradeLedger({ deploymentId }: { deploymentId: string }) {
  const [data, setData] = useState<{ closed: MonitorTrade[]; open: MonitorTrade[] } | null>(null);
  const [failed, setFailed] = useState(false);
  const [all, setAll] = useState(false);

  useEffect(() => {
    monitorTrades(deploymentId, 200)
      .then((r) => setData({ closed: r.trades, open: r.open }))
      .catch(() => setFailed(true));
  }, [deploymentId]);

  const rows = useMemo(() => {
    if (!data) return [];
    const when = (t: MonitorTrade) => String(t.exit_ts ?? t.entry_ts ?? "");
    return [...data.closed].sort((a, b) => when(b).localeCompare(when(a)));
  }, [data]);

  if (failed) return <div className="rounded-md bg-muted/40 px-3.5 py-3 text-body text-muted-foreground">Could not load trades.</div>;
  if (!data) return <div className="rounded-md bg-muted/40 px-3.5 py-3 text-body text-muted-foreground">Loading…</div>;
  if (rows.length === 0 && data.open.length === 0) {
    return <div className="rounded-md bg-muted/40 px-3.5 py-3 text-body text-muted-foreground">No trades yet.</div>;
  }

  const profit = rows.reduce((s, t) => s + (num(t.net_pnl) ?? 0), 0);
  const wins = rows.filter((t) => (num(t.net_pnl) ?? 0) > 0).length;
  const shown = all ? rows : rows.slice(0, 8);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-4">
        <div>
          <div className="text-xs text-muted-foreground">Profit</div>
          <div className={cn("mt-0.5 text-lg font-semibold tabular-nums", tone(profit))}>{signed(profit)}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Won</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums">
            {wins}<span className="text-sm font-normal text-muted-foreground"> / {rows.length}</span>
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Holding</div>
          <div className="mt-0.5 text-lg font-semibold tabular-nums">{data.open.length}</div>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border/60">
        <table className="w-full border-collapse text-left">
          <thead>
            <tr className="border-b border-border/50">
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Stock</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Bought</th>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Sold</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Profit</th>
            </tr>
          </thead>
          <tbody>
            {data.open.map((t, i) => (
              <tr key={`open-${String(t.trade_id ?? i)}`} className="border-b border-border/50 bg-muted/20 align-top">
                <td className="px-3 py-1.5 text-body font-medium">
                  {String(t.symbol).replace(/-EQ$/, "")}
                  <div className="text-caption font-normal text-muted-foreground tabular-nums">{num(t.quantity)}</div>
                </td>
                <td className="px-3 py-1.5 text-body tabular-nums">
                  {day(t.entry_ts)} · {num(t.entry_price) != null ? price(num(t.entry_price)!) : "—"}
                  <Trigger raw={t.signal_reason} empty="—" />
                </td>
                <td className="px-3 py-1.5 text-body text-muted-foreground">Holding</td>
                <td className="px-3 py-1.5 text-right text-body text-muted-foreground">open</td>
              </tr>
            ))}
            {shown.map((t, i) => {
              const net = num(t.net_pnl) ?? 0;
              const cost = (num(t.entry_price) ?? 0) * (num(t.quantity) ?? 0);
              const pct = cost > 0 ? (net / cost) * 100 : null;
              const days = num(t.duration_sec) != null ? Math.max(1, Math.round(num(t.duration_sec)! / 86400)) : null;
              return (
                <tr key={String(t.trade_id ?? i)} className="border-b border-border/50 align-top">
                  <td className="px-3 py-1.5 text-body font-medium">
                    {String(t.symbol).replace(/-EQ$/, "")}
                    <div className="text-caption font-normal text-muted-foreground tabular-nums">{num(t.quantity)}</div>
                  </td>
                  <td className="px-3 py-1.5 text-body tabular-nums">
                    {day(t.entry_ts)} · {num(t.entry_price) != null ? price(num(t.entry_price)!) : "—"}
                    <Trigger raw={t.signal_reason} empty="—" />
                  </td>
                  <td className="px-3 py-1.5 text-body tabular-nums">
                    {day(t.exit_ts)} · {num(t.exit_price) != null ? price(num(t.exit_price)!) : "—"}
                    {days != null && <span className="text-caption text-muted-foreground"> · held {days}d</span>}
                    <Trigger raw={t.exit_detail ?? t.exit_reason} empty="—" />
                  </td>
                  <td className={cn("px-3 py-1.5 text-right text-body font-semibold tabular-nums", tone(net))}>
                    {signed(net)}
                    {pct != null && <div className="text-caption font-normal">{`${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(1)}%`}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length > 8 && (
        <Button size="inline" variant="link" className="text-xs" onClick={() => setAll((v) => !v)}>
          {all ? "Fewer" : `All ${rows.length}`}
        </Button>
      )}
    </div>
  );
}
