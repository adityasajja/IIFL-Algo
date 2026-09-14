import { Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { getAlphaHunt, type AlphaHuntHypothesis, type AlphaHuntReport } from "./api";
import { Card, CardHeader, Hint } from "./components/ui/card";
import { Badge, Callout, fmtNum, fmtPct } from "./components/ui/stat";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/**
 * Pre-registered candidate edges, each scored against a null that removes only
 * its signal.
 *
 * The panel shows the *prior* and the *null* next to every number on purpose.
 * A candidate list assembled after seeing results is not a test, and a Sharpe
 * without a control is not evidence — it is a number that happens to be large.
 */
export default function AlphaHuntPanel() {
  const [report, setReport] = useState<AlphaHuntReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await getAlphaHunt();
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
        <CardHeader title="Candidate edges" sub="Pre-registered hypotheses" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">{error}</div>
      </Card>
    );
  }

  if (!report) {
    return (
      <Card>
        <CardHeader title="Candidate edges" sub="Loading…" />
        <div className="px-4 pb-4 text-sm text-muted-foreground">Reading results…</div>
      </Card>
    );
  }

  if (!report.available) {
    return (
      <Card>
        <CardHeader title="Candidate edges" sub="Not measured yet" />
        <div className="px-4 pb-4">
          <Callout tone="warn">
            Run the harness first:
            <code className="mt-2 block rounded bg-muted/60 px-2 py-1 text-[12px]">
              {report.hint ?? ".venv/Scripts/python.exe scripts/research_alpha_hunt.py"}
            </code>
          </Callout>
        </div>
      </Card>
    );
  }

  const universes = Object.entries(report.universes ?? {});
  const all = universes.flatMap(([, u]) => Object.values(u.hypotheses ?? {}));
  const scored = all.filter((h) => !h.error);
  const passed = scored.filter((h) => h.verdict?.passed).length;
  const priors = report.pre_registered ?? {};

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Candidate edges — pre-registered"
          sub="Priors and parameter grids fixed before any result was seen"
          action={
            <Badge tone={passed > 0 ? "good" : "warn"}>
              {passed}/{scored.length} passed
            </Badge>
          }
        />
        <div className="space-y-3 px-4 pb-4">
          <Callout tone={passed > 0 ? "info" : "warn"}>
            <span className="font-medium">
              Every candidate is scored against a null that removes only its signal.
            </span>{" "}
            Reversal is compared to the same universe and cadence with{" "}
            <em>random names</em>; the exposure overlays to the same exposure
            sequence with the <em>dates shifted</em> — same sizing, wrong days.
            {passed === 0 && scored.length > 0 && (
              <>
                {" "}
                None cleared the bar. Two of them are directionally positive but
                under-powered at this sample size, which is a different verdict
                from "wrong" — see the note underneath.
              </>
            )}
          </Callout>

          {universes.map(([name, u]) => (
            <div key={name} className="space-y-2">
              <div className="flex items-baseline justify-between">
                <span className="text-[13px] font-medium">{prettyUniverse(name)}</span>
                <span className="text-[11px] text-muted-foreground">
                  {u.symbols} symbols · {u.sessions} sessions
                </span>
              </div>
              <div className="overflow-x-auto rounded-lg border border-border/60">
                <table className="w-full text-[13px]">
                  <thead className="bg-muted/40 text-[11px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Candidate</th>
                      <th className="px-3 py-2 text-right font-medium">Sharpe</th>
                      <th className="px-3 py-2 text-right font-medium">Calmar</th>
                      <th className="px-3 py-2 text-right font-medium">Return</th>
                      <th className="px-3 py-2 text-right font-medium">Buy &amp; hold</th>
                      <th className="px-3 py-2 text-right font-medium">Control</th>
                      <th className="px-3 py-2 text-right font-medium">vs control</th>
                      <th className="px-3 py-2 text-right font-medium">Trades</th>
                      <th className="px-3 py-2 text-center font-medium">Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(u.hypotheses ?? {}).map(([h, r]) => (
                      <Row key={h} name={h} r={r} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {Object.keys(priors).length > 0 && (
        <Card>
          <CardHeader title="Why these three" sub="The prior, and the condition that falsifies it" />
          <div className="space-y-3 px-4 pb-4">
            {Object.entries(priors).map(([name, spec]) => (
              <div key={name} className="rounded-lg border border-border/60 p-3">
                <div className="text-[13px] font-medium">{prettyName(name)}</div>
                <div className="mt-0.5 text-[12.5px] text-muted-foreground">{spec.prior}</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {Object.entries(spec.grid).map(([param, values]) => (
                    <span
                      key={param}
                      className="rounded bg-muted/60 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                    >
                      {param}: {values.join(", ")}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card>
        <CardHeader title="Every check, including the failures" sub="Per candidate and universe" />
        <div className="space-y-3 px-4 pb-4">
          {universes.flatMap(([uname, u]) =>
            Object.entries(u.hypotheses ?? {}).map(([h, r]) => (
              <div key={`${uname}-${h}`} className="rounded-lg border border-border/60 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-[13px] font-medium">
                    {prettyName(h)}
                    <span className="ml-2 text-[11px] font-normal text-muted-foreground">
                      {prettyUniverse(uname)}
                    </span>
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {r.metrics?.n_trials} trials
                    {r.folds && ` · folds ${r.folds.map((f) => fmtNum(f.test_sharpe, 1)).join(" / ")}`}
                  </span>
                </div>
                {r.error ? (
                  <div className="text-[12.5px] text-destructive">{r.error}</div>
                ) : (
                  <ul className="space-y-1">
                    {(r.verdict?.checks ?? []).map((c) => (
                      <li key={c.name} className="flex items-start gap-2 text-[12.5px]">
                        <span className="mt-0.5 shrink-0">
                          {c.ok ? (
                            <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
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
        <span className="font-medium">vs control</span> is a t-statistic, not a
        z-score: the control&apos;s spread is estimated from a handful of runs, so
        the threshold is the one-sided 95% t value at that many degrees of freedom
        — <span className="font-medium">2.92</span> at 3 runs,{" "}
        <span className="font-medium">1.90</span> at 8. A flat "2 sigma" would
        pass comparisons the data cannot support. The first run of this harness
        reported +2.51σ off three control runs; with eight it was +0.56σ.
      </Hint>

      <Hint>
        Generated <RelativeTime value={report.generated_at} /> ·{" "}
        {String(report.config?.train_bars)} train / {String(report.config?.test_bars)} test /{" "}
        {String(report.config?.warmup_bars)} warmup · cost model{" "}
        {String(report.config?.cost_model ?? "ibkr")} ·{" "}
        {String(report.config?.slippage_bps)} bps slippage.
      </Hint>
    </div>
  );
}

function Row({ name, r }: { name: string; r: AlphaHuntHypothesis }) {
  const m = r.metrics;
  const z = r.verdict?.sharpe_z_vs_control;
  const critical = r.verdict?.t_critical;
  if (r.error || !m) {
    return (
      <tr className="border-t border-border/60">
        <td className="px-3 py-2 font-medium">{prettyName(name)}</td>
        <td className="px-3 py-2 text-destructive" colSpan={8}>
          {r.error ?? "no metrics"}
        </td>
      </tr>
    );
  }
  const clears = z !== null && z !== undefined && critical !== null && critical !== undefined && z >= critical;
  return (
    <tr className="border-t border-border/60">
      <td className="px-3 py-2 whitespace-nowrap font-medium">{prettyName(name)}</td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          m.oos_sharpe >= m.bench_sharpe ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground",
        )}
      >
        {fmtNum(m.oos_sharpe)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(m.oos_calmar)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtPct(m.oos_return_pct, 1)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtPct(m.bench_return_pct, 1)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {fmtNum(r.control?.sharpe_mean ?? undefined)}
      </td>
      <td
        className={cn(
          "px-3 py-2 text-right tabular-nums",
          clears ? "font-medium text-emerald-600 dark:text-emerald-400" : "text-muted-foreground",
        )}
      >
        {z === null || z === undefined ? "—" : `${z >= 0 ? "+" : ""}${fmtNum(z)}σ`}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
        {m.trades.toLocaleString()}
      </td>
      <td className="px-3 py-2 text-center">
        {r.verdict?.passed ? <Badge tone="good">pass</Badge> : <Badge tone="warn">fail</Badge>}
      </td>
    </tr>
  );
}

const NAMES: Record<string, string> = {
  reversal_short_horizon: "Short-horizon reversal",
  vol_managed_exposure: "Volatility-managed exposure",
  trend_filtered_exposure: "Trend-filtered exposure",
};

const UNIVERSE_NAMES: Record<string, string> = {
  nifty50: "Nifty 50",
  midcap150: "Midcap 150",
  smallcap250: "Smallcap 250",
};

const prettyName = (s: string) =>
  NAMES[s] ?? s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const prettyUniverse = (s: string) => UNIVERSE_NAMES[s] ?? s;
