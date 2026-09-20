import { AlertTriangle, Play, Plus, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  createSavedStrategy,
  createStrategyVersion,
  listDeployments,
  listSavedStrategies,
  removeSavedStrategy,
  createDeployment,
  getResearchedStocks,
  startDeployment,
  listStrategyVersions,
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
      onOpenPaper={onOpenPaper}
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

type Rules = {
  kind: "breakout" | "triple" | "gap";
  gap: string;
  market: string;
  lookback: string;
  volume: string;
  stop: string;
  target: string;
  below: string;
  sellAt: string;
};

const START_RULES: Rules = { kind: "breakout", lookback: "20", volume: "1.5", stop: "5", target: "10", below: "30", sellAt: "50", gap: "1", market: "1" };

/** The rules a person fills in, turned into the stored definition (same shape the engine reads). */
function buildDefinition(r: Rules) {
  const stop = Number(r.stop);
  const off = { trailing_stop_pct: null, trend_sma: 0, trend_confirm_bars: 3, min_history_bars: 60 };
  if (r.kind === "gap") {
    // Monday only, after a week the market rose, buy what opens lower, sell at target, stop or Friday's close.
    const entry = {
      setup: "gap_down",
      gap_down_pct: Number(r.gap),
      gap_market_min_pct: Number(r.market),
      gap_weekday: 0,
      gap_entry_minutes: 15,
      min_history_bars: 60,
    };
    const exit = { stop_loss_pct: stop, take_profit_pct: Number(r.target), rsi_overbought: null, exit_at_week_end: true, ...off };
    return { rules: { entry, exit } };
  }
  if (r.kind === "triple") {
    // Buy after 3 falling days with the 5-day RSI low, in a stock above its 200-day average.
    const entry = {
      setup: "triple_rsi",
      triple_rsi_period: 5,
      triple_rsi_below: Number(r.below),
      triple_rsi_prior_below: 60,
      triple_rsi_trend_sma: 200,
      min_history_bars: 210,
    };
    const exit = { stop_loss_pct: stop, take_profit_pct: null, rsi_overbought: Number(r.sellAt), rsi_period: 5, ...off };
    return { rules: { entry, exit }, engine_key: "signals_entry", params: { ...entry, ...exit, lookback: 260, allocation: 0.1, max_positions: 5 } };
  }
  const lookback = Number(r.lookback);
  const entry = { breakout_lookback: lookback, breakout_proximity_pct: 2.0, volume_multiple: Number(r.volume), volume_lookback: lookback, setup: "breakout", min_history_bars: 60 };
  const exit = { stop_loss_pct: stop, take_profit_pct: Number(r.target), rsi_overbought: null, ...off };
  return { rules: { entry, exit }, engine_key: "signals_entry", params: { ...entry, ...exit, lookback: 120, allocation: 0.1, max_positions: 5 } };
}

function Authoring({ tiles, onOpenPaper }: { tiles: React.ReactNode; onOpenPaper: () => void }) {
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [versions, setVersions] = useState<StrategyVersion[]>([]);
  const [name, setName] = useState("");
  const [about, setAbout] = useState("");
  const [creating, setCreating] = useState(false);
  const [rules, setRules] = useState<Rules>(START_RULES);
  const [runFor, setRunFor] = useState<number | null>(null);
  const [stocks, setStocks] = useState("");
  const [capital, setCapital] = useState("500000");
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
                      {editing ? "Close" : "Set rules"}
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
                      <div className="flex items-center gap-3">
                        <span className="text-xs text-muted-foreground">
                          <RelativeTime value={v.created_at} absolute={false} className="text-muted-foreground" />
                        </span>
                        {v.deployable && (
                          <button type="button" onClick={() => setRunFor(v.version)} className={ghost}>
                            <Play className="h-3 w-3" />
                            Run on paper
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {versions.length === 0 && (
                    <div className="px-4 py-3 text-xs text-muted-foreground">No versions yet.</div>
                  )}
                </div>

                {runFor !== null && (
                  <div className="space-y-2 border-t border-border p-4">
                    <div className="flex justify-end">
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() =>
                          run("stocks", async () => setStocks((await getResearchedStocks()).symbols.join(", ")))
                        }
                        className={cn(ghost, "text-muted-foreground")}
                      >
                        {busy === "stocks" ? "Loading…" : "Use the researched stocks"}
                      </button>
                    </div>
                    <input
                      autoFocus
                      value={stocks}
                      onChange={(e) => setStocks(e.target.value)}
                      placeholder="Stocks, e.g. RELIANCE, TCS, INFY"
                      className="w-full rounded-full border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="flex items-center gap-2 text-xs text-muted-foreground">
                        Practice money ₹
                        <input
                          value={capital}
                          onChange={(e) => setCapital(e.target.value)}
                          inputMode="numeric"
                          className="w-28 rounded-full border border-border bg-transparent px-3 py-1.5 text-sm text-foreground outline-none focus:border-primary/50"
                        />
                      </label>
                      <button
                        type="button"
                        disabled={busy !== null || !stocks.trim() || !Number(capital)}
                        onClick={() =>
                          run("paper", async () => {
                            const symbols = stocks.split(/[\s,]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
                            const made = await createDeployment({
                              strategy_id: selected as string,
                              strategy_version: runFor,
                              capital: Number(capital),
                              mode: "PAPER",
                              config: { symbols, exchange: "NSEEQ", timeframe: "1d", max_open_positions: 20 },
                            });
                            await startDeployment(made.deployment_id);
                            setRunFor(null);
                            onOpenPaper();
                          })
                        }
                        className={ghost}
                      >
                        {busy === "paper" ? "Starting…" : "Start"}
                      </button>
                      <button type="button" onClick={() => setRunFor(null)} className={cn(ghost, "text-muted-foreground")}>
                        Cancel
                      </button>
                    </div>
                  </div>
                )}

                {editing ? (
                  <div className="space-y-3 border-t border-border p-4">
                    <div className="grid grid-cols-3 gap-2 sm:w-96">
                      {(
                        [
                          ["breakout", "Breakout"],
                          ["triple", "Triple RSI"],
                          ["gap", "Monday gap"],
                        ] as const
                      ).map(([kind, label]) => (
                        <button
                          key={kind}
                          type="button"
                          onClick={() =>
                            setRules({ ...rules, kind, stop: kind === "triple" ? "8" : "5", target: kind === "gap" ? "3" : "10" })
                          }
                          className={cn(
                            "rounded-xl border px-3 py-2 text-sm transition-colors",
                            rules.kind === kind ? "border-primary/40 bg-primary/10" : "border-border text-muted-foreground hover:text-foreground",
                          )}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    {rules.kind === "triple" && (
                      <p className="text-xs text-muted-foreground">
                        Buys after three falling days, in a stock above its 200-day average. Sells when the 5-day RSI recovers.
                      </p>
                    )}
                    {rules.kind === "gap" && (
                      <p className="text-xs text-muted-foreground">
                        Mondays only, within 15 minutes of the open. Sells at the target or stop, else at Friday's close. Needs the broker login for live prices.
                      </p>
                    )}
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                      {(
                        (rules.kind === "triple"
                          ? [
                              ["below", "Buy when 5-day RSI is under"],
                              ["sellAt", "Sell when RSI passes"],
                              ["stop", "Stop loss %"],
                            ]
                          : rules.kind === "gap"
                          ? [
                              ["gap", "Buy if it opens this % below Friday"],
                              ["market", "Only after a week the market rose over %"],
                              ["target", "Take profit %"],
                              ["stop", "Stop loss %"],
                            ]
                          : [
                              ["lookback", "Buy a breakout over (days)"],
                              ["volume", "Volume times normal"],
                              ["stop", "Stop loss %"],
                              ["target", "Take profit %"],
                            ]) as [keyof Rules, string][]
                      ).map(([key, label]) => (
                        <label key={key} className="space-y-1 text-xs text-muted-foreground">
                          {label}
                          <input
                            value={rules[key]}
                            onChange={(e) => setRules({ ...rules, [key]: e.target.value })}
                            inputMode="decimal"
                            className="w-full rounded-xl border border-border bg-transparent px-3 py-2 text-sm text-foreground outline-none focus:border-primary/50"
                          />
                        </label>
                      ))}
                    </div>
                    <button
                      type="button"
                      disabled={busy !== null || !selected || (rules.kind === "triple" ? [rules.below, rules.sellAt, rules.stop] : rules.kind === "gap" ? [rules.gap, rules.market, rules.target, rules.stop] : [rules.lookback, rules.volume, rules.stop, rules.target]).some((x) => !Number(x))}
                      title="Saves these rules as a new version. A saved version can't be edited later."
                      onClick={() =>
                        run("version", async () => {
                          const created = await createStrategyVersion(selected as string, {
                            definition: buildDefinition(rules),
                          });
                          setReport(created.validation);
                          setNotice(`Saved version ${created.version}.`);
                          setEditing(false);
                          await pick(selected as string);
                        })
                      }
                      className={ghost}
                    >
                      <Plus className="h-3 w-3" />
                      {busy === "version" ? "Saving…" : "Save rules"}
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          </>
        )}

        {!creating ? (
          <div>
            <button type="button" onClick={() => setCreating(true)} className={ghost}>
              <Plus className="h-3 w-3" />
              New strategy
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Name"
              className="w-full rounded-full border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
            />
            <input
              value={about}
              onChange={(e) => setAbout(e.target.value)}
              placeholder="What it does (optional)"
              className="w-full rounded-full border border-border bg-transparent px-4 py-2 text-sm outline-none focus:border-primary/50"
            />
            <div className="flex items-center gap-2">
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
                    setCreating(false);
                    setNotice(`Created “${row.name}”. Set its rules, then run it on paper.`);
                    await refresh(row.strategy_id);
                    setEditing(true);
                  })
                }
                className={ghost}
              >
                {busy === "create" ? "Creating…" : "Create"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setCreating(false);
                  setName("");
                  setAbout("");
                }}
                className={cn(ghost, "text-muted-foreground")}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

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

