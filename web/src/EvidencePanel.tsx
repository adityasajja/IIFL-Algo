import { AlertTriangle, ShieldCheck, ShieldX } from "lucide-react";
import { useEffect, useState } from "react";
import { getEvidence, type EvidenceFinding, type EvidenceReport } from "./api";
import { Card, CardHeader, Hint } from "./components/ui/card";
import { Badge, Callout, Stat, VerdictPill } from "./components/ui/stat";
import { RelativeTime } from "./lib/time";
import { cn } from "./lib/utils";

/**
 * Findings with their credibility verdicts attached.
 *
 * Why every number here carries a verdict rather than standing alone: each of
 * these results was individually correct when it was first produced, and
 * collectively they were misleading.
 *
 * - A +480% index that was really +124% once a look-ahead universe selection
 * was removed.
 * - A "factor" that returned +4.07%/month when the market rose and -2.06% when
 * it fell — beta in a costume.
 * - Five risk overlays that beat buy-and-hold on the full window and lost on
 * every second half.
 *
 * So the panel is built the other way round from a normal results page. The
 * verdict comes first, the headline number second, and the evidence that
 * produced the verdict is always shown. A number cannot be read in isolation.
 */
export default function EvidencePanel() {
  const [report, setReport] = useState<EvidenceReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await getEvidence();
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
        <CardHeader title="Evidence" sub="Findings with their verdicts" />
        <div className="px-5 pb-4 text-sm text-muted-foreground">{error}</div>
      </Card>
    );
  }

  if (!report) {
    return (
      <Card>
        <CardHeader title="Evidence" sub="Loading…" />
        <div className="px-5 pb-4 text-sm text-muted-foreground">
          Loading…
        </div>
      </Card>
    );
  }

  if (!report.available) {
    return (
      <Card>
        <CardHeader title="Evidence" sub="No run yet" />
        <div className="px-5 pb-4">
          <Callout tone="warn">
            Generate findings:
            <code className="mt-2 block rounded-md bg-muted/60 px-2 py-1 text-xs">
              .venv/Scripts/python.exe scripts/research_honest_verdicts.py
            </code>
          </Callout>
          {report.hint ? <Hint className="mt-2">{report.hint}</Hint> : null}
        </div>
      </Card>
    );
  }

  const findings = report.findings ?? [];
  const credible = report.credible_count ?? 0;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Evidence"
          sub={
            <>
              {findings.length} findings ·{" "}
              {report.generated_at ? (
                <RelativeTime value={report.generated_at} />
              ) : (
                "generated date unknown"
              )}
            </>
          }
        />
        <div className="grid grid-cols-2 gap-3 px-5 pb-4 sm:grid-cols-4">
          <Stat label="Findings" value={findings.length} />
          <Stat
            label="Credible"
            value={credible}
            tone={credible > 0 ? "good" : "bad"}
            sub={credible === 0 ? "none survived" : undefined}
          />
          <Stat
            label="Rejected"
            value={findings.length - credible}
            tone={findings.length - credible > 0 ? "warn" : "neutral"}
          />
          <Stat
            label="Tests applied"
            value="7"
            sub="bias · trials · regime · stability"
          />
        </div>
        <div className="px-5 pb-5">
          <Callout tone={credible === 0 ? "warn" : "info"}>
            {credible === 0
              ? "Nothing here is safe to trade."
              : `${credible} of ${findings.length} cleared every test. Not the same as profitable.`}
          </Callout>
        </div>
      </Card>

      {findings.map((f) => (
        <FindingCard key={f.title} finding={f} />
      ))}
    </div>
  );
}

function FindingCard({ finding }: { finding: EvidenceFinding }) {
  const { verdict } = finding;
  const ok = verdict.credible;
  const regime = finding.regime;
  const stability = finding.stability;
  const bias = finding.selection_bias;

  return (
    <Card>
      <CardHeader
        title={finding.title}
        sub={finding.claim}
        action={
          <VerdictPill passed={ok}>
            <span className="flex items-center gap-1">
              {ok ? (
                <ShieldCheck className="h-3.5 w-3.5" />
              ) : (
                <ShieldX className="h-3.5 w-3.5" />
              )}
              {ok ? "Credible" : "Rejected"}
            </span>
          </VerdictPill>
        }
      />

      <div className="grid grid-cols-2 gap-3 px-5 pt-3 sm:grid-cols-4">
        <Stat
          label="Headline"
          value={`${finding.headline_pct >= 0 ? "+" : ""}${finding.headline_pct.toFixed(1)}%`}
          sub="rejected = ignore"
          tone={ok ? "neutral" : "warn"}
        />
        <Stat
          label="OOS Sharpe"
          value={finding.oos_sharpe?.toFixed(2) ?? "n/a"}
          sub={
            finding.benchmark_sharpe != null
              ? `buy & hold ${finding.benchmark_sharpe.toFixed(2)}`
              : undefined
          }
          tone={
            finding.oos_sharpe != null &&
            finding.benchmark_sharpe != null &&
            finding.oos_sharpe > finding.benchmark_sharpe
              ? "good"
              : "bad"
          }
        />
        <Stat
          label="Max drawdown"
          value={`${finding.max_drawdown_pct.toFixed(1)}%`}
          sub={`buy & hold ${finding.benchmark_drawdown_pct.toFixed(1)}%`}
          tone={
            finding.max_drawdown_pct > finding.benchmark_drawdown_pct + 5
              ? "bad"
              : "neutral"
          }
        />
        <Stat
          label="Trials run"
          value={finding.n_trials}
          sub="ideas tried"
          tone={finding.n_trials >= 20 ? "warn" : "neutral"}
        />
      </div>

      <div className="space-y-3 px-5 py-4">
        <div className="text-caption font-medium uppercase tracking-[0.05em] text-muted-foreground">
          Why this verdict
        </div>
        <ul className="space-y-1.5">
          {verdict.reasons.map((r, i) => (
            <li key={i} className="flex gap-2 text-body leading-relaxed">
              <AlertTriangle
                className={cn(
                  "mt-0.5 h-3.5 w-3.5 shrink-0",
                  ok ? "text-muted-foreground" : "text-warning",
                )}
              />
              <span className={ok ? "text-muted-foreground" : ""}>{r}</span>
            </li>
          ))}
        </ul>

        <div className="flex flex-wrap gap-1.5 pt-1">
          {bias ? (
            <Badge tone={bias.meaningful ? "bad" : "good"}>
              selection gap {bias.gap_pct >= 0 ? "+" : ""}
              {bias.gap_pct.toFixed(0)} pts
            </Badge>
          ) : null}
          {regime ? (
            <Badge tone={regime.is_leverage ? "bad" : "good"}>
              {regime.is_leverage ? "leverage, not edge" : "edge holds both ways"}
            </Badge>
          ) : null}
          {stability ? (
            <Badge tone={stability.consistent ? "good" : "bad"}>
              {stability.consistent ? "stable" : "unstable"} across time
            </Badge>
          ) : null}
          <span title={finding.notes.join(" ") || undefined}>
            <Badge tone="flat">{finding.universe}</Badge>
          </span>
        </div>

      </div>
    </Card>
  );
}
