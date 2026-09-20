import { AlertTriangle, Plus, ShieldCheck, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  createSavedStrategy,
  createStrategyVersion,
  listDeployments,
  listSavedStrategies,
  removeSavedStrategy,
  listStrategyVersions,
  validateStrategy,
  type SavedStrategy,
  type StrategyValidation,
  type StrategyVersion,
} from "./api";
import { Card, CardHeader, ErrorBox } from "./components/ui/card";
import { useDialog } from "./components/ui/dialog-context";
import { Badge } from "./components/ui/stat";
import { StrategyTiles } from "./StrategyTiles";
import { strategyLabel } from "./lib/format";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/**
 * Strategies — what is running, and the rules you have built.
 *
 * The status tiles at the top show what is doing something now. The 18 built-in
 * strategies that used to be listed here were removed: none had passed validation, so
 * the list was noise. Their engines still exist for backtests and the Evidence page.
 *
 * **Your strategies** is the authoring half: create a strategy, append an
 * immutable version, validate it, and then deploy that exact version on the Paper
 * screen. A deployment pins `(strategy_id, strategy_version)`, and this is the
 * only place that pair can be produced from the screen. Nothing here claims a
 * strategy works; validation answers "will this execute", and the panel says so
 * in as many words rather than letting a green tick imply the other question.
 */
export default function StrategiesPanel({
  onOpenPaper,
}: {
  onOpenPaper: () => void;
}) {
  const [running, setRunning] = useState(0);

  useEffect(() => {
    listDeployments()
      .then((r) => setRunning((r?.deployments ?? []).filter((d) => d.status === "RUNNING").length))
      .catch(() => setRunning(0));
  }, []);

  // The 18 built-in strategies used to be listed here. None had proven itself, so the list
  // was noise; the engines still exist for backtests and the Evidence page.
  return (
    <Authoring
      tiles={<StrategyTiles running={running} onOpenPaper={onOpenPaper} />}
    />
  );
}

/** The server's own sentence, not the JSON it arrived in. */
function readable(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    const body = JSON.parse(raw) as { detail?: string | { detail?: string } };
    const d = body.detail;
    const text = typeof d === "string" ? d : d?.detail;
    if (text) return text;
  } catch {
    // not JSON: use it as it is
  }
  return raw;
}

function Authoring({ tiles }: { tiles: React.ReactNode }) {
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [name, setName] = useState("");
  const [about, setAbout] = useState("");
  const [draft, setDraft] = useState("");
  const [report, setReport] = useState<StrategyValidation | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async (keep?: string | null) => {
    try {
      const r = await listSavedStrategies();
      setSaved(r.strategies);
      const next = keep ?? r.strategies[0]?.strategy_id ?? null;
      setSelected(next);
      setVersions(next ? (await listStrategyVersions(next)).versions : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const pick = async (id: string) => {
    setSelected(id);
    setReport(null);
    try {
      setVersions((await listStrategyVersions(id)).versions);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(readable(e));
    } finally {
      setBusy(null);
    }
  };

  const parsedDraft = (): unknown => {
    const text = draft.trim();
    if (!text) return null;
    return JSON.parse(text) as unknown;
  };

  const current = saved.find((s) => s.strategy_id === selected) ?? null;
  const [editing, setEditing] = useState(false);
  const dialog = useDialog();

  /** Remove a strategy from the list. The server refuses while a paper run still uses it, and says why. */
  const remove = async (s: SavedStrategy) => {
    const ok = await dialog.confirm({
      title: `Remove ${s.name}?`,
      description: "This erases it, with its versions, backtests and paper history. It cannot be undone.",
      confirmLabel: "Remove",
      tone: "danger",
    });
    if (!ok) return;
    await run("remove", async () => {
      await removeSavedStrategy(s.strategy_id);
      setEditing(false);
      setReport(null);
      await refresh(null);
      setNotice(`Removed ${s.name}.`);
    });
  };

  const ghost =
    "inline-flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-xs transition-colors hover:bg-muted disabled:opacity-50";

  return (
    <Card>
      <CardHeader title="Strategies" />

      <div className="space-y-4 px-5 py-4">
        {tiles}
        {error && <ErrorBox>{error}</ErrorBox>}
        {notice && (
          <div className="rounded-xl border border-border bg-muted/40 px-3.5 py-2.5 text-[13px]">{notice}</div>
        )}

        {saved.length === 0 ? (
          <div className="py-4 text-center text-sm text-muted-foreground">No strategy of your own yet.</div>
        ) : (
          <>
            {/* A picker only earns its place when there is more than one to pick between. */}
            <div className={cn("flex flex-wrap gap-2", saved.length < 2 && "hidden")}>
              {saved.map((s) => (
                <button
                  key={s.strategy_id}
                  type="button"
                  onClick={() => void pick(s.strategy_id)}
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs transition-colors",
                    s.strategy_id === selected
                      ? "border-primary/40 bg-primary/10 text-foreground"
                      : "border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground",
                  )}
                >
                  {s.name}
                  {s.latest_version !== null && (
                    <span className="ml-1.5 text-[11px] text-muted-foreground">v{s.latest_version}</span>
                  )}
                </button>
              ))}
            </div>

            {current && (
              <div className="rounded-2xl border border-border">
                <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-3">
                  <div className="text-sm" title={current.strategy_id}>
                    <span className="font-medium">{current.name}</span>
                    {current.engine_key && (
                      <span className="ml-2 text-xs text-muted-foreground">{strategyLabel(current.engine_key)}</span>
                    )}
                    {current.description && (
                      <div className="mt-0.5 text-xs text-muted-foreground">{current.description}</div>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <button type="button" onClick={() => setEditing((v) => !v)} className={ghost}>
                      {editing ? "Close editor" : "Edit rules"}
                    </button>
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => void remove(current)}
                      className={cn(ghost, "text-muted-foreground hover:text-rose-500")}
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div className="divide-y divide-border">
                  {versions.map((v) => (
                    <div key={v.version} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm">
                          <span className="font-medium">v{v.version}</span>
                          {v.deployable ? (
                            <Badge tone="good">ready to deploy</Badge>
                          ) : (
                            <Badge tone="warn">can't deploy</Badge>
                          )}
                          {v.is_deployed && <Badge tone="flat">deployed</Badge>}
                        </div>
                        {!v.deployable && v.not_deployable_reason && (
                          <div className="mt-0.5 text-xs text-muted-foreground">{v.not_deployable_reason}</div>
                        )}
                        {v.change_note && <div className="mt-0.5 text-xs text-muted-foreground">{v.change_note}</div>}
                      </div>
                      <span className="text-xs text-muted-foreground">
                        <RelativeTime value={v.created_at} absolute={false} className="text-muted-foreground" />
                      </span>
                    </div>
                  ))}
                  {versions.length === 0 && (
                    <div className="px-4 py-3 text-xs text-muted-foreground">No versions yet.</div>
                  )}
                </div>

                {editing ? (
                  <div className="space-y-3 border-t border-border p-4">
                    <textarea
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      rows={8}
                      spellCheck={false}
                      placeholder={'{\n  "rules": {\n    "entry": { "breakout_lookback": 20, "min_history_bars": 60 },\n    "exit": { "stop_loss_pct": 5.0, "take_profit_pct": 10.0 }\n  }\n}'}
                      className="w-full rounded-xl border border-border bg-transparent px-3 py-2 font-mono text-[11.5px] leading-relaxed outline-none focus:border-primary/50"
                    />
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        disabled={busy !== null || !selected}
                        title="Checks that the rules will run. It does not say whether they make money."
                        onClick={() =>
                          run("validate", async () => {
                            const body = draft.trim() ? { definition: parsedDraft() } : { version: null };
                            setReport(await validateStrategy(selected as string, body));
                          })
                        }
                        className={ghost}
                      >
                        <ShieldCheck className="h-3 w-3" />
                        {busy === "validate" ? "Checking…" : "Check rules"}
                      </button>
                      <button
                        type="button"
                        disabled={busy !== null || !selected || !draft.trim()}
                        title="Saves a new version. A saved version can't be edited later."
                        onClick={() =>
                          run("version", async () => {
                            const created = await createStrategyVersion(selected as string, {
                              definition: parsedDraft(),
                            });
                            setReport(created.validation);
                            setNotice(`Saved version ${created.version}.`);
                            setDraft("");
                            await pick(selected as string);
                          })
                        }
                        className={ghost}
                      >
                        <Plus className="h-3 w-3" />
                        {busy === "version" ? "Saving…" : "Save as new version"}
                      </button>
                    </div>
                  </div>
                ) : null}
              </div>
            )}
          </>
        )}

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Name a new strategy"
              className="min-w-[14rem] flex-1 rounded-full border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
            />
            <button
              type="button"
              disabled={busy !== null || !name.trim()}
              onClick={() =>
                run("create", async () => {
                  const row = await createSavedStrategy({
                    name: name.trim(),
                    kind: "rules",
                    description: about.trim() || null,
                  });
                  setName("");
                  setAbout("");
                  setNotice(`Created “${row.name}”. Add its rules to make it deployable.`);
                  await refresh(row.strategy_id);
                  setEditing(true);
                })
              }
              className={ghost}
            >
              <Plus className="h-3 w-3" />
              {busy === "create" ? "Creating…" : "Create"}
            </button>
          </div>
          <input
            value={about}
            onChange={(e) => setAbout(e.target.value)}
            placeholder="What it does (optional)"
            className="w-full rounded-full border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
          />
        </div>

        {report && <ValidationReport report={report} />}
      </div>
    </Card>
  );
}

function ValidationReport({ report }: { report: StrategyValidation }) {
  return (
    <div className="rounded-xl border border-border/60 px-3.5 py-3 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-2">
        {report.ok ? <Badge tone="good">structurally valid</Badge> : <Badge tone="bad">will not run</Badge>}
        <span className="text-muted-foreground">
          {report.checked === "draft" ? "draft, nothing saved" : `stored version ${report.version}`}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className={cn(report.paper.resolvable ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>
          {report.paper.resolvable ? "paper: deployable" : "paper: not deployable"}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className={cn(report.backtest.resolvable ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>
          {report.backtest.resolvable ? "backtest: runnable" : "backtest: not runnable"}
        </span>
      </div>

      {!report.paper.resolvable && report.paper.reason && (
        <div className="mt-1.5 text-muted-foreground">{report.paper.reason}</div>
      )}
      {report.backtest.resolvable === false && report.backtest.reason && report.paper.resolvable && (
        <div className="mt-1 text-muted-foreground">{report.backtest.reason}</div>
      )}

      {report.errors.length > 0 && (
        <ul className="mt-2 space-y-1">
          {report.errors.map((e) => (
            <li key={`${e.code}-${e.field}`} className="flex gap-1.5 text-destructive">
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <code className="text-[11.5px]">{e.field}</code> — {e.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      {report.warnings.length > 0 && (
        <ul className="mt-2 space-y-1">
          {report.warnings.map((w) => (
            <li key={`${w.code}-${w.field}`} className="flex gap-1.5 text-amber-700 dark:text-amber-300">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <code className="text-[11.5px]">{w.field}</code> — {w.message}
              </span>
            </li>
          ))}
        </ul>
      )}

      {report.paper.default_fields && report.paper.default_fields.length > 0 && (
        <div className="mt-2 text-[11.5px] text-muted-foreground">
          {report.paper.default_fields.length} rule fields left at their defaults
          {report.paper.authored_fields?.length
            ? `; you set ${report.paper.authored_fields.length}`
            : ""}
          . Those defaults are the module's starting points, not findings.
        </div>
      )}

      <div className="mt-2 text-[11.5px] text-muted-foreground">
        {report.statistical_validation.reason}
      </div>
    </div>
  );
}

