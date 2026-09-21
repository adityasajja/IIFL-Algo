/**
 * Paper deployment and monitoring.
 *
 * The screen for the loop the whole system exists to close:
 *
 *   Strategy → Version → Deploy Paper → Live market data → Signal → Risk →
 *   OMS → Fill → Position → P&L
 *
 * Three things about this panel are deliberate and worth reading before
 * changing it.
 *
 * **The version is pinned, not chosen per run.** A deployment names an
 * *immutable* strategy version. That is what makes "why did this trade?" a
 * question with an answer a week later: the rules that produced it cannot have
 * changed, because a new version is a new row and this deployment still points
 * at the old one. The version dropdown therefore offers only saved strategies —
 * a built-in engine has no version history, and deploying one would put paper
 * orders on a strategy that cannot be reproduced.
 *
 * **"Running" and "trading" are different questions.** A deployment can be
 * `RUNNING` and place no orders — its rules failed to resolve, the cash session
 * is shut, or the runner has not attached a loop. The backend returns all three
 * answers (`trading`, `not_trading_because`, `blocked_reason`) precisely so this
 * screen can show *why* rather than a reassuring green dot. So the header never
 * shows one without the other.
 *
 * **Both P&L figures are shown, and a null one says so.** Today's P&L is the
 * change in equity since the IST session boundary. On the first day there is no
 * earlier equity to subtract, so the backend returns `null` — which is
 * "not measured yet", not "flat". The two must never print the same.
 */

import { PaperRuns } from "./PaperRuns";
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  CircleSlash,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  Square,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  backtestOptions,
  compareChampionChallenger,
  createDeployment,
  getForwardEvidenceCounts,
  getRunnerStatus,
  launchChallenger,
  listDeployments,
  monitorOverview,
  pauseDeployment,
  resetDeployment,
  startDeployment,
  stopDeployment,
  type BacktestOptions,
  type ChampionComparison,
  type Deployment,
  type ForwardEvidenceCounts,
  type MonitorOverview,
  type RunnerStatus,
  type StrategyOption,
  type TimelineStage,
  previewPositionSize,
  type SizingPreviewResult,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Stat, Badge, Callout, fmtMoney } from "./components/ui/stat";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Input } from "./components/ui/input";
import { Select } from "./components/ui/select";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";
import {
  STAGES,
  STAGE_BLURB,
  STAGE_LABEL,
  comparisonVerdictBlurb,
  comparisonVerdictTone,
  controlGates,
  deltaSample,
  deployReadiness,
  fmtDelta,
  fmtDiffValue,
  fmtMoneyOrDash,
  fmtPctOrDash,
  idleReason,
  istClock,
  istDate,
  newestVersion,
  openPositions,
  outcomeTone,
  parseSymbols,
  toChain,
  unpricedOf,
} from "./lib/paper-monitor";
import { setVisibleInterval } from "./lib/visibleInterval";

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 disabled:cursor-not-allowed disabled:opacity-50 [&>option]:bg-card";

/** How often the monitor re-reads while the tab is open. One second is far
 *  finer than the daily rules can distinguish, and the overview is one request. */
const POLL_MS = 4000;

type View = "deploy" | "monitor" | "compare";

export default function PaperDeploymentPanel({ onOpenStrategies }: { onOpenStrategies?: () => void }) {
  const { toast } = useToast();

  const [view, setView] = useState<View>("deploy");
  const [options, setOptions] = useState<BacktestOptions | null>(null);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [overview, setOverview] = useState<MonitorOverview | null>(null);
  const [evidenceCounts, setEvidenceCounts] = useState<ForwardEvidenceCounts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<ButtonState>("idle");
  const [actionBusy, setActionBusy] = useState<string | null>(null);

  // ── the deploy form ────────────────────────────────────────────────────────
  const [strategyId, setStrategyId] = useState("");
  const [version, setVersion] = useState("");
  const [capital, setCapital] = useState("500000");
  const [exchange, setExchange] = useState("NSEEQ");
  const [universe, setUniverse] = useState("");
  const [symbols, setSymbols] = useState("");
  const [timeframe, setTimeframe] = useState("1d");
  const [stopLoss, setStopLoss] = useState("");
  const [maxPositions, setMaxPositions] = useState("10");

  // ── the reason prompt, shared by pause/stop/reset ──────────────────────────
  type ReasonedKind = "pause" | "stop" | "reset";
  const [pendingAction, setPendingAction] = useState<
    { kind: ReasonedKind; deploymentId: string } | null
  >(null);
  const [reason, setReason] = useState("");

  // ── champion vs challenger ─────────────────────────────────────────────────
  const [cmpStrategyId, setCmpStrategyId] = useState("");
  const [cmpChampion, setCmpChampion] = useState("");
  const [cmpChallenger, setCmpChallenger] = useState("");
  const [comparison, setComparison] = useState<ChampionComparison | null>(null);
  const [cmpError, setCmpError] = useState<string | null>(null);
  const [cmpBusy, setCmpBusy] = useState(false);
  const [launchDepId, setLaunchDepId] = useState("");
  const [launchVersion, setLaunchVersion] = useState("");
  const [launchBusy, setLaunchBusy] = useState(false);

  const loadList = useCallback(async () => {
    try {
      const [list, opts, run, evCounts] = await Promise.all([
        listDeployments(),
        backtestOptions().catch(() => null),
        getRunnerStatus().catch(() => null),
        getForwardEvidenceCounts().catch(() => null),
      ]);
      setDeployments(list.deployments);
      if (opts) setOptions(opts);
      setRunner(run);
      if (evCounts) setEvidenceCounts(evCounts);
      setError(null);
      setSelectedId((prev) => prev ?? list.deployments[0]?.deployment_id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  // The deployment whose overview is wanted right now. A response for any other
  // id is stale (the user switched while it was in flight) and must be dropped,
  // or the monitor shows one deployment's P&L under another's name.
  const wantedOverviewId = useRef<string | null>(null);
  const loadOverview = useCallback(async (id: string) => {
    wantedOverviewId.current = id;
    try {
      const next = await monitorOverview(id);
      if (wantedOverviewId.current !== id) return;
      setOverview(next);
      setError(null);
    } catch (e) {
      if (wantedOverviewId.current !== id) return;
      setError(e instanceof Error ? e.message : String(e));
      setOverview(null);
    }
  }, []);

  useEffect(() => {
    if (!selectedId) {
      wantedOverviewId.current = null;
      setOverview(null);
      return;
    }
    setOverview(null);
    void loadOverview(selectedId);
    const t = setVisibleInterval(() => {
      void loadOverview(selectedId);
      // The deployments list (status labels, the Start/Pause/Stop gates) was
      // only ever refreshed right after an action, one time — if that single
      // refresh failed (rate limiting, a momentary DB lock), it stayed stale
      // indefinitely with no way to self-correct short of a full reload.
      void loadList();
      void getRunnerStatus()
        .then(setRunner)
        .catch(() => undefined);
      void getForwardEvidenceCounts()
        .then(setEvidenceCounts)
        .catch(() => undefined);
    }, POLL_MS);
    return () => clearInterval(t);
  }, [selectedId, loadOverview, loadList]);

  // Only saved strategies list a version, and a deployment must pin one.
  const deployable = useMemo(
    () => (options?.strategies ?? []).filter((s) => s.kind === "saved"),
    [options],
  );
  const chosen = useMemo<StrategyOption | undefined>(
    () => deployable.find((s) => s.strategy_id === strategyId),
    [deployable, strategyId],
  );

  // Preselect the newest version when the strategy changes: the newest is
  // almost always what somebody means, and leaving it blank would make the form
  // unsubmittable for a reason that has nothing to do with their intent.
  // `newestVersion` compares the numbers rather than indexing the list — see its
  // docstring for why the ordering of the response is not a safe assumption.
  useEffect(() => {
    if (!chosen) return;
    setVersion(newestVersion(chosen.versions));
  }, [chosen]);

  const readiness = deployReadiness({ strategyId, version, capital, symbols, universe });

  // Running PAPER arms per strategy, for the compare defaults: the oldest
  // running version reads as champion, the newest as challenger — the same
  // inference the backend applies when versions are left blank.
  const cmpRunningByStrategy = useMemo(() => {
    const map = new Map<string, number[]>();
    for (const d of deployments) {
      if (d.mode !== "PAPER" || d.status !== "RUNNING") continue;
      const list = map.get(d.strategy_id) ?? [];
      list.push(d.strategy_version);
      map.set(d.strategy_id, list);
    }
    return map;
  }, [deployments]);

  // A new strategy clears the version pins; the defaults below refill them.
  useEffect(() => {
    setCmpChampion("");
    setCmpChallenger("");
    setComparison(null);
  }, [cmpStrategyId]);

  useEffect(() => {
    const keys = [...cmpRunningByStrategy.keys()];
    const sid = cmpStrategyId || keys[0] || "";
    if (!sid) return;
    if (!cmpStrategyId) setCmpStrategyId(sid);
    const versions = [...(cmpRunningByStrategy.get(sid) ?? [])].sort((a, b) => a - b);
    if (versions.length >= 2) {
      setCmpChampion((prev) => prev || String(versions[0]));
      setCmpChallenger((prev) => prev || String(versions[versions.length - 1]));
    }
  }, [cmpRunningByStrategy, cmpStrategyId]);

  async function runCompare() {
    if (!cmpStrategyId) {
      setCmpError("Pick a strategy to compare.");
      return;
    }
    setCmpBusy(true);
    setCmpError(null);
    try {
      setComparison(
        await compareChampionChallenger({
          strategyId: cmpStrategyId,
          championVersion: cmpChampion ? Number(cmpChampion) : undefined,
          challengerVersion: cmpChallenger ? Number(cmpChallenger) : undefined,
        }),
      );
    } catch (e) {
      setComparison(null);
      setCmpError(e instanceof Error ? e.message : String(e));
    } finally {
      setCmpBusy(false);
    }
  }

  async function launch() {
    if (!launchDepId || !launchVersion) {
      toast({ title: "Pick a champion deployment and a version", status: "error" });
      return;
    }
    const champion = deployments.find((d) => d.deployment_id === launchDepId);
    if (!champion) return;
    setLaunchBusy(true);
    try {
      const created = await launchChallenger({
        strategy_id: champion.strategy_id,
        champion_deployment_id: launchDepId,
        challenger_version: Number(launchVersion),
      });
      // The write above already succeeded — everything past this point is
      // just refreshing local view state. `loadList` can be slow (the
      // backend recomputes evidence synchronously) or fail outright under
      // load; either must not report the launch itself as failed, or leave
      // the button stuck on "Launching…" waiting on a call that isn't part
      // of the actual action.
      toast({
        title: "Challenger launched",
        description: `v${created.strategy_version} copies the champion's capital, universe and config. Start it to begin the comparison.`,
        status: "neutral",
      });
      setCmpStrategyId(champion.strategy_id);
      setCmpChallenger(String(created.strategy_version));
      setView("compare");
      void loadList();
    } catch (e) {
      toast({
        title: "Challenger refused",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setLaunchBusy(false);
    }
  }

  async function deploy() {
    if (!readiness.ready) return;
    setBusy("loading");
    try {
      const symbols_ = parseSymbols(symbols);
      const created = await createDeployment({
        strategy_id: strategyId,
        strategy_version: Number(version),
        capital: Number(capital),
        mode: "PAPER",
        config: {
          // The runner reads the universe from `config.symbols` and nothing
          // else. A universe *name* is resolved here, client-side, because the
          // runner has no way to resolve one — a deployment whose config named a
          // universe the loop cannot read would be RUNNING and evaluating an
          // empty symbol list forever.
          symbols: symbols_,
          exchange,
          timeframe,
          max_open_positions: Number(maxPositions) || 10,
          ...(stopLoss.trim() ? { stop_loss_pct: Number(stopLoss) } : {}),
        },
      });
      // Start it immediately: creating a deployment that sits in PENDING is a
      // state nobody asked for, and the user's next click would always be this.
      await startDeployment(created.deployment_id);
      // Both writes above already succeeded — report success and move on
      // without waiting on the list refresh, which can be slow (the backend
      // recomputes evidence synchronously) and must not leave the button
      // stuck on "Deploying…" for something that already happened.
      setBusy("success");
      toast({
        title: "Deployed to paper",
        description: `v${created.strategy_version} on ${symbols_.length} symbol(s), ${fmtMoney(Number(capital))}.`,
        status: "neutral",
      });
      setSelectedId(created.deployment_id);
      setView("monitor");
      void loadList();
    } catch (e) {
      setBusy("error");
      toast({
        title: "Deployment refused",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setTimeout(() => setBusy("idle"), 900);
    }
  }

  async function runAction(kind: "start" | "pause" | "stop" | "reset", id: string, why?: string) {
    setActionBusy(`${kind}:${id}`);
    try {
      let freshId: string | null = null;
      if (kind === "start") await startDeployment(id);
      else if (kind === "pause") await pauseDeployment(id, why ?? "");
      else if (kind === "stop") await stopDeployment(id, why ?? "");
      else {
        const fresh = await resetDeployment(id, why ?? "");
        freshId = fresh.deployment_id;
        toast({
          title: "Reset created a new deployment",
          description: `The previous run stays readable; this one is ${fresh.deployment_id.slice(0, 8)}.`,
          status: "neutral",
        });
      }
      // The write above already succeeded — dismiss the confirmation and
      // report success regardless of whether the refresh below lands. A
      // refresh failing here (rate limiting, a momentary DB lock) must not
      // masquerade as the action itself having failed, and must not leave
      // the confirmation form stuck open for something that already happened.
      setPendingAction(null);
      setReason("");
      if (freshId) setSelectedId(freshId);
      if (kind !== "reset") toast({ title: `Deployment ${kind}`, status: "neutral" });
      try {
        await loadList();
        if (selectedId) await loadOverview(selectedId);
      } catch {
        // Best-effort refresh; the next poll cycle will catch up.
      }
    } catch (e) {
      toast({
        title: `Could not ${kind}`,
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setActionBusy(null);
    }
  }

  const status = overview?.status ?? null;
  // Pausing/stopping writes the deployment row immediately, but the monitor
  // snapshot (`status.status`) reflects the runner's own in-memory state,
  // which can lag a cycle behind. Gating Start/Pause/Stop off that stale
  // value locks up the controls right after the action that should have
  // freed them — prefer the deployment list's status, just re-fetched from
  // the same write, and only fall back to the monitor snapshot if the
  // deployment isn't in that list yet (e.g. immediately after deploying).
  const listedStatus = deployments.find((d) => d.deployment_id === selectedId)?.status;
  const gates = controlGates(listedStatus ?? status?.status);
  const idle = idleReason(status);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex overflow-hidden rounded-lg border border-border/60 text-sm font-semibold">
          {(["deploy", "monitor", "compare"] as View[]).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => setView(v)}
              className={cn(
                "px-4 py-1.5 transition-colors",
                view === v
                  ? "bg-primary text-primary-foreground"
                  : "bg-background text-muted-foreground hover:text-foreground",
              )}
            >
              {v === "deploy" ? "New deployment" : v === "monitor" ? "Monitor" : "Compare"}
            </button>
          ))}
        </div>

        {/* The runner is a platform process, not a user's resource, so its state
            is shown on every view: "no signals fired" and "nothing is watching
            for signals" look identical from the timeline alone. */}
        <div
          className="flex items-center gap-2 text-xs text-muted-foreground"
          title={runner?.running ? "The paper runner is watching for signals." : "The paper runner is not running, so nothing will trade."}
        >
          <span className={cn("h-2 w-2 rounded-full", runner?.running ? "bg-emerald-500" : "bg-muted-foreground/40")} />
          {runner?.running ? "Runner on" : "Runner off"}
          <span className="text-border">·</span>
          {runner?.in_market_hours ? "Market open" : "Market closed"}
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      {view === "compare" ? (
        <CompareView
          strategies={[...cmpRunningByStrategy.keys()]}
          deployments={deployments}
          cmpStrategyId={cmpStrategyId}
          cmpChampion={cmpChampion}
          cmpChallenger={cmpChallenger}
          comparison={comparison}
          cmpError={cmpError}
          cmpBusy={cmpBusy}
          launchDepId={launchDepId}
          launchVersion={launchVersion}
          launchBusy={launchBusy}
          onStrategy={setCmpStrategyId}
          onChampion={setCmpChampion}
          onChallenger={setCmpChallenger}
          onCompare={() => void runCompare()}
          onLaunchDep={setLaunchDepId}
          onLaunchVersion={setLaunchVersion}
          onLaunch={() => void launch()}
        />
      ) : view === "deploy" ? (
        <DeployForm
          onOpenStrategies={onOpenStrategies}
          options={options}
          deployable={deployable}
          chosen={chosen}
          strategyId={strategyId}
          version={version}
          capital={capital}
          exchange={exchange}
          universe={universe}
          symbols={symbols}
          timeframe={timeframe}
          stopLoss={stopLoss}
          maxPositions={maxPositions}
          readiness={readiness}
          busy={busy}
          onStrategy={(id) => setStrategyId(id)}
          onVersion={setVersion}
          onCapital={setCapital}
          onExchange={setExchange}
          onUniverse={setUniverse}
          onSymbols={setSymbols}
          onTimeframe={setTimeframe}
          onStopLoss={setStopLoss}
          onMaxPositions={setMaxPositions}
          onDeploy={() => void deploy()}
          onOpen={(id) => {
            setSelectedId(id);
            setView("monitor");
          }}
          deployments={deployments}
        />
      ) : (
        <MonitorView
          deployments={deployments}
          selectedId={selectedId}
          overview={overview}
          evidenceCounts={evidenceCounts}
          gates={gates}
          idle={idle}
          actionBusy={actionBusy}
          pendingAction={pendingAction}
          reason={reason}
          onSelect={setSelectedId}
          onReason={setReason}
          onAsk={(kind, id) => {
            setPendingAction({ kind, deploymentId: id });
            setReason("");
          }}
          onCancelAsk={() => {
            setPendingAction(null);
            setReason("");
          }}
          onStart={(id) => void runAction("start", id)}
          onConfirm={(kind, id, why) => void runAction(kind, id, why)}
        />
      )}
    </div>
  );
}

// ─── Position Sizing & Risk Budgeting Preview Section ────────────────────────

function PositionSizingSection({
  capital,
  stopLoss,
}: {
  capital: string;
  stopLoss: string;
}) {
  const [method, setMethod] = useState("RISK_PER_TRADE");
  const [entryPrice, setEntryPrice] = useState("1000");
  const [stopPriceInput, setStopPriceInput] = useState("950");
  const [riskPct, setRiskPct] = useState("1.0");
  const [atrInput, setAtrInput] = useState("20");
  const [atrMult, setAtrMult] = useState("2.0");
  const [fixedQtyInput, setFixedQtyInput] = useState("100");
  const [fixedValInput, setFixedValInput] = useState("100000");
  const [capFractionInput, setCapFractionInput] = useState("10");
  const [maxStockExpInput, setMaxStockExpInput] = useState("");
  const [preview, setPreview] = useState<SizingPreviewResult | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchPreview = useCallback(async () => {
    const ep = parseFloat(entryPrice) || 0;
    const cap = parseFloat(capital) || 500000;
    if (ep <= 0 || cap <= 0) return;

    setLoading(true);
    try {
      const res = await previewPositionSize({
        entry_price: ep,
        capital: cap,
        stop_price: parseFloat(stopPriceInput) || undefined,
        stop_loss_pct: parseFloat(stopLoss) || undefined,
        atr: parseFloat(atrInput) || undefined,
        max_stock_exposure: parseFloat(maxStockExpInput) || undefined,
        sizing: {
          method,
          risk_per_trade_pct: parseFloat(riskPct) || 1.0,
          atr_multiplier: parseFloat(atrMult) || 2.0,
          fixed_quantity: parseFloat(fixedQtyInput) || 100,
          fixed_rupee_value: parseFloat(fixedValInput) || 100000,
          capital_fraction: (parseFloat(capFractionInput) || 10) / 100.0,
        },
      });
      setPreview(res);
    } catch {
      // preview is advisory
    } finally {
      setLoading(false);
    }
  }, [
    entryPrice,
    capital,
    stopPriceInput,
    stopLoss,
    atrInput,
    maxStockExpInput,
    method,
    riskPct,
    atrMult,
    fixedQtyInput,
    fixedValInput,
    capFractionInput,
  ]);

  useEffect(() => {
    void fetchPreview();
  }, [fetchPreview]);

  return (
    <Section
      n={5}
      title="Position size"
      note="Deterministic position sizing formula and multi-tier limit capping."
    >
      <div className="space-y-4">
        <div className="grid gap-3.5 sm:grid-cols-3">
          <Field label="Sizing Method">
            <Select
              value={method}
              onChange={setMethod}
              options={[
                { value: "RISK_PER_TRADE", label: "Risk per trade (stop-based)" },
                { value: "ATR_VOLATILITY_SIZING", label: "ATR volatility sizing" },
                { value: "PERCENT_OF_CAPITAL", label: "Percent of capital" },
                { value: "PERCENT_OF_AVAILABLE_CAPITAL", label: "Percent of available capital" },
                { value: "FIXED_RUPEE_VALUE", label: "Fixed rupee value" },
                { value: "FIXED_QUANTITY", label: "Fixed quantity" },
              ]}
            />
          </Field>

          <Input
            label="Hypothetical Entry (₹)"
            value={entryPrice}
            onChange={setEntryPrice}
            inputMode="decimal"
          />

          {method === "RISK_PER_TRADE" && (
            <Input
              label="Stop Price (₹)"
              value={stopPriceInput}
              onChange={setStopPriceInput}
              inputMode="decimal"
              placeholder="e.g. 950"
            />
          )}

          {method === "ATR_VOLATILITY_SIZING" && (
            <>
              <Input
                label="ATR (₹)"
                value={atrInput}
                onChange={setAtrInput}
                inputMode="decimal"
                placeholder="20"
              />
              <Input
                label="ATR Multiplier"
                value={atrMult}
                onChange={setAtrMult}
                inputMode="decimal"
                placeholder="2.0"
              />
            </>
          )}

          {(method === "RISK_PER_TRADE" || method === "ATR_VOLATILITY_SIZING") && (
            <Input
              label="Risk per Trade (%)"
              value={riskPct}
              onChange={setRiskPct}
              inputMode="decimal"
              placeholder="1.0"
            />
          )}

          {method === "FIXED_QUANTITY" && (
            <Input
              label="Quantity"
              value={fixedQtyInput}
              onChange={setFixedQtyInput}
              inputMode="numeric"
            />
          )}

          {method === "FIXED_RUPEE_VALUE" && (
            <Input
              label="Rupee Value (₹)"
              value={fixedValInput}
              onChange={setFixedValInput}
              inputMode="numeric"
            />
          )}

          {(method === "PERCENT_OF_CAPITAL" || method === "PERCENT_OF_AVAILABLE_CAPITAL") && (
            <Input
              label="Capital % per Trade"
              value={capFractionInput}
              onChange={setCapFractionInput}
              inputMode="decimal"
              placeholder="10"
            />
          )}

          <Input
            label="Stock Exposure Cap (₹, optional)"
            value={maxStockExpInput}
            onChange={setMaxStockExpInput}
            inputMode="numeric"
            placeholder="e.g. 150000"
          />
        </div>

        {/* Live Preview Card */}
        {preview && (
          <div className="rounded-2xl border border-border/70 bg-primary/[0.02] p-4 text-sm">
            <div className="flex items-center justify-between border-b border-border/50 pb-2">
              <span className="font-semibold text-foreground">Sizing Preview & Budget Impact</span>
              {loading && <span className="text-xs text-muted-foreground">Calculating...</span>}
              {preview.capped_by && (
                <Badge tone="warn">
                  Capped by: {preview.capped_by}
                </Badge>
              )}
            </div>

            <div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
              <div className="space-y-1">
                <span className="text-muted-foreground">Entry Price</span>
                <p className="font-mono text-sm font-semibold">{fmtMoney(preview.entry_price)}</p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Stop Price</span>
                <p className="font-mono text-sm font-semibold">
                  {preview.stop_price ? fmtMoney(preview.stop_price) : "—"}
                </p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Risk / Share</span>
                <p className="font-mono text-sm font-semibold">
                  {preview.risk_per_share ? fmtMoney(preview.risk_per_share) : "—"}
                </p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Max Allowed Loss</span>
                <p className="font-mono text-sm font-semibold">
                  {preview.risk_amount ? fmtMoney(preview.risk_amount) : "—"}
                </p>
              </div>

              <div className="space-y-1">
                <span className="text-muted-foreground">Calculated Qty</span>
                <p className="font-mono text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                  {preview.final_quantity} shares
                  {preview.raw_quantity !== preview.final_quantity && (
                    <span className="ml-1 text-[11px] text-muted-foreground line-through">
                      ({preview.raw_quantity})
                    </span>
                  )}
                </p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Position Value</span>
                <p className="font-mono text-sm font-semibold">{fmtMoney(preview.position_value)}</p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Portfolio Impact</span>
                <p className="font-mono text-sm font-semibold">{preview.portfolio_impact_pct}%</p>
              </div>
              <div className="space-y-1">
                <span className="text-muted-foreground">Remaining Capital</span>
                <p className="font-mono text-sm font-semibold">{fmtMoney(preview.remaining_capital)}</p>
              </div>
            </div>

            {preview.rejection_reason && (
              <div className="mt-2.5 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-600 dark:text-red-400">
                {preview.rejection_reason}
              </div>
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

// ─── the deploy form ──────────────────────────────────────────────────────────

function DeployForm({
  options,
  deployable,
  chosen,
  strategyId,
  version,
  capital,
  exchange,
  universe,
  symbols,
  timeframe,
  stopLoss,
  maxPositions,
  readiness,
  busy,
  onStrategy,
  onVersion,
  onCapital,
  onExchange,
  onUniverse,
  onSymbols,
  onTimeframe,
  onStopLoss,
  onMaxPositions,
  onDeploy,
  onOpen,
  deployments,
  onOpenStrategies,
}: {
  options: BacktestOptions | null;
  deployable: StrategyOption[];
  chosen: StrategyOption | undefined;
  strategyId: string;
  version: string;
  capital: string;
  exchange: string;
  universe: string;
  symbols: string;
  timeframe: string;
  stopLoss: string;
  maxPositions: string;
  readiness: { ready: boolean; problem: string | null };
  busy: ButtonState;
  onStrategy: (v: string) => void;
  onVersion: (v: string) => void;
  onCapital: (v: string) => void;
  onExchange: (v: string) => void;
  onUniverse: (v: string) => void;
  onSymbols: (v: string) => void;
  onTimeframe: (v: string) => void;
  onStopLoss: (v: string) => void;
  onMaxPositions: (v: string) => void;
  onDeploy: () => void;
  onOpen: (id: string) => void;
  deployments: Deployment[];
  onOpenStrategies?: () => void;
}) {
  const [pickedUniverse, setPickedUniverse] = useState(false);

  if (deployable.length === 0) {
    return (
      <Card>
        <div className="flex flex-col items-center gap-3 px-5 py-12 text-center">
          <div className="text-sm font-medium">Nothing to deploy yet</div>
          <p className="max-w-sm text-xs text-muted-foreground">
            Save a strategy and commit a version first. A paper run needs a fixed version so its results can be trusted.
          </p>
          {onOpenStrategies && (
            <Button variant="secondary" size="sm" onClick={onOpenStrategies}>
              Go to Strategies
            </Button>
          )}
        </div>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card>
        <CardHeader
          title="Deploy to paper"
        />
        <div className="space-y-4 p-5 pt-3">
          <Section
            n={1}
            title="Strategy"
            note="The version is pinned. A later edit creates a new version; this deployment keeps running the rules it was deployed with."
          >
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label="Strategy">
                <Select
                  value={strategyId}
                  onChange={onStrategy}
                  options={[
                    { value: "", label: "— pick a saved strategy —" },
                    ...deployable.map((s) => ({
                      value: s.strategy_id ?? "",
                      label: `${s.name}${s.versions.length ? ` · ${s.versions.length} version(s)` : ""}`,
                    })),
                  ]}
                />
              </Field>

              <Field
                label="Version (immutable)"
                hint={
                  chosen && chosen.versions.length === 0
                    ? "this strategy has no committed versions yet"
                    : undefined
                }
                warn={Boolean(chosen && chosen.versions.length === 0)}
              >
                <Select
                  value={version}
                  onChange={onVersion}
                  disabled={!chosen || chosen.versions.length === 0}
                  options={
                    chosen && chosen.versions.length === 0
                      ? [{ value: "", label: "no versions" }]
                      : (chosen?.versions.map((v) => ({
                          value: String(v.version),
                          label: `v${v.version}${v.change_note ? ` · ${v.change_note}` : ""}${
                            v.created_at ? ` · ${istDate(v.created_at)}` : ""
                          }`,
                        })) ?? [])
                  }
                />
              </Field>
            </div>
          </Section>

          <Section n={2} title="Capital" note="The allocation this deployment's P&L is measured against.">
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Input
                label="Capital (₹)"
                value={capital}
                onChange={onCapital}
                inputMode="numeric"
                placeholder="500000"
                error={capital && !(Number(capital) > 0) ? "must be positive" : undefined}
              />
              <Field
                label="Max open positions"
                
              >
                <input
                  className={selectClass}
                  value={maxPositions}
                  onChange={(e) => onMaxPositions(e.target.value)}
                  inputMode="numeric"
                />
              </Field>
            </div>
          </Section>

          <Section
            n={3}
            title="Universe"
            note="Symbols go to the runner as config; the runner reads its universe from there and nowhere else."
          >
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label="Universe" >
                <Select
                  value={universe}
                  onChange={(v) => {
                    onUniverse(v);
                    const found = (options?.universes ?? []).find((u) => u.name === v);
                    const list = found && Array.isArray(found.symbols) ? (found.symbols as string[]) : [];
                    if (list.length) {
                      onSymbols(list.join(","));
                      setPickedUniverse(true);
                    }
                  }}
                  options={[
                    { value: "", label: "— none —" },
                    ...(options?.universes ?? []).map((u) => ({
                      value: u.name,
                      label: `${u.name}${typeof u.count === "number" ? ` (${u.count})` : ""}`,
                    })),
                  ]}
                />
              </Field>

              <Field label="Exchange">
                <Select
                  value={exchange}
                  onChange={onExchange}
                  options={(options?.exchanges ?? ["NSEEQ", "BSEEQ"]).map((x) => ({
                    value: x,
                    label: x,
                  }))}
                />
              </Field>
            </div>

            <div className="mt-3.5">
              <Input
                label="Symbols (comma separated)"
                value={symbols}
                onChange={(v) => {
                  onSymbols(v);
                  setPickedUniverse(false);
                }}
                placeholder="RELIANCE-EQ,INFY-EQ,TCS-EQ"
                rightIcon={
                  parseSymbols(symbols).length ? (
                    <span className="pr-3.5 text-[11px] font-semibold text-muted-foreground">
                      {parseSymbols(symbols).length}
                    </span>
                  ) : null
                }
              />
              {pickedUniverse && (
                <Hint className="mt-1.5 px-1">
                  Loaded from the universe above. Edit the field to override —
                  these are what the runner will actually evaluate.
                </Hint>
              )}
            </div>
          </Section>

          <Section n={4} title="Timeframe and stop" note="The trailing stop the runner applies to every paper position.">
            <div className="grid gap-3.5 sm:grid-cols-3">
              <Field
                label="Timeframe"
                hint={(() => {
                  const tf = (options?.timeframes ?? []).find((t) => t.value === timeframe);
                  return tf?.available === false ? (tf.reason ?? "unavailable") : undefined;
                })()}
                warn={
                  (options?.timeframes ?? []).find((t) => t.value === timeframe)?.available ===
                  false
                }
              >
                <Select
                  value={timeframe}
                  onChange={onTimeframe}
                  options={(options?.timeframes ?? [{ value: "1d", label: "1d" }]).map((t) => ({
                    value: t.value,
                    label: `${t.label}${t.available === false ? " — unavailable" : ""}`,
                  }))}
                />
              </Field>

              <Input
                label="Stop loss % (optional)"
                value={stopLoss}
                onChange={onStopLoss}
                inputMode="decimal"
                placeholder="leave blank for the version's own"
              />
            </div>
          </Section>

          <PositionSizingSection
            capital={capital}
            stopLoss={stopLoss}
          />

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-4">
            <Hint className="max-w-md">
              {readiness.ready
                ? "Deploying starts the loop immediately against the live feed."
                : `Cannot deploy yet — ${readiness.problem}.`}
            </Hint>
            <StatefulButton
              state={busy}
              onClick={onDeploy}
              disabled={!readiness.ready}
              loadingText="Deploying"
              successText="Deployed"
              errorText="Refused"
              icon={<Play className="size-4" />}
            >
              Deploy to paper
            </StatefulButton>
          </div>
        </div>
      </Card>

      <div className="space-y-4">
        <Card>
          <CardHeader title="Existing deployments" sub={undefined} />
          <div className="max-h-[420px] space-y-2 overflow-y-auto p-4 pt-3">
            {deployments.length === 0 ? (
              <Hint>None yet. Everything you deploy appears here.</Hint>
            ) : (
              deployments.map((d) => (
                <button
                  key={d.deployment_id}
                  type="button"
                  onClick={() => onOpen(d.deployment_id)}
                  className="w-full rounded-xl border border-border/60 px-3 py-2.5 text-left transition-colors hover:border-border hover:bg-primary/[0.03]"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[12.5px] font-semibold">
                      {d.strategy_id.slice(0, 8)} · v{d.strategy_version}
                    </span>
                    <StatusPill status={d.status} />
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                    <span className="truncate">{fmtMoney(d.capital)}</span>
                    <span className="tabular-nums">
                      {d.pnl ? fmtMoneyOrDash(d.pnl.equity - d.capital) : "—"}
                    </span>
                  </div>
                </button>
              ))
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

// ─── the monitor ──────────────────────────────────────────────────────────────

function MonitorView({
  deployments,
  selectedId,
  overview,
  evidenceCounts,
  gates,
  idle,
  actionBusy,
  pendingAction,
  reason,
  onSelect,
  onReason,
  onAsk,
  onCancelAsk,
  onStart,
  onConfirm,
}: {
  deployments: Deployment[];
  selectedId: string | null;
  overview: MonitorOverview | null;
  evidenceCounts: ForwardEvidenceCounts | null;
  gates: ReturnType<typeof controlGates>;
  idle: string | null;
  actionBusy: string | null;
  pendingAction: { kind: "pause" | "stop" | "reset"; deploymentId: string } | null;
  reason: string;
  onSelect: (id: string) => void;
  onReason: (v: string) => void;
  onAsk: (kind: "pause" | "stop" | "reset", id: string) => void;
  onCancelAsk: () => void;
  onStart: (id: string) => void;
  onConfirm: (kind: "pause" | "stop" | "reset", id: string, why: string) => void;
}) {
  const [details, setDetails] = useState(false);
  if (deployments.length === 0) {
    return (
      <Card>
        <CardHeader title="No deployments" sub="Deploy one to start monitoring it." />
        <div className="p-5 pt-3">
          <Hint>Nothing to watch yet.</Hint>
        </div>
      </Card>
    );
  }

  const status = overview?.status;
  const pnl = overview?.pnl;
  const positions = openPositions(overview);
  const unpriced = unpricedOf(overview);
  const chain = overview ? toChain(overview.timeline) : [];
  const reached = new Set(chain.map((c) => c.stage));

  return (
    <div className="space-y-4">
      <PaperRuns
        deployments={deployments}
        onManage={(id) => {
          onSelect(id);
          setDetails(true);
        }}
      />
      <button
        type="button"
        onClick={() => setDetails((v) => !v)}
        className="text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        {details ? "Hide details and controls" : "Details and controls"}
      </button>
      {details && (<>
      {/* 1. The prominent Forward Evidence Counter */}
      <ForwardEvidenceCounterCard
        counts={evidenceCounts}
        selectedStrategyId={status?.strategy_id}
        selectedVersion={status?.strategy_version}
      />

      {/* the picker + the four controls */}
      <Card>
        <div className="flex flex-wrap items-end justify-between gap-3 p-5">
          <div className="min-w-[220px] flex-1">
            <label className="mb-1.5 block px-1 text-sm font-medium">Deployment</label>
            <Select
              value={selectedId ?? ""}
              onChange={onSelect}
              options={deployments.map((d) => ({
                value: d.deployment_id,
                label: `${d.deployment_id.slice(0, 8)} · v${d.strategy_version} · ${d.status} · ${fmtMoney(d.capital)}`,
              }))}
            />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={!selectedId || !gates.canStart || actionBusy !== null}
              title={gates.startBlocked ?? "Start or resume this deployment"}
              onClick={() => selectedId && onStart(selectedId)}
            >
              <Play className="size-3.5" /> Start
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={!selectedId || !gates.canPause || actionBusy !== null}
              onClick={() => selectedId && onAsk("pause", selectedId)}
            >
              <Pause className="size-3.5" /> Pause
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={!selectedId || !gates.canStop || actionBusy !== null}
              onClick={() => selectedId && onAsk("stop", selectedId)}
            >
              <Square className="size-3.5" /> Stop
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!selectedId || actionBusy !== null}
              title="Stop this deployment and create a fresh one with the same configuration"
              onClick={() => selectedId && onAsk("reset", selectedId)}
            >
              <RotateCcw className="size-3.5" /> Reset
            </Button>
          </div>
        </div>

        {/* The reason prompt */}
        {pendingAction && (
          <div className="border-t border-border/60 p-5 pt-4">
            <div className="mb-2 text-[13px] font-semibold">
              Why are you {pendingAction.kind === "reset" ? "resetting" : `${pendingAction.kind}ing`}{" "}
              this deployment?
            </div>
            <Hint className="mb-2.5">
              This sentence is what shows up when somebody asks why the strategy stopped.
              {pendingAction.kind === "reset" &&
                " Reset stops this deployment and creates a new one — the history stays readable."}
            </Hint>
            <div className="flex flex-wrap items-center gap-2">
              <input
                autoFocus
                value={reason}
                onChange={(e) => onReason(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && reason.trim()) {
                    onConfirm(pendingAction.kind, pendingAction.deploymentId, reason.trim());
                  }
                  if (e.key === "Escape") onCancelAsk();
                }}
                placeholder="e.g. testing the breakout entry after the param change"
                className={cn(selectClass, "flex-1 min-w-[240px] rounded-xl")}
              />
              <Button
                size="sm"
                disabled={!reason.trim() || actionBusy !== null}
                onClick={() =>
                  onConfirm(pendingAction.kind, pendingAction.deploymentId, reason.trim())
                }
              >
                Confirm {pendingAction.kind}
              </Button>
              <Button size="sm" variant="ghost" onClick={onCancelAsk}>
                Cancel
              </Button>
            </div>
          </div>
        )}
      </Card>

      {/* the header: running vs trading, and why not */}
      {status && (
        <Card>
          <div className="flex flex-wrap items-start justify-between gap-4 p-5">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill
                  status={status.status}
                  trading={status.trading}
                  diagnosticState={status.diagnostic_state}
                />
                <TradingPill trading={status.trading} reason={status.not_trading_because} />
                <Badge tone="flat">{status.mode}</Badge>
                <Badge tone="flat">v{status.strategy_version}</Badge>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {status.deployment_id.slice(0, 8)}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-muted-foreground">
                <span>{status.symbols.length} symbol(s)</span>
                <span>{status.exchange}</span>
                <span>{status.timeframe}</span>
                <span>
                  session {status.in_market_hours ? "open" : "closed"}
                </span>
                <span>
                  loop {status.loop_attached ? "attached" : "not attached"}
                </span>
                {status.started_at && <span>started {istDate(status.started_at)}</span>}
              </div>
            </div>
          </div>

          {/* The honesty block */}
          {(!status.trading || idle) && (
            <div className="px-5 pb-5">
              <Callout
                tone={status.blocked_reason || status.diagnostic_state === "system_error" || status.diagnostic_state === "stale_tick" ? "bad" : "warn"}
                title={
                  status.blocked_reason
                    ? "Not trading — rules could not be resolved"
                    : status.diagnostic_state === "stale_tick"
                    ? "Not trading — stale price feed"
                    : status.diagnostic_state === "no_live_tick"
                    ? "Not trading — waiting for fresh ticks"
                    : status.diagnostic_state === "market_closed"
                    ? "Not trading — cash market session is closed"
                    : "Not trading"
                }
              >
                {status.not_trading_because || idle || status.skipped_reason}
                {status.blocked_reason && (
                  <span className="mt-1 block text-[12px] opacity-90">
                    The runner refuses to trade on a guessed default rather than
                    silently using generic entry/exit rules and attributing the
                    resulting orders to v{status.strategy_version}.
                  </span>
                )}
                {status.diagnostic_state === "stale_tick" && (
                  <span className="mt-1 block text-[12px] opacity-90">
                    Received tick age exceeds the maximum freshness threshold.
                    Orders are strictly gated to avoid filling against old session prices.
                  </span>
                )}
              </Callout>
            </div>
          )}

          {status.stop_reason && (
            <div className="px-5 pb-5">
              <Hint>
                Last recorded reason: <span className="text-foreground">{status.stop_reason}</span>
              </Hint>
            </div>
          )}
        </Card>
      )}

      {/* 2. Operations Telemetry Rail */}
      {status && <TelemetryBar status={status} />}

      {/* 3. Live Execution Pipeline Inspection */}
      {status && <PipelineInspectionCard status={status} />}

      {/* the numbers */}
      {pnl && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="Today's P&L"
            value={fmtMoneyOrDash(pnl.today_pnl)}
            sub={
              pnl.today_pnl === null ? (
                <span className="text-amber-600 dark:text-amber-400">
                  not measured — no fill before the session boundary
                </span>
              ) : (
                fmtPctOrDash(pnl.today_pct)
              )
            }
            tone={pnl.today_pnl === null ? "neutral" : pnl.today_pnl >= 0 ? "good" : "bad"}
            hint={
              pnl.today_since
                ? `Since ${istClock(pnl.today_since)} IST`
                : "No earlier equity to subtract; shown as unknown rather than as zero."
            }
          />
          <Stat
            label="Total P&L"
            value={fmtMoneyOrDash(pnl.total_pnl)}
            sub={fmtPctOrDash(pnl.total_pct)}
            tone={
              pnl.total_pnl === null ? "neutral" : pnl.total_pnl >= 0 ? "good" : "bad"
            }
            hint="Measured against the allocated capital, not against cash on hand."
          />
          <Stat
            label="Capital"
            value={fmtMoney(pnl.capital)}
            sub={`equity ${fmtMoney(pnl.equity)}`}
            hint="The allocation this deployment was created with and is judged on."
          />
          <Stat
            label="Exposure"
            value={fmtMoney(pnl.gross_exposure)}
            sub={fmtPctOrDash(pnl.exposure_pct)}
            hint={`Net ${fmtMoney(pnl.net_exposure)}; cash ${fmtMoney(pnl.cash)}`}
          />
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        {/* the chain */}
        <Card>
          <CardHeader
            title="Signal → Risk → Order → Fill → Position"
            sub="Every step in the causal chain, newest first within each stage."
          />
          <div className="p-5 pt-4">
            <div className="mb-4 flex flex-wrap items-stretch gap-1.5">
              {STAGES.map((stage, i) => {
                const link = chain.find((c) => c.stage === stage);
                const hit = reached.has(stage);
                return (
                  <div key={stage} className="flex items-center gap-1.5">
                    <div
                      title={STAGE_BLURB[stage]}
                      className={cn(
                        "rounded-lg border px-3 py-2",
                        hit
                          ? "border-emerald-500/40 bg-emerald-500/[0.07]"
                          : "border-border/60 bg-muted/20",
                      )}
                    >
                      <div className="flex items-center gap-1.5">
                        <span
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            hit ? "bg-emerald-500" : "bg-muted-foreground/30",
                          )}
                        />
                        <span
                          className={cn(
                            "text-[11px] font-bold uppercase tracking-[0.04em]",
                            hit ? "text-foreground" : "text-muted-foreground",
                          )}
                        >
                          {STAGE_LABEL[stage]}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[10.5px] tabular-nums text-muted-foreground">
                        {link ? istClock(link.ts) : "—"}
                        {link && link.count > 1 ? ` · ${link.count}` : ""}
                      </div>
                    </div>
                    {i < STAGES.length - 1 && (
                      <ArrowRight
                        className={cn(
                          "size-3.5 shrink-0",
                          reached.has(STAGES[i + 1]) || hit
                            ? "text-muted-foreground"
                            : "text-muted-foreground/25",
                        )}
                      />
                    )}
                  </div>
                );
              })}
            </div>

            {!overview ? (
              <Hint>Loading…</Hint>
            ) : overview.timeline.length === 0 ? (
              <Hint>
                Nothing has happened yet. A row appears the moment a rule fires on
                a live tick — this is a log of what was done, not a plan of what
                will be.
              </Hint>
            ) : (
              <div className="max-h-[440px] space-y-1.5 overflow-y-auto pr-1">
                {[...overview.timeline]
                  .reverse()
                  .map((e, i) => (
                    <TimelineRow key={`${e.ts}-${e.stage}-${e.order_id ?? ""}-${i}`} event={e} />
                  ))}
              </div>
            )}
          </div>
        </Card>

        <div className="space-y-4">
          {/* positions */}
          <Card>
            <CardHeader
              title="Open positions"
              sub={`${positions.length} held`}
              action={
                unpriced.length ? (
                  <Badge tone="warn">{unpriced.length} unpriced</Badge>
                ) : undefined
              }
            />
            <div className="p-4 pt-3">
              {unpriced.length > 0 && (
                <div className="mb-2.5">
                  <Callout tone="warn" title="Some positions could not be valued">
                    {unpriced.join(", ")}. The totals above cover only the priced
                    positions — reporting the rest at zero would show a fabricated
                    loss of their whole cost basis.
                  </Callout>
                </div>
              )}
              {positions.length === 0 ? (
                <Hint>Flat. No open positions in this deployment.</Hint>
              ) : (
                <div className="space-y-1.5">
                  {positions.map((p) => (
                    <div
                      key={p.symbol}
                      className="flex items-center justify-between gap-2 rounded-lg border border-border/60 px-3 py-2"
                    >
                      <div className="min-w-0">
                        <div className="truncate text-[12.5px] font-semibold">{p.symbol}</div>
                        <div className="text-[11px] text-muted-foreground tabular-nums">
                          {p.quantity} @ {fmtMoney(p.avg_price)}
                          {p.priced && p.last_price !== null && (
                            <> → {fmtMoney(p.last_price)}</>
                          )}
                        </div>
                      </div>
                      <div className="text-right">
                        <div
                          className={cn(
                            "text-[12.5px] font-bold tabular-nums",
                            p.unrealized_pnl === null
                              ? "text-muted-foreground"
                              : p.unrealized_pnl >= 0
                                ? "text-emerald-600 dark:text-emerald-400"
                                : "text-destructive",
                          )}
                        >
                          {fmtMoneyOrDash(p.unrealized_pnl)}
                        </div>
                        <div className="text-[10.5px] text-muted-foreground">
                          {p.priced ? "unrealised" : "unpriced"}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>

          {/* risk */}
          {overview && (
            <Card>
              <CardHeader
                title="Risk"
                sub="Platform limits, next to this deployment's own figures"
                action={
                  overview.risk.kill_switch ? (
                    <Badge tone="bad">
                      <ShieldCheck className="mr-1 size-3" /> kill switch
                    </Badge>
                  ) : (
                    <Badge tone="good">clear</Badge>
                  )
                }
              />
              <div className="space-y-2 p-4 pt-3 text-[12px]">
                <RiskRow label="Open positions" value={String(overview.risk.observed.open_positions)} />
                <RiskRow
                  label="Gross exposure"
                  value={fmtMoney(overview.risk.observed.gross_exposure)}
                />
                <RiskRow label="Equity" value={fmtMoney(overview.risk.observed.equity)} />
                <RiskRow
                  label="Max position / symbol"
                  value={limitText(overview.risk.limits, "max_position_per_symbol")}
                />
                <RiskRow
                  label="Max open positions"
                  value={limitText(overview.risk.limits, "max_open_positions")}
                />
                <RiskRow
                  label="Max gross exposure"
                  value={limitText(overview.risk.limits, "max_gross_exposure")}
                />
                {overview.risk.reason && (
                  <Hint className="pt-1">
                    Last change: {overview.risk.reason}
                    {overview.risk.changed_by ? ` — ${overview.risk.changed_by}` : ""}
                  </Hint>
                )}
              </div>
            </Card>
          )}

          {/* orders, fills, signals, trades — counts, with the timeline as the
              detail. Four separate tables of the same events would be four
              renderings of one truth. */}
          {overview && (
            <Card>
              <CardHeader title="Log" sub="Counts; the timeline above is the detail" />
              <div className="grid grid-cols-2 gap-2 p-4 pt-3 text-[12px]">
                <LogCount label="Orders" value={overview.orders.length} />
                <LogCount label="Fills" value={overview.fills.length} />
                <LogCount label="Signals" value={overview.signals.length} />
                <LogCount
                  label="Trades"
                  value={overview.trades.total}
                  sub={
                    overview.trades.open_count
                      ? `${overview.trades.open_count} open`
                      : undefined
                  }
                />
              </div>
              {overview.signals.length > 0 && (
                <div className="border-t border-border/60 p-4">
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted-foreground">
                    Latest signals
                  </div>
                  <div className="space-y-1.5">
                    {overview.signals.slice(0, 4).map((s) => (
                      <div key={`${s.order_id}-${s.ts}`} className="text-[11.5px]">
                        <div className="flex items-center gap-1.5">
                          <Badge tone={s.side === "BUY" ? "good" : "bad"}>{s.side}</Badge>
                          <span className="font-semibold">{s.symbol}</span>
                          <span className="text-muted-foreground">{istClock(s.ts)}</span>
                        </div>
                        {s.reason && (
                          <div className="mt-0.5 text-muted-foreground">{s.reason}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
      </>)}
    </div>
  );
}

function TimelineRow({ event }: { event: MonitorOverview["timeline"][number] }) {
  const tone = outcomeTone(event.outcome);
  const Icon =
    tone === "bad"
      ? CircleSlash
      : tone === "good"
        ? CheckCircle2
        : tone === "warn"
          ? Activity
          : ArrowRight;
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-border/50 px-3 py-2">
      <span
        className={cn(
          "mt-0.5 grid size-5 shrink-0 place-items-center rounded-full",
          tone === "bad" && "bg-destructive/12 text-destructive",
          tone === "good" && "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400",
          tone === "warn" && "bg-amber-500/12 text-amber-600 dark:text-amber-400",
          tone === "flat" && "bg-muted text-muted-foreground",
        )}
      >
        <Icon className="size-3" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.05em] text-muted-foreground">
            {STAGE_LABEL[event.stage as TimelineStage] ?? event.stage}
          </span>
          <span className="text-[11.5px] text-foreground">{event.summary}</span>
        </div>
        {event.reason && event.reason !== event.summary && (
          <div className="mt-0.5 text-[11px] text-muted-foreground">{event.reason}</div>
        )}
      </div>
      <span className="shrink-0 text-[10.5px] tabular-nums text-muted-foreground">
        {istClock(event.ts)}
      </span>
    </div>
  );
}

// ─── small pieces & telemetry cards ──────────────────────────────────────────

function ForwardEvidenceCounterCard({
  counts,
  selectedStrategyId,
  selectedVersion,
}: {
  counts: ForwardEvidenceCounts | null;
  selectedStrategyId?: string | null;
  selectedVersion?: number | null;
}) {
  const [showTable, setShowTable] = useState(false);
  const totalForward = counts?.total_genuine_forward ?? 0;
  const todayForward = counts?.today_genuine_forward ?? 0;
  const last7dForward = counts?.last_7d_genuine_forward ?? 0;

  const inSample = counts?.by_class?.IN_SAMPLE?.total ?? 0;
  const paperForward = counts?.by_class?.PAPER_FORWARD?.total ?? 0;
  const liveForward = counts?.by_class?.LIVE_FORWARD?.total ?? 0;

  return (
    <Card className="border-primary/25 bg-gradient-to-br from-card via-card to-primary/[0.03] shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4 p-5 pb-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="flex h-2.5 w-2.5 rounded-full bg-emerald-500 ring-4 ring-emerald-500/20" />
            <h3 className="text-base font-bold tracking-tight text-foreground">
              Genuine Forward Observations
            </h3>
            <Badge tone="good" className="text-[10.5px]">Evidence Grade</Badge>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Strict forward trading data accumulated without backfilled rows or cached price leakage.
          </p>
        </div>

        {counts?.as_of && (
          <div className="text-right text-[11px] text-muted-foreground">
            As of {istClock(counts.as_of)} IST
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 p-5 pt-0 sm:grid-cols-3 lg:grid-cols-6">
        <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/[0.07] p-3.5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
            Total Forward
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-foreground">
            {totalForward}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">all-time observations</div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-3.5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Today
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-foreground">
            {todayForward}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">session forward</div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-3.5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Last 7 Days
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-foreground">
            {last7dForward}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">rolling 7 trading days</div>
        </div>

        <div className="rounded-xl border border-border/60 bg-muted/20 p-3.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-emerald-500">
              Paper forward
            </span>
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-foreground">
            {paperForward}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">
            {counts?.by_class?.PAPER_FORWARD?.today ?? 0} today · {counts?.by_class?.PAPER_FORWARD?.last_7d ?? 0} 7d
          </div>
        </div>

        <div className="rounded-xl border border-border/60 bg-muted/20 p-3.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-blue-500">
              Live forward
            </span>
            <span className="h-1.5 w-1.5 rounded-full bg-blue-500" />
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-foreground">
            {liveForward}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">
            {counts?.by_class?.LIVE_FORWARD?.today ?? 0} today · {counts?.by_class?.LIVE_FORWARD?.last_7d ?? 0} 7d
          </div>
        </div>

        <div className="rounded-xl border border-border/60 bg-muted/20 p-3.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              In sample
            </span>
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
          </div>
          <div className="mt-1 text-2xl font-extrabold tabular-nums text-muted-foreground">
            {inSample}
          </div>
          <div className="mt-0.5 text-[10.5px] text-muted-foreground">backtest/pre-live</div>
        </div>
      </div>

      {counts && counts.by_strategy && counts.by_strategy.length > 0 && (
        <div className="border-t border-border/50 px-5 py-3">
          <button
            type="button"
            onClick={() => setShowTable((v) => !v)}
            className="flex w-full items-center justify-between text-[11.5px] font-semibold text-muted-foreground hover:text-foreground"
          >
            <span>
              Breakdown by Strategy & Version ({counts.by_strategy.length} tracked)
            </span>
            <span className="text-[11px] underline">
              {showTable ? "Hide details" : "Show details"}
            </span>
          </button>

          {showTable && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-[11.5px]">
                <thead>
                  <tr className="border-b border-border/50 text-[10.5px] uppercase tracking-wider text-muted-foreground">
                    <th className="pb-1.5 font-semibold">Strategy</th>
                    <th className="pb-1.5 font-semibold">Version</th>
                    <th className="pb-1.5 text-right font-semibold">Genuine Forward</th>
                    <th className="pb-1.5 text-right font-semibold">Today</th>
                    <th className="pb-1.5 text-right font-semibold">Last 7d</th>
                    <th className="pb-1.5 text-right font-semibold">Paper Forward</th>
                    <th className="pb-1.5 text-right font-semibold">In-Sample</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {counts.by_strategy.map((s, idx) => {
                    const isSelected =
                      selectedStrategyId &&
                      s.strategy_id === selectedStrategyId &&
                      (selectedVersion === undefined || s.strategy_version === selectedVersion);
                    return (
                      <tr
                        key={`${s.strategy_id}-${s.strategy_version}-${idx}`}
                        className={cn(
                          "transition-colors hover:bg-muted/20",
                          isSelected && "bg-primary/[0.04] font-semibold",
                        )}
                      >
                        <td className="py-2 font-mono text-[11px]">
                          {s.strategy_id.slice(0, 12)}
                        </td>
                        <td className="py-2">
                          {s.strategy_version !== null ? `v${s.strategy_version}` : "—"}
                        </td>
                        <td className="py-2 text-right font-bold text-emerald-600 dark:text-emerald-400 tabular-nums">
                          {s.total_genuine_forward}
                        </td>
                        <td className="py-2 text-right tabular-nums">{s.today_genuine_forward}</td>
                        <td className="py-2 text-right tabular-nums">{s.last_7d_genuine_forward}</td>
                        <td className="py-2 text-right tabular-nums">{s.paper_forward}</td>
                        <td className="py-2 text-right tabular-nums text-muted-foreground">
                          {s.in_sample}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function TelemetryBar({ status }: { status: MonitorOverview["status"] }) {
  const tickTime = status.last_tick_time ? istClock(status.last_tick_time) : "—";
  const tickAge = status.last_tick_age_seconds;
  const evalTime = status.last_strategy_evaluation ? istClock(status.last_strategy_evaluation) : "—";
  const isStale = tickAge !== null && tickAge !== undefined && tickAge > 60;

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <div className="rounded-xl border border-border/60 bg-card p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Market Status
        </div>
        <div className="mt-1 flex items-center gap-1.5 font-bold">
          <span className={cn("h-2 w-2 rounded-full", status.in_market_hours ? "bg-emerald-500" : "bg-amber-500")} />
          <span className="text-sm">
            {status.in_market_hours ? "Regular Session Open" : "Market Closed"}
          </span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          NSE/BSE 09:15 to 15:30 IST
        </div>
      </div>

      <div className="rounded-xl border border-border/60 bg-card p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Live Tick & Age
        </div>
        <div className="mt-1 flex items-center gap-1.5 font-bold">
          <span className={cn("h-2 w-2 rounded-full", isStale || !status.last_tick_time ? "bg-amber-500" : "bg-emerald-500 animate-pulse")} />
          <span className={cn("text-sm tabular-nums", isStale && "text-destructive font-extrabold")}>
            {tickAge !== null && tickAge !== undefined ? `${tickAge.toFixed(1)}s ago` : "No ticks"}
          </span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Last tick at {tickTime} IST
        </div>
      </div>

      <div className="rounded-xl border border-border/60 bg-card p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Last Evaluation
        </div>
        <div className="mt-1 text-sm font-bold text-foreground">
          {evalTime} IST
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Evaluates once per second in session
        </div>
      </div>

      <div className="rounded-xl border border-border/60 bg-card p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Skipped Cycles
        </div>
        <div className="mt-1 flex items-center gap-2 text-sm font-bold tabular-nums text-foreground">
          <span>{status.skipped_evaluations_count ?? 0} eval</span>
          <span className="text-muted-foreground">/</span>
          <span>{status.skipped_fills_count ?? 0} fills</span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Recorded skip events
        </div>
      </div>

      <div className="rounded-xl border border-border/60 bg-card p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Genuine Trades
        </div>
        <div className="mt-1 text-sm font-bold tabular-nums text-foreground">
          {status.trade_count ?? 0} closed ({status.open_trades_count ?? 0} open)
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Paper forward in journal
        </div>
      </div>
    </div>
  );
}

function PipelineInspectionCard({ status }: { status: MonitorOverview["status"] }) {
  const signal = status.last_signal;
  const risk = status.last_risk_decision;
  const order = status.last_order;
  const fill = status.last_fill;

  return (
    <Card>
      <CardHeader
        title="Live Execution Pipeline"
        sub="Continuous causal link: Live Ticks → Strategy → Signal → Risk Decision → OMS Order → Paper Fill"
      />
      <div className="grid gap-3 p-5 pt-3 sm:grid-cols-2 lg:grid-cols-4">
        {/* 1. Last Signal */}
        <div className="rounded-xl border border-border/60 bg-muted/10 p-3.5">
          <div className="flex items-center justify-between gap-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span>1. Strategy Signal</span>
            {signal ? <Badge tone="good">Emitted</Badge> : <Badge tone="flat">Idle</Badge>}
          </div>
          {signal ? (
            <div className="mt-2 space-y-1 text-[12px]">
              <div className="flex items-center gap-1.5 font-bold">
                <Badge tone={signal.side === "BUY" ? "good" : "bad"}>{signal.side}</Badge>
                <span>{signal.symbol}</span>
              </div>
              <div className="text-[11px] text-muted-foreground">
                Rule: <span className="text-foreground">{signal.rule || "breakout"}</span>
              </div>
              {signal.reason && (
                <div className="line-clamp-2 text-[10.5px] text-muted-foreground">{signal.reason}</div>
              )}
              {signal.at && (
                <div className="text-[10px] text-muted-foreground/80">{istClock(signal.at)} IST</div>
              )}
            </div>
          ) : (
            <div className="mt-2 text-[11.5px] text-muted-foreground">
              No signal generated yet on current ticks
            </div>
          )}
        </div>

        {/* 2. Last Risk Decision */}
        <div className="rounded-xl border border-border/60 bg-muted/10 p-3.5">
          <div className="flex items-center justify-between gap-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span>2. Risk Decision</span>
            {risk ? (
              <Badge tone={risk.approved ? "good" : "bad"}>
                {risk.approved ? "Approved" : "Rejected"}
              </Badge>
            ) : (
              <Badge tone="flat">Awaiting</Badge>
            )}
          </div>
          {risk ? (
            <div className="mt-2 space-y-1 text-[12px]">
              <div className="font-semibold text-foreground">
                {risk.approved ? "Passed Risk Check" : "Refused by Risk Gate"}
              </div>
              <div className="line-clamp-2 text-[11px] text-muted-foreground">
                {risk.reason}
              </div>
              {risk.symbol && (
                <div className="text-[11px] text-muted-foreground">
                  {risk.symbol} · {risk.quantity ? `${risk.quantity} qty` : ""}
                </div>
              )}
              {risk.at && (
                <div className="text-[10px] text-muted-foreground/80">{istClock(risk.at)} IST</div>
              )}
            </div>
          ) : (
            <div className="mt-2 text-[11.5px] text-muted-foreground">
              Evaluates immediately when signal is emitted
            </div>
          )}
        </div>

        {/* 3. Last OMS Order */}
        <div className="rounded-xl border border-border/60 bg-muted/10 p-3.5">
          <div className="flex items-center justify-between gap-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span>3. OMS Order</span>
            {order ? (
              <Badge tone={order.status === "FILLED" ? "good" : order.status === "REJECTED" ? "bad" : "info"}>
                {order.status}
              </Badge>
            ) : (
              <Badge tone="flat">Idle</Badge>
            )}
          </div>
          {order ? (
            <div className="mt-2 space-y-1 text-[12px]">
              <div className="font-semibold text-foreground">
                {order.side} {order.symbol} x{order.quantity}
              </div>
              {order.order_id && (
                <div className="font-mono text-[10.5px] text-muted-foreground">
                  ID: {order.order_id.slice(0, 10)}
                </div>
              )}
              {order.reason && (
                <div className="line-clamp-2 text-[10.5px] text-muted-foreground">
                  {order.reason}
                </div>
              )}
            </div>
          ) : (
            <div className="mt-2 text-[11.5px] text-muted-foreground">
              Submits through OMS upon risk approval
            </div>
          )}
        </div>

        {/* 4. Last Paper Fill */}
        <div className="rounded-xl border border-border/60 bg-muted/10 p-3.5">
          <div className="flex items-center justify-between gap-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span>4. Paper Fill</span>
            {fill ? <Badge tone="good">Filled</Badge> : <Badge tone="flat">No Fill</Badge>}
          </div>
          {fill ? (
            <div className="mt-2 space-y-1 text-[12px]">
              <div className="font-semibold text-foreground">
                {fill.side} {fill.symbol} x{fill.quantity}
              </div>
              <div className="text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
                Paper Venue Matched
              </div>
              <div className="text-[10.5px] text-muted-foreground">
                Enters journal as <span className="font-semibold text-foreground">PAPER_FORWARD</span>
              </div>
            </div>
          ) : (
            <div className="mt-2 text-[11.5px] text-muted-foreground">
              Matches at live quote with modeled slippage
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function StatusPill({
  status,
  trading,
  diagnosticState,
}: {
  status: string;
  trading?: boolean;
  diagnosticState?: string;
}) {
  if (status !== "RUNNING") {
    const tone =
      status === "PAUSED" ? "warn" : status === "STOPPED" ? "bad" : "flat";
    return <Badge tone={tone}>{status}</Badge>;
  }

  switch (diagnosticState) {
    case "market_closed":
      return (
        <Badge tone="warn" className="border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400">
          MARKET CLOSED
        </Badge>
      );
    case "no_live_tick":
      return (
        <Badge tone="warn" className="border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400">
          WAITING FOR TICKS
        </Badge>
      );
    case "stale_tick":
      return (
        <Badge tone="bad" className="border-destructive/40 bg-destructive/10 text-destructive">
          STALE TICK
        </Badge>
      );
    case "system_error":
      return (
        <Badge tone="bad" className="border-destructive/40 bg-destructive/10 text-destructive">
          SYSTEM ERROR
        </Badge>
      );
    case "risk_rejected":
      return (
        <Badge tone="warn" className="border-purple-500/40 bg-purple-500/10 text-purple-600 dark:text-purple-400">
          RISK REJECTED
        </Badge>
      );
    case "order_rejected":
      return (
        <Badge tone="bad" className="border-destructive/40 bg-destructive/10 text-destructive">
          ORDER REJECTED
        </Badge>
      );
    case "order_waiting_for_fill":
      return (
        <Badge tone="info" className="border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400">
          ORDER PENDING FILL
        </Badge>
      );
    case "paper_fill_completed":
      return (
        <Badge tone="good" className="border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
          FILL COMPLETED
        </Badge>
      );
    case "no_signal":
      return (
        <Badge tone="good" className="border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
          ACTIVE (NO SIGNAL)
        </Badge>
      );
    case "running":
    default:
      if (trading === false) {
        return (
          <Badge tone="warn" className="border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400">
            NOT TRADING
          </Badge>
        );
      }
      return (
        <Badge tone="good" className="border-emerald-500/40 bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
          <span className="mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
          RUNNING (LIVE FEED)
        </Badge>
      );
  }
}

function TradingPill({ trading, reason }: { trading: boolean; reason?: string | null }) {
  return (
    <span
      title={reason ?? (trading ? "Deployment actively processing live ticks" : "Deployment not currently trading")}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-bold",
        trading
          ? "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400"
          : "bg-muted text-muted-foreground",
      )}
    >
      <span
        className={cn("h-1.5 w-1.5 rounded-full", trading ? "bg-emerald-500" : "bg-muted-foreground/40")}
      />
      {trading ? "trading" : "not trading"}
    </span>
  );
}

function RiskRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function LogCount({ label, value, sub }: { label: string; value: number; sub?: string }) {
  return (
    <div className="rounded-lg border border-border/60 px-3 py-2">
      <div className="text-[10.5px] font-medium uppercase tracking-[0.04em] text-muted-foreground">
        {label}
      </div>
      <div className="text-[15px] font-bold tabular-nums">{value}</div>
      {sub && <div className="text-[10.5px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

/** Render a risk limit from the loosely-typed limits map.
 *
 *  `null` in that map means *unbounded*, which the backend emits by collapsing
 *  an `inf` sentinel — `Infinity` is not valid JSON. So `null` prints as
 *  "unbounded", never as a number nobody set. */
function limitText(limits: Record<string, unknown>, key: string): string {
  const raw = limits?.[key];
  if (raw === null || raw === undefined) return "unbounded";
  if (typeof raw === "number") {
    return Number.isFinite(raw) ? fmtMoney(raw) : "unbounded";
  }
  return String(raw);
}

function CompareView({
  strategies,
  deployments,
  cmpStrategyId,
  cmpChampion,
  cmpChallenger,
  comparison,
  cmpError,
  cmpBusy,
  launchDepId,
  launchVersion,
  launchBusy,
  onStrategy,
  onChampion,
  onChallenger,
  onCompare,
  onLaunchDep,
  onLaunchVersion,
  onLaunch,
}: {
  strategies: string[];
  deployments: Deployment[];
  cmpStrategyId: string;
  cmpChampion: string;
  cmpChallenger: string;
  comparison: ChampionComparison | null;
  cmpError: string | null;
  cmpBusy: boolean;
  launchDepId: string;
  launchVersion: string;
  launchBusy: boolean;
  onStrategy: (v: string) => void;
  onChampion: (v: string) => void;
  onChallenger: (v: string) => void;
  onCompare: () => void;
  onLaunchDep: (v: string) => void;
  onLaunchVersion: (v: string) => void;
  onLaunch: () => void;
}) {
  const champions = deployments.filter((d) => d.mode === "PAPER" && d.status === "RUNNING");
  const metric = comparison?.metric ?? "return_pct";
  const money = metric === "net_pnl";
  const cell = (v: number | null | undefined) =>
    v == null ? "—" : money ? fmtMoneyOrDash(v) : fmtPctOrDash(v);
  const arms = comparison?.comparison;
  const rows: { label: string; champ: number | null | undefined; chall: number | null | undefined; delta: number | null | undefined; pct?: boolean }[] = arms
    ? [
        { label: "Forward trades", champ: arms.champion.n, chall: arms.challenger.n, delta: null },
        { label: "Mean", champ: arms.champion.stats.mean, chall: arms.challenger.stats.mean, delta: arms.deltas.mean },
        { label: "Median", champ: arms.champion.stats.median, chall: arms.challenger.stats.median, delta: arms.deltas.median },
        { label: "Win rate", champ: pctOf(arms.champion.stats.win_rate), chall: pctOf(arms.challenger.stats.win_rate), delta: pctOf(arms.deltas.win_rate), pct: true },
        { label: "Profit factor", champ: arms.champion.stats.profit_factor, chall: arms.challenger.stats.profit_factor, delta: arms.deltas.profit_factor },
        { label: "Max drawdown", champ: arms.champion.max_drawdown, chall: arms.challenger.max_drawdown, delta: null },
      ]
    : [];
  const tone = comparison ? comparisonVerdictTone(comparison.comparison.verdict) : "muted";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Champion vs challenger"
          sub="Two versions, one market, two books. This screen reads readiness — it never declares a winner and cannot promote."
        />
        <div className="flex flex-wrap items-end gap-3 p-5 pt-3">
          <div className="min-w-[220px] flex-1">
            <label className="mb-1.5 block px-1 text-sm font-medium">Strategy</label>
            <Select
              value={cmpStrategyId}
              onChange={onStrategy}
              options={[
                { value: "", label: "Pick a strategy…" },
                ...strategies.map((s) => ({ value: s, label: s.slice(0, 12) })),
              ]}
            />
          </div>
          <div className="w-[130px]">
            <label className="mb-1.5 block px-1 text-sm font-medium">Champion</label>
            <Input value={cmpChampion} onChange={onChampion} placeholder="v1" inputMode="numeric" />
          </div>
          <div className="w-[130px]">
            <label className="mb-1.5 block px-1 text-sm font-medium">Challenger</label>
            <Input value={cmpChallenger} onChange={onChallenger} placeholder="v2" inputMode="numeric" />
          </div>
          <Button onClick={onCompare} disabled={cmpBusy || !cmpStrategyId}>
            {cmpBusy ? "Comparing…" : "Compare"}
          </Button>
        </div>
        {cmpError && (
          <div className="px-5 pb-4">
            <ErrorBox>{cmpError}</ErrorBox>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Launch a challenger"
          sub="Copies the champion's capital, universe and config — only the version differs, so parity is structural rather than typed correctly."
        />
        <div className="flex flex-wrap items-end gap-3 p-5 pt-3">
          <div className="min-w-[260px] flex-1">
            <label className="mb-1.5 block px-1 text-sm font-medium">Champion deployment</label>
            <Select
              value={launchDepId}
              onChange={onLaunchDep}
              options={[
                { value: "", label: "Pick a running PAPER deployment…" },
                ...champions.map((d) => ({
                  value: d.deployment_id,
                  label: `${d.deployment_id.slice(0, 8)} · ${d.strategy_id.slice(0, 8)} v${d.strategy_version} · ${fmtMoneyOrDash(d.capital)}`,
                })),
              ]}
            />
          </div>
          <div className="w-[130px]">
            <label className="mb-1.5 block px-1 text-sm font-medium">Version</label>
            <Input value={launchVersion} onChange={onLaunchVersion} placeholder="v2" inputMode="numeric" />
          </div>
          <Button onClick={onLaunch} disabled={launchBusy || !launchDepId || !launchVersion}>
            {launchBusy ? "Launching…" : "Launch challenger"}
          </Button>
        </div>
      </Card>

      {comparison && (
        <>
          <Card>
            <div className="p-5">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={tone === "good" ? "good" : tone === "warn" ? "warn" : "flat"}>
                  {comparison.comparison.verdict}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  {deltaSample(comparison.comparison.champion.n, comparison.comparison.challenger.n)} · metric {metric}
                </span>
              </div>
              <p className="mt-2 text-[12px] text-muted-foreground">
                {comparisonVerdictBlurb(comparison.comparison.verdict)}{" "}
                {comparison.comparison.verdict_reasons.join(" ")}
              </p>
            </div>
          </Card>

          <Card>
            <CardHeader
              title={`V${comparison.champion_version} (champion) vs V${comparison.challenger_version} (challenger)`}
              sub="Deltas are challenger-minus-champion descriptions, read beside both sample sizes."
            />
            <div className="overflow-x-auto p-5 pt-3">
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="border-b border-border/60 text-left text-[10px] uppercase tracking-wider text-muted-foreground">
                    <th className="px-2 py-1.5 font-medium">Figure</th>
                    <th className="px-2 py-1.5 font-medium">Champion V{comparison.champion_version}</th>
                    <th className="px-2 py-1.5 font-medium">Challenger V{comparison.challenger_version}</th>
                    <th className="px-2 py-1.5 font-medium">Δ (challenger − champion)</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.label} className="border-b border-border/40 last:border-0">
                      <td className="px-2 py-1.5 text-muted-foreground">{r.label}</td>
                      <td className="px-2 py-1.5 tabular-nums">{r.label === "Forward trades" ? (r.champ ?? "—") : r.pct ? fmtPctOrDash(r.champ) : cell(r.champ)}</td>
                      <td className="px-2 py-1.5 tabular-nums">{r.label === "Forward trades" ? (r.chall ?? "—") : r.pct ? fmtPctOrDash(r.chall) : cell(r.chall)}</td>
                      <td className="px-2 py-1.5 tabular-nums text-muted-foreground">
                        {r.delta == null ? "—" : r.pct ? fmtPctOrDash(r.delta) : fmtDelta(r.delta, metric)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card>
            <CardHeader title="Exact strategy difference" sub="Changed parameters between the two pinned definitions." />
            <div className="p-5 pt-3 text-[12px]">
              {comparison.definition_diff.identical ? (
                <p className="text-muted-foreground">The pinned definitions are identical.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full table-fixed">
                    <thead>
                      <tr className="border-b border-border/60 text-left text-[10px] uppercase tracking-wider text-muted-foreground">
                        <th className="w-1/5 px-2 py-1.5 font-medium">Changed</th>
                        <th className="w-2/5 px-2 py-1.5 font-medium">Champion V{comparison.champion_version}</th>
                        <th className="w-2/5 px-2 py-1.5 font-medium">Challenger V{comparison.challenger_version}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.definition_diff.changes.map((c) => (
                        <tr key={c.parameter} className="border-b border-border/40 font-mono text-[11px] last:border-0">
                          <td className="break-words px-2 py-1.5">{c.parameter}</td>
                          <td className="break-words px-2 py-1.5">{fmtDiffValue(c.champion)}</td>
                          <td className="break-words px-2 py-1.5">{fmtDiffValue(c.challenger)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader title="Version timeline" sub="Forward observations per version, with roles." />
            <div className="p-5 pt-3 text-[12px]">
              {comparison.timeline.map((t) => (
                <div key={t.version} className="flex items-center gap-2 py-1">
                  <span className="font-semibold tabular-nums">V{t.version}</span>
                  <span className="text-muted-foreground">→</span>
                  {t.role ? (
                    <Badge tone={t.role === "CHAMPION" ? "info" : "warn"}>{t.role}</Badge>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                  <span className="ml-auto tabular-nums text-muted-foreground">
                    {t.forward_observations} forward
                  </span>
                </div>
              ))}
            </div>
          </Card>

          <Card>
            <CardHeader title="Identical conditions" sub="Parity is reported, not assumed." />
            <div className="space-y-1 p-5 pt-3 text-[12px]">
              <ParityLine ok={comparison.parity.identical_capital} label="Capital" />
              <ParityLine ok={comparison.parity.identical_config} label="Universe & config" />
              <ParityLine ok={comparison.parity.venue_shared} label="Market data, costs, slippage (shared venue)" />
              {comparison.parity.mismatches.map((m) => (
                <p key={m} className="text-amber-500">
                  • {m}
                </p>
              ))}
              <p className="text-muted-foreground">{comparison.parity.note}</p>
            </div>
          </Card>

          {(["champion", "challenger"] as const).map((arm) => {
            const ctx = comparison.context[arm];
            const summary = comparison.comparison[arm];
            const version = arm === "champion" ? comparison.champion_version : comparison.challenger_version;
            return (
              <Card key={arm}>
                <CardHeader
                  title={`${arm === "champion" ? "Champion" : "Challenger"} V${version} — context & regime`}
                  sub={
                    ctx.unavailable
                      ? "Context evidence unavailable for this arm."
                      : ctx.statement || "No context finding."
                  }
                />
                <div className="space-y-2 p-5 pt-3 text-[12px]">
                  <p className="text-muted-foreground">
                    Context score bands:{" "}
                    {(summary.score_bands || [])
                      .map((b) => `${b.band} ${b.forward_trades}`)
                      .join(" · ") || "—"}
                    {"  "}· {ctx.forward_n} forward with context
                  </p>
                  <p className="text-muted-foreground">
                    Regimes:{" "}
                    {Object.entries(summary.regime_distribution || {})
                      .map(([k, v]) => `${k} ${v}`)
                      .join(" · ") || "—"}
                  </p>
                  <p className="text-muted-foreground">
                    Sectors:{" "}
                    {Object.entries(summary.sector_distribution || {})
                      .map(([k, v]) => `${k} ${v}`)
                      .join(" · ") || "—"}
                  </p>
                  <p className="text-muted-foreground">
                    Recent ({summary.recent_vs_historical.window_days}d):{" "}
                    {summary.recent_vs_historical.recent_mean ?? "—"} over{" "}
                    {summary.recent_vs_historical.recent_n} vs historical{" "}
                    {summary.recent_vs_historical.historical_mean ?? "—"} over{" "}
                    {summary.recent_vs_historical.historical_n}
                  </p>
                </div>
              </Card>
            );
          })}

          <Callout>
            Read-only comparison. Nothing here promotes the challenger, pauses an
            arm, edits a strategy or touches risk — the verdict only says whether
            the evidence can be read yet.
          </Callout>
        </>
      )}
    </div>
  );
}

function pctOf(fraction: number | null | undefined): number | null {
  return fraction == null ? null : fraction * 100;
}

function ParityLine({ ok, label }: { ok: boolean; label: string }) {
  return (
    <p className={ok ? "text-emerald-500" : "text-amber-500"}>
      {ok ? "✓" : "•"} {label}
    </p>
  );
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
          <div className="text-[13px] font-semibold" title={note}>{title}</div>
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
