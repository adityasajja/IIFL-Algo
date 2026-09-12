import { useEffect, useState } from "react";
import {
  getStrategies,
  runBacktest,
  type BacktestResponse,
  type StrategyInfo,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { EquityChart } from "./components/ui/equity-chart";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Callout, Stat, fmtNum, fmtPct } from "./components/ui/stat";
import { Switch } from "./components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";

type Source = "synthetic" | "history";

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 [&>option]:bg-card";

function fmt(v: number | string | boolean | null): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    return Math.abs(v) >= 1000 || Number.isInteger(v)
      ? v.toLocaleString("en-IN", { maximumFractionDigits: 2 })
      : v.toFixed(4);
  }
  return String(v);
}

export default function BacktestPanel() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategy, setStrategy] = useState("sma_crossover");
  const [source, setSource] = useState<Source>("synthetic");
  const [symbols, setSymbols] = useState("AAPL,MSFT");
  const [exchange, setExchange] = useState("NSEEQ");
  const [cash, setCash] = useState(1000000);
  const [fast, setFast] = useState(20);
  const [slow, setSlow] = useState(50);
  const [slippage, setSlippage] = useState(5);
  const [futures, setFutures] = useState(false);
  const [squareOff, setSquareOff] = useState(false);
  const [runState, setRunState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResponse | null>(null);

  useEffect(() => {
    getStrategies()
      .then((r) => setStrategies(r.strategies))
      .catch(() => setStrategies([]));
  }, []);

  function switchSource(next: Source) {
    setSource(next);
    setResult(null);
    setError(null);
    setSymbols(next === "history" ? "RELIANCE-EQ,INFY-EQ,TCS-EQ" : "AAPL,MSFT");
  }

  async function onRun() {
    setRunState("loading");
    setError(null);
    try {
      const res = await runBacktest({
        strategy,
        symbols: symbols
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
        initial_cash: cash,
        fast,
        slow,
        slippage_bps: slippage,
        square_off_eod: squareOff,
        futures,
        source,
        exchange: exchange.trim().toUpperCase(),
      });
      setResult(res);
      setRunState("success");
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
      setRunState("error");
    }
  }

  const m = result?.metrics;
  const tradeCols = result?.trades?.length
    ? Object.keys(result.trades[0]).filter((k) => k !== "symbol").slice(0, 7)
    : [];

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title="Strategy backtest"
          sub="One configuration, one dataset. In-sample by construction — use Research for the number you would act on."
        />
        <div className="space-y-3.5 p-5 pt-3">
          <Tabs
            value={source}
            onValueChange={(v) => switchSource(v as Source)}
            variant="segment"
          >
            <TabsList>
              <TabsTrigger value="synthetic">Synthetic</TabsTrigger>
              <TabsTrigger value="history">Real cached history</TabsTrigger>
            </TabsList>
          </Tabs>

          <Callout tone={source === "synthetic" ? "warn" : "info"}>
            {source === "synthetic" ? (
              <>
                Synthetic random walks. The harness is exercised correctly, but the
                numbers are <strong>not evidence about any market</strong>.
              </>
            ) : (
              <>
                Replays real NSE dailies from the local cache — the same data the scanner
                reads. Refresh it with <code className="font-mono">atr history sync</code>.
              </>
            )}
          </Callout>

          <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <label className="px-1 text-sm font-medium text-foreground">Strategy</label>
              <select
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
                className={selectClass}
              >
                {(strategies.length
                  ? strategies.map((s) => s.name)
                  : ["sma_crossover", "opening_range_breakout", "signals_entry"]
                ).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <Input
              label="Symbols (comma separated)"
              value={symbols}
              onChange={setSymbols}
              placeholder={source === "history" ? "RELIANCE-EQ,INFY-EQ" : "AAPL,MSFT"}
            />
            <Input label="Exchange" value={exchange} onChange={setExchange} />
            <Input
              label="Initial cash"
              type="number"
              value={String(cash)}
              onChange={(v) => setCash(Number(v))}
            />
            {strategy === "sma_crossover" && (
              <>
                <Input label="Fast" type="number" value={String(fast)} onChange={(v) => setFast(Number(v))} />
                <Input label="Slow" type="number" value={String(slow)} onChange={(v) => setSlow(Number(v))} />
              </>
            )}
            <Input
              label="Slippage (bps)"
              type="number"
              value={String(slippage)}
              onChange={(v) => setSlippage(Number(v))}
            />
            {source === "synthetic" && (
              <div className="flex items-end gap-6 pb-2.5">
                <Switch checked={futures} onCheckedChange={setFutures} label="Futures margin" />
              </div>
            )}
            <div className="flex items-end gap-6 pb-2.5">
              <Switch checked={squareOff} onCheckedChange={setSquareOff} label="Square off EOD" />
            </div>
          </div>

          <StatefulButton
            state={runState}
            onClick={() => void onRun()}
            loadingText="Running…"
            successText="Done"
            errorText="Failed — retry"
          >
            Run backtest
          </StatefulButton>

          {error && <ErrorBox>{error}</ErrorBox>}
        </div>
      </Card>

      {result && m && (
        <>
          <Card className="p-5">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Total return"
                value={fmtPct(m.total_return_pct)}
                tone={Number(m.total_return_pct) >= 0 ? "good" : "bad"}
                sub={`CAGR ${fmtPct(m.cagr_pct)}`}
              />
              <Stat label="Sharpe" value={fmtNum(m.sharpe)} sub={`Sortino ${fmtNum(m.sortino)}`} />
              <Stat
                label="Max drawdown"
                value={fmtPct(-Math.abs(Number(m.max_drawdown_pct)))}
                sub={`${fmtNum(m.max_drawdown_days, 1)} days underwater`}
              />
              <Stat
                label="Trades"
                value={fmtNum(m.num_trades, 0)}
                sub={`win rate ${fmtPct(m.win_rate_pct, 1)} · ${result.num_fills} fills`}
              />
            </div>

            {result.killed && (
              <div className="mt-3.5">
                <Callout tone="bad" title="Halted by the risk engine">
                  {result.kill_reason ?? "Daily loss limit reached."}
                </Callout>
              </div>
            )}

            <div className="mt-4">
              <EquityChart
                height={210}
                series={[
                  {
                    name: `${result.strategy} equity`,
                    points: result.equity,
                    color: "var(--primary)",
                  },
                ]}
              />
            </div>
          </Card>

          <Card>
            <CardHeader title="Full metrics" sub={`${result.strategy} · ${result.source} · ${result.symbols.join(", ")}`} />
            <div className="p-5 pt-3">
              <div className="overflow-hidden rounded-xl border border-border">
                <table className="w-full border-collapse text-[13px]">
                  <tbody>
                    {Object.entries(m).map(([k, v]) => (
                      <tr key={k} className="border-b border-border/60 last:border-0">
                        <td className="px-3 py-1.5 text-muted-foreground">{k}</td>
                        <td className="px-3 py-1.5 text-right font-medium tabular-nums">
                          {fmt(v)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </Card>

          {result.trades.length > 0 && (
            <Card>
              <CardHeader
                title="Trades"
                sub={`${result.trades.length} round trips — first few columns shown`}
              />
              <div className="p-5 pt-3">
                <div className="overflow-x-auto rounded-xl border border-border">
                  <table className="w-full border-collapse text-[13px]">
                    <thead>
                      <tr className="border-b border-border bg-muted/40 text-left">
                        {tradeCols.map((c) => (
                          <th key={c} className="whitespace-nowrap px-3 py-2 font-semibold">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.slice(0, 100).map((t, i) => (
                        <tr key={i} className="border-b border-border/60 last:border-0">
                          {tradeCols.map((c) => (
                            <td key={c} className="whitespace-nowrap px-3 py-1.5 tabular-nums">
                              {fmt(t[c] as number | string | null)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Hint className="mt-3">
                  Verified by diffing the equity curve and every fill against the previous
                  implementation — passing tests alone would not catch a silent change in
                  behaviour.
                </Hint>
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
