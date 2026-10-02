import { useCallback, useEffect, useState } from "react";
import { getLatestReconcile, runReconcile, type ReconcileRun } from "./api";
import { Button } from "./components/ui/button";
import { Badge, Callout } from "./components/ui/stat";

const TONE = { ok: "good", warning: "warn", critical: "bad" } as const;

/**
 * Does the app's record of orders, positions and funds match the broker's own? A mismatch means a
 * limit may be guarding the wrong number, so this is shown next to the account, not buried.
 */
export default function ReconcileCard() {
  const [run, setRun] = useState<ReconcileRun | null>(null);
  const [none, setNone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getLatestReconcile()
      .then(setRun)
      .catch(() => setNone(true));
  }, []);

  const check = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      setRun(await runReconcile());
      setNone(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const tone = run ? (TONE[run.severity as keyof typeof TONE] ?? "flat") : "flat";
  const skipped = run?.results.filter((r) => !r.compared) ?? [];

  return (
    <div className="rounded-lg border border-border/80 bg-card/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          Matches the broker?
          {run && <Badge tone={tone}>{run.severity === "ok" ? "MATCHES" : run.severity.toUpperCase()}</Badge>}
          {!run && none && <Badge tone="flat">NOT CHECKED YET</Badge>}
        </div>
        <Button size="xs" variant="outline" onClick={() => void check()} disabled={busy}>
          {busy ? "Checking…" : "Check now"}
        </Button>
      </div>
      {err && (
        <div className="mt-3">
          <Callout tone="bad">{err}</Callout>
        </div>
      )}
      {run?.error && (
        <div className="mt-3">
          <Callout tone="bad" title="Could not read the broker">
            {run.error}
          </Callout>
        </div>
      )}
      {run && run.detail.length > 0 && (
        <ul className="mt-3 space-y-1.5 text-xs">
          {run.detail.slice(0, 8).map((d, i) => (
            <li key={`${d.scope}-${d.key}-${i}`} className="flex items-start gap-2">
              <Badge tone={TONE[d.severity as keyof typeof TONE] ?? "flat"}>{d.scope}</Badge>
              <span>
                <span className="font-medium">{d.key}</span> — {d.detail}
              </span>
            </li>
          ))}
        </ul>
      )}
      {skipped.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          Not compared: {skipped.map((r) => `${r.scope}${r.skipped_reason ? ` (${r.skipped_reason})` : ""}`).join(", ")}.
        </p>
      )}
    </div>
  );
}
