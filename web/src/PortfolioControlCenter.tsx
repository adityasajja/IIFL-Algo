import { useCallback, useEffect, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  Coins,
  FlaskConical,
  RefreshCw,
  ShieldOff,
  Sliders,
} from "lucide-react";
import {
  getPortfolioControlCenter,
  getRiskStatus,
  setKillSwitch,
  updatePortfolioPolicy,
  type PortfolioControlCenterData,
  type PortfolioPolicyConfig,
  type RiskStatus,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Select } from "./components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { Tooltip } from "./components/motion/tooltip";
import { Stat } from "./components/ui/stat";
import { AnimatedBadge } from "./components/motion/animated-badge";
import { TiltCard } from "./components/motion/tilt-card";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { useToast } from "./components/ui/toast-context";
import { useDialog } from "./components/ui/dialog-context";
import { cn } from "./lib/utils";
import { formatInr as INR, formatPct as PCT, TYPOGRAPHY } from "./lib/theme";

export function PortfolioControlCenter() {
  const [data, setData] = useState<PortfolioControlCenterData | null>(null);
  const [riskData, setRiskData] = useState<RiskStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const [showConfig, setShowConfig] = useState(false);
  const [statusFilter, setStatusFilter] = useState<"ACTIVE" | "ALL" | "STOPPED">("ACTIVE");
  const [policyForm, setPolicyForm] = useState<Partial<PortfolioPolicyConfig>>({});
  const [policyReason, setPolicyReason] = useState("");
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [killSwitchEngaged, setKillSwitchEngaged] = useState<boolean>(false);
  const [killSwitchBusy, setKillSwitchBusy] = useState(false);
  const { toast } = useToast();
  const dialog = useDialog();

  const load = useCallback(async () => {
    setState("loading");
    try {
      const [res, risk] = await Promise.all([
        getPortfolioControlCenter(),
        getRiskStatus().catch(() => null),
      ]);
      setData(res);
      setPolicyForm(res.policy);
      if (risk) {
        setRiskData(risk);
        setKillSwitchEngaged(Boolean(risk.kill_switch));
      }
      setError(null);
      setState("success");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      setState("error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSavePolicy = async () => {
    if (!policyReason.trim()) {
      toast({
        title: "Reason required",
        description: "Updating portfolio risk limits requires a stated audit reason.",
        status: "error",
      });
      return;
    }
    setSavingPolicy(true);
    try {
      await updatePortfolioPolicy({
        ...policyForm,
        reason: policyReason.trim(),
      });
      toast({
        title: "Policy updated",
        description: "Portfolio risk limits have been updated and are now active.",
        status: "success",
      });
      setShowConfig(false);
      setPolicyReason("");
      void load();
    } catch (err) {
      toast({
        title: "Update failed",
        description: err instanceof Error ? err.message : String(err),
        status: "error",
      });
    } finally {
      setSavingPolicy(false);
    }
  };

  const handleToggleKillSwitch = async () => {
    const engage = !killSwitchEngaged;
    const reason = await dialog.prompt({
      title: engage ? "Engage Emergency Kill Switch?" : "Disengage Emergency Kill Switch?",
      description: engage
        ? "All new order placement across every strategy will be instantly blocked."
        : "Order placement will be resumed across approved strategies.",
      label: "Audit Reason",
      placeholder: "State why this safety change is being made...",
      required: true,
      confirmLabel: engage ? "Halt All Orders" : "Resume Orders",
      tone: engage ? "danger" : "default",
    });
    if (!reason) return;

    setKillSwitchBusy(true);
    try {
      await setKillSwitch(engage, reason.trim());
      setKillSwitchEngaged(engage);
      toast({
        title: engage ? "Emergency Kill Switch Engaged" : "Kill Switch Disengaged",
        description: engage
          ? "All new orders are immediately halted."
          : "Order placement has safely resumed.",
        status: engage ? "neutral" : "success",
      });
      void load();
    } catch (err) {
      toast({
        title: "Kill Switch Error",
        description: err instanceof Error ? err.message : String(err),
        status: "error",
      });
    } finally {
      setKillSwitchBusy(false);
    }
  };

  if (loading && !data) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Hint>Loading Portfolio Control Center…</Hint>
      </div>
    );
  }

  if (error && !data) {
    return <ErrorBox>{error}</ErrorBox>;
  }

  if (!data) return null;

  const { capital, exposure, today, drawdown, concentrations, strategies, open_positions, conflicts, limits } = data;

  return (
    <div className="space-y-6">
      {/* Kill Switch Alert Banner if engaged */}
      {killSwitchEngaged && (
        <div className="flex items-center justify-between rounded-md border border-destructive/50 bg-destructive/15 px-4 py-3 text-destructive dark:text-loss">
          <div className="flex items-center gap-2.5">
            <AlertOctagon className="size-5 shrink-0 text-destructive animate-pulse" />
            <div>
              <p className="text-sm font-semibold">Emergency Kill Switch is ENGAGED</p>
              <p className="text-xs text-muted-foreground">Order placement is blocked across the entire system.</p>
            </div>
          </div>
          <Button
            type="button"
            size="sm"
            variant="primary"
            disabled={killSwitchBusy}
            onClick={() => void handleToggleKillSwitch()}
            className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
          >
            {killSwitchBusy ? "Processing…" : "Disengage Kill Switch"}
          </Button>
        </div>
      )}

      {/* Dual Context Strip: Explicit Paper Portfolio vs Live Broker Margin */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <TiltCard
          max={6}
          className="flex items-center gap-3 border border-gain/30 bg-gain/[0.06] p-3 text-xs"
        >
          <div className="grid size-9 shrink-0 place-items-center rounded-lg border border-gain/20 bg-gain/[0.08] text-gain">
            <FlaskConical size={18} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
            <span className="font-semibold text-foreground">Paper Portfolio (Simulated Book)</span>
            <AnimatedBadge status="success" size="sm">LOCAL PAPER ONLY</AnimatedBadge>
            </div>
            <p className="text-caption text-muted-foreground mt-0.5">
            Simulated capital allocated across automated paper trading strategies. Zero broker fills or money movements.
            </p>
          </div>
        </TiltCard>

        <TiltCard
          max={6}
          className="flex items-center gap-3 border border-border/80 bg-card/60 p-3 text-xs"
        >
          <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
            <Coins size={18} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 justify-between">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-foreground">Real Broker Demat (IIFL)</span>
                <AnimatedBadge status={riskData?.live_orders_allowed ? "warning" : "neutral"} size="sm">
                {riskData?.live_orders_allowed ? "LIVE ORDERS ALLOWED" : "READ ONLY / PAPER GUARD"}
                </AnimatedBadge>
              </div>
            </div>
            <div className="flex items-center justify-between text-caption text-muted-foreground mt-0.5">
              <span>Available Broker Margin:</span>
              <span className="font-semibold tabular-nums text-foreground">
                {riskData?.margin?.AvailableMargin !== undefined
                  ? INR(riskData.margin.AvailableMargin)
                  : riskData?.margin?.OpeningCashLimit !== undefined
                    ? INR(riskData.margin.OpeningCashLimit)
                    : "Connect broker"}
              </span>
            </div>
          </div>
        </TiltCard>
      </div>

      {/* Header & Quick Action */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border/60 pb-4">
        <div>
          <div className="flex items-center gap-2">
          <h2 className={TYPOGRAPHY.h2}>
          Paper Strategy Portfolio Control
            </h2>
            <AnimatedBadge
              status={data.policy_configured ? "success" : "warning"}
              pulse={data.policy_configured}
              size="sm"
            >
              {data.policy_configured ? "Risk Engine Active" : "Uncapped / Pass-Through"}
            </AnimatedBadge>
          </div>
          <p className={TYPOGRAPHY.sub}>
          Paper-level limits, cross-strategy capital allocation, concentration tracking, and conflict governance.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
        <Tooltip content={killSwitchEngaged ? "Disengage Kill Switch" : "Emergency Kill Switch"} side="bottom" delay={400}>
            <Button
            type="button"
              size="sm"
              variant={killSwitchEngaged ? "primary" : "outline"}
              disabled={killSwitchBusy}
              onClick={() => void handleToggleKillSwitch()}
            className={cn(
              killSwitchEngaged && "bg-destructive text-destructive-foreground hover:bg-destructive/90 animate-pulse border-destructive",
              !killSwitchEngaged && "text-destructive border-destructive/30 hover:bg-destructive hover:text-destructive-foreground"
            )}
          >
          {killSwitchEngaged ? <ShieldOff size={13} className="mr-1.5" /> : <AlertOctagon size={13} className="mr-1.5" />}
              {killSwitchEngaged ? "Kill Switch Active" : "Emergency Kill Switch"}
            </Button>
          </Tooltip>

          <Button
            type="button"
            size="sm"
            variant={showConfig ? "primary" : "secondary"}
            onClick={() => setShowConfig(!showConfig)}
          >
            <Sliders size={13} className="mr-1.5" />
            Configure Limits & Rules
          </Button>
          <StatefulButton
            state={state}
            variant="secondary"
            size="sm"
            onClick={() => void load()}
            loadingText="…"
            successText="Done"
            errorText="Retry"
            icon={<RefreshCw size={11} />}
            className="h-8 px-3 text-xs"
          >
            Refresh
          </StatefulButton>
        </div>
      </div>

      {/* Proximity Warnings Banner */}
      {concentrations.warnings && concentrations.warnings.length > 0 && (
        <div className="rounded-md border border border-warning/20 bg-warning/15/10 p-4 text-xs text-warning space-y-1.5">
          <div className="flex items-center gap-2 font-semibold text-warning">
            <AlertTriangle size={15} className="text-warning shrink-0" />
            <span>Concentration & Risk Proximity Warnings</span>
          </div>
          <ul className="list-disc pl-5 space-y-0.5">
            {concentrations.warnings.map((w, idx) => (
              <li key={idx}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Modal / Inline Policy Configuration */}
      {showConfig && (
        <Card className="border-primary/40 bg-card/90 ">
          <CardHeader
            title="Configure Portfolio-Level Limits & Conflict Rules"
            sub="These limits sit ABOVE all individual strategy limits and are strictly enforced before OMS submission."
          />
          <div className="p-5 pt-3 space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <div>
                <label className="text-xs font-semibold text-foreground">Total Portfolio Exposure (₹)</label>
                <input
                  type="number"
                  placeholder="e.g. 1000000"
                  value={policyForm.max_total_exposure ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_total_exposure: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Absolute rupee gross cap</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Max Daily Loss (₹)</label>
                <input
                  type="number"
                  placeholder="e.g. 50000"
                  value={policyForm.max_daily_loss ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_daily_loss: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Portfolio stop-out threshold</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Max Capital Per Strategy (₹)</label>
                <input
                  type="number"
                  placeholder="e.g. 300000"
                  value={policyForm.max_capital_per_strategy ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_capital_per_strategy: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Deployment creation budget cap</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Max Open Positions</label>
                <input
                  type="number"
                  placeholder="e.g. 10"
                  value={policyForm.max_open_positions ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_open_positions: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Max concurrent open symbols</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Max Exposure Per Stock (₹)</label>
                <input
                  type="number"
                  placeholder="e.g. 150000"
                  value={policyForm.max_stock_exposure ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_stock_exposure: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Max concentration per ticker</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Max Sector Exposure (0.01 - 1.0)</label>
                <input
                  type="number"
                  step="0.05"
                  placeholder="e.g. 0.35 (35%)"
                  value={policyForm.max_sector_exposure_pct ?? ""}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      max_sector_exposure_pct: e.target.value ? Number(e.target.value) : null,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Fraction of total capital</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Conflict Resolution Rule</label>
                <Select
                  className="w-full"
                  value={policyForm.conflict_mode ?? "reject"}
                  onChange={(v) =>
                    setPolicyForm({
                      ...policyForm,
                      conflict_mode: v,
                    })
                  }
                  options={[
                    { value: "reject", label: "Reject conflicting order (Conservative)" },
                    { value: "priority", label: "Strategy Priority Allocation (Explicit rank)" },
                    { value: "net", label: "Allow Netting / Opposite positions" },
                  ]}
                />
                <span className="text-[10.5px] text-muted-foreground">Rules when Strategies disagree</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Warning Threshold (Fraction)</label>
                <input
                  type="number"
                  step="0.05"
                  placeholder="0.80"
                  value={policyForm.warn_at_pct_of_limit ?? 0.8}
                  onChange={(e) =>
                    setPolicyForm({
                      ...policyForm,
                      warn_at_pct_of_limit: Number(e.target.value) || 0.8,
                    })
                  }
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Alert when usage reaches % of limit</span>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground">Audit Reason *</label>
                <input
                  type="text"
                  placeholder="e.g. Adjusted risk limits for Q3 market volatility"
                  value={policyReason}
                  onChange={(e) => setPolicyReason(e.target.value)}
                  className="mt-1 h-9 w-full rounded-lg border border-border bg-background px-3 text-xs"
                />
                <span className="text-[10.5px] text-muted-foreground">Mandatory audit trail log</span>
              </div>
            </div>

            <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-border/60">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setShowConfig(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={savingPolicy}
                onClick={() => void handleSavePolicy()}
              >
                {savingPolicy ? "Saving…" : "Save Active Limits"}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* KPI Ribbon Strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <Stat
      label="Paper Capital"
      value={INR(capital.total)}
      sub="Total simulated book"
      />
      <Stat
      label="Deployed Capital"
      value={INR(capital.deployed)}
      sub="Active running arms"
      />
        <Stat
          label="Available Capital"
          value={INR(capital.available)}
          sub="Unallocated capacity"
          tone="good"
        />
        <Stat
          label="Gross Exposure"
          value={INR(exposure.gross)}
          sub={`L: ${INR(exposure.long)} | S: ${INR(exposure.short)}`}
        />
        <Stat
          label="Paper P&L (Today)"
          value={`${today.total >= 0 ? "+" : ""}${INR(today.total)}`}
          sub={`Realized: ${INR(today.realized)}`}
          tone={today.total > 0 ? "good" : today.total < 0 ? "bad" : "neutral"}
        />
        <Stat
          label="Max Drawdown"
          value={drawdown.value !== null ? INR(Math.abs(drawdown.value)) : "—"}
          sub={`Sim cash util: ${PCT(capital.cash_utilization)}`}
          tone="bad"
        />
      </div>

      {/* Strategy Capital Allocation & Objective Metrics Comparison */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 p-4 pb-2 border-b border-border/60">
          <div>
            <h3 className="text-sm font-semibold text-foreground">
              Paper Strategy Allocations & Performance
            </h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              Simulated deployments running on paper capital, ordered by allocated capital.
            </p>
          </div>
          <div>
            <Tabs
              value={statusFilter}
              onValueChange={(v) => setStatusFilter(v as "ACTIVE" | "ALL" | "STOPPED")}
              variant="segment"
            >
              <TabsList>
                <TabsTrigger value="ACTIVE">
                  Active ({strategies.filter((s) => s.deployment_status === "RUNNING").length})
                </TabsTrigger>
                <TabsTrigger value="ALL">
                All ({strategies.length})
                </TabsTrigger>
                <TabsTrigger value="STOPPED">
                  Stopped ({strategies.filter((s) => s.deployment_status !== "RUNNING").length})
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
        </div>

        <div className="overflow-x-auto p-1">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border/60 text-caption text-muted-foreground uppercase font-semibold">
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Strategy / Deployment</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Allocated</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Used</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Available</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Exposure</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Realized P&L</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Total P&L</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Return %</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Trades</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">P&L Share</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {strategies.filter((s) => {
                if (statusFilter === "ACTIVE") return s.deployment_status === "RUNNING";
                if (statusFilter === "STOPPED") return s.deployment_status !== "RUNNING";
                return true;
              }).length === 0 ? (
                <tr>
                  <td colSpan={11} className="p-6 text-center text-muted-foreground">
                    No {statusFilter.toLowerCase()} paper deployments found.
                  </td>
                </tr>
              ) : (
                strategies
                  .filter((s) => {
                    if (statusFilter === "ACTIVE") return s.deployment_status === "RUNNING";
                    if (statusFilter === "STOPPED") return s.deployment_status !== "RUNNING";
                    return true;
                  })
                  .map((strat) => {
                  const pnlPos = strat.total_pnl >= 0;
                  return (
                    <tr key={strat.deployment_id} className="hover:bg-muted/20 transition-colors">
                      <td className="px-3 py-1.5 font-semibold text-foreground">
                        <div className="flex items-center gap-1.5">
                          <span>{strat.strategy_name}</span>
                            <span className="text-micro text-muted-foreground font-mono">
                            #{strat.deployment_id.slice(0, 6)}
                          </span>
                        </div>
                      </td>
                      <td className="px-3 py-1.5">
                          <AnimatedBadge
                            status={
                            strat.deployment_status === "RUNNING"
                                ? "success"
                              : strat.deployment_status === "PAUSED"
                                  ? "warning"
                                  : "neutral"
                          }
                            pulse={strat.deployment_status === "RUNNING"}
                            size="sm"
                        >
                          {strat.deployment_status}
                          </AnimatedBadge>
                      </td>
                      <td className="px-3 py-1.5 text-right font-medium tabular-nums">{INR(strat.allocated)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">{INR(strat.used)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-gain font-medium">
                        {INR(strat.available)}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{INR(strat.exposure)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{INR(strat.realized)}</td>
                      <td className={cn("p-3 text-right font-semibold tabular-nums", pnlPos ? "text-gain" : "text-destructive")}>
                        {pnlPos ? "+" : ""}{INR(strat.total_pnl)}
                      </td>
                      <td className={cn("p-3 text-right font-medium tabular-nums", (strat.return_pct ?? 0) >= 0 ? "text-gain" : "text-destructive")}>
                        {strat.return_pct !== null ? `${strat.return_pct > 0 ? "+" : ""}${strat.return_pct}%` : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">
                        {strat.trades_closed} closed {strat.trades_open > 0 ? `(${strat.trades_open} open)` : ""}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground font-medium">
                        {PCT(strat.contribution)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* Active Limits & Signal Conflicts Grid */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Active Risk Limits */}
        <Card>
          <CardHeader
            title="Portfolio Risk Limits Status"
            sub="Independent portfolio-level constraints evaluated at every order validation."
          />
          <div className="p-4 space-y-3">
            <div className="divide-y divide-border/40">
              {limits.map((l) => {
                const util = l.utilization ?? 0;
                const isBreaching = util >= 1.0;
                const isWarning = util >= (policyForm.warn_at_pct_of_limit ?? 0.8);
                return (
                  <div key={l.key} className="py-2.5 flex items-center justify-between text-xs">
                    <div className="space-y-0.5 min-w-0 pr-2">
                      <div className="font-medium text-foreground">{l.label}</div>
                      <div className="text-caption text-muted-foreground">
                        Configured: {l.configured !== null ? (l.unit === "INR" ? INR(l.configured) : l.unit === "fraction" ? PCT(l.configured) : String(l.configured)) : "Unset"}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="font-semibold tabular-nums text-foreground">
                        Current: {l.current !== null ? (l.unit === "INR" ? INR(l.current) : l.unit === "fraction" ? PCT(l.current) : String(l.current)) : "—"}
                      </div>
                      {l.utilization !== null ? (
                        <div
                          className={cn(
                            "text-[10.5px] font-semibold tabular-nums",
                            isBreaching ? "text-destructive" : isWarning ? "text-warning" : "text-muted-foreground"
                          )}
                        >
                          {PCT(l.utilization)} utilized
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </Card>

        {/* Signal Conflicts View */}
        <Card>
          <CardHeader
            title="Cross-Strategy Signal Conflicts"
            sub={`Current rule: ${data.policy.conflict_mode.toUpperCase()}. Transparent governance without hidden auto-picking.`}
          />
          <div className="p-4 space-y-3">
            {conflicts.length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">
                <CheckCircle2 size={24} className="mx-auto text-gain mb-2 opacity-80" />
                No cross-strategy position or signal conflicts detected across running deployments.
              </div>
            ) : (
              <div className="space-y-2.5">
                {conflicts.map((c) => (
                  <div key={c.symbol} className="rounded-lg border border-border/80 bg-muted/20 p-3 text-xs">
                    <div className="flex items-center justify-between font-semibold">
                      <span className="text-foreground">{c.symbol}</span>
                      <span className="text-caption text-muted-foreground">
                        Net: {c.net_qty > 0 ? `+${c.net_qty} LONG` : `${c.net_qty} SHORT`}
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2 text-caption">
                      <div className="bg-gain/15/20 border border-gain/20 rounded-md p-1.5 text-gain">
                        <div className="font-semibold uppercase tracking-wider text-[9.5px]">Long Arm(s)</div>
                        {c.long.map((l) => (
                          <div key={l.deployment_id}>
                            {l.strategy_id}: +{l.qty}
                          </div>
                        ))}
                      </div>
                      <div className="bg-loss/15/20 border border-loss/20 rounded-md p-1.5 text-loss">
                        <div className="font-semibold uppercase tracking-wider text-[9.5px]">Short Arm(s)</div>
                        {c.short.map((s) => (
                          <div key={s.deployment_id}>
                            {s.strategy_id}: -{s.qty}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      </div>

      {/* Concentration Analytics: Sector & Stock Concentration Breakdown */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Sector Exposure */}
        <Card>
          <CardHeader
            title="Sector Concentration"
            sub="Exposure distributed across market sectors relative to portfolio capital."
          />
          <div className="p-4 space-y-3">
            {Object.keys(data.sectors).length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">No open sector exposures.</div>
            ) : (
              Object.entries(data.sectors).map(([sec, entry]) => (
                <div key={sec} className="space-y-1 text-xs">
                  <div className="flex justify-between font-medium">
                    <span className="text-foreground">{sec}</span>
                    <span className="text-muted-foreground tabular-nums">
                      {INR(entry.value)} ({PCT(entry.pct)})
                    </span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted/40">
                    <div
                      className="h-full bg-primary transition-all"
                      style={{ width: `${Math.min(100, (entry.pct ?? 0) * 100)}%` }}
                    />
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>

        {/* Stock Concentration */}
        <Card>
          <CardHeader
            title="Stock Concentration (Top 5)"
            sub="Largest individual stock exposures across all combined strategies."
          />
          <div className="p-4 space-y-3">
            {concentrations.stocks.length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">No open stock exposures.</div>
            ) : (
              concentrations.stocks.map((item) => (
                <div key={item.name} className="space-y-1 text-xs">
                  <div className="flex justify-between font-medium">
                    <span className="text-foreground">{item.name}</span>
                    <span className="text-muted-foreground tabular-nums">
                      {INR(item.value)} ({PCT(item.pct)})
                    </span>
                  </div>
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted/40">
                    <div
                      className="h-full bg-violet-500 transition-all"
                      style={{ width: `${Math.min(100, (item.pct ?? 0) * 100)}%` }}
                    />
                  </div>
                </div>
              ))
            )}
          </div>
        </Card>
      </div>

      {/* Aggregate Open Positions across Deployments */}
      <Card>
        <CardHeader
          title="Aggregated Open Positions"
          sub="Combined net holdings across all simultaneous deployment books."
        />
        <div className="overflow-x-auto p-1">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border/60 text-caption text-muted-foreground uppercase font-semibold">
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Symbol</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Sector</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Net Quantity</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Value (₹)</th>
                <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">% of Capital</th>
                <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Active Deployments</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {open_positions.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    No open positions held across any strategy deployments.
                  </td>
                </tr>
              ) : (
                open_positions.map((p) => (
                  <tr key={p.symbol} className="hover:bg-muted/20 transition-colors">
                    <td className="px-3 py-1.5 font-semibold text-foreground">{p.symbol}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{p.sector}</td>
                    <td className="px-3 py-1.5 text-right font-medium tabular-nums">
                      {p.qty > 0 ? `+${p.qty}` : p.qty}
                    </td>
                    <td className="px-3 py-1.5 text-right font-medium tabular-nums">{INR(p.value)}</td>
                    <td className="px-3 py-1.5 text-right text-muted-foreground tabular-nums">{PCT(p.pct_of_capital)}</td>
                    <td className="px-3 py-1.5">
                      <div className="flex flex-wrap gap-1">
                        {p.deployments.map((d) => (
                          <span key={d} className="rounded-md bg-muted px-1.5 py-0.5 text-micro font-mono text-muted-foreground">
                            #{d.slice(0, 6)}
                          </span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

export default PortfolioControlCenter;
