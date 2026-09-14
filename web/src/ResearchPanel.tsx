import { Check, HelpCircle, ShieldCheck, Shuffle, X, Zap } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useState } from "react";
import {
  getStrategies,
  runResearch,
  type FoldRow,
  type ResearchResponse,
  type StrategyInfo,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, Hint } from "./components/ui/card";
import { EquityChart } from "./components/ui/equity-chart";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import {
  Badge,
  Callout,
  Stat,
  VerdictPill,
  fmtNum,
  fmtPct,
} from "./components/ui/stat";
import { Switch } from "./components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { cn } from "./lib/utils";
import ValidationPanel from "./ValidationPanel";
import EpisodicPivotPanel from "./EpisodicPivotPanel";
import AlphaHuntPanel from "./AlphaHuntPanel";

type Source = "cache" | "fetch" | "synthetic";

const SOURCE_BLURB: Record<Source, string> = {
  cache: "Quick (saved, ~1 year)",
  fetch: "Full history (from broker)",
  synthetic: "Fake data (machinery check)",
};

const SOURCE_HELP: Record<Source, string> = {
  cache:
    "Runs instantly and needs no login, but only holds about a year of prices — usually too short for a conclusive answer. Fine for a first look.",
  fetch:
    "Pulls years of daily prices from your broker. Takes a few minutes, but it is the only option that gives a strategy enough room to prove itself. Use this when you want a real answer.",
  synthetic:
    "Computer-generated random prices. Only useful for confirming the testing machinery works — any result here is meaningless.",
};

const EXPLAINER_KEY = "atr.research.explainer";

type Preset = "quick" | "full" | "control";

interface PresetDef {
  id: Preset;
  icon: typeof Zap;
  title: string;
  blurb: string;
  badge?: string;
}

const PRESETS: PresetDef[] = [
  {
    id: "quick",
    icon: Zap,
    title: "Quick smoke test",
    blurb: "Run on the saved data (~1 year). Fast, but the verdict will not be conclusive — good for a first look.",
  },
  {
    id: "full",
    icon: ShieldCheck,
    title: "Full validation",
    blurb: "Run on broker history (~6 years) and search a grid of settings. The real answer — use this for a verdict you trust.",
    badge: "Recommended",
  },
  {
    id: "control",
    icon: Shuffle,
    title: "Control test",
    blurb: "Run on computer-generated prices. Only confirms the testing machinery works — the numbers themselves are meaningless.",
  },
];

/** What the run is about to do, phrased for a non-trader. */
function previewLine(strategy: string, source: Source, nSymbols: number, search: boolean): string {
  const stocks = nSymbols > 0 ? `${nSymbols} stocks` : "your default list of stocks";
  const data =
    source === "cache"
      ? "on the saved data (~1 year)"
      : source === "fetch"
        ? "on broker history (~6 years)"
        : "on computer-generated prices";
  const search_ = search ? " It will search a grid of settings to be honest about the bar." : "";
  return `Will test ${strategy} against ${stocks} ${data}.${search_}`;
}


/** Plain-language verdict: the one sentence a non-trader can act on. */
function plainVerdict(
  result: ResearchResponse,
  oos: Record<string, number>,
  bench: Record<string, number>,
): string {
  if (result.source === "synthetic") {
    return "This ran on computer-generated prices, so the numbers mean nothing. It only tells you the testing machinery works.";
  }
  const ret = oos.total_return_pct ?? 0;
  const bh = bench.total_return_pct ?? 0;
  const folds = result.folds.length;
  const trials = result.n_trials;
  if (result.verdict.passed) {
    return `This held up. Across ${folds} stretches of prices it had never seen, it returned ${ret.toFixed(2)}%, against ${bh.toFixed(2)}% from simply buying and holding the same stocks. After adjusting for the ${trials} settings tried, the edge looks real rather than lucky.`;
  }
  return `Do not trade this. Across ${folds} stretches of prices it had never seen, it returned ${ret.toFixed(2)}%, while simply buying and holding the same stocks would have returned ${bh.toFixed(2)}%. After adjusting for the ${trials} settings tried, the edge is not statistically real.`;
}

/** Why this page exists, in words anyone can follow. Dismissible. */
function PlainExplainer({ onDismiss }: { onDismiss: () => void }) {
  return (
    <Card className="border-primary/25 bg-primary/[0.03]">
      <div className="flex items-start gap-3 p-4">
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
          <HelpCircle size={15} />
        </span>
        <div className="min-w-0 flex-1 space-y-2 text-[12.5px] leading-relaxed text-muted-foreground">
          <div className="text-[13px] font-semibold text-foreground">Why this page exists</div>
          <p>
            A normal backtest is easy to fool. It scores a strategy on the same
            prices that were used to pick its settings, so a great-looking number
            often just means the strategy memorised the past.
          </p>
          <p>
            This page cuts history into consecutive chunks. The strategy learns
            its settings on one chunk, then is scored on the chunk immediately
            after — prices it has never seen. It slides forward and repeats. Only
            the unseen chunks count.
          </p>
          <p>
            Two extra guards a normal backtest skips: the result is compared
            against simply buying and holding the same stocks, and the passing
            bar gets stricter the more settings you try — so trying hundreds of
            combinations cannot manufacture a result.
          </p>
          <p className="font-medium text-foreground">
            Read the verdict as a yes/no on &ldquo;is this real?&rdquo;. Everything
            else on this page is the evidence behind that answer.
          </p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Hide this explanation"
          className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <X size={13} />
        </button>
      </div>
    </Card>
  );
}

function paramKeys(folds: FoldRow[]): string[] {
  const keys = new Set<string>();
  for (const f of folds) {
    for (const k of Object.keys(f)) if (k.startsWith("param_")) keys.add(k);
  }
  return [...keys].sort();
}

function shortDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" });
}

export default function ResearchPanel({
  forcedTab,
  onTabChange,
}: {
  /** Lets the parent (Evidence section) drive which sub-view is showing. */
  forcedTab?: "harness" | "measured";
  onTabChange?: (tab: "harness" | "measured") => void;
} = {}) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategy, setStrategy] = useState("signals_entry");
  const [source, setSource] = useState<Source>("cache");
  const [symbols, setSymbols] = useState("");
  const [search, setSearch] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [train, setTrain] = useState("");
  const [test, setTest] = useState("");
  const [warmup, setWarmup] = useState("");
  const [minFolds, setMinFolds] = useState("3");
  const [minTrades, setMinTrades] = useState("20");
  const [confidence, setConfidence] = useState("0.95");
  const [mode, setMode] = useState<"run" | "measured">("run");
  const view: "run" | "measured" =
    forcedTab === undefined ? mode : forcedTab === "measured" ? "measured" : "run";
  const [showExplainer, setShowExplainer] = useState(
    () => localStorage.getItem(EXPLAINER_KEY) !== "hidden",
  );
  const [preset, setPreset] = useState<Preset>("full");
  const reduce = useReducedMotion();
  const [state, setState] = useState<ButtonState>("idle");

  function applyPreset(p: Preset) {
    setPreset(p);
    if (p === "quick")   { setSource("cache");     setSearch(false); }
    if (p === "full")    { setSource("fetch");     setSearch(true);  }
    if (p === "control") { setSource("synthetic"); setSearch(false); }
  }
  const [result, setResult] = useState<ResearchResponse | null>(null);

  useEffect(() => {
    getStrategies()
      .then((r) => setStrategies(r.strategies))
      .catch(() => setStrategies([]));
  }, []);

  const info = strategies.find((s) => s.name === strategy);
  const pKeys = useMemo(() => (result ? paramKeys(result.folds) : []), [result]);

  async function onRun() {
    setState("loading");
    try {
      const res = await runResearch({
        strategy,
        source,
        symbols: symbols
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
        search: strategy === "signals_entry" ? search : undefined,
        train: train === "" ? null : Number(train),
        test: test === "" ? null : Number(test),
        warmup: warmup === "" ? null : Number(warmup),
        min_folds: Number(minFolds) || 3,
        min_trades: Number(minTrades) || 0,
        confidence: Number(confidence) || 0.95,
      });
      setResult(res);
      setState("success");
    } catch (e) {
      setResult(null);
      throw e instanceof Error ? e : new Error(String(e));
      setState("error");
    }
  }

  const oos = result?.oos;
  const bench = result?.benchmark;
  const beats = oos && bench ? oos.sharpe > bench.sharpe : null;

  return (
    <div className="space-y-3.5">
      {/* When the Evidence section drives the view, its own sub-tabs are the
          switcher — showing a second one here would just be two controls
          fighting over the same state. */}
      {forcedTab === undefined && (
        <div className="flex rounded-lg border border-border/60 overflow-hidden w-fit text-sm font-semibold">
          {(["run", "measured"] as const).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => {
                setMode(v);
                onTabChange?.(v === "measured" ? "measured" : "harness");
              }}
              className={cn(
                "px-4 py-1.5 transition-colors",
                view === v
                  ? "bg-primary text-primary-foreground"
                  : "bg-background text-muted-foreground hover:text-foreground",
              )}
            >
              {v === "run" ? "Run a test" : "Measured results"}
            </button>
          ))}
        </div>
      )}

      {view === "measured" ? (
        <>
          <ValidationPanel />
          <EpisodicPivotPanel />
          <AlphaHuntPanel />
        </>
      ) : (
        <>
      {showExplainer ? (
        <PlainExplainer
          onDismiss={() => {
            setShowExplainer(false);
            localStorage.setItem(EXPLAINER_KEY, "hidden");
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => {
            setShowExplainer(true);
            localStorage.removeItem(EXPLAINER_KEY);
          }}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <HelpCircle size={12} /> What is this page for?
        </button>
      )}

      <Card>
        <CardHeader
          title="Set up the test"
          sub="A preset does the right thing by default. Override anything you want."
        />
        <div className="space-y-5 p-5">
          <div>
            <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
              Step 1 · Pick a test
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              {PRESETS.map((p) => {
                const Icon = p.icon;
                const active = preset === p.id;
                return (
                  <motion.button
                    key={p.id}
                    type="button"
                    onClick={() => applyPreset(p.id)}
                    whileHover={reduce ? undefined : { y: -2 }}
                    transition={{ type: "spring", stiffness: 380, damping: 28 }}
                    className={cn(
                      "relative rounded-2xl border bg-card p-4 text-left transition-colors",
                      active
                        ? "border-primary bg-primary/[0.06]"
                        : "border-border hover:border-foreground/30",
                    )}
                  >
                    <div className="flex items-start justify-between">
                      <span
                        className={cn(
                          "grid h-9 w-9 place-items-center rounded-lg",
                          active
                            ? "bg-primary text-primary-foreground"
                            : "bg-primary/[0.09] text-primary",
                        )}
                      >
                        <Icon size={16} />
                      </span>
                      {active ? (
                        <span className="grid h-5 w-5 place-items-center rounded-full bg-foreground text-background">
                          <Check size={11} />
                        </span>
                      ) : p.badge ? (
                        <span className="rounded-full bg-foreground/[0.07] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                          {p.badge}
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-3 text-sm font-semibold text-foreground">{p.title}</div>
                    <div className="mt-1 text-[12px] leading-snug text-muted-foreground">
                      {p.blurb}
                    </div>
                  </motion.button>
                );
              })}
            </div>
          </div>

          <div className="border-t border-border pt-4">
            <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
              Step 2 · Configure
            </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <Tabs
              value={source}
              onValueChange={(v) => setSource(v as Source)}
              variant="segment"
            >
              <TabsList>
                {(Object.keys(SOURCE_BLURB) as Source[]).map((s) => (
                  <TabsTrigger key={s} value={s}>
                    {SOURCE_BLURB[s]}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setAdvanced((a) => !a)}
              title="Window sizing, fold and confidence requirements"
            >
              {advanced ? "Hide advanced" : "Advanced"}
            </Button>
          </div>

          <Hint>{SOURCE_HELP[source]}</Hint>

          <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <label className="px-1 text-sm font-medium text-foreground">What to test</label>
              <select
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
                className="h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 [&>option]:bg-card"
              >
                {(strategies.length
                  ? strategies.map((s) => s.name)
                  : ["signals_entry", "sma_crossover"]
                ).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <Input
              label="Which stocks? (blank = your default list)"
              value={symbols}
              onChange={setSymbols}
              placeholder="RELIANCE-EQ,INFY-EQ"
            />
            {info?.warmup_bars ? (
              <div className="flex items-end pb-2.5">
                <Hint>
                  Needs about <strong>{info.warmup_bars}</strong> bars of history before
                  its rules fire.
                </Hint>
              </div>
            ) : null}
          </div>

          {strategy === "signals_entry" && (
            <Switch
              checked={search}
              onCheckedChange={setSearch}
              label="Try several settings (this makes the bar stricter, not the result better)"
            />
          )}

          {advanced && (
            <div className="grid gap-3.5 border-t border-border pt-3.5 sm:grid-cols-2 lg:grid-cols-5">
              <Input label="Train bars (blank = auto)" value={train} onChange={setTrain} placeholder="auto" />
              <Input label="Test bars (blank = auto)" value={test} onChange={setTest} placeholder="auto" />
              <Input label="Warmup bars (blank = auto)" value={warmup} onChange={setWarmup} placeholder="auto" />
              <Input label="Min folds" value={minFolds} onChange={setMinFolds} />
              <Input label="Min trades" value={minTrades} onChange={setMinTrades} />
              <Input label="Min confidence" value={confidence} onChange={setConfidence} />
            </div>
          )}

          <div className="flex items-center gap-3">
          </div>
        </div>
      </div>
      </Card>

      <Card className="overflow-hidden">
        <div className="grid items-stretch gap-0 sm:grid-cols-[1fr_auto]">
          <div className="space-y-2 p-5">
            <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
              Step 3 · Run it
            </div>
            <p className="text-[14px] leading-relaxed text-foreground">
              {previewLine(
                strategy,
                source,
                symbols.trim() ? symbols.split(",").filter(Boolean).length : 0,
                search,
              )}
            </p>
            <p className="text-[11.5px] text-muted-foreground">
              A normal run takes 30–60 seconds. A broker-history run can take 2–3 minutes the first time.
            </p>
          </div>
          <div className="flex items-center justify-end p-5 sm:p-5">
            <StatefulButton
              state={state}
              size="lg"
              onClick={() => void onRun()}
              loadingText={source === "fetch" ? "Pulling history…" : "Running the test…"}
              successText="Done"
              errorText="Failed — retry"
              className="w-full sm:w-auto"
            >
              Run the test
            </StatefulButton>
          </div>
        </div>
      </Card>

      {result && oos && bench && (
        <>
          <Card className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2.5">
                <VerdictPill passed={result.verdict.passed}>
                  {result.verdict.passed ? "Passed" : "Failed"}
                </VerdictPill>
                <span className="text-[13px] text-muted-foreground">
                  {result.strategy} · {result.symbols.length} symbols ·{" "}
                  {result.bars.toLocaleString("en-IN")} bars ·{" "}
                  {result.folds.length} folds · {result.n_trials} trials
                </span>

                <p
                  className={cn(
                    "mt-3 w-full rounded-xl border px-3.5 py-2.5 text-[13px] leading-relaxed",
                    result.verdict.passed
                      ? "border-emerald-500/30 bg-emerald-500/[0.06] text-emerald-700 dark:text-emerald-300"
                      : "border-destructive/30 bg-destructive/[0.06] text-destructive",
                  )}
                >
                  <strong className="font-semibold">
                    {result.verdict.passed ? "What this means: " : "What this means: "}
                  </strong>
                  {plainVerdict(result, oos, bench)}
                </p>
              </div>
              <Badge tone={result.source === "synthetic" ? "warn" : "flat"}>
                {result.source === "synthetic" ? "synthetic data" : `${result.source} data`}
              </Badge>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Return on unseen prices"
                value={fmtPct(oos.total_return_pct)}
                tone={oos.total_return_pct >= 0 ? "good" : "bad"}
                sub={`buy & hold ${fmtPct(bench.total_return_pct)}`}
                hint="Total return across only the stretches of prices the strategy had never seen."
              />
              <Stat
                label="Return vs risk (unseen)"
                value={fmtNum(oos.sharpe)}
                tone={beats ? "good" : "bad"}
                sub={`buy & hold ${fmtNum(bench.sharpe)}`}
                hint="Return earned per unit of risk taken, measured only on unseen prices. Higher is better; above 1 is decent."
              />
              <Stat
                label="Edge after adjusting for attempts"
                value={fmtNum(result.deflated_sharpe, 3)}
                tone={result.deflated_sharpe >= 0.95 ? "good" : "bad"}
                sub={`hurdle ${fmtNum(result.required_sharpe, 3)} for ${result.n_trials} trials`}
                hint="The risk-adjusted return after discounting for how many settings were tried. Must clear the hurdle to count as real."
              />
              <Stat
                label="Worst fall (unseen)"
                value={fmtPct(-Math.abs(oos.max_drawdown_pct))}
                sub={`${oos.num_trades} trades · win rate ${fmtPct(oos.win_rate_pct, 1)}`}
              />
            </div>

            {result.warnings.length > 0 && (
              <div className="mt-3.5 space-y-2">
                {result.warnings.map((w) => (
                  <Callout key={w} tone="warn" title="Read this before trusting the numbers">
                    {w}
                  </Callout>
                ))}
              </div>
            )}

            <div className="mt-4">
              <EquityChart
                height={230}
                series={[
                  { name: "Strategy (out of sample)", points: result.equity, color: "var(--primary)" },
                  {
                    name: "Buy & hold",
                    points: result.benchmark_equity,
                    color: "var(--muted-foreground)",
                    dashed: true,
                    fill: false,
                  },
                ]}
              />
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Verdict checks"
              sub="Every box must be ticked. A red one is the test doing its job — it is protecting you."
            />
            <div className="p-5 pt-3">
              <div className="overflow-hidden rounded-xl border border-border">
                <table className="w-full border-collapse text-[13px]">
                  <tbody>
                    {result.verdict.checks.map((c) => (
                      <tr
                        key={c.name}
                        className="border-b border-border/60 last:border-0"
                      >
                        <td className="w-8 px-3 py-2">
                          <span
                            className={cn(
                              "grid h-4.5 w-4.5 place-items-center rounded-full text-[10px] font-bold text-white",
                              c.ok ? "bg-emerald-500" : "bg-destructive",
                            )}
                          >
                            {c.ok ? "✓" : "✕"}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-medium">{c.name}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                          {c.detail}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Per-fold detail"
              sub={`train ${result.train_bars} · test ${result.test_bars} · step ${result.step_bars} · warmup ${result.warmup_bars} bars`}
            />
            <div className="p-5 pt-3">
              <div className="overflow-x-auto rounded-xl border border-border">
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-border bg-muted/40 text-left">
                      <th className="px-3 py-2 font-semibold">Fold</th>
                      <th className="px-3 py-2 font-semibold">Train</th>
                      <th className="px-3 py-2 font-semibold">Test</th>
                      {pKeys.map((k) => (
                        <th key={k} className="px-3 py-2 font-semibold">
                          {k.replace("param_", "")}
                        </th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Train Sharpe</th>
                      <th className="px-3 py-2 text-right font-semibold">Test Sharpe</th>
                      <th className="px-3 py-2 text-right font-semibold">Test return</th>
                      <th className="px-3 py-2 text-right font-semibold">Test DD</th>
                      <th className="px-3 py-2 text-right font-semibold">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.folds.map((f) => (
                      <tr key={f.fold} className="border-b border-border/60 last:border-0">
                        <td className="px-3 py-1.5 font-semibold">{f.fold}</td>
                        <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground">
                          {shortDate(f.train_start)} → {shortDate(f.train_end)}
                        </td>
                        <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground">
                          {shortDate(f.test_start)} → {shortDate(f.test_end)}
                        </td>
                        {pKeys.map((k) => (
                          <td key={k} className="px-3 py-1.5 tabular-nums">
                            {fmtNum(f[k], 2)}
                          </td>
                        ))}
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                          {fmtNum(f.train_sharpe)}
                        </td>
                        <td
                          className={cn(
                            "px-3 py-1.5 text-right font-medium tabular-nums",
                            f.test_sharpe >= 0
                              ? "text-emerald-600 dark:text-emerald-400"
                              : "text-destructive",
                          )}
                        >
                          {fmtNum(f.test_sharpe)}
                        </td>
                        <td
                          className={cn(
                            "px-3 py-1.5 text-right tabular-nums",
                            f.test_return_pct >= 0
                              ? "text-emerald-600 dark:text-emerald-400"
                              : "text-destructive",
                          )}
                        >
                          {fmtPct(f.test_return_pct, 1)}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {fmtNum(f.test_max_dd_pct, 1)}%
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {fmtNum(f.test_trades, 0)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Hint className="mt-3">
                A train Sharpe far above the test Sharpe is the signature of a
                parameter set that was fitted to its window. Spread across folds
                matters more than the average.
              </Hint>
            </div>
          </Card>
        </>
      )}
        </>
      )}
    </div>
  );
}
