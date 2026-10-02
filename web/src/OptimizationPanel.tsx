import React, { useEffect, useState } from "react";
import {
  AdaptiveParameterSpec,
  OptimizationRecommendation,
  SavedStrategy,
  applyOptimizationRecommendation,
  approveOptimizationRecommendation,
  getAdaptiveParameters,
  listOptimizationRecommendations,
  listSavedStrategies,
  rejectOptimizationRecommendation,
  runOptimizationCycle,
} from "./api";
import { Select } from "./components/ui/select";
import { Button } from "./components/ui/button";
import { Card, EmptyState } from "./components/ui/card";
import { surface, surfaceInset } from "./components/ui/surface";
import { Badge, Callout } from "./components/ui/stat";
import { toneText, type Tone } from "./lib/tone";
import { cn } from "./lib/utils";
import { StrategyExperimentLab } from "./StrategyExperimentLab";
import { Tabs, TabsList, TabsTrigger } from "./components/motion/tabs";
import { useDialog } from "./components/ui/dialog-context";

export const OptimizationPanel: React.FC = () => {
  const [strategies, setStrategies] = useState<SavedStrategy[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string>("");
  const [adaptiveParams, setAdaptiveParams] = useState<Record<string, AdaptiveParameterSpec>>({});
  const [recommendations, setRecommendations] = useState<OptimizationRecommendation[]>([]);
  const [loading, setLoading] = useState(false);
  const [runningOpt, setRunningOpt] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const dialog = useDialog();
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [activeSubTab, setActiveSubTab] = useState<"lab" | "recommendations">("lab");

  // Load available strategies
  useEffect(() => {
    listSavedStrategies()
      .then((res: { strategies: SavedStrategy[] }) => {
        const list = res.strategies || [];
        setStrategies(list);
        if (list.length > 0 && !selectedStrategyId) {
          setSelectedStrategyId(list[0].strategy_id);
        }
      })
      .catch((err: unknown) => {
        console.error("Failed to load strategies:", err);
      });
  }, []);

  // Fetch adaptive parameters and recommendations when strategy changes
  useEffect(() => {
    if (!selectedStrategyId) return;
    setLoading(true);
    setError(null);
    setSuccessMessage(null);

    Promise.all([
      getAdaptiveParameters(selectedStrategyId).catch(() => ({ adaptive_parameters: {} })),
      listOptimizationRecommendations(selectedStrategyId).catch(() => ({ recommendations: [] })),
    ])
      .then(([paramsRes, recsRes]) => {
        setAdaptiveParams(paramsRes.adaptive_parameters || {});
        setRecommendations(recsRes.recommendations || []);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load optimization data");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [selectedStrategyId]);

  const handleRunOptimization = async () => {
    if (!selectedStrategyId) return;
    setRunningOpt(true);
    setError(null);
    setSuccessMessage(null);
    try {
      const res = await runOptimizationCycle(selectedStrategyId);
      setRecommendations(res.recommendations || []);
      setSuccessMessage(
        `Optimization cycle complete. Evaluated ${res.count} candidate(s) using Backtest, Walk-Forward, and Robustness scans.`
      );
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Optimization run failed");
    } finally {
      setRunningOpt(false);
    }
  };

  const handleApprove = async (recId: string) => {
    setActionLoading(recId);
    setError(null);
    try {
      await approveOptimizationRecommendation(recId);
      setSuccessMessage("Recommendation explicitly approved by user.");
      // Refresh recommendations
      const recsRes = await listOptimizationRecommendations(selectedStrategyId);
      setRecommendations(recsRes.recommendations || []);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to approve recommendation");
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async (recId: string) => {
    const reason = await dialog.prompt({
      title: "Reject this recommendation?",
      label: "Reason (optional)",
      confirmLabel: "Reject",
      tone: "danger",
    });
    // Cancelling must cancel; the native prompt version rejected anyway.
    if (reason === null) return;
    setActionLoading(recId);
    setError(null);
    try {
      await rejectOptimizationRecommendation(recId, reason || undefined);
      setSuccessMessage("Recommendation rejected.");
      const recsRes = await listOptimizationRecommendations(selectedStrategyId);
      setRecommendations(recsRes.recommendations || []);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to reject recommendation");
    } finally {
      setActionLoading(null);
    }
  };

  const handleApply = async (recId: string) => {
    const ok = await dialog.confirm({
      title: "Create a new version?",
      description: "This saves the change as a new version. The current version stays as it is.",
      confirmLabel: "Create version",
    });
    if (!ok) return;
    setActionLoading(recId);
    setError(null);
    try {
      const res = await applyOptimizationRecommendation(recId);
      setSuccessMessage(
        `Recommendation applied! New immutable Strategy Version V${res.new_version.version} successfully created.`
      );
      const recsRes = await listOptimizationRecommendations(selectedStrategyId);
      setRecommendations(recsRes.recommendations || []);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to apply recommendation");
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <div className="text-foreground">
      <div className="mb-4">
        <Tabs
          value={activeSubTab}
          onValueChange={(v) => setActiveSubTab(v as "lab" | "recommendations")}
          variant="segment"
        >
          <TabsList>
            <TabsTrigger value="lab">Strategy Experiment Lab</TabsTrigger>
            <TabsTrigger value="recommendations">Recommendations</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {activeSubTab === "lab" ? (
        <StrategyExperimentLab />
      ) : (
        <>
          {/* Top Header */}
          <div className="flex justify-between items-start mb-5">
            <div>
              <h1 className="m-0 mb-1.5 text-xl font-semibold tracking-tight text-foreground">
                Controlled Strategy Optimization
              </h1>
              <p className="m-0 text-muted-foreground text-sm">
                Forward Evidence → Hypothesis → Candidate Parameter Change → Backtest → Walk-Forward Validation → Robustness Check → User Approval → New Immutable Version
              </p>
            </div>

            <div className="flex gap-3 items-center">
          <Select
            value={selectedStrategyId}
            onChange={setSelectedStrategyId}
            options={strategies.map((s) => ({
              value: s.strategy_id,
              label: `${s.name} (${s.strategy_id.slice(0, 8)})`,
            }))}
          />

              <Button onClick={handleRunOptimization} disabled={runningOpt || !selectedStrategyId}>
            {runningOpt ? "Evaluating Candidates..." : "Run Optimization Cycle"}
              </Button>
        </div>
      </div>

      {/* Strict Safety Guarantee Banner */}
          <div className={cn(surface, "mb-6 flex items-center gap-3 border-info px-4 py-3.5")}>
            <div className="text-xl">🛡️</div>
            <div className="text-body text-foreground leading-normal">
            <strong className="text-info">Safety Boundaries Enforced:</strong> Automatic live strategy modification is strictly prohibited.
            Only parameters explicitly designated as <code className="text-info">adaptive: true</code> are tuned.
            Approval never overwrites existing version <code className="text-primary-soft">V_N</code>; it generates <code className="text-primary-soft">V_{'{N+1}'}</code> with full audit provenance.
        </div>
      </div>

      {/* Messages */}
      {error && (
            <div className="mb-5">
              <Callout tone="bad">⚠️ {error}</Callout>
        </div>
      )}
      {successMessage && (
            <div className="mb-5">
              <Callout tone="good">✓ {successMessage}</Callout>
        </div>
      )}

      {/* Adaptive Parameters Grid */}
          <Card padding="md" className="mb-7">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-sm font-semibold m-0 text-foreground">
            Configured Adaptive Parameters ({Object.keys(adaptiveParams).length})
          </h3>
              <span className="text-xs text-muted-foreground">
            Only these parameters may be optimized
          </span>
        </div>

        {Object.keys(adaptiveParams).length === 0 ? (
              <div className="p-4 text-center text-muted-foreground text-body">
            No parameters are marked adaptive on this strategy. To optimize, declare parameters under <code>adaptive_parameters</code> in the strategy definition.
          </div>
        ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3">
            {Object.values(adaptiveParams).map((p) => (
                  <div key={p.name} className={cn(surfaceInset, "bg-card p-3")}>
                    <div className="flex justify-between mb-1.5">
                      <span className="font-semibold text-info text-body">{p.name}</span>
                      <span className="text-caption bg-info/13 text-info py-0.5 px-1.5 rounded-md">
                    {p.block}
                  </span>
                </div>
                    <div className="text-xs text-muted-foreground mb-1.5">
                    Current: <strong className="text-foreground">{p.current}</strong> | Range: [{p.minimum} - {p.maximum}]
                </div>
                    <div className="text-caption text-muted-foreground">
                  Step size: {p.step} {p.description ? `• ${p.description}` : ""}
                </div>
              </div>
            ))}
          </div>
        )}
          </Card>

      {/* Recommendations Section */}
      <div>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-semibold m-0 text-foreground">
            Optimization Recommendations ({recommendations.length})
          </h2>
              <span className="text-body text-muted-foreground">
            Requires explicit user approval before version generation
          </span>
        </div>

        {loading ? (
          <div className="p-10 text-center text-muted-foreground">Loading recommendations...</div>
        ) : recommendations.length === 0 ? (
              <EmptyState>
            No recommendations generated yet. Click <strong>"Run Optimization Cycle"</strong> above to evaluate forward observations against backtests and walk-forward validation.
              </EmptyState>
        ) : (
              <div className="flex flex-col gap-5">
            {recommendations.map((rec) => {
              const isRecommended = rec.status === "RECOMMENDED";
              const isApproved = rec.status === "APPROVED";
              const isApplied = rec.status === "APPLIED";
              const isRejected = rec.status === "REJECTED";

              const statusTone: Tone = isRecommended ? "good" : isApproved || isApplied ? "info" : "bad";

              const baseM = rec.baseline_metrics || {};
              const candM = rec.candidate_metrics || {};
              const wfM = rec.walk_forward_metrics || {};
              const rob = rec.robustness_results || {};

              return (
                <div key={rec.recommendation_id} className={cn(surface, "overflow-hidden", isRecommended && "border-gain/40")}>
                  {/* Card Header */}
                      <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
                        <div className="flex items-center gap-3">
                          <span className="text-base font-semibold text-foreground">
                        Parameter: {rec.parameter}
                      </span>
                          <Badge tone={statusTone}>{rec.status}</Badge>
                      <span
                      className="text-caption py-0.5 px-2 rounded-md bg-border text-foreground"
                      >
                        Confidence: {rec.confidence.toUpperCase()}
                      </span>
                    </div>

                        <div className="text-xs text-muted-foreground">
                      Source Version: V{rec.source_strategy_version}
                      {rec.target_strategy_version && ` → Target Version: V${rec.target_strategy_version}`}
                    </div>
                  </div>

                  {/* Card Body: Comparison */}
                      <div className="p-5">
                        <div className="mb-4 grid grid-cols-2 gap-6 rounded-lg border border-border bg-muted/40 p-4">
                      {/* Current Version */}
                      <div>
                            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                          CURRENT VERSION (V{rec.source_strategy_version})
                        </div>
                            <div className="text-xl font-semibold text-foreground mb-3">
                          {rec.current_value}
                        </div>
                            <div className="flex flex-col gap-1.5 text-body text-muted-foreground">
                            <div>Profit Factor: <strong className="text-foreground">{baseM.profit_factor ?? "N/A"}</strong></div>
                            <div>Sharpe Ratio: <strong className="text-foreground">{baseM.sharpe ?? "N/A"}</strong></div>
                            <div>Max Drawdown: <strong className="text-foreground">{baseM.max_drawdown_pct ? `${baseM.max_drawdown_pct}%` : "N/A"}</strong></div>
                            <div>Win Rate: <strong className="text-foreground">{baseM.win_rate_pct ? `${baseM.win_rate_pct}%` : "N/A"}</strong></div>
                            <div>Trades: <strong className="text-foreground">{baseM.num_trades ?? 0}</strong></div>
                        </div>
                      </div>

                      {/* Proposed Version */}
                          <div className="border-l border-border pl-6">
                            <div className="text-xs font-semibold text-info uppercase tracking-wider mb-2">
                          PROPOSED VERSION
                        </div>
                            <div className="text-xl font-semibold text-info mb-3">
                          {rec.proposed_value}
                        </div>
                            <div className="flex flex-col gap-1.5 text-body text-muted-foreground">
                          <div>
                          Profit Factor: <strong className="text-gain">{candM.profit_factor ?? "N/A"}</strong>
                            {baseM.profit_factor && candM.profit_factor && (
                              <span className={cn("ml-1.5 text-caption", toneText[candM.profit_factor >= baseM.profit_factor ? "good" : "bad"])}>
                                ({baseM.profit_factor} → {candM.profit_factor})
                              </span>
                            )}
                          </div>
                          <div>
                          Sharpe Ratio: <strong className="text-gain">{candM.sharpe ?? "N/A"}</strong>
                            {baseM.sharpe && candM.sharpe && (
                              <span className={cn("ml-1.5 text-caption", toneText[candM.sharpe >= baseM.sharpe ? "good" : "bad"])}>
                                ({baseM.sharpe} → {candM.sharpe})
                              </span>
                            )}
                          </div>
                          <div>
                          Max Drawdown: <strong className="text-info">{candM.max_drawdown_pct ? `${candM.max_drawdown_pct}%` : "N/A"}</strong>
                            {baseM.max_drawdown_pct && candM.max_drawdown_pct && (
                              <span className={cn("ml-1.5 text-caption", toneText[candM.max_drawdown_pct <= baseM.max_drawdown_pct ? "good" : "bad"])}>
                                ({baseM.max_drawdown_pct}% → {candM.max_drawdown_pct}%)
                              </span>
                            )}
                          </div>
                          <div>
                          Win Rate: <strong className="text-foreground">{candM.win_rate_pct ? `${candM.win_rate_pct}%` : "N/A"}</strong>
                          </div>
                          <div>
                          Trades: <strong className="text-foreground">{candM.num_trades ?? 0}</strong>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Evidence & Validation Details Grid */}
                        <div className="grid grid-cols-3 gap-3 mb-4 text-xs">
                          <div className="bg-card p-3 rounded-md">
                          <div className="text-muted-foreground mb-1 font-semibold">FORWARD EVIDENCE</div>
                            <div className="text-foreground font-medium">
                          {rec.sample_size} trades observed
                        </div>
                            <div className="text-muted-foreground text-caption mt-1">
                          {rec.source_observations?.statement || rec.source_observations?.kind || "Genuine paper-forward evidence"}
                        </div>
                      </div>

                          <div className="bg-card p-3 rounded-md">
                          <div className="text-muted-foreground mb-1 font-semibold">WALK-FORWARD VALIDATION</div>
                          <div className={cn("font-semibold", toneText[wfM.passed ? "good" : "bad"])}>
                          {wfM.passed ? "Passed OOS Validation" : "Failed OOS"}
                        </div>
                            <div className="text-muted-foreground text-caption mt-1">
                          OOS PF: {wfM.out_of_sample_profit_factor ?? "N/A"} | Eff: {wfM.efficiency_ratio ?? "N/A"}
                        </div>
                      </div>

                          <div className="bg-card p-3 rounded-md">
                          <div className="text-muted-foreground mb-1 font-semibold">ROBUSTNESS SCAN</div>
                          <div className={cn("font-semibold", toneText[rob.is_stable ? "info" : "bad"])}>
                          {rob.is_stable ? "Stable Plateau" : "Isolated Peak (Overfitting Risk)"}
                        </div>
                            <div className="text-muted-foreground text-caption mt-1">
                          {rob.sensitivity_verdict || (rob.stable_range ? `Stable between ${rob.stable_range[0]} and ${rob.stable_range[1]}` : "")}
                        </div>
                      </div>
                    </div>

                    {/* Optimization Reason */}
                        <div className="mb-4 rounded-md bg-muted/40 px-3.5 py-2.5 text-body text-foreground">
                        <span className="text-muted-foreground font-semibold">Evaluation Synthesis: </span>
                      {rec.reason}
                    </div>

                    {/* Action Controls */}
                        <div className="flex justify-end gap-3 items-center">
                      {isRecommended && (
                        <>
                              <Button
                                variant="outline"
                            onClick={() => handleReject(rec.recommendation_id)}
                            disabled={actionLoading === rec.recommendation_id}
                          >
                            Reject
                              </Button>
                              <Button
                            onClick={() => handleApprove(rec.recommendation_id)}
                            disabled={actionLoading === rec.recommendation_id}
                          >
                            {actionLoading === rec.recommendation_id ? "Processing..." : "Approve Recommendation"}
                              </Button>
                        </>
                      )}

                      {isApproved && (
                            <Button
                          onClick={() => handleApply(rec.recommendation_id)}
                          disabled={actionLoading === rec.recommendation_id}
                        >
                          {actionLoading === rec.recommendation_id
                            ? "Applying..."
                            : `Apply & Generate Immutable Version (V${rec.source_strategy_version} → V${rec.source_strategy_version + 1})`}
                            </Button>
                      )}

                      {isApplied && (
                            <span className="text-body font-semibold text-info">
                          ✓ Applied to Strategy Version V{rec.target_strategy_version}
                        </span>
                      )}

                      {isRejected && (
                            <span className="text-body text-loss">
                          Rejected {rec.rejection_reason ? `(${rec.rejection_reason})` : ""}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      </>
      )}
    </div>
  );
};
