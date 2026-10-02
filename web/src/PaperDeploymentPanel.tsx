/**
 * Paper deployment and monitoring.
 *
 * The screen for the loop the whole system exists to close:
 *
 * Strategy → Version → Deploy Paper → Live market data → Signal → Risk →
 * OMS → Fill → Position → P&L
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
import { useLiveTicks } from "./lib/useLiveTicks";
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
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { Tooltip } from "./components/motion/tooltip";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Stat, Badge, Callout, fmtMoney } from "./components/ui/stat";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Input } from "./components/motion/input";
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
import { useSmoothScroll } from "./components/motion/smooth-scroll";

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 disabled:cursor-not-allowed disabled:opacity-50 [&>option]:bg-card";

/** How often the monitor re-reads while the tab is open. One second is far
 * finer than the daily rules can distinguish, and the overview is one request. */
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

  // Options are static for the session, load once on mount
  useEffect(() => {
    backtestOptions()
      .then(setOptions)
      .catch(() => null);
  }, []);

  const loadList = useCallback(async () => {
    try {
      const [list, run] = await Promise.all([
        listDeployments(),
        getRunnerStatus().catch(() => null),
      ]);
      setDeployments(list.deployments);
      setRunner(run);
      setError(null);
      setSelectedId((prev) => {
        if (prev && list.deployments.some((d) => d.deployment_id === prev)) {
          return prev;
        }
        return list.deployments[0]?.deployment_id ?? null;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  // The evidence counter rebuilds the learning dataset on the server, about half a minute of
  // work. It is fetched once a minute, one request at a time, and never waited for, so it
  // cannot hold the page back or pile up work behind the paper runner.
  useEffect(() => {
    let inFlight = false;
    const load = () => {
      if (inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      // The server rebuilds the learning dataset per call (~30s cold). Bound
      // the wait so one slow response cannot wedge `inFlight` and silence
      // every later poll — a timeout settles the promise and retries resume.
      const ctrl = new AbortController();
      const killer = setTimeout(() => ctrl.abort(), 25000);
      getForwardEvidenceCounts(undefined, undefined, ctrl.signal)
        .then(setEvidenceCounts)
        .catch(() => undefined)
        .finally(() => {
          clearTimeout(killer);
          inFlight = false;
        });
    };
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);

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
    let pollCount = 0;
    const t = setVisibleInterval(() => {
      void loadOverview(selectedId);
      pollCount++;
      // Only refresh the deployment list every 3rd cycle (12s) to prevent backend thrashing
      if (pollCount % 3 === 0) {
      void loadList();
      } else {
      void getRunnerStatus()
        .then(setRunner)
        .catch(() => undefined);
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [selectedId, loadOverview, loadList]);

  // Only saved strategies list a version, and a deployment must pin one.
  const deployable = useMemo(
    () => (options?.strategies ?? []).filter((s) => s.kind === "saved"),
    [options],
  );
  // strategy_id -> its saved name, so a deployment picker can read "Triple RSI"
  // instead of a hex id nobody can tell apart from any other hex id.
  const strategyNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const s of options?.strategies ?? []) {
      if (s.strategy_id) map[s.strategy_id] = s.name;
    }
    return map;
  }, [options]);
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
        <Tabs value={view} onValueChange={(v) => setView(v as View)} variant="pill">
          <TabsList>
            <TabsTrigger value="deploy">New deployment</TabsTrigger>
            <TabsTrigger value="monitor">Monitor</TabsTrigger>
            <TabsTrigger value="compare">Compare</TabsTrigger>
          </TabsList>
        </Tabs>

        {/* The runner is a platform process, not a user's resource, so its state
            is shown on every view: "no signals fired" and "nothing is watching
            for signals" look identical from the timeline alone. */}
            <Tooltip content={runner?.running ? "The paper runner is watching for signals." : "The paper runner is not running, so nothing will trade."} side="bottom" delay={400}>
        <div
          className="flex items-center gap-2 text-xs text-muted-foreground"
        >
        <span className={cn("h-1.5 w-1.5 rounded-full ring-2 ring-gain/20", runner?.running ? "bg-gain" : "bg-muted-foreground/40")} />
          {runner?.running ? "Runner on" : "Runner off"}
          <span className="text-border">·</span>
          {runner?.in_market_hours ? "Market open" : "Market closed"}
        </div>
        </Tooltip>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      {view === "compare" ? (
        <CompareView
          strategies={[...cmpRunningByStrategy.keys()]}
          strategyNames={strategyNames}
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
          strategyNames={strategyNames}
        />
      ) : (
        <MonitorView
          deployments={deployments}
          strategyNames={strategyNames}
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
          <div className="rounded-lg border border-border/70 bg-primary/[0.02] p-4 text-sm">
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
                <p className="font-mono text-sm font-semibold text-gain">
                  {preview.final_quantity} shares
                  {preview.raw_quantity !== preview.final_quantity && (
                    <span className="ml-1 text-caption text-muted-foreground line-through">
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
              <div className="mt-2.5 rounded-md border border-destructive/20 bg-destructive/[0.08] px-3 py-1.5 text-xs text-loss">
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
  strategyNames,
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
  strategyNames: Record<string, string>;
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
                    <span className="pr-3.5 text-caption font-semibold text-muted-foreground">
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
                hint={
                  timeframe === "1d"
                    ? "Evaluates on daily close / live forming candle."
                    : `Intraday: aggregates real-time ticks into ${timeframe} bars.`
                }
              >
                <Select
                  value={timeframe}
                  onChange={onTimeframe}
                  options={[
                    { value: "1d", label: "1d (Daily)" },
                    { value: "1h", label: "1h (Hourly)" },
                    { value: "15m", label: "15m (15 Minutes)" },
                    { value: "5m", label: "5m (5 Minutes)" },
                    { value: "1m", label: "1m (1 Minute)" },
                  ]}
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
        <Card className="overflow-hidden">
          <div className="relative z-10 bg-card">
          <CardHeader title="Existing deployments" sub={undefined} />
            <div className="h-3" />
          </div>
          <div data-lenis-prevent className="max-h-[420px] space-y-2 overflow-y-auto px-4 pb-4 pt-1 [scrollbar-width:thin] [mask-image:linear-gradient(to_bottom,transparent,black_14px,black_calc(100%_-_14px),transparent)] [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-button]:hidden">
            {deployments.length === 0 ? (
              <Hint>None yet. Everything you deploy appears here.</Hint>
            ) : (
              deployments.map((d) => (
                <button
                  key={d.deployment_id}
                  type="button"
                  onClick={() => onOpen(d.deployment_id)}
                  className="w-full rounded-xl border border-border/70 bg-card/40 px-3.5 py-3 text-left transition-all hover:border-border/90 hover:bg-muted/30 shadow-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[12.5px] font-semibold">
                    {strategyNames[d.strategy_id] ?? d.strategy_id.slice(0, 8)} · v{d.strategy_version}
                    </span>
                    <StatusPill status={d.status} />
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 text-caption text-muted-foreground">
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
  strategyNames,
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
  strategyNames: Record<string, string>;
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
  const [details, setDetails] = useState(true);

  // `overview` (and the "Latest signals" block it unlocks) arrives on a poll
  // well after the page's first paint. Lenis measures scrollable height once
  // at mount via ResizeObserver on its own wrapper, which does not always
  // catch content added to a *sibling* card below the fold — the symptom is
  // exactly this: the page appears to stop scrolling short of the real
  // bottom. Nudging it to remeasure whenever the data that grows the page
  // changes is cheap and correct even when it turns out to be a no-op.
  const { lenis } = useSmoothScroll();
  useEffect(() => {
    lenis?.resize();
  }, [lenis, overview]);

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
      {/* 1. The prominent Forward Evidence Counter */}
      <ForwardEvidenceCounterCard
        counts={evidenceCounts}
        strategyNames={strategyNames}
        selectedStrategyId={status?.strategy_id}
        selectedVersion={status?.strategy_version}
      />

      <PaperRuns
        deployments={deployments}
        onManage={(id) => {
          onSelect(id);
          setDetails(true);
        }}
      />
      <Button
        size="inline"
        variant="link"
        className="text-muted-foreground hover:text-foreground hover:no-underline text-sm"
        onClick={() => setDetails((v) => !v)}
      >
        {details ? "Hide details and controls" : "Details and controls"}
      </Button>
      {details && (<>
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
                    label: `${strategyNames[d.strategy_id] ?? d.deployment_id.slice(0, 8)} · v${d.strategy_version} · ${d.status} · ${fmtMoney(d.capital)}`,
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
                <div className="mb-2 text-body font-semibold">
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
                    className={cn(selectClass, "flex-1 min-w-[240px]")}
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
          {/* the header: running vs trading, and why not */}
      {status && (
            <Card className="relative overflow-hidden border-border/80 transition-all duration-300">
              {/* Subtle live radar ping in card background when running */}
              {status.status === "RUNNING" && (
                <div className="pointer-events-none absolute -right-20 -top-20 h-64 w-64 rounded-full bg-gain/[0.04] blur-2xl animate-pulse" />
              )}

              <div className="relative z-10 flex flex-wrap items-start justify-between gap-4 p-5">
            <div className="min-w-0">
                  <div className="mb-1.5 truncate text-base font-semibold text-foreground flex items-center gap-2">
                    <span>{strategyNames[status.strategy_id] ?? "Unnamed strategy"}</span>
                    {status.status === "RUNNING" && (
                      <span className="flex h-2 w-2 relative">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-gain opacity-75" />
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-gain" />
                      </span>
                    )}
                  </div>
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill
                  status={status.status}
                  trading={status.trading}
                  diagnosticState={status.diagnostic_state}
                />
                <TradingPill trading={status.trading} reason={status.not_trading_because} />
                <Badge tone="flat" className="border-border/60 bg-muted/40">{status.mode}</Badge>
                <Badge tone="flat" className="border-border/60 bg-muted/40">v{status.strategy_version}</Badge>
                    <span className="font-mono text-caption text-muted-foreground bg-muted/30 px-1.5 py-0.5 rounded-md">
                  {status.deployment_id.slice(0, 8)}
                </span>
              </div>
                  <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11.5px] text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <span className="h-1 w-1 rounded-full bg-muted-foreground/60" />
                      {status.symbols.length} symbol(s)
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1 w-1 rounded-full bg-muted-foreground/60" />
                      {status.exchange}
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1 w-1 rounded-full bg-muted-foreground/60" />
                      {status.timeframe}
                    </span>
                    <span className="flex items-center gap-1">
                    <span className={cn("h-1.5 w-1.5 rounded-full", status.in_market_hours ? "bg-gain animate-pulse" : "bg-warning")} />
                  session {status.in_market_hours ? "open" : "closed"}
                </span>
                    <span className="flex items-center gap-1">
                    <span className={cn("h-1.5 w-1.5 rounded-full", status.loop_attached ? "bg-gain animate-pulse" : "bg-muted-foreground/40")} />
                  loop {status.loop_attached ? "attached" : "not attached"}
                </span>
                {status.started_at && <span>started {istDate(status.started_at)}</span>}
              </div>
            </div>
          </div>

          {/* The honesty block */}
          {(!status.trading || idle) && (
            <div className="px-5 pb-5">
                  <div className="relative overflow-hidden rounded-xl border border-border/80 bg-muted/[0.08] p-3.5 ">
                    <div className="relative z-10 flex items-start gap-3">
                      <div className="mt-0.5 relative flex h-4 w-4 shrink-0">
                        <span className="relative inline-flex h-4 w-4 rounded-full border border-border/80 bg-muted/30 text-muted-foreground grid place-items-center text-micro font-semibold">
                          !
                        </span>
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="text-body font-semibold text-foreground flex items-center gap-2">
                          <span>
                            {status.blocked_reason
                    ? "Not trading — rules could not be resolved"
                    : status.diagnostic_state === "stale_tick"
                    ? "Not trading — stale price feed"
                    : status.diagnostic_state === "no_live_tick"
                                  ? "Listening for ticks"
                    : status.diagnostic_state === "market_closed"
                                    ? "Cash market session is closed"
                                    : "Not trading"}
                          </span>
                          <span className="inline-flex items-center rounded-lg border border-border/60 bg-muted/30 px-2 py-0.5 text-micro font-medium text-muted-foreground">
                            Scanning
                          </span>
                        </div>
                        <div className="mt-1 text-xs text-muted-foreground leading-relaxed">
                {status.not_trading_because || idle || status.skipped_reason}
                        </div>
                        {status.diagnostic_state === "no_live_tick" && (
                          <div className="mt-1.5 flex items-center gap-1.5 text-caption text-muted-foreground/80">
                            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
                            <span>Runner active on background loop. Fires immediately upon arrival of next tick stream.</span>
                          </div>
                )}
                      </div>
                    </div>
                  </div>
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
                    <span className="text-warning">
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
            <Card className="flex h-full flex-col">
          <CardHeader
            title="Signal → Risk → Order → Fill → Position"
            sub="Every step in the causal chain, newest first within each stage."
          />
              <div className="flex min-h-0 flex-1 flex-col p-5 pt-4">
                <div className="mb-4 flex flex-wrap items-stretch gap-2">
              {STAGES.map((stage, i) => {
                const link = chain.find((c) => c.stage === stage);
                const hit = reached.has(stage);
                return (
                      <div key={stage} className="flex items-center gap-1.5 sm:gap-2">
                    <div
                      title={STAGE_BLURB[stage]}
                      className={cn(
                            "relative overflow-hidden rounded-xl border px-3 py-2 transition-all duration-300",
                        hit
                              ? "border-border/90 bg-card hover:border-foreground/20"
                              : "border-border/40 bg-muted/10 opacity-50",
                      )}
                    >
                          <div className="flex items-center gap-2">
                            <span className="relative flex h-2 w-2">
                            {hit && <span className="absolute inline-flex h-full w-full animate-ping-slow rounded-full bg-foreground/30 opacity-75" />}
                        <span
                          className={cn(
                                  "relative inline-flex h-2 w-2 rounded-full",
                                  hit ? "bg-foreground/80" : "bg-muted-foreground/30",
                          )}
                        />
                            </span>
                        <span
                          className={cn(
                                "text-[10.5px] font-semibold uppercase tracking-[0.06em]",
                            hit ? "text-foreground" : "text-muted-foreground",
                          )}
                        >
                          {STAGE_LABEL[stage]}
                        </span>
                      </div>
                          <div className="mt-1 text-micro font-mono tabular-nums text-muted-foreground">
                        {link ? istClock(link.ts) : "—"}
                        {link && link.count > 1 ? ` · ${link.count}` : ""}
                      </div>
                    </div>
                    {i < STAGES.length - 1 && (
                          <div className="relative flex items-center justify-center px-0.5">
                      <ArrowRight
                        className={cn(
                                "size-3.5 shrink-0 transition-colors",
                                hit ? "text-foreground/40" : "text-muted-foreground/20",
                        )}
                      />
                          </div>
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
                  <div
                    data-lenis-prevent
                    className="min-h-[220px] flex-1 space-y-1.5 overflow-y-auto pr-1"
                  >
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
                    <div className="space-y-2">
                      {positions.map((p) => {
                        const isProfit = (p.unrealized_pnl ?? 0) >= 0;
                        return (
                    <div
                      key={p.symbol}
                            className={cn(
                              "flex items-center justify-between gap-3 rounded-md border p-3 transition-all duration-200 hover:scale-[1.01]",
                              isProfit
                                ? "border-gain/25 bg-gradient-to-r from-gain/[0.04] to-card/60 -[0_0_12px_rgba(16,185,129,0.03)]"
                                : "border-loss/25 bg-gradient-to-r from-loss/[0.04] to-card/60 -[0_0_12px_rgba(244,63,94,0.03)]"
                            )}
                    >
                      <div className="min-w-0">
                              <div className="flex items-center gap-2">
                              <span className="font-extrabold text-body tracking-tight text-foreground">{p.symbol}</span>
                                <span className="rounded-md bg-muted/60 px-1.5 py-0.2 font-mono text-micro font-semibold text-muted-foreground">
                                  {p.quantity} qty
                                </span>
                              </div>
                              <div className="mt-1 text-caption text-muted-foreground tabular-nums flex items-center gap-2 flex-wrap">
                              <span>Avg <strong className="font-semibold text-foreground/90">{fmtMoney(p.avg_price, 2)}</strong></span>
                                <span className="text-border">·</span>
                                {p.priced && p.last_price !== null ? (
                                  <span>LTP <strong className="font-semibold text-foreground/90">{fmtMoney(p.last_price, 2)}</strong></span>
                                ) : (
                                  <span className="text-warning font-medium">Unpriced</span>
                          )}
                        </div>
                      </div>
                      <div className="text-right">
                        <div
                          className={cn(
                                  "text-body font-black tabular-nums tracking-tight",
                            p.unrealized_pnl === null
                              ? "text-muted-foreground"
                                    : isProfit
                                      ? "text-gain"
                                      : "text-loss",
                          )}
                        >
                                {p.unrealized_pnl !== null && p.unrealized_pnl > 0 ? "+" : ""}
                                {fmtMoneyOrDash(p.unrealized_pnl, 2)}
                        </div>
                              <div className="text-micro font-semibold uppercase tracking-wider text-muted-foreground/80 mt-0.5">
                          {p.priced ? "unrealised" : "unpriced"}
                        </div>
                      </div>
                    </div>
                        );
                      })}
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
                  <div className="space-y-2 p-4 pt-3 text-xs">
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
                  <div className="grid grid-cols-2 gap-2 p-4 pt-3 text-xs">
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
                      <div className="mb-2 text-caption font-semibold uppercase tracking-[0.05em] text-muted-foreground">
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
          tone === "good" && "border border-gain/20 bg-gain/[0.08] text-gain",
          tone === "warn" && "border border-warning/20 bg-warning/[0.08] text-warning",
          tone === "flat" && "bg-muted text-muted-foreground",
        )}
      >
        <Icon className="size-3" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-micro font-semibold uppercase tracking-[0.05em] text-muted-foreground">
            {STAGE_LABEL[event.stage as TimelineStage] ?? event.stage}
          </span>
          {event.side && (
            <span
              className={cn(
                "rounded-md px-1.5 py-[1px] text-[9.5px] font-semibold uppercase tracking-wider",
                event.side === "SELL"
                  ? "border border-destructive/20 bg-destructive/[0.08] text-destructive"
                  : "border border-gain/20 bg-gain/[0.08] text-gain",
              )}
            >
              {event.side}
            </span>
          )}
          <span className="text-[11.5px] text-foreground">{event.summary}</span>
        </div>
        {event.reason && event.reason !== event.summary && (
          <div className="mt-0.5 text-caption text-muted-foreground">{event.reason}</div>
        )}
      </div>
      <span className="shrink-0 text-[10.5px] tabular-nums text-muted-foreground">
        {istClock(event.ts)}
      </span>
    </div>
  );
}

// ─── small pieces & telemetry cards ──────────────────────────────────────────

function EvCell({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub: string; tone?: "muted" }) {
  return (
    <div className="rounded-xl border border-border/70 bg-card/60 p-3.5">
    <div className="text-caption font-medium uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
    <div className={cn("mt-1 text-3xl font-semibold tabular-nums tracking-tight leading-none", tone === "muted" ? "text-muted-foreground" : "text-foreground")}>{value}</div>
      <div className="mt-0.5 text-[10.5px] text-muted-foreground">{sub}</div>
    </div>
  );
}
function ForwardEvidenceCounterCard({
  counts,
  strategyNames,
  selectedStrategyId,
  selectedVersion,
}: {
  counts: ForwardEvidenceCounts | null;
  strategyNames: Record<string, string>;
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
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-4 p-5 pb-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="size-2 rounded-full bg-gain" />
            <h3 className="text-base font-semibold tracking-tight text-foreground">
              Forward track record
            </h3>
            <Badge tone="good" className="text-[10.5px]">Live</Badge>
          </div>
        </div>

        {counts?.as_of && (
          <div className="flex items-center gap-2 rounded-lg border border-border/50 bg-muted/20 px-2.5 py-1 text-caption text-muted-foreground">
            Live sync: {istClock(counts.as_of)} IST
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 px-5 pb-5 sm:grid-cols-3 lg:grid-cols-6">
        <EvCell label="Total" value={totalForward} sub="all-time" />
        <EvCell label="Today" value={todayForward} sub="today" />
        <EvCell label="Last 7 days" value={last7dForward} sub="rolling 7 days" />
        <EvCell label="Paper" value={paperForward} sub={`${counts?.by_class?.PAPER_FORWARD?.today ?? 0} today · ${counts?.by_class?.PAPER_FORWARD?.last_7d ?? 0} 7d`} />
        <EvCell label="Live" value={liveForward} sub={`${counts?.by_class?.LIVE_FORWARD?.today ?? 0} today · ${counts?.by_class?.LIVE_FORWARD?.last_7d ?? 0} 7d`} />
        <EvCell label="Backtest" value={inSample} sub="historical tests" tone="muted" />
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
            <span className="text-caption underline">
              {showTable ? "Hide details" : "Show details"}
            </span>
          </button>

          {showTable && (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-[11.5px]">
                <thead>
                  <tr className="border-b border-border/50 text-[10.5px] uppercase tracking-wider text-muted-foreground">
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Strategy</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Version</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Genuine Forward</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Today</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Last 7d</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Paper Forward</th>
                    <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">In-Sample</th>
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
                        <td className="px-3 py-1.5 text-[11.5px]">
                          {strategyNames[s.strategy_id] ?? (
                            <span className="font-mono text-caption text-muted-foreground">
                          {s.strategy_id.slice(0, 12)}
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-1.5">
                          {s.strategy_version !== null ? `v${s.strategy_version}` : "—"}
                        </td>
                        <td className="px-3 py-1.5 text-right font-semibold text-gain tabular-nums">
                          {s.total_genuine_forward}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{s.today_genuine_forward}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{s.last_7d_genuine_forward}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{s.paper_forward}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
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
  const { ticks: wsTicks, connected: wsConnected } = useLiveTicks(status.symbols);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  // Sub-second precision ticker for real-time age calculation (100ms interval)
  useEffect(() => {
    const timer = setVisibleInterval(() => {
      setNowMs(Date.now());
    }, 100);
    return () => clearInterval(timer);
  }, []);

  // Compute live sub-second tick age across active websocket stream ticks
  let liveTickAgeSec: number | null = null;
  let liveTickTimeStr = status.last_tick_time ? istClock(status.last_tick_time) : "—";
  let latestEpoch = 0;

  for (const tick of Object.values(wsTicks)) {
    if (tick && tick.epoch) {
      if (tick.epoch > latestEpoch) {
        latestEpoch = tick.epoch;
        if (tick.ts) {
          liveTickTimeStr = istClock(tick.ts);
        }
      }
    }
  }

  if (latestEpoch > 0) {
    liveTickAgeSec = Math.max(0, (nowMs / 1000) - latestEpoch);
  } else if (status.last_tick_age_seconds !== null && status.last_tick_age_seconds !== undefined) {
    liveTickAgeSec = status.last_tick_age_seconds;
  }

  const tickTime = liveTickTimeStr;
  const tickAge = liveTickAgeSec;
  const evalTime = status.last_strategy_evaluation ? istClock(status.last_strategy_evaluation) : "—";
  const isStale = tickAge !== null && tickAge !== undefined && tickAge > 60;
  const isSubSecond = tickAge !== null && tickAge < 1.0;

  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <div className="relative overflow-hidden rounded-xl border border-border/70 bg-card p-3 transition-all hover:border-border">
        <div className="text-caption font-medium uppercase tracking-wider text-muted-foreground flex items-center justify-between">
          <span>Market Status</span>
          <span className={cn("h-1.5 w-1.5 rounded-full", status.in_market_hours ? "bg-gain animate-ping" : "bg-warning/80")} />
        </div>
        <div className="mt-1 flex items-center gap-1.5 font-semibold">
        <span className={cn("h-2 w-2 rounded-full", status.in_market_hours ? "bg-gain -[0_0_8px_rgba(16,185,129,0.8)]" : "bg-warning")} />
          <span className="text-sm">
            {status.in_market_hours ? "Regular Session Open" : "Market Closed"}
          </span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          NSE/BSE 09:15 to 15:30 IST
        </div>
      </div>

      <div className="relative overflow-hidden rounded-xl border border-border/70 bg-card p-3 transition-all hover:border-border">
        <div className="text-caption font-medium uppercase tracking-wider text-muted-foreground flex items-center justify-between">
          <span className="flex items-center gap-1.5">
            <span>Live Ticks</span>
            {wsConnected && (
              <span className="inline-flex items-center rounded-md bg-muted/40 border border-border/60 px-1 py-0.2 text-micro font-mono font-medium text-foreground">
                WS
              </span>
            )}
          </span>
          <span className={cn("h-1.5 w-1.5 rounded-full", isStale || tickAge === null ? "bg-muted-foreground/40" : "bg-foreground/70 animate-ping-slow")} />
        </div>
        <div className="mt-1 flex items-center gap-1.5 font-semibold">
        <span className={cn("h-2 w-2 rounded-full", isStale || tickAge === null ? "bg-muted-foreground/40" : "bg-foreground/80")} />
        <span className={cn("text-sm tabular-nums", isStale && "text-muted-foreground", isSubSecond && "text-foreground font-semibold")}>
            {tickAge !== null && tickAge !== undefined
              ? tickAge < 1.0
                ? `${Math.round(tickAge * 1000)}ms ago`
                : `${tickAge.toFixed(1)}s ago`
              : "No ticks"}
          </span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Last tick at {tickTime} IST
        </div>
      </div>

      <div className="relative overflow-hidden rounded-xl border border-border/70 bg-card p-3 transition-all hover:border-border">
        <div className="text-caption font-medium uppercase tracking-wider text-muted-foreground flex items-center justify-between">
          <span>Engine Eval</span>
          <span className="h-1.5 w-1.5 rounded-full bg-primary/60 animate-pulse" />
        </div>
        <div className="mt-1 text-sm font-semibold text-foreground">
          {evalTime} IST
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Evaluates once per second in session
        </div>
      </div>

      <div className="rounded-lg border border-border/60 bg-card p-3">
        <div className="text-caption font-medium uppercase tracking-wider text-muted-foreground">
          Skipped Cycles
        </div>
        <div className="mt-1 flex items-center gap-2 text-sm font-semibold tabular-nums text-foreground">
          <span>{status.skipped_evaluations_count ?? 0} eval</span>
          <span className="text-muted-foreground">/</span>
          <span>{status.skipped_fills_count ?? 0} fills</span>
        </div>
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">
          Recorded skip events
        </div>
      </div>

      <div className="rounded-lg border border-border/60 bg-card p-3">
        <div className="text-caption font-medium uppercase tracking-wider text-muted-foreground">
          Genuine Trades
        </div>
        <div className="mt-1 text-sm font-semibold tabular-nums text-foreground">
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

  // Determine active symbol in transit across the causal pipeline
  const activeSymbol = fill?.symbol || order?.symbol || risk?.symbol || signal?.symbol || null;

  return (
    <Card className="relative overflow-hidden border-border/70 bg-card ">
      {/* Header bar with subtle active trade transit tracker */}
      <div className="flex flex-wrap items-center justify-between border-b border-border/50 px-5 py-3">
        <div className="flex items-center gap-2.5">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping-slow rounded-full bg-foreground/40 opacity-70" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/80" />
          </span>
          <h3 className="text-sm font-semibold tracking-tight text-foreground">
            Causal Execution Pipeline
          </h3>
          <span className="rounded-md border border-border/80 bg-muted/30 px-2 py-0.5 text-micro font-medium text-muted-foreground">
            Sequential Bucket Flow
          </span>
          </div>

        {activeSymbol ? (
          <div className="flex items-center gap-2 text-caption text-muted-foreground">
            <span className="text-micro uppercase tracking-wider">Active Transit:</span>
            <span className="rounded-lg border border-border/80 bg-muted/20 px-2 py-0.5 font-mono text-caption font-semibold text-foreground">
              {activeSymbol}
            </span>
            </div>
          ) : (
          <div className="text-caption text-muted-foreground">
            Evaluating ticks across active universe
            </div>
          )}
        </div>

      {/* Subtle Pipeline Motion Transit Channel */}
      <div className="relative border-b border-border/40 bg-muted/[0.04] px-5 py-2">
        <div className="relative h-1 w-full overflow-hidden rounded-full bg-muted/40">
          <div className="animate-dot-travel absolute top-0 h-full w-24 -translate-x-full rounded-full bg-gradient-to-r from-transparent via-foreground/30 to-transparent" />
        </div>
        <div className="mt-1 flex items-center justify-between text-[9.5px] font-mono uppercase tracking-wider text-muted-foreground/70">
          <span className={cn(signal ? "text-foreground font-semibold" : "")}>Stage 1: Signal</span>
          <span className={cn(risk ? "text-foreground font-semibold" : "")}>Stage 2: Risk Gate</span>
          <span className={cn(order ? "text-foreground font-semibold" : "")}>Stage 3: OMS Order</span>
          <span className={cn(fill ? "text-foreground font-semibold" : "")}>Stage 4: Venue Fill</span>
        </div>
      </div>

      {/* 4 Execution Buckets */}
      <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4 relative">
        {/* Bucket 1: Strategy Signal */}
        <div className={cn(
            "group relative flex flex-col justify-between rounded-md border p-3.5 transition-all duration-300",
            signal
            ? "border-border/90 bg-card/90 "
            : "border-border/40 bg-muted/[0.06] opacity-60"
          )}>
          <div>
            <div className="flex items-center justify-between text-caption font-medium tracking-wide">
              <span className="text-muted-foreground flex items-center gap-1.5">
              <span className="size-4 grid place-items-center rounded-full border border-border/80 bg-muted/30 text-micro font-mono text-muted-foreground">1</span>
                <span className="font-semibold text-foreground/90">Strategy Signal</span>
              </span>
              <Badge tone="flat" className="text-micro px-1.5 py-0 border border-border/60">
                {signal ? "Emitted" : "Idle"}
              </Badge>
          </div>

            {signal ? (
              <div className="mt-3 space-y-1.5 text-xs">
                <div className="flex items-center gap-2">
                  <span className="rounded-lg border border-border px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide text-foreground bg-muted/40">
                    {signal.side}
                  </span>
                  <span className="font-semibold text-body tracking-tight text-foreground">{signal.symbol}</span>
              </div>
                <div className="text-caption text-muted-foreground">
                Rule: <span className="font-mono text-foreground/80">{signal.rule || "reversion"}</span>
              </div>
                {signal.reason && (
                  <div className="rounded-lg border border-border/50 bg-muted/[0.12] p-2 text-[10.5px] text-muted-foreground leading-snug">
                    {signal.reason}
                </div>
              )}
            </div>
          ) : (
              <div className="mt-3 text-caption text-muted-foreground/70 leading-relaxed">
                Scanning universe for entry conditions...
            </div>
          )}
        </div>

          {signal?.at && (
            <div className="mt-3 pt-2 border-t border-border/30 text-micro text-muted-foreground/60 font-mono">
              {istClock(signal.at)} IST
            </div>
            )}
          </div>

        {/* Bucket 2: Risk Gate */}
        <div className={cn(
            "group relative flex flex-col justify-between rounded-md border p-3.5 transition-all duration-300",
            risk
            ? "border-border/90 bg-card/90 "
            : "border-border/40 bg-muted/[0.06] opacity-60"
          )}>
          <div>
            <div className="flex items-center justify-between text-caption font-medium tracking-wide">
              <span className="text-muted-foreground flex items-center gap-1.5">
              <span className="size-4 grid place-items-center rounded-full border border-border/80 bg-muted/30 text-micro font-mono text-muted-foreground">2</span>
                <span className="font-semibold text-foreground/90">Risk Gate</span>
              </span>
              <Badge tone={risk ? (risk.approved ? "flat" : "bad") : "flat"} className="text-micro px-1.5 py-0 border border-border/60">
                {risk ? (risk.approved ? "Approved" : "Rejected") : "Awaiting"}
              </Badge>
            </div>

            {risk ? (
              <div className="mt-3 space-y-1.5 text-xs">
                <div className="text-xs font-semibold text-foreground">
                  {risk.approved ? "Passed Risk Check" : "Refused by Guard"}
                </div>
                <div className="rounded-lg border border-border/50 bg-muted/[0.12] p-2 text-[10.5px] text-muted-foreground leading-snug">
                  {risk.reason}
                </div>
                {risk.symbol && (
                  <div className="text-caption text-muted-foreground">
                    {risk.symbol} {risk.quantity ? `· ${risk.quantity} qty` : ""}
                  </div>
                )}
              </div>
            ) : (
              <div className="mt-3 text-caption text-muted-foreground/70 leading-relaxed">
                Awaiting trade call for limits and exposure check...
              </div>
            )}
          </div>

          {risk?.at && (
            <div className="mt-3 pt-2 border-t border-border/30 text-micro text-muted-foreground/60 font-mono">
              {istClock(risk.at)} IST
            </div>
          )}
        </div>

        {/* Bucket 3: OMS Order */}
        <div className={cn(
            "group relative flex flex-col justify-between rounded-md border p-3.5 transition-all duration-300",
            order
            ? "border-border/90 bg-card/90 "
            : "border-border/40 bg-muted/[0.06] opacity-60"
          )}>
          <div>
            <div className="flex items-center justify-between text-caption font-medium tracking-wide">
              <span className="text-muted-foreground flex items-center gap-1.5">
              <span className="size-4 grid place-items-center rounded-full border border-border/80 bg-muted/30 text-micro font-mono text-muted-foreground">3</span>
                <span className="font-semibold text-foreground/90">OMS Order</span>
              </span>
              <Badge tone={order ? (order.status === "FILLED" ? "flat" : order.status === "REJECTED" ? "bad" : "flat") : "flat"} className="text-micro px-1.5 py-0 border border-border/60">
                {order ? order.status : "Idle"}
              </Badge>
            </div>

          {order ? (
              <div className="mt-3 space-y-1.5 text-xs">
                <div className="font-semibold text-body text-foreground tracking-tight">
                {order.side} {order.symbol} <span className="font-mono text-muted-foreground text-caption">x{order.quantity}</span>
              </div>
              {order.order_id && (
                  <div className="font-mono text-micro text-muted-foreground/80">
                  ID: <span className="text-foreground/80 font-medium">{order.order_id.slice(0, 10)}</span>
                </div>
              )}
              {order.reason && (
                  <div className="rounded-lg border border-border/50 bg-muted/[0.12] p-2 text-[10.5px] text-muted-foreground leading-snug">
                  {order.reason}
                </div>
              )}
            </div>
          ) : (
              <div className="mt-3 text-caption text-muted-foreground/70 leading-relaxed">
                Awaiting risk release to build broker order packet...
            </div>
          )}
        </div>

          <div className="mt-3 pt-2 border-t border-border/30 text-micro text-muted-foreground/60 font-mono">
            {order ? "Dispatched" : "—"}
          </div>
        </div>

        {/* Bucket 4: Paper Fill */}
        <div className={cn(
            "group relative flex flex-col justify-between rounded-md border p-3.5 transition-all duration-300",
            fill
            ? "border-border/90 bg-card/90 "
            : "border-border/40 bg-muted/[0.06] opacity-60"
          )}>
          <div>
            <div className="flex items-center justify-between text-caption font-medium tracking-wide">
              <span className="text-muted-foreground flex items-center gap-1.5">
              <span className="size-4 grid place-items-center rounded-full border border-border/80 bg-muted/30 text-micro font-mono text-muted-foreground">4</span>
                <span className="font-semibold text-foreground/90">Paper Fill</span>
              </span>
              <Badge tone="flat" className="text-micro px-1.5 py-0 border border-border/60">
                {fill ? "Matched" : "No Fill"}
              </Badge>
            </div>

          {fill ? (
              <div className="mt-3 space-y-1.5 text-xs">
                <div className="font-semibold text-body text-foreground tracking-tight">
                {fill.side} {fill.symbol} <span className="font-mono text-muted-foreground text-caption">x{fill.quantity}</span>
              </div>
                <div className="inline-flex items-center gap-1.5 rounded-lg border border-border/70 bg-muted/20 px-2 py-0.5 text-[10.5px] font-medium text-foreground/90">
                  <span className="size-1.5 rounded-full bg-foreground/60" />
                  Venue Fill Executed
              </div>
              <div className="text-[10.5px] text-muted-foreground">
              Recorded in journal as <strong className="font-medium text-foreground">PAPER_FORWARD</strong>
              </div>
            </div>
          ) : (
              <div className="mt-3 text-caption text-muted-foreground/70 leading-relaxed">
                Matches against live book quote with slippage model.
            </div>
          )}
          </div>

          <div className="mt-3 pt-2 border-t border-border/30 text-micro text-muted-foreground/60 font-mono">
            {fill ? "Recorded" : "—"}
          </div>
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

  // Refined Stripe-style status pills: subdued tints, calm dots, elegant typography
  switch (diagnosticState) {
    case "market_closed":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2.5 py-0.5 text-caption font-medium text-slate-300">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
          Market closed
        </span>
      );
    case "no_live_tick":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-warning/25 bg-warning/10 px-2.5 py-0.5 text-caption font-medium text-warning">
          <span className="h-1.5 w-1.5 rounded-full bg-warning animate-pulse" />
          Waiting for ticks
        </span>
      );
    case "stale_tick":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-loss/25 bg-loss/10 px-2.5 py-0.5 text-caption font-medium text-loss">
          <span className="h-1.5 w-1.5 rounded-full bg-loss" />
          Stale tick
        </span>
      );
    case "system_error":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-loss/30 bg-loss/10 px-2.5 py-0.5 text-caption font-medium text-loss">
          <span className="h-1.5 w-1.5 rounded-full bg-loss" />
          System error
        </span>
      );
    case "risk_rejected":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-warning/25 bg-warning/10 px-2.5 py-0.5 text-caption font-medium text-warning">
          <span className="h-1.5 w-1.5 rounded-full bg-warning" />
          Risk rejected
        </span>
      );
    case "order_rejected":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-loss/25 bg-loss/10 px-2.5 py-0.5 text-caption font-medium text-loss">
          <span className="h-1.5 w-1.5 rounded-full bg-loss" />
          Order rejected
        </span>
      );
    case "order_waiting_for_fill":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-indigo-500/25 bg-indigo-500/10 px-2.5 py-0.5 text-caption font-medium text-indigo-300">
          <span className="h-1.5 w-1.5 rounded-full bg-indigo-400 animate-pulse" />
          Order pending
        </span>
      );
    case "paper_fill_completed":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-gain/20 bg-gain/10 px-2.5 py-0.5 text-caption font-medium text-gain">
          <span className="h-1.5 w-1.5 rounded-full bg-gain" />
          Fill complete
        </span>
      );
    case "no_signal":
      return (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2.5 py-0.5 text-caption font-medium text-slate-300">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
          Monitoring
        </span>
      );
    case "running":
    default:
      if (trading === false) {
        return (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-warning/25 bg-warning/10 px-2.5 py-0.5 text-caption font-medium text-warning">
            <span className="h-1.5 w-1.5 rounded-full bg-warning" />
            Not trading
          </span>
        );
      }
      return (
        <Tooltip content="Receiving live feed" side="top" delay={400}>
        <span role="img" aria-label="Live feed" className="relative grid size-5 shrink-0 place-items-center">
            <span className="absolute inline-flex size-3 animate-ping rounded-full bg-gain/40" />
            <span className="relative size-1.5 rounded-full bg-gain" />
          </span>
        </Tooltip>
      );
  }
}

function TradingPill({ trading, reason }: { trading: boolean; reason?: string | null }) {
  if (trading) {
  return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-gain/20 bg-gain/15/30 px-2 py-0.5 text-caption font-medium text-gain">
        <span className="h-1.5 w-1.5 rounded-full bg-gain" />
        Trading
      </span>
    );
  }
  return (
    <span title={reason ?? undefined} className="inline-flex">
      <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/60 px-2 py-0.5 text-caption font-medium text-slate-400">
        <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
        Not trading
      </span>
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
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      {sub && <div className="text-[10.5px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

/** Render a risk limit from the loosely-typed limits map.
 *
 * `null` in that map means *unbounded*, which the backend emits by collapsing
 * an `inf` sentinel — `Infinity` is not valid JSON. So `null` prints as
 * "unbounded", never as a number nobody set. */
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
  strategyNames,
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
  strategyNames: Record<string, string>;
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
          title="Compare versions"
          sub="Pick a strategy and two versions to see which is doing better. Then launch the challenger below."
        />
        <div className="flex flex-wrap items-end gap-3 p-5 pt-3">
          <div className="min-w-[220px] flex-1">
            <label className="mb-1.5 block px-1 text-sm font-medium">Strategy</label>
            <Select
              value={cmpStrategyId}
              onChange={onStrategy}
              options={[
                { value: "", label: "Pick a strategy…" },
                ...strategies.map((s) => ({ value: s, label: strategyNames[s] ?? s.slice(0, 12) })),
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
        <div className="mx-5 border-t border-border/60" aria-hidden="true" />
        <div className="flex flex-wrap items-end gap-3 p-5">
          <div className="min-w-[260px] flex-1">
            <label className="mb-1.5 block px-1 text-sm font-medium">Running deployment</label>
            <Select
              value={launchDepId}
              onChange={onLaunchDep}
              options={[
                { value: "", label: "Pick a running PAPER deployment…" },
                ...champions.map((d) => ({
                  value: d.deployment_id,
                  label: `${strategyNames[d.strategy_id] ?? d.strategy_id.slice(0, 8)} v${d.strategy_version} · ${fmtMoneyOrDash(d.capital)}`,
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
              <p className="mt-2 text-xs text-muted-foreground">
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
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border/60 text-left text-micro uppercase tracking-wider text-muted-foreground">
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Figure</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Champion V{comparison.champion_version}</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Challenger V{comparison.challenger_version}</th>
                    <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Δ (challenger − champion)</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.label} className="border-b border-border/40 last:border-0">
                      <td className="px-3 py-1.5 text-muted-foreground">{r.label}</td>
                      <td className="px-3 py-1.5 tabular-nums">{r.label === "Forward trades" ? (r.champ ?? "—") : r.pct ? fmtPctOrDash(r.champ) : cell(r.champ)}</td>
                      <td className="px-3 py-1.5 tabular-nums">{r.label === "Forward trades" ? (r.chall ?? "—") : r.pct ? fmtPctOrDash(r.chall) : cell(r.chall)}</td>
                      <td className="px-3 py-1.5 tabular-nums text-muted-foreground">
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
            <div className="p-5 pt-3 text-xs">
              {comparison.definition_diff.identical ? (
                <p className="text-muted-foreground">The pinned definitions are identical.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full table-fixed">
                    <thead>
                      <tr className="border-b border-border/60 text-left text-micro uppercase tracking-wider text-muted-foreground">
                        <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground w-1/5">Changed</th>
                        <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground w-2/5">Champion V{comparison.champion_version}</th>
                        <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground w-2/5">Challenger V{comparison.challenger_version}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.definition_diff.changes.map((c) => (
                        <tr key={c.parameter} className="border-b border-border/40 font-mono text-caption last:border-0">
                          <td className="px-3 py-1.5 text-body break-words">{c.parameter}</td>
                          <td className="px-3 py-1.5 text-body break-words">{fmtDiffValue(c.champion)}</td>
                          <td className="px-3 py-1.5 text-body break-words">{fmtDiffValue(c.challenger)}</td>
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
            <div className="p-5 pt-3 text-xs">
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
            <div className="space-y-1 p-5 pt-3 text-xs">
              <ParityLine ok={comparison.parity.identical_capital} label="Capital" />
              <ParityLine ok={comparison.parity.identical_config} label="Universe & config" />
              <ParityLine ok={comparison.parity.venue_shared} label="Market data, costs, slippage (shared venue)" />
              {comparison.parity.mismatches.map((m) => (
                <p key={m} className="text-warning">
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
                <div className="space-y-2 p-5 pt-3 text-xs">
                  <p className="text-muted-foreground">
                    Context score bands:{" "}
                    {(summary.score_bands || [])
                      .map((b) => `${b.band} ${b.forward_trades}`)
                      .join(" · ") || "—"}
                      {" "}· {ctx.forward_n} forward with context
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
    <p className={ok ? "text-gain" : "text-warning"}>
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
    <div className="rounded-lg border border-border/60 p-4">
      <div className="mb-3 flex items-start gap-2.5">
        <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-primary/10 text-caption font-semibold">
          {n}
        </span>
        <div className="min-w-0">
        <div className="text-body font-semibold" title={note}>{title}</div>
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
            "px-1 text-caption",
            warn ? "text-warning" : "text-muted-foreground",
          )}
        >
          {hint}
        </span>
      )}
    </div>
  );
}
