/**
 * Backtest workflow — Strategy → Version → Configure → Run → Results.
 *
 * This is the replaceable surface for the legacy in-sample `BacktestPanel`. It
 * is a *state machine*, not a page of forms, because the thing that goes wrong
 * with backtest UIs is that they collapse "I ran something" and "I have a
 * result" into one screen. Here the three states are distinct and the URL-ish
 * stepper says which one you are in:
 *
 *   Configuring  → no run selected, the form is the page
 *   Running      → a run is QUEUED/RUNNING, the page polls and shows progress
 *   Results      → a run is terminal, the page shows the numbers
 *
 * The honesty rules this panel enforces, all of which are the point of the
 * project rather than decoration:
 *
 *  1. **Ranking is not evidence.** Every strategy in the picker carries its
 *     walk-forward verdict, and the picker refuses to imply that an untested
 *     strategy is a good one. A backtest of an untested strategy is a number
 *     about the past, and the panel says so on the results page too.
 *  2. **A version is what makes a run reproducible.** Built-ins have no version
 *     history; saved strategies do. When you pick a saved version, its number
 *     is pinned into the run and shown on every trade, so "which version made
 *     this trade" is answerable a year later.
 *  3. **The monthly matrix must reconcile.** The panel compounds the twelve
 *     months of each year itself and shows the result next to the run's stated
 *     total return. If those disagree the matrix is wrong and the mismatch is
 *     rendered as an error, not smoothed over.
 *  4. **A stop is a trigger, not a fill.** Realised exits below the configured
 *     stop are labelled "gapped through" rather than presented as a rule
 *     failure — the engine fills on the next open, which is what actually
 *     happens.
 *  5. **Costs are stated.** The cost model and its round-trip estimate are on
 *     the results header, because a backtest is only as good as its cost
 *     assumption — the single most common way a backtest lies.
 */
import {
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  HelpCircle,
  Info,
  Layers,
  Play,
  RefreshCw,
  Repeat,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  backtestCurves,
  backtestMonthly,
  backtestOptions,
  backtestTrade,
  backtestTrades,
  cancelBacktest,
  getBacktest,
  listBacktests,
  submitBacktest,
  type BacktestConfig,
  type BacktestCurves,
  type BacktestMonthly,
  type BacktestOptions,
  type BacktestRun,
  type BacktestRunSummary,
  type BacktestStatus,
  type BacktestTrade,
  type BacktestTradeDetail,
  type StrategyOption,
  type StrategyVersionOption,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { ButtonLoader, PageLoader } from "./components/ui/loading";
import { EquityChart } from "./components/ui/equity-chart";
import { Input } from "./components/ui/input";
import { MorphingModal } from "./components/ui/modal";
import { Select } from "./components/ui/select";
import { strategyLabel } from "./lib/format";
import { Badge, Callout, Stat, fmtMoney, fmtNum, fmtPct } from "./components/ui/stat";
import { Switch } from "./components/ui/switch";
import { cn } from "./lib/utils";
import { RelativeTime } from "./lib/time";
import {
  gappedThroughStop,
  isTerminal,
  pageRange,
  plottable,
  progressLabel,
  progressWidth,
  reconcile,
} from "./lib/backtest-results";

/** Poll interval while a run is in flight. The engine reports progress in
 *  coarse steps (0.0 → 0.35 → 1.0), so polling faster than this only adds load
 *  without adding information. */
const POLL_MS = 1500;

/** Stop polling after this long and tell the user, rather than spinning
 *  forever on a worker that has silently died. */
const POLL_GIVE_UP_MS = 20 * 60 * 1000;

type View = "configure" | "run" | "results";

/** Newest version of a saved strategy. Written as a helper rather than
 *  `versions.at(-1)` because the TS lib target here predates `Array.at`. */
const newestVersion = (
  versions: StrategyVersionOption[] | undefined,
): StrategyVersionOption | undefined =>
  versions && versions.length ? versions[versions.length - 1] : undefined;

// ─── main panel ───────────────────────────────────────────────────────────────

export default function BacktestWorkflowPanel() {
  const [options, setOptions] = useState<BacktestOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [active, setActive] = useState<BacktestRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadOptions = useCallback(async () => {
    try {
      setOptions(await backtestOptions());
      setOptionsError(null);
    } catch (e) {
      setOptionsError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const r = await listBacktests({ limit: 40 });
      setRuns(r.runs);
    } catch {
      // History is context, not the task. A history failure must not blank the
      // form — the user can still configure and run.
    }
  }, []);

  useEffect(() => {
    void loadOptions();
    void loadRuns();
  }, [loadOptions, loadRuns]);

  // ── polling ────────────────────────────────────────────────────────────
  // One timer, created when a run becomes active and cleared when it reaches a
  // terminal state. Deliberately not a `setInterval` in an effect keyed on the
  // id: that would keep ticking after completion.
  const startedAt = useRef<number>(0);
  useEffect(() => {
    if (!activeId) return;
    if (isTerminal(active?.status)) return;

    startedAt.current = startedAt.current || Date.now();
    let cancelled = false;

    const tick = async () => {
      if (cancelled) return;
      if (Date.now() - startedAt.current > POLL_GIVE_UP_MS) {
        setError(
          "This run has not reported for 20 minutes. It may have been lost with the worker. " +
            "The run is still recorded — reload to check its status.",
        );
        return;
      }
      try {
        const r = await getBacktest(activeId);
        if (cancelled) return;
        setActive(r);
        if (isTerminal(r.status)) {
          void loadRuns();
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };

    void tick();
    const timer = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeId, active?.status, loadRuns]);

  const view: View = useMemo(() => {
    if (!activeId) return "configure";
    if (active && isTerminal(active.status)) return "results";
    return "run";
  }, [activeId, active]);

  async function onSubmitted(runId: string) {
    startedAt.current = Date.now();
    setError(null);
    setActiveId(runId);
    // Fetch immediately so the run view has a status before the first poll.
    try {
      setActive(await getBacktest(runId));
    } catch {
      setActive(null);
    }
    void loadRuns();
  }

  function backToConfigure() {
    setActiveId(null);
    setActive(null);
    setError(null);
    void loadRuns();
  }

  return (
    <div className="space-y-4">
      <Stepper view={view} />

      {error && <ErrorBox>{error}</ErrorBox>}

      {view === "configure" && (
        <>
          {optionsError && <ErrorBox>{optionsError}</ErrorBox>}
          <ConfigForm
            options={options}
            busy={busy}
            setBusy={setBusy}
            onSubmitted={onSubmitted}
            onError={setError}
            runs={runs}
            onOpenRun={(id) => {
              startedAt.current = Date.now();
              setActiveId(id);
              setActive(null);
            }}
          />
        </>
      )}

      {view === "run" && activeId && (
        <RunProgress
          runId={activeId}
          run={active}
          onCancel={async () => {
            try {
              await cancelBacktest(activeId);
              setActive(await getBacktest(activeId));
              void loadRuns();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
          onBack={backToConfigure}
        />
      )}

      {view === "results" && activeId && active && (
        <Results runId={activeId} run={active} onBack={backToConfigure} />
      )}

      {view === "results" && activeId && !active && (
        <div className="grid place-items-center rounded-2xl border border-dashed border-border py-12">
          <PageLoader label="Loading results" />
        </div>
      )}
    </div>
  );
}

// ─── stepper ──────────────────────────────────────────────────────────────────

function Stepper({ view }: { view: View }) {
  const steps: { id: View | "strategy"; label: string; done: boolean }[] = [
    { id: "strategy", label: "Strategy & version", done: true },
    { id: "configure", label: "Configure", done: view !== "configure" },
    { id: "run", label: "Run", done: view === "results" },
    { id: "results", label: "Results", done: view === "results" },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-2 px-1 text-[12px]">
      {steps.map((s, i) => {
        const current = s.id === view;
        return (
          <span key={s.id} className="inline-flex items-center gap-1">
            {i > 0 && <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/50" />}
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1",
                current
                  ? "bg-primary/10 font-semibold text-foreground"
                  : s.done
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60",
              )}
            >
              {s.done && !current ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
              ) : (
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-current" />
              )}
              {s.label}
            </span>
          </span>
        );
      })}
    </div>
  );
}

// ─── config form ──────────────────────────────────────────────────────────────

interface FormState {
  strategyName: string;
  strategyId: string | null;
  version: string;
  universe: string;
  symbols: string;
  exchange: string;
  timeframe: string;
  start: string;
  end: string;
  source: "cache" | "synthetic";
  cash: string;
  sizingMode: string;
  sizingPercent: string;
  sizingQuantity: string;
  stopLoss: string;
  takeProfit: string;
  trailingStop: string;
  costModel: string;
  slippage: string;
  allowShort: boolean;
  squareOff: boolean;
}

const BLANK: FormState = {
  strategyName: "sma_crossover",
  strategyId: null,
  version: "",
  universe: "",
  symbols: "RELIANCE,INFY,TCS",
  exchange: "NSEEQ",
  timeframe: "1d",
  start: "",
  end: "",
  source: "cache",
  cash: "1000000",
  sizingMode: "fixed_fraction",
  sizingPercent: "25",
  sizingQuantity: "100",
  stopLoss: "5",
  takeProfit: "20",
  trailingStop: "8",
  costModel: "india_delivery",
  slippage: "5",
  allowShort: false,
  squareOff: false,
};

function ConfigForm({
  options,
  busy,
  setBusy,
  onSubmitted,
  onError,
  runs,
  onOpenRun,
}: {
  options: BacktestOptions | null;
  busy: boolean;
  setBusy: (v: boolean) => void;
  onSubmitted: (runId: string) => void;
  onError: (msg: string | null) => void;
  runs: BacktestRunSummary[];
  onOpenRun: (runId: string) => void;
}) {
  const [form, setForm] = useState<FormState>(BLANK);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const strategies = options?.strategies ?? [];
  // A strategy named in the URL-less default may not exist if the registry
  // changed; fall back to the first real one rather than submitting a 404.
  useEffect(() => {
    if (strategies.length && !strategies.some((s) => s.name === form.strategyName)) {
      set("strategyName", strategies[0].name);
      set("strategyId", strategies[0].strategy_id);
      set("version", newestVersion(strategies[0].versions)?.version?.toString() ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategies]);

  const selected: StrategyOption | undefined = strategies.find(
    (s) => s.name === form.strategyName && s.strategy_id === form.strategyId,
  );

  const availableTimeframes = options?.timeframes ?? [];
  const timeframe = availableTimeframes.find((t) => t.value === form.timeframe);

  function pickStrategy(name: string, id: string | null) {
    const s = strategies.find((x) => x.name === name && x.strategy_id === id);
    setForm((f) => ({
      ...f,
      strategyName: name,
      strategyId: id,
      // Pin the newest version by default: a saved strategy without a version
      // is not reproducible, which is the whole point of saving one.
      version: newestVersion(s?.versions)?.version?.toString() ?? "",
    }));
  }

  const num = (v: string, fallback: number | null = null): number | null => {
    if (v.trim() === "") return fallback;
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };

  function build(): BacktestConfig {
    const cfg: BacktestConfig = {
      engine_key: selected?.key ?? form.strategyName,
      strategy: form.strategyName,
      strategy_id: form.strategyId,
      strategy_version: num(form.version),
      universe: form.universe || null,
      symbols: form.universe
        ? []
        : form.symbols
            .split(",")
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean),
      exchange: form.exchange.trim().toUpperCase(),
      timeframe: form.timeframe,
      start: form.start || null,
      end: form.end || null,
      source: form.source,
      initial_cash: num(form.cash, 1_000_000) as number,
      sizing: {
        mode: form.sizingMode,
        percent: form.sizingMode === "fixed_fraction" ? num(form.sizingPercent) : null,
        quantity: form.sizingMode === "fixed_quantity" ? num(form.sizingQuantity) : null,
      },
      stops: {
        stop_loss_pct: num(form.stopLoss),
        take_profit_pct: num(form.takeProfit),
        trailing_stop_pct: num(form.trailingStop),
      },
      costs: { model: form.costModel, slippage_bps: num(form.slippage, 0) as number },
      allow_short: form.allowShort,
      square_off_eod: form.squareOff,
      params: {},
    };
    return cfg;
  }

  async function onRun() {
    setBusy(true);
    onError(null);
    try {
      const res = await submitBacktest(build());
      onSubmitted(res.run_id);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // Equal-weight over one symbol is a contradiction the backend rejects. Catch
  // it here so the user gets the reason next to the field instead of a failed
  // submit — the backend check stays as the real guard.
  const symbolCount = form.universe
    ? 0
    : form.symbols.split(",").filter((s) => s.trim()).length;
  const equalWeightConflict = form.sizingMode === "equal_weight" && symbolCount === 1;

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title="Configure the run"
          sub="Everything here is stored with the run, so it can be replayed exactly."
        />
        <div className="space-y-4 p-5 pt-3">
          {/* 1 — strategy and version */}
          <Section
            n={1}
            title="Strategy & version"
            note="A version is what makes a run reproducible — a built-in has no version history."
          >
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label="Strategy">
                <Select
                  value={`${form.strategyId ?? ""}::${form.strategyName}`}
                  onChange={(v) => {
                    const [id, name] = v.split("::");
                    pickStrategy(name, id || null);
                  }}
                  options={
                    strategies.length
                      ? strategies.map((s) => ({
                          value: `${s.strategy_id ?? ""}::${s.name}`,
                          label: s.kind === "saved" ? `★ ${s.name}` : prettyStrategyName(s.name),
                        }))
                      : [{ value: "::sma_crossover", label: prettyStrategyName("sma_crossover") }]
                  }
                />
              </Field>

              <Field
                label="Version"
                hint={
                  selected?.kind === "builtin"
                    ? "built-ins are not versioned"
                    : (selected?.versions.length ?? 0) === 0
                      ? "this saved strategy has no versions yet"
                      : undefined
                }
              >
                <Select
                  value={form.version}
                  onChange={(v) => set("version", v)}
                  disabled={!selected || selected.versions.length === 0}
                  options={[
                    {
                      value: "",
                      label: selected?.kind === "builtin" ? "latest (unversioned)" : "latest saved",
                    },
                    ...(selected?.versions.map((v) => ({
                      value: String(v.version),
                      label: `v${v.version}${
                        v.created_at ? ` · ${new Date(v.created_at).toLocaleDateString("en-IN")}` : ""
                      }${v.change_note ? ` · ${v.change_note}` : ""}`,
                    })) ?? []),
                  ]}
                />
              </Field>
            </div>

            {selected && (
              <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px]">
                <Badge tone={selected.kind === "saved" ? "good" : "flat"}>
                  {selected.kind === "saved" ? "saved strategy" : "built-in engine"}
                </Badge>
                {selected.key && (
                  <span className="font-mono text-muted-foreground">{selected.key}</span>
                )}
                {selected.description && (
                  <span className="text-muted-foreground">{selected.description}</span>
                )}
              </div>
            )}
          </Section>

          {/* 2 — what to run over */}
          <Section n={2} title="Universe & window">
            <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
              <Field label="Universe" hint="or type symbols below">
                <Select
                  value={form.universe}
                  onChange={(v) => set("universe", v)}
                  options={[
                    { value: "", label: "— none (use symbols) —" },
                    ...(options?.universes ?? []).map((u) => ({
                      value: u.name,
                      label: `${u.name}${typeof u.count === "number" ? ` (${u.count})` : ""}`,
                    })),
                  ]}
                />
              </Field>

              <Input
                label="Symbols (comma separated)"
                value={form.symbols}
                onChange={(v) => set("symbols", v)}
                placeholder="RELIANCE,INFY,TCS"
              />

              <Field label="Exchange">
                <Select
                  value={form.exchange}
                  onChange={(v) => set("exchange", v)}
                  options={(options?.exchanges ?? ["NSEEQ", "BSEEQ"]).map((x) => ({
                    value: x,
                    label: x,
                  }))}
                />
              </Field>

              <Input
                label="Start (YYYY-MM-DD)"
                value={form.start}
                onChange={(v) => set("start", v)}
                placeholder="2015-01-01"
              />
              <Input
                label="End (YYYY-MM-DD)"
                value={form.end}
                onChange={(v) => set("end", v)}
                placeholder="leave blank for latest"
              />

              <Field
                label="Timeframe"
                hint={timeframe?.available === false ? (timeframe.reason ?? "unavailable") : undefined}
                warn={timeframe?.available === false}
              >
                <Select
                  value={form.timeframe}
                  onChange={(v) => set("timeframe", v)}
                  options={availableTimeframes.map((t) => ({
                    value: t.value,
                    disabled: t.available === false,
                    label: `${t.label}${t.available === false ? " — not available" : ""}`,
                  }))}
                />
              </Field>
            </div>

            <div className="mt-3 max-w-md">
              <Field label="Data source">
                <Select
                  value={form.source}
                  onChange={(v) => set("source", v as "cache" | "synthetic")}
                  options={(options?.sources ?? []).map((s) => ({ value: s.value, label: s.label }))}
                />
              </Field>
            </div>

            {form.source === "synthetic" && (
              <div className="mt-3">
                <Callout tone="warn">
                  Synthetic random walks exercise the harness correctly, but the numbers
                  are <strong>not evidence about any market</strong>. Use the cache for
                  anything you would act on.
                </Callout>
              </div>
            )}
          </Section>

          {/* 3 — sizing and risk */}
          <Section n={3} title="Capital, sizing & protective exits">
            <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
              <Field
                label="Starting capital (₹)"
                hint={
                  options?.limits?.min_capital
                    ? `minimum ${options.limits.min_capital.toLocaleString("en-IN")}`
                    : undefined
                }
              >
                <Input value={form.cash} onChange={(v) => set("cash", v)} />
              </Field>

              <Field label="Position sizing">
                <Select
                  value={form.sizingMode}
                  onChange={(v) => set("sizingMode", v)}
                  options={(options?.sizing_modes ?? []).map((m) => ({ value: m.value, label: m.label }))}
                />
              </Field>

              {form.sizingMode === "fixed_fraction" && (
                <Input
                  label="Percent of equity per position"
                  value={form.sizingPercent}
                  onChange={(v) => set("sizingPercent", v)}
                />
              )}
              {form.sizingMode === "fixed_quantity" && (
                <Input
                  label="Shares per position"
                  value={form.sizingQuantity}
                  onChange={(v) => set("sizingQuantity", v)}
                />
              )}

              <Field label="Stop loss %" hint="blank = none">
                <Input value={form.stopLoss} onChange={(v) => set("stopLoss", v)} />
              </Field>
              <Field label="Target %" hint="blank = none">
                <Input value={form.takeProfit} onChange={(v) => set("takeProfit", v)} />
              </Field>
              <Field label="Trailing stop %" hint="ratchets from the high-water mark">
                <Input value={form.trailingStop} onChange={(v) => set("trailingStop", v)} />
              </Field>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-3">
              <Switch
                checked={form.allowShort}
                onCheckedChange={(v) => set("allowShort", v)}
                label="Allow short positions"
              />
              <Switch
                checked={form.squareOff}
                onCheckedChange={(v) => set("squareOff", v)}
                label="Square off at end of day"
              />
            </div>

            {equalWeightConflict && (
              <div className="mt-3">
                <Callout tone="bad" title="Equal-weight sizing needs more than one symbol">
                  You asked to spread risk across a set with one member. Add symbols or
                  pick a different sizing mode.
                </Callout>
              </div>
            )}
          </Section>

          {/* 4 — costs */}
          <Section
            n={4}
            title="Transaction costs"
            note="A backtest is only as good as its cost assumption. This is the field that most often makes a strategy look profitable when it is not."
          >
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label="Cost model">
                <Select
                  value={form.costModel}
                  onChange={(v) => set("costModel", v)}
                  options={(options?.cost_models ?? []).map((m) => ({ value: m.value, label: m.label }))}
                />
              </Field>
              <Field label="Slippage (bps)" hint={slippageNote(form.slippage)}>
                <Input value={form.slippage} onChange={(v) => set("slippage", v)} />
              </Field>
            </div>
            {form.costModel === "none" && (
              <div className="mt-3">
                <Callout tone="warn">
                  Zero costs. Every trade is free, so any strategy with positive gross
                  edge looks profitable. Use this only to measure how much the cost
                  assumption is worth.
                </Callout>
              </div>
            )}
          </Section>

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Button
              onClick={() => void onRun()}
              disabled={busy || options === null || equalWeightConflict}
            >
              {busy ? (
                <>
                  <ButtonLoader size={16} />
                  Submitting…
                </>
              ) : (
                <>
                  <Play className="mr-1.5 h-4 w-4" />
                  Run backtest
                </>
              )}
            </Button>
            <Hint>
              Returns a run id immediately and executes on a worker — the page polls
              for progress rather than holding the request open.
            </Hint>
          </div>
        </div>
      </Card>

      <RunHistory runs={runs} onOpen={onOpenRun} />
    </div>
  );
}

function slippageNote(v: string): string | undefined {
  const n = Number(v);
  if (!Number.isFinite(n) || v.trim() === "") return undefined;
  return `${(n / 100).toFixed(2)}% per fill`;
}

function Section({
  n,
  title,
  note,
  children,
}: {
  n: number;
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border/60 p-4">
      <div className="mb-3 flex items-start gap-2.5">
        <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-primary/10 text-[11px] font-bold">
          {n}
        </span>
        <div className="min-w-0">
          <div className="text-[13px] font-semibold">{title}</div>
          {note && <div className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">{note}</div>}
        </div>
      </div>
      {children}
    </div>
  );
}

function Field({
  label,
  hint,
  warn,
  children,
}: {
  label: string;
  hint?: string;
  warn?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="px-1 text-sm font-medium text-foreground">{label}</label>
      {children}
      {hint && (
        <span
          className={cn(
            "px-1 text-[11px]",
            warn ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
          )}
        >
          {hint}
        </span>
      )}
    </div>
  );
}

// ─── run history ──────────────────────────────────────────────────────────────

function RunHistory({
  runs,
  onOpen,
}: {
  runs: BacktestRunSummary[];
  onOpen: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  if (runs.length === 0) return null;

  return (
    <Card>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-5 py-3.5 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-semibold">
          <Clock className="h-4 w-4 text-muted-foreground" />
          Previous runs
          <Badge tone="flat">{runs.length}</Badge>
        </span>
        <ChevronDown
          className={cn("h-4 w-4 text-muted-foreground transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <div className="overflow-x-auto border-t border-border/60">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-2 font-semibold">Run</th>
                <th className="px-3 py-2 font-semibold">Strategy</th>
                <th className="px-3 py-2 font-semibold">Status</th>
                <th className="px-3 py-2 text-right font-semibold">Return</th>
                <th className="px-3 py-2 text-right font-semibold">Sharpe</th>
                <th className="px-3 py-2 text-right font-semibold">Trades</th>
                <th className="px-3 py-2 font-semibold">Created</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.run_id}
                  onClick={() => onOpen(r.run_id)}
                  className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40"
                >
                  <td className="px-3 py-2 font-mono text-[11.5px] text-muted-foreground">
                    {r.run_id.slice(0, 8)}
                  </td>
                  <td className="px-3 py-2">
                    {r.strategy ?? "—"}
                    {r.strategy_version !== null && (
                      <span className="ml-1 text-muted-foreground">v{r.strategy_version}</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={r.status} />
                  </td>
                  <td
                    className={cn(
                      "px-3 py-2 text-right tabular-nums",
                      (r.total_return_pct ?? 0) < 0 && "text-destructive",
                    )}
                  >
                    {fmtPct(r.total_return_pct)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r.sharpe)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{fmtNum(r.num_trades, 0)}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    <RelativeTime value={r.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function StatusBadge({ status }: { status: BacktestStatus }) {
  if (status === "COMPLETED") return <Badge tone="good">completed</Badge>;
  if (status === "FAILED") return <Badge tone="bad">failed</Badge>;
  if (status === "CANCELLED") return <Badge tone="flat">cancelled</Badge>;
  return <Badge tone="warn">{status.toLowerCase()}</Badge>;
}

// ─── run progress ─────────────────────────────────────────────────────────────

/** The engine reports progress in named steps, not a per-bar percentage. Stating
 *  the step is honest; a smooth animated bar would only be reporting that time
 *  is passing. See `lib/backtest-results.ts`. */
function RunProgress({
  runId,
  run,
  onCancel,
  onBack,
}: {
  runId: string;
  run: BacktestRun | null;
  onCancel: () => void;
  onBack: () => void;
}) {
  const status = run?.status ?? "QUEUED";
  const progress = run?.progress ?? 0;

  return (
    <Card className="p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            {status === "RUNNING" ? (
              <ButtonLoader size={16} />
            ) : (
              <Clock className="h-4 w-4 text-muted-foreground" />
            )}
            <span className="text-sm font-semibold">
              {status === "QUEUED" ? "Queued" : "Running"}
            </span>
            <StatusBadge status={status} />
          </div>
          <div className="mt-2 text-[13px] text-muted-foreground">{progressLabel(progress)}</div>
          <div className="mt-1 font-mono text-[11.5px] text-muted-foreground">
            run {runId}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="secondary" size="sm" onClick={onBack}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back
          </Button>
          <Button variant="outline" size="sm" onClick={onCancel}>
            <XCircle className="mr-1.5 h-3.5 w-3.5" />
            Cancel
          </Button>
        </div>
      </div>

      <div className="mt-5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-500"
          style={{ width: `${progressWidth(progress)}%` }}
        />
      </div>

      <div className="mt-5 grid gap-x-6 gap-y-2 text-[12px]">
        {run?.created_at && (
          <Row k="Submitted">
            <RelativeTime value={run.created_at} />
          </Row>
        )}
        {run?.engine_key && (
          <Row k="Engine">
            <span className="font-mono">{run.engine_key}</span>
          </Row>
        )}
        {run?.config?.symbols && run.config.symbols.length > 0 && (
          <Row k="Symbols">{run.config.symbols.join(", ")}</Row>
        )}
        {run?.config?.universe && <Row k="Universe">{run.config.universe}</Row>}
        {run?.config?.start && (
          <Row k="Window">
            {run.config.start} → {run.config.end ?? "latest"}
          </Row>
        )}
      </div>

      <div className="mt-5">
        <Callout tone="info">
          The run executes on a worker thread and is written to the database when it
          finishes, so it survives a page reload. Progress reports in coarse steps — the
          engine exposes no per-bar callback, and a smooth bar would only be reporting
          the passage of time.
        </Callout>
      </div>
    </Card>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2">
      <span className="w-24 shrink-0 text-muted-foreground">{k}</span>
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

// ─── results ──────────────────────────────────────────────────────────────────

/** Friendly names for the keys the engine actually emits.
 *
 *  Everything not listed here still renders — `prettyMetric` title-cases the
 *  raw key — so adding a metric on the backend does not require a frontend
 *  change to become visible. */
const METRIC_LABELS: Record<string, string> = {
  total_return_pct: "Total return %",
  cagr_pct: "CAGR %",
  max_drawdown_pct: "Max drawdown %",
  max_drawdown_days: "Max drawdown days",
  sharpe: "Sharpe",
  sortino: "Sortino",
  calmar: "Calmar",
  win_rate_pct: "Win rate %",
  profit_factor: "Profit factor",
  annualized_vol_pct: "Annualised volatility %",
  exposure_pct: "Time in market %",
  num_trades: "Trades",
  avg_trade: "Average trade P&L",
  best_trade: "Best trade",
  worst_trade: "Worst trade",
  avg_holding_days: "Average holding days",
  start_equity: "Starting equity",
  end_equity: "Ending equity",
  total_commission: "Total commission & levies",
  total_slippage: "Total slippage",
  trades_truncated: "Trade list truncated",
  killed: "Halted by the risk engine",
  kill_reason: "Why it halted",
  final_positions: "Open positions at the end",
};

/** Metrics the summary strip promotes. Order is the order a trader reads them. */
const HEADLINE = [
  "total_return_pct",
  "cagr_pct",
  "max_drawdown_pct",
  "sharpe",
  "sortino",
  "win_rate_pct",
  "profit_factor",
  "num_trades",
];

const prettyMetric = (k: string) =>
  METRIC_LABELS[k] ??
  k.replace(/_pct$/, " %").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// Built-in engines have no separate display name — only the snake_case module
// key (`sma_crossover`, `paper_avellaneda_lee`). The key still belongs next to
// the label as a technical detail (see the mono badge below the picker); it
// just should not double as the label itself.
const prettyStrategyName = strategyLabel;

function Results({
  runId,
  run,
  onBack,
}: {
  runId: string;
  run: BacktestRun;
  onBack: () => void;
}) {
  const [curves, setCurves] = useState<BacktestCurves | null>(null);
  const [monthly, setMonthly] = useState<BacktestMonthly | null>(null);
  const [trades, setTrades] = useState<BacktestTrade[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<BacktestTradeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAllMetrics, setShowAllMetrics] = useState(false);

  const LIMIT = 100;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const [c, m, t] = await Promise.all([
          backtestCurves(runId),
          backtestMonthly(runId).catch(() => ({ run_id: runId, matrix: [] })),
          backtestTrades(runId, { limit: LIMIT, offset: 0 }),
        ]);
        if (cancelled) return;
        setCurves(c);
        setMonthly(m);
        setTrades(t.trades);
        setTotal(t.total);
        setOffset(0);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  async function loadPage(next: number) {
    try {
      setLoading(true);
      const t = await backtestTrades(runId, { limit: LIMIT, offset: next });
      setTrades(t.trades);
      setTotal(t.total);
      setOffset(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function openTrade(seq: number) {
    try {
      setDetail(await backtestTrade(runId, seq));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const metrics = run.metrics ?? {};
  const failed = run.status === "FAILED";

  return (
    <div className="space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <StatusBadge status={run.status} />
          <span className="text-sm font-semibold">{run.engine_key ?? "run"}</span>
          {run.strategy_version !== null && (
            <Badge tone="flat">v{run.strategy_version}</Badge>
          )}
          <span className="font-mono text-[11.5px] text-muted-foreground">
            {runId.slice(0, 8)}
          </span>
        </div>
        <Button variant="secondary" size="sm" onClick={onBack}>
          <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
          New run
        </Button>
      </div>

      {failed && (
        <Callout tone="bad" title="The run failed">
          {run.error ?? "No reason was recorded."}
        </Callout>
      )}

      {error && <ErrorBox>{error}</ErrorBox>}

      {!failed && (
        <>
          <Headline metrics={metrics} run={run} />

          <Reproducibility run={run} />

          <Card>
            <CardHeader
              title="Equity curve"
              sub="Marked once per bar, after costs. The drawdown below it is measured from the running peak."
            />
            <div className="p-5 pt-3">
              {plottable(curves?.equity) ? (
                <EquityChart
                  height={230}
                  series={[
                    { name: "Equity", points: curves!.equity, color: "var(--primary)" },
                  ]}
                />
              ) : loading ? (
                <ChartSkeleton />
              ) : (
                <EmptyChart label="No equity curve was recorded for this run." />
              )}

              {plottable(curves?.drawdown) && (
                <div className="mt-5">
                  <div className="mb-1.5 px-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Drawdown from peak (%)
                  </div>
                  <EquityChart
                    height={130}
                    valueFormat={(v) => `${v.toFixed(1)}%`}
                    series={[
                      {
                        name: "Drawdown",
                        points: curves!.drawdown,
                        color: "var(--destructive)",
                      },
                    ]}
                  />
                </div>
              )}

              {plottable(curves?.exposure) && (
                <details className="mt-5">
                  <summary className="cursor-pointer px-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Exposure — capital deployed per bar
                  </summary>
                  <div className="mt-2">
                    <EquityChart
                      height={120}
                      series={[
                        { name: "Exposure", points: curves!.exposure, color: "var(--muted-foreground)" },
                      ]}
                    />
                  </div>
                </details>
              )}
            </div>
          </Card>

          {monthly && monthly.matrix.length > 0 && (
            <MonthlyMatrix matrix={monthly.matrix} total={metrics.total_return_pct} />
          )}

          <Card>
            <CardHeader
              title="Trades"
              sub={`${total.toLocaleString("en-IN")} round trips${
                total > LIMIT ? ` — showing ${offset + 1}–${Math.min(offset + LIMIT, total)}` : ""
              }`}
            />
            <div className="p-5 pt-3">
              {trades.length === 0 ? (
                <EmptyChart label="This run produced no closed trades." />
              ) : (
                <>
                  <div className="overflow-x-auto rounded-xl border border-border">
                    <table className="w-full border-collapse text-[12.5px]">
                      <thead>
                        <tr className="border-b border-border bg-muted/40 text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                          <th className="px-3 py-2 font-semibold">#</th>
                          <th className="px-3 py-2 font-semibold">Symbol</th>
                          <th className="px-3 py-2 font-semibold">Side</th>
                          <th className="px-3 py-2 text-right font-semibold">Qty</th>
                          <th className="px-3 py-2 font-semibold">Entry</th>
                          <th className="px-3 py-2 font-semibold">Exit</th>
                          <th className="px-3 py-2 text-right font-semibold">Net P&L</th>
                          <th className="px-3 py-2 text-right font-semibold">Return</th>
                          <th className="px-3 py-2 font-semibold">Exited on</th>
                        </tr>
                      </thead>
                      <tbody>
                        {trades.map((t) => (
                          <tr
                            key={t.seq}
                            onClick={() => void openTrade(t.seq)}
                            className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40"
                          >
                            <td className="px-3 py-2 text-muted-foreground">{t.seq}</td>
                            <td className="px-3 py-2 font-medium">{t.symbol}</td>
                            <td className="px-3 py-2">
                              <span
                                className={cn(
                                  "text-[11px] font-semibold",
                                  t.direction === "LONG"
                                    ? "text-emerald-600 dark:text-emerald-400"
                                    : "text-destructive",
                                )}
                              >
                                {t.direction}
                              </span>
                            </td>
                            <td className="px-3 py-2 text-right tabular-nums">
                              {fmtNum(t.quantity, 0)}
                            </td>
                            <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                              {shortDate(t.entry_ts)}
                              <span className="ml-1.5 tabular-nums">
                                {fmtNum(t.entry_price, 2)}
                              </span>
                            </td>
                            <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                              {shortDate(t.exit_ts)}
                              <span className="ml-1.5 tabular-nums">
                                {fmtNum(t.exit_price, 2)}
                              </span>
                            </td>
                            <td
                              className={cn(
                                "px-3 py-2 text-right tabular-nums font-medium",
                                (t.net_pnl ?? 0) < 0 && "text-destructive",
                              )}
                            >
                              {fmtMoney(t.net_pnl)}
                            </td>
                            <td
                              className={cn(
                                "px-3 py-2 text-right tabular-nums",
                                (t.return_pct ?? 0) < 0 && "text-destructive",
                              )}
                            >
                              {fmtPct(t.return_pct)}
                            </td>
                            <td className="px-3 py-2">
                              <ExitReason
                                reason={t.exit_reason}
                                stopPct={run.config?.stops?.stop_loss_pct ?? null}
                                returnPct={t.return_pct}
                              />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {total > LIMIT && (
                    <div className="mt-3 flex items-center justify-between">
                      <Hint>
                        Showing {pageRange(offset, LIMIT, total).from}–
                        {pageRange(offset, LIMIT, total).to} of{" "}
                        {total.toLocaleString("en-IN")}
                      </Hint>
                      <div className="flex gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={offset === 0 || loading}
                          onClick={() => void loadPage(Math.max(0, offset - LIMIT))}
                        >
                          Previous
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={offset + LIMIT >= total || loading}
                          onClick={() => void loadPage(offset + LIMIT)}
                        >
                          Next
                        </Button>
                      </div>
                    </div>
                  )}

                  <Hint className="mt-3">
                    Click any row to see the full trade: entry, exit, quantity, P&L, the
                    strategy version that produced it, and the signal conditions behind
                    the entry.
                  </Hint>
                </>
              )}
            </div>
          </Card>

          <Card>
            <button
              type="button"
              onClick={() => setShowAllMetrics((v) => !v)}
              className="flex w-full items-center justify-between px-5 py-3.5 text-left"
            >
              <span className="flex items-center gap-2 text-sm font-semibold">
                <BarChart3 className="h-4 w-4 text-muted-foreground" />
                Every metric the engine reported
                <Badge tone="flat">{Object.keys(metrics).length}</Badge>
              </span>
              <ChevronDown
                className={cn(
                  "h-4 w-4 text-muted-foreground transition-transform",
                  showAllMetrics && "rotate-180",
                )}
              />
            </button>
            {showAllMetrics && (
              <div className="border-t border-border/60 p-5">
                <div className="overflow-hidden rounded-xl border border-border">
                  <table className="w-full border-collapse text-[13px]">
                    <tbody>
                      {Object.entries(metrics)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([k, v]) => (
                          <tr key={k} className="border-b border-border/60 last:border-0">
                            <td className="px-3 py-1.5 text-muted-foreground">
                              {prettyMetric(k)}
                            </td>
                            <td className="px-3 py-1.5 text-right font-medium tabular-nums">
                              {typeof v === "number" ? fmtNum(v, 4) : String(v)}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
                <Hint className="mt-3">
                  Shown as reported. The headline strip above uses the same numbers —
                  nothing here is recomputed for display.
                </Hint>
              </div>
            )}
          </Card>

          <Hint>
            This is an <strong>in-sample</strong> result: the strategy was run on a
            dataset it may have been chosen using. Treat it as a description of the past,
            not a forecast. The out-of-sample number lives in Evidence → Walk-forward.
          </Hint>
        </>
      )}

      <MorphingModal viewId={detail ? "trade" : null} onClose={() => setDetail(null)}>
        {detail && <TradeDetail t={detail} />}
      </MorphingModal>
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div className="grid h-[230px] place-items-center rounded-xl border border-dashed border-border">
      <PageLoader label="Loading chart" />
    </div>
  );
}

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="grid place-items-center rounded-xl border border-dashed border-border py-10 text-[13px] text-muted-foreground">
      {label}
    </div>
  );
}

/** The summary strip. Kept to the metrics a trader actually decides on, with
 *  the full set one click below. */
function Headline({
  metrics,
  run,
}: {
  metrics: Record<string, number | string | boolean | null>;
  run: BacktestRun;
}) {
  const present = HEADLINE.filter((k) => metrics[k] !== undefined && metrics[k] !== null);
  if (present.length === 0) return null;

  const total = Number(metrics.total_return_pct ?? 0);

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Stat
        label="Total return"
        value={fmtPct(metrics.total_return_pct)}
        tone={total >= 0 ? "good" : "bad"}
        sub={`CAGR ${fmtPct(metrics.cagr_pct)}`}
      />
      <Stat
        label="Max drawdown"
        value={fmtPct(-Math.abs(Number(metrics.max_drawdown_pct ?? 0)))}
        tone="bad"
        sub={
          metrics.max_drawdown_days
            ? `${fmtNum(metrics.max_drawdown_days, 0)} days underwater`
            : undefined
        }
      />
      <Stat
        label="Sharpe"
        value={fmtNum(metrics.sharpe)}
        sub={`Sortino ${fmtNum(metrics.sortino)}`}
      />
      <Stat
        label="Win rate"
        value={fmtPct(metrics.win_rate_pct, 1)}
        sub={`profit factor ${fmtNum(metrics.profit_factor)}`}
      />
      <Stat
        label="Trades"
        value={fmtNum(metrics.num_trades, 0)}
        sub={run.config?.sizing?.mode?.replace(/_/g, " ")}
      />
      <Stat
        label="Ending equity"
        value={fmtMoney(metrics.end_equity)}
        tone={
          metrics.start_equity !== undefined &&
          Number(metrics.end_equity ?? 0) >= Number(metrics.start_equity)
            ? "good"
            : "bad"
        }
        sub={
          metrics.start_equity !== undefined
            ? `from ${fmtMoney(metrics.start_equity)}`
            : undefined
        }
      />
      <Stat
        label="Costs paid"
        value={fmtMoney(
          Number(metrics.total_commission ?? 0) + Number(metrics.total_slippage ?? 0),
        )}
        sub={`${fmtMoney(metrics.total_commission)} commission · ${fmtMoney(
          metrics.total_slippage,
        )} slippage`}
        hint="Cost model plus slippage, over the whole run. A passing backtest is only as good as its cost assumption."
      />
      <Stat
        label="Window"
        value={
          run.config?.start && run.config?.end
            ? `${run.config.start.slice(0, 4)}–${run.config.end.slice(0, 4)}`
            : "full history"
        }
        sub={
          run.config?.symbols?.length
            ? `${run.config.symbols.length} symbols`
            : (run.config?.universe ?? undefined)
        }
      />
    </div>
  );
}

/** Reproducibility block. A run is only reproducible if the data it consumed is
 *  fingerprinted, and the panel says which of the two conditions hold. */
function Reproducibility({ run }: { run: BacktestRun }) {
  const cfg = run.config;
  if (!cfg) return null;
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start gap-x-8 gap-y-3 text-[12px]">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Repeat className="h-3.5 w-3.5" />
            Reproducibility
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {run.reproducible ? (
              <Badge tone="good">data fingerprinted</Badge>
            ) : (
              <Badge tone="warn">no data fingerprint</Badge>
            )}
            <span className="text-muted-foreground">
              the full config is stored, so this run can be replayed exactly
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <KV k="Engine" v={run.engine_key ?? "—"} mono />
          <KV k="Version" v={run.strategy_version !== null ? `v${run.strategy_version}` : "unversioned"} />
          <KV k="Timeframe" v={cfg.timeframe ?? "1d"} />
          <KV k="Sizing" v={cfg.sizing?.mode?.replace(/_/g, " ") ?? "—"} />
          <KV
            k="Stops"
            v={[
              cfg.stops?.stop_loss_pct ? `SL ${cfg.stops.stop_loss_pct}%` : null,
              cfg.stops?.take_profit_pct ? `TP ${cfg.stops.take_profit_pct}%` : null,
              cfg.stops?.trailing_stop_pct ? `TS ${cfg.stops.trailing_stop_pct}%` : null,
            ]
              .filter(Boolean)
              .join(" · ") || "none"}
          />
        </div>
      </div>
      {run.data_fingerprint && (
        <div className="mt-3 font-mono text-[11px] text-muted-foreground">
          data {run.data_fingerprint.slice(0, 16)}…
        </div>
      )}
    </Card>
  );
}

function KV({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{k}</div>
      <div className={cn("font-medium", mono && "font-mono text-[11.5px]")}>{v}</div>
    </div>
  );
}

/** How a trade closed. A realised loss past the configured stop is labelled as
 *  a gap-through, because that is what happened: the engine fills on the next
 *  open, so a stop is a trigger and not a guaranteed price. */
function ExitReason({
  reason,
  stopPct,
  returnPct,
}: {
  reason: string | null;
  stopPct: number | null;
  returnPct: number | null;
}) {
  if (!reason) return <span className="text-muted-foreground">—</span>;

  const gapped = gappedThroughStop(reason, stopPct, returnPct);

  const label = reason.replace(/_/g, " ");
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span
        className={cn(
          "text-[11.5px] font-medium",
          reason === "stop_loss" || reason === "trailing_stop"
            ? "text-destructive"
            : reason === "take_profit"
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-muted-foreground",
        )}
      >
        {label}
      </span>
      {gapped && (
        <span
          title={`Closed at ${returnPct!.toFixed(2)}% against a ${stopPct}% stop — the engine fills on the next open, so the stop fired and the gap set the price.`}
          className="cursor-help text-[10.5px] text-amber-600 dark:text-amber-400"
        >
          gapped through
        </span>
      )}
    </span>
  );
}

// ─── monthly matrix ───────────────────────────────────────────────────────────

function MonthlyMatrix({
  matrix,
  total,
}: {
  matrix: BacktestMonthly["matrix"];
  total: number | string | boolean | null | undefined;
}) {
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // Reconcile the matrix against the stated total. This is the check that makes
  // the matrix evidence rather than decoration.
  const { compounded, stated, drift, reconciled } = useMemo(
    () => reconcile(matrix, total),
    [matrix, total],
  );

  function cell(v: number | null | undefined) {
    if (v === null || v === undefined) {
      return <span className="text-muted-foreground/40">·</span>;
    }
    return (
      <span
        className={cn(
          "tabular-nums",
          v > 0.01
            ? "text-emerald-600 dark:text-emerald-400"
            : v < -0.01
              ? "text-destructive"
              : "text-muted-foreground",
        )}
      >
        {v.toFixed(2)}
      </span>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Monthly returns"
        sub="Percentage return per calendar month. A dot means the series had no bars that month, which is not the same as a flat month."
      />
      <div className="p-5 pt-3">
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full border-collapse text-[11.5px]">
            <thead>
              <tr className="border-b border-border bg-muted/40 text-[10.5px] uppercase tracking-wide text-muted-foreground">
                <th className="px-2.5 py-2 text-left font-semibold">Year</th>
                {months.map((m) => (
                  <th key={m} className="px-2 py-2 text-right font-semibold">
                    {m}
                  </th>
                ))}
                <th className="px-2.5 py-2 text-right font-semibold">Year</th>
              </tr>
            </thead>
            <tbody>
              {matrix.map((row) => (
                <tr key={row.year} className="border-b border-border/60 last:border-0">
                  <td className="px-2.5 py-2 font-medium tabular-nums">{row.year}</td>
                  {row.months.map((v, i) => (
                    <td key={i} className="px-2 py-2 text-right">
                      {cell(v)}
                    </td>
                  ))}
                  <td
                    className={cn(
                      "px-2.5 py-2 text-right font-semibold tabular-nums",
                      (row.year_total ?? 0) < 0 && "text-destructive",
                      (row.year_total ?? 0) > 0 && "text-emerald-600 dark:text-emerald-400",
                    )}
                  >
                    {row.year_total === null ? "·" : `${row.year_total.toFixed(2)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12px]">
          {reconciled ? (
            <>
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
              <span className="text-muted-foreground">
                Compounding the months gives{" "}
                <span className="font-medium tabular-nums text-foreground">
                  {fmtPct(compounded, 3)}
                </span>
                , matching the run total of{" "}
                <span className="font-medium tabular-nums text-foreground">
                  {fmtPct(stated, 3)}
                </span>
                .
              </span>
            </>
          ) : compounded !== null ? (
            <>
              <AlertTriangle className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" />
              <span className="text-muted-foreground">
                Compounding the months gives{" "}
                <span className="font-medium tabular-nums">{fmtPct(compounded, 3)}</span> but the
                run states{" "}
                <span className="font-medium tabular-nums">{fmtPct(stated, 3)}</span> — a
                difference of {drift?.toFixed(3)}pp.
              </span>
            </>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

// ─── trade detail ─────────────────────────────────────────────────────────────

function TradeDetail({ t }: { t: BacktestTradeDetail }) {
  const win = (t.net_pnl ?? 0) >= 0;
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-base font-bold">{t.symbol}</span>
            <span
              className={cn(
                "text-[11px] font-bold uppercase tracking-wide",
                t.direction === "LONG"
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-destructive",
              )}
            >
              {t.direction}
            </span>
          </div>
          <div className="mt-0.5 text-[11.5px] text-muted-foreground">
            trade #{t.seq} · held {fmtNum(t.duration_days, 1)} days
          </div>
        </div>
        <div className="text-right">
          <div
            className={cn(
              "text-lg font-bold tabular-nums",
              win ? "text-emerald-600 dark:text-emerald-400" : "text-destructive",
            )}
          >
            {fmtMoney(t.net_pnl)}
          </div>
          <div className="text-[11.5px] tabular-nums text-muted-foreground">
            {fmtPct(t.return_pct)}
          </div>
        </div>
      </div>

      {/* entry / exit */}
      <div className="grid grid-cols-2 gap-3">
        <Leg
          title="Entry"
          ts={t.entry_ts}
          price={t.entry_price}
          qty={t.quantity}
        />
        <Leg title="Exit" ts={t.exit_ts} price={t.exit_price} qty={t.quantity} />
      </div>

      {/* P&L breakdown */}
      <div className="rounded-xl border border-border">
        <table className="w-full border-collapse text-[12.5px]">
          <tbody>
            <DetailRow k="Gross P&L" v={fmtMoney(t.gross_pnl)} />
            <DetailRow
              k="Commission & levies"
              v={`− ${fmtMoney(t.commission)}`}
              tone="bad"
            />
            <DetailRow k="Net P&L" v={fmtMoney(t.net_pnl)} strong />
            <DetailRow k="Return" v={fmtPct(t.return_pct)} />
          </tbody>
        </table>
      </div>

      {/* why it closed */}
      <div>
        <DetailLabel>How it closed</DetailLabel>
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <ExitReason
            reason={t.exit_reason}
            stopPct={t.exits?.stop_loss_pct ?? null}
            returnPct={t.return_pct}
          />
        </div>
        <div className="mt-2 rounded-xl border border-border/60 bg-muted/30 px-3 py-2.5 text-[12px]">
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            <span>
              stop loss{" "}
              <span className="font-medium text-foreground">
                {t.exits?.stop_loss_pct != null ? `${t.exits.stop_loss_pct}%` : "none"}
              </span>
            </span>
            <span>
              target{" "}
              <span className="font-medium text-foreground">
                {t.exits?.take_profit_pct != null ? `${t.exits.take_profit_pct}%` : "none"}
              </span>
            </span>
            <span>
              trailing{" "}
              <span className="font-medium text-foreground">
                {t.exits?.trailing_stop_pct != null ? `${t.exits.trailing_stop_pct}%` : "none"}
              </span>
            </span>
          </div>
          <div className="mt-1.5 text-[11.5px] leading-relaxed text-muted-foreground">
            A stop is a trigger, not a guaranteed fill — the engine fills on the next open,
            so a gap can close a trade past its stop.
          </div>
        </div>
      </div>

      {/* signal conditions — the answer to "why did this trade happen" */}
      <div>
        <DetailLabel>Signal conditions</DetailLabel>
        {t.signal_reason ? (
          <div className="mt-1.5 rounded-xl border border-border bg-muted/30 px-3 py-2.5 font-mono text-[12px] leading-relaxed">
            {t.signal_reason}
          </div>
        ) : (
          <div className="mt-1.5 rounded-xl border border-dashed border-border px-3 py-2.5 text-[12px] text-muted-foreground">
            Not recorded. This strategy does not report why it opened a position, so the
            reason cannot be reconstructed after the fact — it is not stored anywhere else.
          </div>
        )}
      </div>

      {/* strategy version */}
      <div>
        <DetailLabel>Strategy that produced this trade</DetailLabel>
        <div className="mt-1.5 grid grid-cols-2 gap-2 text-[12px]">
          <div className="rounded-xl border border-border px-3 py-2">
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              Engine
            </div>
            <div className="font-mono text-[12px]">{t.strategy?.engine_key ?? "—"}</div>
          </div>
          <div className="rounded-xl border border-border px-3 py-2">
            <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
              Version
            </div>
            <div>
              {t.strategy?.version != null ? (
                `v${t.strategy.version}`
              ) : (
                <span className="text-muted-foreground">built-in, unversioned</span>
              )}
            </div>
          </div>
        </div>
        {t.strategy?.params && Object.keys(t.strategy.params).length > 0 && (
          <div className="mt-2 rounded-xl border border-border/60 bg-muted/30 px-3 py-2 font-mono text-[11.5px]">
            {Object.entries(t.strategy.params)
              .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
              .join("  ")}
          </div>
        )}
        {t.strategy?.strategy_id && (
          <div className="mt-1.5 font-mono text-[10.5px] text-muted-foreground">
            strategy_id {t.strategy.strategy_id}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 rounded-xl border border-border/60 bg-muted/30 px-3 py-2 text-[11.5px] text-muted-foreground">
        <Info className="h-3.5 w-3.5 shrink-0" />
        <span>
          Cost model <span className="font-medium">{t.costs?.model ?? "—"}</span> at{" "}
          {fmtNum(t.costs?.slippage_bps ?? 0, 0)} bps slippage · sizing{" "}
          {t.sizing?.mode?.replace(/_/g, " ") ?? "—"}
        </span>
      </div>
    </div>
  );
}

function Leg({
  title,
  ts,
  price,
  qty,
}: {
  title: string;
  ts: string | null;
  price: number | null;
  qty: number;
}) {
  return (
    <div className="rounded-xl border border-border px-3 py-2.5">
      <div className="text-[10.5px] uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="mt-1 text-[13px] font-semibold tabular-nums">{fmtNum(price, 2)}</div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{longDate(ts)}</div>
      <div className="mt-1 text-[11px] tabular-nums text-muted-foreground">
        {fmtNum(qty, 0)} shares
      </div>
    </div>
  );
}

function DetailLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
      <Layers className="h-3.5 w-3.5" />
      {children}
    </div>
  );
}

function DetailRow({
  k,
  v,
  strong,
  tone,
}: {
  k: string;
  v: string;
  strong?: boolean;
  tone?: "bad";
}) {
  return (
    <tr className="border-b border-border/60 last:border-0">
      <td className="px-3 py-1.5 text-muted-foreground">{k}</td>
      <td
        className={cn(
          "px-3 py-1.5 text-right tabular-nums",
          strong ? "font-bold" : "font-medium",
          tone === "bad" && "text-destructive",
        )}
      >
        {v}
      </td>
    </tr>
  );
}

// ─── dates ────────────────────────────────────────────────────────────────────

function shortDate(ts: string | null): string {
  if (!ts) return "open";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(0, 10);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" });
}

function longDate(ts: string | null): string {
  if (!ts) return "still open";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

// Keep the unused-import checker honest about icons that document intent.
void HelpCircle;
void RefreshCw;
