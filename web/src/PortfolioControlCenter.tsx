import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  RefreshCw,
  Sliders,
} from "lucide-react";
import {
  getPortfolioControlCenter,
  updatePortfolioPolicy,
  type PortfolioControlCenterData,
  type PortfolioPolicyConfig,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Select } from "./components/ui/select";
import { Badge } from "./components/ui/stat";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

const INR = (v: number | null | undefined, frac = 0) => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: frac })}`;
};

const PCT = (v: number | null | undefined, frac = 1) => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return `${(v * 100).toFixed(frac)}%`;
};

export function PortfolioControlCenter() {
  const [data, setData] = useState<PortfolioControlCenterData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const [showConfig, setShowConfig] = useState(false);
  const [policyForm, setPolicyForm] = useState<Partial<PortfolioPolicyConfig>>({});
  const [policyReason, setPolicyReason] = useState("");
  const [savingPolicy, setSavingPolicy] = useState(false);
  const { toast } = useToast();

  const load = useCallback(async () => {
    setState("loading");
    try {
      const res = await getPortfolioControlCenter();
      setData(res);
      setPolicyForm(res.policy);
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
      {/* Header & Quick Action */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border/60 pb-4">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold tracking-tight text-foreground sm:text-xl">
              Portfolio Control Center
            </h2>
            <Badge tone={data.policy_configured ? "good" : "warn"}>
              {data.policy_configured ? "Active Risk Engine" : "Uncapped / Pass-Through"}
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Portfolio-level limits, cross-strategy capital allocation, concentration tracking, and conflict governance.
          </p>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() => setShowConfig(!showConfig)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold border transition-all",
              showConfig
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-muted/30 border-border/70 text-muted-foreground hover:text-foreground hover:bg-muted/60"
            )}
          >
            <Sliders size={13} />
            Configure Limits & Rules
          </button>
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
        <div className="rounded-xl border border-amber-500/40 bg-amber-950/20 p-4 text-xs text-amber-200 space-y-1.5">
          <div className="flex items-center gap-2 font-semibold text-amber-300">
            <AlertTriangle size={15} className="text-amber-400 shrink-0" />
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
        <Card className="border-primary/40 bg-card/90 shadow-xl">
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
              <button
                type="button"
                onClick={() => setShowConfig(false)}
                className="px-3 py-1.5 rounded-lg border border-border text-xs text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={savingPolicy}
                onClick={() => void handleSavePolicy()}
                className="px-4 py-1.5 rounded-lg bg-primary text-primary-foreground font-semibold text-xs hover:bg-primary/90 disabled:opacity-50"
              >
                {savingPolicy ? "Saving…" : "Save Active Limits"}
              </button>
            </div>
          </div>
        </Card>
      )}

      {/* KPI Ribbon Strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Total Capital</div>
          <div className="mt-1 text-lg font-bold tabular-nums text-foreground">{INR(capital.total)}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">Allocated book size</div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Deployed Capital</div>
          <div className="mt-1 text-lg font-bold tabular-nums text-foreground">{INR(capital.deployed)}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">Active running arms</div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Available Capital</div>
          <div className="mt-1 text-lg font-bold tabular-nums text-emerald-500">{INR(capital.available)}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">Free margin capacity</div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Total Gross Exposure</div>
          <div className="mt-1 text-lg font-bold tabular-nums text-foreground">{INR(exposure.gross)}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            L: {INR(exposure.long)} | S: {INR(exposure.short)}
          </div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Today's P&L</div>
          <div
            className={cn(
              "mt-1 text-lg font-bold tabular-nums",
              today.total > 0 ? "text-emerald-500" : today.total < 0 ? "text-destructive" : "text-foreground"
            )}
          >
            {today.total >= 0 ? "+" : ""}{INR(today.total)}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            Realized: {INR(today.realized)}
          </div>
        </div>

        <div className="rounded-xl border border-border/70 bg-card/60 p-4">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Max Drawdown</div>
          <div className="mt-1 text-lg font-bold tabular-nums text-destructive">
            {drawdown.value !== null ? INR(Math.abs(drawdown.value)) : "—"}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            Cash util: {PCT(capital.cash_utilization)}
          </div>
        </div>
      </div>

      {/* Strategy Capital Allocation & Objective Metrics Comparison */}
      <Card>
        <CardHeader
          title="Strategy Capital Allocation & Performance Comparison"
          sub="Objective comparison across all paper deployments ordered by allocated capital (no arbitrary 'best' ranking)."
        />
        <div className="overflow-x-auto p-1">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border/60 text-[11px] text-muted-foreground uppercase font-semibold">
                <th className="p-3">Strategy / Deployment</th>
                <th className="p-3">Status</th>
                <th className="p-3 text-right">Allocated</th>
                <th className="p-3 text-right">Used</th>
                <th className="p-3 text-right">Available</th>
                <th className="p-3 text-right">Exposure</th>
                <th className="p-3 text-right">Realized P&L</th>
                <th className="p-3 text-right">Total P&L</th>
                <th className="p-3 text-right">Return %</th>
                <th className="p-3 text-right">Trades</th>
                <th className="p-3 text-right">P&L Share</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {strategies.length === 0 ? (
                <tr>
                  <td colSpan={11} className="p-6 text-center text-muted-foreground">
                    No active deployments found. Create a deployment in Paper trading to allocate capital.
                  </td>
                </tr>
              ) : (
                strategies.map((strat) => {
                  const pnlPos = strat.total_pnl >= 0;
                  return (
                    <tr key={strat.deployment_id} className="hover:bg-muted/20 transition-colors">
                      <td className="p-3 font-semibold text-foreground">
                        <div className="flex items-center gap-1.5">
                          <span>{strat.strategy_name}</span>
                          <span className="text-[10px] text-muted-foreground font-mono">
                            #{strat.deployment_id.slice(0, 6)}
                          </span>
                        </div>
                      </td>
                      <td className="p-3">
                        <Badge
                          tone={
                            strat.deployment_status === "RUNNING"
                              ? "good"
                              : strat.deployment_status === "PAUSED"
                              ? "warn"
                              : "flat"
                          }
                        >
                          {strat.deployment_status}
                        </Badge>
                      </td>
                      <td className="p-3 text-right font-medium tabular-nums">{INR(strat.allocated)}</td>
                      <td className="p-3 text-right tabular-nums text-muted-foreground">{INR(strat.used)}</td>
                      <td className="p-3 text-right tabular-nums text-emerald-500 font-medium">
                        {INR(strat.available)}
                      </td>
                      <td className="p-3 text-right tabular-nums">{INR(strat.exposure)}</td>
                      <td className="p-3 text-right tabular-nums">{INR(strat.realized)}</td>
                      <td className={cn("p-3 text-right font-semibold tabular-nums", pnlPos ? "text-emerald-500" : "text-destructive")}>
                        {pnlPos ? "+" : ""}{INR(strat.total_pnl)}
                      </td>
                      <td className={cn("p-3 text-right font-medium tabular-nums", (strat.return_pct ?? 0) >= 0 ? "text-emerald-500" : "text-destructive")}>
                        {strat.return_pct !== null ? `${strat.return_pct > 0 ? "+" : ""}${strat.return_pct}%` : "—"}
                      </td>
                      <td className="p-3 text-right tabular-nums text-muted-foreground">
                        {strat.trades_closed} closed {strat.trades_open > 0 ? `(${strat.trades_open} open)` : ""}
                      </td>
                      <td className="p-3 text-right tabular-nums text-muted-foreground font-medium">
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
                      <div className="text-[11px] text-muted-foreground">
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
                            "text-[10.5px] font-bold tabular-nums",
                            isBreaching ? "text-destructive" : isWarning ? "text-amber-500" : "text-muted-foreground"
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
                <CheckCircle2 size={24} className="mx-auto text-emerald-500 mb-2 opacity-80" />
                No cross-strategy position or signal conflicts detected across running deployments.
              </div>
            ) : (
              <div className="space-y-2.5">
                {conflicts.map((c) => (
                  <div key={c.symbol} className="rounded-lg border border-border/80 bg-muted/20 p-3 text-xs">
                    <div className="flex items-center justify-between font-semibold">
                      <span className="text-foreground">{c.symbol}</span>
                      <span className="text-[11px] text-muted-foreground">
                        Net: {c.net_qty > 0 ? `+${c.net_qty} LONG` : `${c.net_qty} SHORT`}
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
                      <div className="bg-emerald-950/20 border border-emerald-500/20 rounded p-1.5 text-emerald-300">
                        <div className="font-semibold uppercase tracking-wider text-[9.5px]">Long Arm(s)</div>
                        {c.long.map((l) => (
                          <div key={l.deployment_id}>
                            {l.strategy_id}: +{l.qty}
                          </div>
                        ))}
                      </div>
                      <div className="bg-rose-950/20 border border-rose-500/20 rounded p-1.5 text-rose-300">
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
              <tr className="border-b border-border/60 text-[11px] text-muted-foreground uppercase font-semibold">
                <th className="p-3">Symbol</th>
                <th className="p-3">Sector</th>
                <th className="p-3 text-right">Net Quantity</th>
                <th className="p-3 text-right">Value (₹)</th>
                <th className="p-3 text-right">% of Capital</th>
                <th className="p-3">Active Deployments</th>
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
                    <td className="p-3 font-semibold text-foreground">{p.symbol}</td>
                    <td className="p-3 text-muted-foreground">{p.sector}</td>
                    <td className="p-3 text-right font-medium tabular-nums">
                      {p.qty > 0 ? `+${p.qty}` : p.qty}
                    </td>
                    <td className="p-3 text-right font-medium tabular-nums">{INR(p.value)}</td>
                    <td className="p-3 text-right text-muted-foreground tabular-nums">{PCT(p.pct_of_capital)}</td>
                    <td className="p-3">
                      <div className="flex flex-wrap gap-1">
                        {p.deployments.map((d) => (
                          <span key={d} className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
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
