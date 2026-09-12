import { useEffect, useMemo, useState } from "react";
import {
  getStrategies,
  runResearch,
  type FoldRow,
  type ResearchResponse,
  type StrategyInfo,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
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

type Source = "cache" | "fetch" | "synthetic";

const SOURCE_HELP: Record<Source, string> = {
  cache:
    "Local parquet dailies — instant, no broker session. Holds about a year, which is short for anything needing a long indicator warmup.",
  fetch:
    "Pulls years of dailies from IIFL. Needs an active session and takes a few minutes, but it is the only source that gives a strategy enough room to warm up.",
  synthetic:
    "Generated random walks. Useful for smoke-testing the harness; the numbers mean nothing.",
};

const SOURCE_BLURB: Record<Source, string> = {
  cache: "Local cache (~1y)",
  fetch: "IIFL deep history",
  synthetic: "Synthetic",
};

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

export default function ResearchPanel() {
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
  const [state, setState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
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
    setError(null);
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
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }

  const oos = result?.oos;
  const bench = result?.benchmark;
  const beats = oos && bench ? oos.sharpe > bench.sharpe : null;

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title="Walk-forward validation"
          sub="Parameters are chosen on a training window, then scored once on the window that follows. Only the unseen part counts."
        />
        <div className="space-y-3.5 p-5">
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
              <label className="px-1 text-sm font-medium text-foreground">Strategy</label>
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
              label="Symbols (blank = default universe)"
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
              label="Search a parameter grid (raises the Sharpe hurdle — a bigger search makes the test stricter)"
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
            <StatefulButton
              state={state}
              onClick={() => void onRun()}
              loadingText={source === "fetch" ? "Fetching history…" : "Validating…"}
              successText="Done"
              errorText="Failed — retry"
            >
              Run validation
            </StatefulButton>
            {state === "loading" && (
              <Hint>
                {source === "fetch"
                  ? "Pulling daily history per symbol — this is the slow path."
                  : "Walking forward through the folds…"}
              </Hint>
            )}
          </div>

          {error && <ErrorBox>{error}</ErrorBox>}
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
              </div>
              <Badge tone={result.source === "synthetic" ? "warn" : "flat"}>
                {result.source === "synthetic" ? "synthetic data" : `${result.source} data`}
              </Badge>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Out-of-sample return"
                value={fmtPct(oos.total_return_pct)}
                tone={oos.total_return_pct >= 0 ? "good" : "bad"}
                sub={`buy & hold ${fmtPct(bench.total_return_pct)}`}
                hint="The concatenation of the unseen windows only."
              />
              <Stat
                label="OOS Sharpe"
                value={fmtNum(oos.sharpe)}
                tone={beats ? "good" : "bad"}
                sub={`buy & hold ${fmtNum(bench.sharpe)}`}
                hint="Annualised from the out-of-sample equity curve."
              />
              <Stat
                label="Deflated Sharpe"
                value={fmtNum(result.deflated_sharpe, 3)}
                tone={result.deflated_sharpe >= 0.95 ? "good" : "bad"}
                sub={`hurdle ${fmtNum(result.required_sharpe, 3)} for ${result.n_trials} trials`}
                hint="Probability the Sharpe survives the number of things that were tried. Needs to clear the hurdle."
              />
              <Stat
                label="OOS max drawdown"
                value={fmtPct(-Math.abs(oos.max_drawdown_pct))}
                sub={`${oos.num_trades} trades · win rate ${fmtPct(oos.win_rate_pct, 1)}`}
              />
            </div>

            {result.warnings.length > 0 && (
              <div className="mt-3.5 space-y-2">
                {result.warnings.map((w) => (
                  <Callout key={w} tone="warn" title="Read this before believing the numbers">
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
              sub="Every check must pass. A failure here is the harness doing its job."
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
    </div>
  );
}
