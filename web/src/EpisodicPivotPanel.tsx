import { Check, Minus, X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  getEpisodicPivot,
  type EpisodicPivotPremise,
  type EpisodicPivotReport,
  type EpisodicPivotVariant,
} from "./api";
import { Card, CardHeader, Hint } from "./components/ui/card";
import { Badge, Callout, fmtNum, fmtPct } from "./components/ui/stat";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/**
 * Pradeep Bonde's Episodic Pivot playbook, measured on NSE universes.
 *
 * The panel shows two things side by side on purpose. The playbook's premise is
 * that a neglected stock routinely gaps 20–40% and re-rates 100–300% in weeks;
 * that is a claim about a *pond*, and it is measurable before any strategy is
 * scored. If the pond has no such fish, "no trades" means the universe cannot
 * express the setup — which is a different finding from "the rules lose money".
 * Reporting a single backtest number would collapse those two into one.
 */
export default function EpisodicPivotPanel() {
  const [report, setReport] = useState<EpisodicPivotReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await getEpisodicPivot();
        if (alive) setReport(r);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <Card>
        <CardHeader title="Episodic Pivot" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">{error}</div>
      </Card>
    );
  }

  if (!report) {
    return (
      <Card>
        <CardHeader title="Episodic Pivot" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">Loading…</div>
      </Card>
    );
  }

  if (!report.available) {
    return (
      <Card>
        <CardHeader title="Episodic Pivot" />
        <div className="px-4 pb-4">
          <Callout tone="warn">
            Not run yet:
            <code className="mt-2 block rounded-md bg-muted/60 px-2 py-1 text-xs">
              {report.hint ??
                ".venv/Scripts/python.exe scripts/validate_episodic_pivot.py"}
            </code>
          </Callout>
        </div>
      </Card>
    );
  }

  const universes = Object.entries(report.universes ?? {});
  const all = universes.flatMap(([, u]) => Object.values(u.variants ?? {}));
  const scored = all.filter((v) => !v.error);
  const passed = scored.filter((v) => v.passed).length;
  const premise = report.premise?.universes ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Episodic Pivot — measured"
          action={
            <Badge tone={passed > 0 ? "good" : "warn"}>
              {passed}/{scored.length} passed
            </Badge>
          }
        />
        <div className="space-y-3 px-4 pb-4">
          {passed === 0 && scored.length > 0 && (
            <Callout tone="warn">
              <span title="A daily bar cannot read an earnings release, so the catalyst is proxied by an abnormal gap plus abnormal volume. Entry fills at the next open and stops are resting sell-stops, so these numbers are what a daily-bar implementation would have paid.">
                None cleared the bar
              </span>
            </Callout>
          )}

          {premise.length > 0 && <PremiseTable rows={premise} />}

          {universes.map(([name, u]) => (
            <div key={name} className="space-y-2">
              <div className="flex items-baseline justify-between">
                <span className="text-body font-medium capitalize">{pretty(name)}</span>
                <span className="text-caption text-muted-foreground">
                  {u.symbols} symbols · {u.sessions} sessions · {u.window}
                </span>
              </div>
              <div className="overflow-x-auto rounded-lg border border-border/60">
                <table className="w-full text-body">
                  <thead className="bg-muted/40">
                    <tr>
                      <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Variant</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">OOS Sharpe</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Buy &amp; hold</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">OOS return</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">vs control</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Trades</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Expectancy</th>
                      <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Hold</th>
                      <th className="px-3 py-2 text-center text-micro font-semibold uppercase tracking-wider text-muted-foreground">Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(u.variants ?? {}).map(([v, r]) => (
                      <VariantRow key={v} name={v} r={r} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}

          <Hint>
            <span title="vs control: same symbols, holding periods and sizes, with only the entry date randomised. Near or below zero means the timing added nothing.">
              vs control: random entry dates
            </span>
          </Hint>
        </div>
      </Card>

      <Card>
        <CardHeader title="Every check" />
        <div className="space-y-3 px-4 pb-4">
          {universes.flatMap(([uname, u]) =>
            Object.entries(u.variants ?? {}).map(([v, r]) => (
              <div key={`${uname}-${v}`} className="rounded-lg border border-border/60 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-body font-medium">
                    {pretty(v)}
                    <span className="ml-2 text-caption font-normal text-muted-foreground">
                      {uname}
                    </span>
                  </span>
                  <span className="text-caption text-muted-foreground">
                    {r.folds} folds · {r.n_trials} trials
                  </span>
                </div>
                {r.error ? (
                  <div className="text-[12.5px] text-destructive">{r.error}</div>
                ) : (
                  <ul className="space-y-1">
                    {(r.checks ?? []).map((c) => (
                      <li key={c.name} className="flex items-start gap-2 text-[12.5px]">
                        <span className="mt-0.5 shrink-0">
                          {c.ok ? (
                            <Check className="h-3.5 w-3.5 text-gain" />
                          ) : (
                            <X className="h-3.5 w-3.5 text-destructive" />
                          )}
                        </span>
                        <span className={cn(c.ok ? "text-muted-foreground" : "text-foreground")}>
                          {c.name}
                          <span className="ml-1.5 text-muted-foreground">— {c.detail}</span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )),
          )}
        </div>
      </Card>

      <Hint>
        Generated <RelativeTime value={report.generated_at} /> ·{" "}
        {String(report.config?.train_bars)} train / {String(report.config?.test_bars)} test /{" "}
        {String(report.config?.warmup_bars)} warmup ·{" "}
        {String(report.config?.slippage_bps)} bps slippage ·{" "}
        {String(report.config?.note ?? "")}
      </Hint>
    </div>
  );
}

function PremiseTable({ rows }: { rows: EpisodicPivotPremise[] }) {
  return (
    <div className="rounded-lg border border-border/60">
      <div className="border-b border-border/60 px-3 py-2">
        <span className="text-[12.5px] font-medium">Does the pond hold the fish?</span>

      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[12.5px]">
          <thead className="bg-muted/40">
            <tr>
              <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Universe</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Gap ≥ 10%</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Gap ≥ 20%</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Catalyst days</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Best 20d move</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">20d &gt; +50%</th>
              <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">20d fwd after catalyst</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.universe} className="border-t border-border/60">
                <td className="px-3 py-1.5 capitalize">{pretty(p.universe)}</td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {p.gap_days?.["10"] ?? 0}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {p.gap_days?.["20"] ?? 0}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {p.joint_catalyst_days}
                  <span className="ml-1 text-caption text-muted-foreground">
                    ({p.joint_catalyst_pct_of_days?.toFixed(3)}%)
                  </span>
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {p.best_20d_move_pct !== null ? fmtPct(p.best_20d_move_pct, 0) : "—"}
                  {p.best_20d_symbol && (
                    <span className="ml-1 text-caption text-muted-foreground">
                      {p.best_20d_symbol}
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {p.windows_20d_over_50pct?.toLocaleString()}
                  <span className="ml-1 text-caption text-muted-foreground">
                    ({p.windows_20d_over_50pct_share?.toFixed(3)}%)
                  </span>
                </td>
                <td
                  className={cn(
                    "px-3 py-2 text-right tabular-nums",
                    (p.fwd20_after_catalyst_pct ?? 0) >= (p.fwd20_unconditional_pct ?? 0)
                      ? "text-gain"
                      : "text-muted-foreground",
                  )}
                >
                  {p.fwd20_after_catalyst_pct !== null
                    ? fmtPct(p.fwd20_after_catalyst_pct, 2)
                    : "—"}
                  <span className="ml-1 text-caption text-muted-foreground">
                    vs {p.fwd20_unconditional_pct !== null
                      ? fmtPct(p.fwd20_unconditional_pct, 2)
                      : "—"}{" "}
                    baseline
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-border/60 px-3 py-2 text-[11.5px] text-muted-foreground">
        The playbook calls 20–40% gaps routine and cites SMCI +189% in 28 sessions,
        ROOT +358% in 16 and ANF +240%. Count the ≥&nbsp;20% column before trusting
        those numbers to transfer to an index universe.
      </div>
    </div>
  );
}

function VariantRow({ name, r }: { name: string; r: EpisodicPivotVariant }) {
  const z = r.sharpe_z_vs_control;
  const m = r.measured;
  if (r.error) {
    return (
      <tr className="border-t border-border/60">
        <td className="px-3 py-1.5 font-medium">{pretty(name)}</td>
        <td className="px-3 py-1.5 text-destructive" colSpan={8}>
          {r.error}
        </td>
      </tr>
    );
  }
  return (
    <tr className="border-t border-border/60">
      <td className="px-3 py-1.5 whitespace-nowrap font-medium">{pretty(name)}</td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          (r.oos_sharpe ?? 0) >= 0 ? "text-gain" : "text-destructive",
        )}
      >
        {fmtNum(r.oos_sharpe)}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
        {fmtNum(r.benchmark_sharpe)}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          (r.oos_return_pct ?? 0) >= 0
          ? "text-gain"
            : "text-destructive",
        )}
      >
        {fmtPct(r.oos_return_pct ?? 0, 1)}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums">
        {z === null || z === undefined ? (
          <Minus className="ml-auto h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <span
            className={cn(
              z >= 2 ? "font-medium text-gain" : "text-muted-foreground",
            )}
          >
            {z >= 0 ? "+" : ""}
            {fmtNum(z)}σ
          </span>
        )}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
        {m?.trades?.toLocaleString() ?? r.oos_trades?.toLocaleString() ?? "—"}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          (m?.expectancy_r ?? 0) >= 0
          ? "text-gain"
            : "text-destructive",
        )}
      >
        {m?.expectancy_r !== null && m?.expectancy_r !== undefined
          ? `${m.expectancy_r >= 0 ? "+" : ""}${fmtNum(m.expectancy_r, 3)}R`
          : "—"}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
        {m?.median_hold_days !== null && m?.median_hold_days !== undefined
          ? `${m.median_hold_days}d`
          : "—"}
      </td>
      <td className="px-3 py-1.5 text-center">
        {r.passed ? <Badge tone="good">pass</Badge> : <Badge tone="warn">fail</Badge>}
      </td>
    </tr>
  );
}

const VARIANTS: Record<string, string> = {
  episodic_pivot_day1: "Day 1 (gap on the day)",
  episodic_pivot_delayed: "Delayed reaction",
  episodic_pivot_9m: "9M (volume only)",
};

const UNIVERSES: Record<string, string> = {
  nifty50: "Nifty 50",
  next50: "Nifty Next 50",
  midcap150: "Midcap 150",
  smallcap250: "Smallcap 250",
};

const pretty = (s: string) =>
  VARIANTS[s] ??
  UNIVERSES[s] ??
  s
    .replace(/^episodic_pivot_?/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
