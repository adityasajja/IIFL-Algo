import { AlertTriangle, CheckCircle2, ChevronDown, FlaskConical, HelpCircle, Plus, ShieldCheck, Sparkles, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  createSavedStrategy,
  createStrategyVersion,
  getStrategies,
  listSavedStrategies,
  listStrategyVersions,
  seedExampleStrategy,
  validateStrategy,
  type SavedStrategy,
  type StrategyInfo,
  type StrategyValidation,
  type StrategyVersion,
  type ValidationState,
} from "./api";
import { Card, CardHeader, ErrorBox } from "./components/ui/card";
import { PageLoader } from "./components/ui/loading";
import { Badge, fmtNum, fmtPct } from "./components/ui/stat";
import { TrackedPlans } from "./TrackedPlans";
import { humanizeSentence, strategyLabel } from "./lib/format";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";

/**
 * Strategies — decide.
 *
 * Two halves, and the split is the point.
 *
 * **The registry** (below) lists what the *engine* can run: strategies compiled
 * into the codebase. They have no version history, cannot be deployed, and each
 * one shows its walk-forward verdict — because a list that renders a validated
 * strategy and an untested one identically invites the exact mistake this project
 * exists to avoid.
 *
 * **Your strategies** is the authoring half: create a strategy, append an
 * immutable version, validate it, and then deploy that exact version on the Paper
 * screen. A deployment pins `(strategy_id, strategy_version)`, and this is the
 * only place that pair can be produced from the screen. Nothing here claims a
 * strategy works; validation answers "will this execute", and the panel says so
 * in as many words rather than letting a green tick imply the other question.
 */
export default function StrategiesPanel({
  onOpenResearch,
  onOpenPlans,
}: {
  onOpenResearch: () => void;
  onOpenPlans: () => void;
}) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [control, setControl] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<ValidationState | "all">("all");
  // Until the first response there is nothing to count; without this the panel
  // printed "0 of 0 … cleared validation" in a warning banner, which reads as a
  // measured result rather than "not loaded yet".
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await getStrategies();
      setLoaded(true);
      setStrategies(r.strategies);
      setAsOf(r.validation_as_of ?? null);
      setControl(r.control_sharpe ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = {
    pass: strategies.filter((s) => s.validation.state === "pass").length,
    fail: strategies.filter((s) => s.validation.state === "fail").length,
    untested: strategies.filter((s) => s.validation.state === "untested").length,
  };
  const shown = filter === "all" ? strategies : strategies.filter((s) => s.validation.state === filter);

  if (!loaded && !error) return <PageLoader label="Loading strategies" />;

  const total = strategies.length;
  const verdict =
    counts.pass > 0
      ? `${counts.pass} of ${total} built-in strategies have proven themselves.`
      : `None of the ${total} built-in strategies has proven itself yet.`;

  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}

      <div
        className="text-lg font-semibold tracking-tight"
        title={control !== null ? `Random picks over the same stocks score about ${fmtNum(control)} (Sharpe).` : undefined}
      >
        {verdict}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Tally
          label="Validated"
          value={counts.pass}
          tone="pass"
          active={filter === "pass"}
          onClick={() => setFilter(filter === "pass" ? "all" : "pass")}
        />
        <Tally
          label="Tested, lost"
          value={counts.fail}
          tone="fail"
          active={filter === "fail"}
          onClick={() => setFilter(filter === "fail" ? "all" : "fail")}
        />
        <Tally
          label="Never tested"
          value={counts.untested}
          tone="untested"
          active={filter === "untested"}
          onClick={() => setFilter(filter === "untested" ? "all" : "untested")}
        />
      </div>

      <TrackedPlans onOpen={onOpenPlans} />

      <Card>
        <CardHeader
          title="Built-in strategies"
          sub={asOf ? <>Last tested <RelativeTime value={asOf} absolute={false} /></> : "Not tested yet"}
        />
        <div className="mt-3 divide-y divide-border/60">
          {shown.map((s) => (
            <Row key={s.name} s={s} onOpenResearch={onOpenResearch} />
          ))}
          {shown.length === 0 && (
            <div className="px-5 py-6 text-center text-[13px] text-muted-foreground">
              Nothing in this group.
            </div>
          )}
        </div>
      </Card>

      <Authoring />
    </div>
  );
}

/**
 * Create a strategy, append an immutable version, validate it.
 *
 * Three deliberate choices:
 *
 * * **Validate before saving is offered first**, because a version cannot be
 *   edited afterwards. The server refuses a definition with structural errors at
 *   creation too, so this is the same check twice — once where it is cheap, once
 *   where it is unmissable.
 * * **The definition is a JSON textarea, not a form.** A rule block has ~20
 *   fields across two dataclasses and they change with the rule layer; a
 *   hand-built form would be a second definition of the schema, and the two would
 *   drift. The server is the schema, and it answers with the field names.
 * * **`deployable` is shown per version and never inferred.** A version that
 *   cannot resolve to entry/exit rules is one a deployment would run while
 *   placing no orders, which on the monitor looks exactly like a quiet market.
 */
function Authoring() {
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [name, setName] = useState("");
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
      setError(e instanceof Error ? e.message : String(e));
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

  const seed = () =>
    run("seed", async () => {
      const r = await seedExampleStrategy();
      setNotice(r.created ? `Created “${r.strategy.name}”.` : "The example already exists.");
      await refresh(r.strategy.strategy_id);
    });

  const ghost =
    "inline-flex items-center gap-1.5 rounded-full border border-border px-3 py-1.5 text-xs transition-colors hover:bg-muted disabled:opacity-50";

  return (
    <Card>
      <CardHeader
        title="Your strategies"
        action={
          saved.length > 0 ? (
            <button type="button" disabled={busy !== null} onClick={seed} className={ghost}>
              <Sparkles className="h-3 w-3" />
              {busy === "seed" ? "Adding…" : "Add example"}
            </button>
          ) : undefined
        }
      />

      <div className="space-y-4 px-5 py-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        {notice && (
          <div className="rounded-xl border border-border bg-muted/40 px-3.5 py-2.5 text-[13px]">{notice}</div>
        )}

        {saved.length === 0 ? (
          <div className="grid justify-items-center gap-3 py-6 text-center">
            <div className="text-sm text-muted-foreground">You haven't made a strategy yet.</div>
            <button type="button" disabled={busy !== null} onClick={seed} className={ghost}>
              <Sparkles className="h-3 w-3" />
              {busy === "seed" ? "Adding…" : "Start from an example"}
            </button>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
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
                  <span className="ml-1.5 text-[11px] text-muted-foreground">
                    {s.latest_version === null ? "no versions" : `v${s.latest_version}`}
                  </span>
                </button>
              ))}
            </div>

            {current && (
              <div className="rounded-2xl border border-border">
                <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-3">
                  <div className="text-sm" title={current.strategy_id}>
                    <span className="font-medium">{current.name}</span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {current.engine_key ? strategyLabel(current.engine_key) : current.kind}
                    </span>
                  </div>
                  <button type="button" onClick={() => setEditing((v) => !v)} className={ghost}>
                    {editing ? "Close editor" : "Edit rules"}
                  </button>
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
                const row = await createSavedStrategy({ name: name.trim(), kind: "rules" });
                setName("");
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

function Row({ s, onOpenResearch }: { s: StrategyInfo; onOpenResearch: () => void }) {
  const v = s.validation;
  const [open, setOpen] = useState(false);
  const beatBench =
    v.oos_sharpe !== null && v.oos_sharpe !== undefined &&
    v.benchmark_sharpe !== null && v.benchmark_sharpe !== undefined
      ? v.oos_sharpe > v.benchmark_sharpe
      : null;

  // The random-picks comparison in one phrase instead of a sigma value.
  const z = v.z_vs_control;
  const vsRandom =
    z === null || z === undefined
      ? null
      : z >= 2
        ? { word: "Beats random picks", cls: "bg-emerald-500/10 text-emerald-500" }
        : z <= -2
          ? { word: "Worse than random picks", cls: "bg-rose-500/10 text-rose-500" }
          : { word: "No better than random", cls: "bg-muted text-muted-foreground" };

  const tested = v.state === "fail" || v.state === "pass";
  const hasDetails = tested || s.warmup_bars !== null || s.tunable.length > 0;

  return (
    <div className="px-5 py-3.5">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <StateIcon state={v.state} />
          <div className="truncate text-sm font-medium">{pretty(s.name)}</div>
        </div>

        <div className="flex items-center gap-2">
          <StateBadge state={v.state} />
          {v.state === "untested" && (
            <button
              type="button"
              onClick={onOpenResearch}
              className="inline-flex items-center gap-1.5 rounded-full border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <FlaskConical className="h-3 w-3" />
              Test it
            </button>
          )}
          {hasDetails && (
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-label={open ? "Hide details" : "Show details"}
              aria-expanded={open}
              className="grid size-7 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
            </button>
          )}
        </div>
      </div>

      {tested && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-2 pl-7 text-sm">
          {v.oos_return_pct !== null && v.oos_return_pct !== undefined && (
            <span className="text-muted-foreground">
              Return{" "}
              <span className={cn("font-medium tabular-nums", v.oos_return_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
                {fmtPct(v.oos_return_pct, 1)}
              </span>
            </span>
          )}
          {v.measured_win_rate !== null && v.measured_win_rate !== undefined && (
            <span className="text-muted-foreground">
              Wins <span className="font-medium tabular-nums text-foreground">{(v.measured_win_rate * 100).toFixed(0)}%</span>
              {v.measured_trades ? <span className="text-xs"> of {v.measured_trades.toLocaleString()} trades</span> : null}
            </span>
          )}
          {beatBench === false && (
            <span className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-500">
              Behind buy &amp; hold
            </span>
          )}
          {vsRandom && (
            <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", vsRandom.cls)}>{vsRandom.word}</span>
          )}
        </div>
      )}

      {open && (
        <div className="mt-3 space-y-2 pl-7 text-[12.5px]">
          {v.note && <div className="text-muted-foreground">{v.note}</div>}
          <div className="grid gap-x-5 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
            {v.oos_sharpe !== null && v.oos_sharpe !== undefined && (
              <Metric label="Sharpe (out of sample)" value={fmtNum(v.oos_sharpe)} bad={beatBench === false} />
            )}
            {v.expectancy_r !== null && v.expectancy_r !== undefined && (
              <Metric
                label="Average result per trade"
                value={`${v.expectancy_r >= 0 ? "+" : ""}${fmtNum(v.expectancy_r, 2)}R`}
                bad={v.expectancy_r < 0}
              />
            )}
            {s.warmup_bars !== null && <Metric label="History needed" value={`${s.warmup_bars} days`} />}
          </div>
          {s.tunable.length > 0 && (
            <div className="text-muted-foreground">
              Adjustable: {s.tunable.map((t) => humanizeSentence(t).toLowerCase()).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  bad,
  note,
}: {
  label: string;
  value: string;
  bad?: boolean;
  note?: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          "font-medium tabular-nums",
          bad ? "text-destructive" : undefined,
        )}
      >
        {value}
      </span>
      {note ? <span className="text-[11px] text-muted-foreground">({note})</span> : null}
    </div>
  );
}

function StateIcon({ state }: { state: ValidationState }) {
  if (state === "pass") return <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />;
  if (state === "fail") return <XCircle className="h-4 w-4 shrink-0 text-destructive" />;
  return <HelpCircle className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

function StateBadge({ state }: { state: ValidationState }) {
  if (state === "pass") return <Badge tone="good">validated</Badge>;
  if (state === "fail") return <Badge tone="bad">tested, lost</Badge>;
  return <Badge tone="flat">not tested</Badge>;
}

function Tally({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone: ValidationState;
  active: boolean;
  onClick: () => void;
}) {
  const palette = {
    pass: "text-emerald-600 dark:text-emerald-400",
    fail: "text-destructive",
    untested: "text-muted-foreground",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-2xl border bg-card p-5 text-left transition-colors",
        active ? "border-primary/50 bg-primary/[0.06]" : "border-border hover:border-foreground/20",
      )}
    >
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {tone === "pass" ? (
          <CheckCircle2 className="h-3.5 w-3.5" />
        ) : tone === "fail" ? (
          <XCircle className="h-3.5 w-3.5" />
        ) : (
          <AlertTriangle className="h-3.5 w-3.5" />
        )}
        {label}
      </div>
      <div className={cn("mt-2 text-4xl font-semibold tracking-tight tabular-nums", palette)}>{value}</div>
    </button>
  );
}

const pretty = strategyLabel;
