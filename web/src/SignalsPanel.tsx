import { useEffect, useState } from "react";
import {
  getSignalsConfig,
  runSignalsScan,
  saveSignalsConfig,
  type SignalRow,
  type SignalScanResponse,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, Callout, fmtNum } from "./components/ui/stat";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

const EXIT_KEYS = [
  "stop_loss_pct",
  "take_profit_pct",
  "trailing_stop_pct",
  "trend_sma",
  "trend_confirm_bars",
  "rsi_overbought",
  "min_history_bars",
];

const ENTRY_KEYS = [
  "trend_fast_sma",
  "trend_slow_sma",
  "pullback_rsi_low",
  "pullback_rsi_high",
  "breakout_lookback",
  "breakout_proximity_pct",
  "volume_multiple",
  "volume_lookback",
  "oversold_rsi",
  "long_sma",
  "min_history_bars",
];

/** Blank means "disabled" for the two optional thresholds. */
function toField(v: number | null | undefined): string {
  return v === null || v === undefined ? "" : String(v);
}

function fromField(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function SignalTable({
  rows,
  tone,
  emptyText,
}: {
  rows: SignalRow[];
  tone: "buy" | "sell";
  emptyText: string;
}) {
  if (rows.length === 0) return <Hint>{emptyText}</Hint>;
  const detailKeys = [
    ...new Set(rows.flatMap((r) => Object.keys(r.detail ?? {}))),
  ].slice(0, 4);

  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            <th className="px-3 py-2 font-semibold">Symbol</th>
            <th className="px-3 py-2 text-right font-semibold">Price</th>
            <th className="px-3 py-2 font-semibold">Rule</th>
            {detailKeys.map((k) => (
              <th key={k} className="px-3 py-2 text-right font-semibold">
                {k.replace(/_/g, " ")}
              </th>
            ))}
            <th className="px-3 py-2 font-semibold">Why</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={`${r.symbol}-${r.rule}`}
              className="border-b border-border/60 transition-colors last:border-0 hover:bg-primary/[0.03]"
            >
              <td className="px-3 py-1.5 font-semibold">{r.symbol.replace("-EQ", "")}</td>
              <td className="px-3 py-1.5 text-right tabular-nums">
                {r.price.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
              </td>
              <td className="px-3 py-1.5">
                <Badge tone={tone === "sell" ? "bad" : "warn"}>{r.rule}</Badge>
              </td>
              {detailKeys.map((k) => (
                <td key={k} className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                  {fmtNum(r.detail?.[k], 1)}
                </td>
              ))}
              <td className="px-3 py-1.5 text-muted-foreground">{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function SignalsPanel() {
  const { toast } = useToast();
  const [result, setResult] = useState<SignalScanResponse | null>(null);
  const [scanState, setScanState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [symbols, setSymbols] = useState("");
  const [withHoldings, setWithHoldings] = useState(true);
  const [withEntries, setWithEntries] = useState(true);

  const [exits, setExits] = useState<Record<string, string>>({});
  const [entries, setEntries] = useState<Record<string, string>>({});
  const [exchange, setExchange] = useState("NSEEQ");
  const [universe, setUniverse] = useState("");
  const [saveState, setSaveState] = useState<ButtonState>("idle");
  const [configError, setConfigError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function loadConfig() {
    try {
      const res = await getSignalsConfig();
      setExits(
        Object.fromEntries(EXIT_KEYS.map((k) => [k, toField(res.config.exits[k] as number | null)])),
      );
      setEntries(
        Object.fromEntries(
          ENTRY_KEYS.map((k) => [k, toField(res.config.entries[k] as number | null)]),
        ),
      );
      setExchange(res.config.exchange);
      setUniverse(res.config.universe.join(", "));
      setLoaded(true);
    } catch (e) {
      setConfigError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void loadConfig();
  }, []);

  async function onScan() {
    setScanState("loading");
    setError(null);
    try {
      const res = await runSignalsScan({
        symbols: symbols
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
        include_holdings: withHoldings,
        include_entries: withEntries,
      });
      setResult(res);
      setScanState("success");
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : String(e));
      setScanState("error");
    }
  }

  async function onSave() {
    setSaveState("loading");
    setConfigError(null);
    try {
      await saveSignalsConfig({
        exits: Object.fromEntries(Object.entries(exits).map(([k, v]) => [k, fromField(v)])),
        entries: Object.fromEntries(Object.entries(entries).map(([k, v]) => [k, fromField(v)])),
        exchange: exchange.trim().toUpperCase() || "NSEEQ",
        universe: universe
          .split(",")
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
      });
      setSaveState("success");
      toast({
        title: "Thresholds saved",
        description: "These are starting points, not findings — validate before acting.",
        status: "success",
      });
    } catch (e) {
      setSaveState("error");
      setConfigError(e instanceof Error ? e.message : String(e));
    }
  }

  const buys = result?.buys ?? [];
  const sells = result?.sells ?? [];

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title="Live signals"
          sub="Sell rules run over the book you actually hold; buy rules run over a watchlist."
          action={
            <StatefulButton
              state={scanState}
              variant="secondary"
              onClick={() => void onScan()}
              loadingText="Scanning…"
              successText="Scanned"
              errorText="Failed — retry"
            >
              Run scan
            </StatefulButton>
          }
        />
        <div className="space-y-3.5 p-5 pt-3">
          <Callout tone="info" title="What each side is">
            <strong>Sell rules are risk management</strong> — stop loss, trailing stop,
            trend break, take profit. They describe a fact about the book and need no
            edge. <strong>Buy rules are predictions</strong>, and these have not passed
            out-of-sample validation, so they come back as a watchlist.
          </Callout>

          <div className="grid gap-3.5 sm:grid-cols-2">
            <Input
              label="Watchlist override (blank = configured universe)"
              value={symbols}
              onChange={setSymbols}
              placeholder="RELIANCE-EQ,INFY-EQ"
            />
            <div className="flex items-end gap-6 pb-2.5">
              <Switch
                checked={withHoldings}
                onCheckedChange={setWithHoldings}
                label="Exits on holdings"
              />
              <Switch
                checked={withEntries}
                onCheckedChange={setWithEntries}
                label="Entries on watchlist"
              />
            </div>
          </div>

          {scanState === "loading" && (
            <Hint>
              One quote and one daily history per symbol — the watchlist size drives how
              long this takes.
            </Hint>
          )}
          {error && <ErrorBox>{error}</ErrorBox>}
          {result && result.errors.length > 0 && (
            <Hint>Skipped: {result.errors.slice(0, 8).join(" · ")}</Hint>
          )}
        </div>
      </Card>

      {result && (
        <>
          <Card>
            <CardHeader
              title="Sell / risk exits"
              sub="Risk management on positions you hold — these do not need to beat the market to be worth obeying."
              action={<Badge tone={sells.length ? "bad" : "flat"}>{sells.length} fired</Badge>}
            />
            <div className="p-5 pt-3">
              <SignalTable
                rows={sells}
                tone="sell"
                emptyText="No exit rules fired. That is a normal outcome, not a bug."
              />
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Buy candidates"
              sub="Unvalidated by construction — the entry rules have not cleared walk-forward."
              action={<Badge tone={buys.length ? "warn" : "flat"}>{buys.length} flagged</Badge>}
            />
            <div className="space-y-3 p-5 pt-3">
              {buys.length > 0 && (
                <Callout tone="warn" title="Not a reason to buy">
                  These entry rules have <strong>not</strong> passed out-of-sample
                  validation. Treat the list as a watchlist. Open the Research tab and run
                  the same strategy to see whether there is anything behind it.
                </Callout>
              )}
              <SignalTable
                rows={buys}
                tone="buy"
                emptyText="No entry rules fired."
              />
            </div>
          </Card>
        </>
      )}

      <Card>
        <CardHeader
          title="Thresholds"
          sub="Where the rules get their numbers. Editable, and saved to data/signals/config.json."
          action={
            <StatefulButton
              state={saveState}
              variant="secondary"
              onClick={() => void onSave()}
              loadingText="Saving…"
              successText="Saved"
              errorText="Failed — retry"
            >
              Save
            </StatefulButton>
          }
        />
        <div className="space-y-3.5 p-5 pt-3">
          {configError && <ErrorBox>{configError}</ErrorBox>}
          {!loaded && !configError && <Hint>Loading thresholds…</Hint>}
          {loaded && (
            <>
              <div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">
                  Exits — risk management
                </div>
                <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
                  {EXIT_KEYS.map((k) => (
                    <Input
                      key={k}
                      label={k.replace(/_/g, " ")}
                      value={exits[k] ?? ""}
                      onChange={(v) => setExits((s) => ({ ...s, [k]: v }))}
                      placeholder={
                        k === "take_profit_pct" || k === "rsi_overbought" ? "disabled" : ""
                      }
                    />
                  ))}
                </div>
              </div>

              <div className="border-t border-border pt-3.5">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">
                  Entries — unvalidated hypotheses
                </div>
                <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
                  {ENTRY_KEYS.map((k) => (
                    <Input
                      key={k}
                      label={k.replace(/_/g, " ")}
                      value={entries[k] ?? ""}
                      onChange={(v) => setEntries((s) => ({ ...s, [k]: v }))}
                    />
                  ))}
                </div>
              </div>

              <div className="grid gap-3.5 border-t border-border pt-3.5 sm:grid-cols-2">
                <Input label="Exchange" value={exchange} onChange={setExchange} />
                <Input
                  label="Watchlist universe (comma separated)"
                  value={universe}
                  onChange={setUniverse}
                  placeholder="blank = scanner default"
                />
              </div>

              <Hint className={cn("leading-relaxed")}>
                <code className="font-mono">min_history_bars</code> must stay comfortably
                below the walk-forward test window. Set it too high and the strategy cannot
                trade inside a fold, at which point &ldquo;no trades&rdquo; looks exactly
                like &ldquo;no edge&rdquo;.
              </Hint>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
