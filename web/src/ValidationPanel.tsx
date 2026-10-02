import { Check, Minus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { getValidation, type ValidationReport, type ValidationResult } from "./api";
import { Card, CardHeader, Hint } from "./components/ui/card";
import { Badge, Callout, fmtNum, fmtPct } from "./components/ui/stat";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/**
 * Measured results for the academic paper strategies.
 *
 * Why this panel exists: `atr.research.papers` used to carry hardcoded win
 * rates (0.58, 0.64, …) and confidence scores (0.85–0.94) that the UI presented
 * as findings. They were citations dressed up as measurements. This renders the
 * walk-forward output instead, including the random-selection control — so the
 * claim and the evidence sit next to each other.
 */
export default function ValidationPanel() {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await getValidation();
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
        <CardHeader title="Measured performance" sub="Out-of-sample results" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">{error}</div>
      </Card>
    );
  }

  if (!report) {
    return (
      <Card>
        <CardHeader title="Measured performance" sub="Loading…" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">Reading validation report…</div>
      </Card>
    );
  }

  if (!report.available) {
    return (
      <Card>
        <CardHeader
          title="Measured performance"
          sub="No run yet"
        />
        <div className="px-4 pb-4">
          <Callout tone="warn">
            Run the harness:
            <code className="mt-2 block rounded-md bg-muted/60 px-2 py-1 text-xs">
              .venv/Scripts/python.exe scripts/validate_paper_strategies.py
            </code>
          </Callout>
        </div>
      </Card>
    );
  }

  const results = (report.results ?? []).filter((r) => !r.error);
  const ctrl = report.control;
  const passed = results.filter((r) => r.passed).length;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Measured performance"
          sub={`Walk-forward over ${report.universe?.length ?? 0} NSE large caps · ${report.window ?? ""}`}
          action={
            <Badge tone={passed > 0 ? "good" : "warn"}>{passed}/{results.length} passed</Badge>
          }
        />
        <div className="space-y-3 px-4 pb-4">
          {passed === 0 && (
            <Callout tone="warn">
              <span title="The best out-of-sample Sharpe is still below buy and hold on the same window. Claimed = literature figure from papers.py; Measured = actual result.">
                None cleared the bar. Not actionable.
              </span>
            </Callout>
          )}

          {ctrl && ctrl.sharpe_mean !== null && (
            <div className="grid gap-3 sm:grid-cols-3">
              <ControlStat
                label="Random control, mean Sharpe"
                value={fmtNum(ctrl.sharpe_mean)}
                hint={`${ctrl.sharpes.length} runs`}
              />
              <ControlStat
                label="Buy & hold Sharpe"
                value={fmtNum(results[0]?.benchmark_sharpe)}
                hint={`return ${fmtPct(results[0]?.benchmark_return_pct ?? 0, 1)}`}
              />
              <ControlStat
                label="Best model Sharpe"
                value={fmtNum(Math.max(...results.map((r) => r.oos_sharpe ?? -Infinity)))}
                hint="must beat both"
              />
            </div>
          )}

          <div className="overflow-x-auto rounded-lg border border-border/60">
            <table className="w-full text-body">
              <thead className="bg-muted/40">
                <tr>
                  <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Strategy</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Claimed</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Measured</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">OOS Sharpe</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">OOS return</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground" title="Std deviations above random selection. Under about +2σ could easily be luck.">vs control</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Trades</th>
                  <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground" title="Realised P&L per unit of risk across out-of-sample trades. Negative = lost money on average.">Expectancy</th>
                  <th className="px-3 py-2 text-center text-micro font-semibold uppercase tracking-wider text-muted-foreground">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <ResultRow key={r.strategy} r={r} />
                ))}
              </tbody>
            </table>
          </div>

        </div>
      </Card>

      {results.length > 0 && (
        <Card>
          <CardHeader title="Per-model detail" sub="All models" />
          <div className="space-y-3 px-4 pb-4">
            {results.map((r) => (
              <div key={r.strategy} className="rounded-lg border border-border/60 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-body font-medium">{pretty(r.strategy)}</span>
                  <span className="text-caption text-muted-foreground">
                    {r.folds} folds · {r.n_trials} trials
                  </span>
                </div>
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
              </div>
            ))}
          </div>
        </Card>
      )}

      <Hint>
        Generated <RelativeTime value={report.generated_at} /> ·{" "}
        {report.symbol_bars?.toLocaleString()} symbol-bars ·{" "}
        {String(report.config?.train_bars)} train / {String(report.config?.test_bars)} test /{" "}
        {String(report.config?.warmup_bars)} warmup · {String(report.config?.slippage_bps)} bps
        slippage · dated, not live
      </Hint>
    </div>
  );
}

function ResultRow({ r }: { r: ValidationResult }) {
  const z = r.sharpe_z_vs_control;
  return (
    <tr className="border-t border-border/60">
      <td className="px-3 py-1.5 whitespace-nowrap font-medium">{pretty(r.strategy)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
        {claimed(r.strategy)}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums">
        {r.measured_win_rate !== undefined ? fmtPct(r.measured_win_rate * 100, 1) : "—"}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          (r.oos_sharpe ?? 0) >= 0 ? "text-gain" : "text-destructive",
        )}
      >
        {fmtNum(r.oos_sharpe)}
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
          <span className={cn(z >= 2 ? "font-medium text-gain" : "text-muted-foreground")}>
            {z >= 0 ? "+" : ""}
            {fmtNum(z)}σ
          </span>
        )}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
        {r.measured_trades?.toLocaleString() ?? "—"}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          (r.measured_expectancy_r ?? 0) >= 0
          ? "text-gain"
            : "text-destructive",
        )}
      >
        {r.measured_expectancy_r !== undefined
          ? `${r.measured_expectancy_r >= 0 ? "+" : ""}${fmtNum(r.measured_expectancy_r, 3)}R`
          : "—"}
      </td>
      <td className="px-3 py-1.5 text-center">
        {r.passed ? (
          <Badge tone="good">pass</Badge>
        ) : (
          <Badge tone="warn">fail</Badge>
        )}
      </td>
    </tr>
  );
}

function ControlStat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="rounded-md bg-muted/40 p-3">
      <div className="text-caption uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-xl font-medium tabular-nums">{value}</div>
      <div className="mt-0.5 text-caption text-muted-foreground">{hint}</div>
    </div>
  );
}

/** The win rates `papers.py` used to assert from the literature. */
const CLAIMED: Record<string, string> = {
  paper_avellaneda_lee: "64.0%",
  paper_iima_nse_momentum: "62.0%",
  paper_jegadeesh_titman: "58.0%",
  paper_multi_factor_composite: "60.0%",
  paper_nism_52w_high: "59.0%",
  paper_sehgal_low_vol: "65.0%",
  paper_volatility_breakout: "54.0%",
};

const claimed = (s: string) => CLAIMED[s] ?? "—";

const pretty = (s: string) =>
  s
    .replace(/^paper_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
