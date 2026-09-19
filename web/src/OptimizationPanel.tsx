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
import { StrategyExperimentLab } from "./StrategyExperimentLab";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
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
    <div style={{ color: "var(--foreground)" }}>
      <div style={{ marginBottom: 16 }}>
        <Tabs
          value={activeSubTab}
          onValueChange={(v) => setActiveSubTab(v as "lab" | "recommendations")}
          variant="segment"
        >
          <TabsList>
            <TabsTrigger value="lab">Strategy Experiment Lab</TabsTrigger>
            <TabsTrigger value="recommendations">Controlled Recommendations</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {activeSubTab === "lab" ? (
        <StrategyExperimentLab />
      ) : (
        <>
          {/* Top Header */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "20px" }}>
            <div>
              <h1 style={{ fontSize: "24px", fontWeight: 700, margin: "0 0 6px 0", color: "var(--foreground)", letterSpacing: "-0.02em" }}>
                Controlled Strategy Optimization
              </h1>
              <p style={{ margin: 0, color: "var(--muted-foreground)", fontSize: "14px" }}>
                Forward Evidence → Hypothesis → Candidate Parameter Change → Backtest → Walk-Forward Validation → Robustness Check → User Approval → New Immutable Version
              </p>
            </div>

        <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
          <Select
            value={selectedStrategyId}
            onChange={setSelectedStrategyId}
            options={strategies.map((s) => ({
              value: s.strategy_id,
              label: `${s.name} (${s.strategy_id.slice(0, 8)})`,
            }))}
          />

          <button
            onClick={handleRunOptimization}
            disabled={runningOpt || !selectedStrategyId}
            style={{
              padding: "8px 18px",
              backgroundColor: runningOpt ? "var(--border)" : "#2563eb",
              color: "#ffffff",
              border: "none",
              borderRadius: "6px",
              fontSize: "14px",
              fontWeight: 600,
              cursor: runningOpt ? "not-allowed" : "pointer",
              transition: "background-color 0.15s ease",
            }}
          >
            {runningOpt ? "Evaluating Candidates..." : "Run Optimization Cycle"}
          </button>
        </div>
      </div>

      {/* Strict Safety Guarantee Banner */}
      <div
        style={{
          backgroundColor: "rgba(30, 41, 59, 0.7)",
          border: "1px solid #3b82f6",
          borderRadius: "8px",
          padding: "14px 18px",
          marginBottom: "24px",
          display: "flex",
          alignItems: "center",
          gap: "12px",
        }}
      >
        <div style={{ fontSize: "20px" }}>🛡️</div>
        <div style={{ fontSize: "13px", color: "var(--foreground)", lineHeight: 1.5 }}>
          <strong style={{ color: "#60a5fa" }}>Safety Boundaries Enforced:</strong> Automatic live strategy modification is strictly prohibited.
          Only parameters explicitly designated as <code style={{ color: "#38bdf8" }}>adaptive: true</code> are tuned.
          Approval never overwrites existing version <code style={{ color: "#a5b4fc" }}>V_N</code>; it generates <code style={{ color: "#a5b4fc" }}>V_{'{N+1}'}</code> with full audit provenance.
        </div>
      </div>

      {/* Messages */}
      {error && (
        <div style={{ padding: "12px 16px", backgroundColor: "#ef444420", border: "1px solid #ef4444", borderRadius: "6px", color: "#fca5a5", marginBottom: "20px", fontSize: "14px" }}>
          ⚠️ {error}
        </div>
      )}
      {successMessage && (
        <div style={{ padding: "12px 16px", backgroundColor: "#10b98120", border: "1px solid #10b981", borderRadius: "6px", color: "#6ee7b7", marginBottom: "20px", fontSize: "14px" }}>
          ✓ {successMessage}
        </div>
      )}

      {/* Adaptive Parameters Grid */}
      <div style={{ backgroundColor: "var(--card)", border: "1px solid var(--card)", borderRadius: "8px", padding: "18px", marginBottom: "28px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
          <h3 style={{ fontSize: "15px", fontWeight: 600, margin: 0, color: "#f1f5f9" }}>
            Configured Adaptive Parameters ({Object.keys(adaptiveParams).length})
          </h3>
          <span style={{ fontSize: "12px", color: "var(--muted-foreground)" }}>
            Only these parameters may be optimized
          </span>
        </div>

        {Object.keys(adaptiveParams).length === 0 ? (
          <div style={{ padding: "16px", textAlign: "center", color: "var(--muted-foreground)", fontSize: "13px" }}>
            No parameters are marked adaptive on this strategy. To optimize, declare parameters under <code>adaptive_parameters</code> in the strategy definition.
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: "12px" }}>
            {Object.values(adaptiveParams).map((p) => (
              <div
                key={p.name}
                style={{
                  backgroundColor: "var(--card)",
                  border: "1px solid var(--border)",
                  borderRadius: "6px",
                  padding: "12px",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "6px" }}>
                  <span style={{ fontWeight: 600, color: "#38bdf8", fontSize: "13px" }}>{p.name}</span>
                  <span style={{ fontSize: "11px", backgroundColor: "#0284c720", color: "#38bdf8", padding: "2px 6px", borderRadius: "4px" }}>
                    {p.block}
                  </span>
                </div>
                <div style={{ fontSize: "12px", color: "var(--muted-foreground)", marginBottom: "6px" }}>
                  Current: <strong style={{ color: "var(--foreground)" }}>{p.current}</strong> | Range: [{p.minimum} - {p.maximum}]
                </div>
                <div style={{ fontSize: "11px", color: "var(--muted-foreground)" }}>
                  Step size: {p.step} {p.description ? `• ${p.description}` : ""}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Recommendations Section */}
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
          <h2 style={{ fontSize: "18px", fontWeight: 600, margin: 0, color: "var(--foreground)" }}>
            Optimization Recommendations ({recommendations.length})
          </h2>
          <span style={{ fontSize: "13px", color: "var(--muted-foreground)" }}>
            Requires explicit user approval before version generation
          </span>
        </div>

        {loading ? (
          <div style={{ padding: "40px", textAlign: "center", color: "var(--muted-foreground)" }}>Loading recommendations...</div>
        ) : recommendations.length === 0 ? (
          <div style={{ padding: "48px", textAlign: "center", backgroundColor: "var(--card)", border: "1px dashed var(--border)", borderRadius: "8px", color: "var(--muted-foreground)" }}>
            No recommendations generated yet. Click <strong>"Run Optimization Cycle"</strong> above to evaluate forward observations against backtests and walk-forward validation.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
            {recommendations.map((rec) => {
              const isRecommended = rec.status === "RECOMMENDED";
              const isApproved = rec.status === "APPROVED";
              const isApplied = rec.status === "APPLIED";
              const isRejected = rec.status === "REJECTED";

              const statusBg = isRecommended
                ? "#10b98120"
                : isApproved
                ? "#3b82f620"
                : isApplied
                ? "#8b5cf620"
                : "#ef444420";
              const statusColor = isRecommended
                ? "#34d399"
                : isApproved
                ? "#60a5fa"
                : isApplied
                ? "#c084fc"
                : "#f87171";

              const baseM = rec.baseline_metrics || {};
              const candM = rec.candidate_metrics || {};
              const wfM = rec.walk_forward_metrics || {};
              const rob = rec.robustness_results || {};

              return (
                <div
                  key={rec.recommendation_id}
                  style={{
                    backgroundColor: "var(--card)",
                    border: `1px solid ${isRecommended ? "#10b98140" : "var(--card)"}`,
                    borderRadius: "8px",
                    overflow: "hidden",
                    boxShadow: "0 4px 6px -1px rgba(0, 0, 0, 0.1)",
                  }}
                >
                  {/* Card Header */}
                  <div
                    style={{
                      padding: "14px 20px",
                      backgroundColor: "var(--card)",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      borderBottom: "1px solid var(--border)",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                      <span style={{ fontSize: "16px", fontWeight: 700, color: "var(--foreground)" }}>
                        Parameter: {rec.parameter}
                      </span>
                      <span
                        style={{
                          fontSize: "12px",
                          fontWeight: 600,
                          padding: "3px 10px",
                          borderRadius: "9999px",
                          backgroundColor: statusBg,
                          color: statusColor,
                          border: `1px solid ${statusColor}40`,
                        }}
                      >
                        {rec.status}
                      </span>
                      <span
                        style={{
                          fontSize: "11px",
                          padding: "2px 8px",
                          borderRadius: "4px",
                          backgroundColor: "var(--border)",
                          color: "var(--foreground)",
                        }}
                      >
                        Confidence: {rec.confidence.toUpperCase()}
                      </span>
                    </div>

                    <div style={{ fontSize: "12px", color: "var(--muted-foreground)" }}>
                      Source Version: V{rec.source_strategy_version}
                      {rec.target_strategy_version && ` → Target Version: V${rec.target_strategy_version}`}
                    </div>
                  </div>

                  {/* Card Body: Comparison */}
                  <div style={{ padding: "20px" }}>
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr 1fr",
                        gap: "24px",
                        backgroundColor: "#141e33",
                        border: "1px solid var(--card)",
                        borderRadius: "8px",
                        padding: "16px",
                        marginBottom: "16px",
                      }}
                    >
                      {/* Current Version */}
                      <div>
                        <div style={{ fontSize: "12px", fontWeight: 700, color: "var(--muted-foreground)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "8px" }}>
                          CURRENT VERSION (V{rec.source_strategy_version})
                        </div>
                        <div style={{ fontSize: "20px", fontWeight: 700, color: "var(--foreground)", marginBottom: "12px" }}>
                          {rec.current_value}
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "13px", color: "var(--muted-foreground)" }}>
                          <div>Profit Factor: <strong style={{ color: "var(--foreground)" }}>{baseM.profit_factor ?? "N/A"}</strong></div>
                          <div>Sharpe Ratio: <strong style={{ color: "var(--foreground)" }}>{baseM.sharpe ?? "N/A"}</strong></div>
                          <div>Max Drawdown: <strong style={{ color: "var(--foreground)" }}>{baseM.max_drawdown_pct ? `${baseM.max_drawdown_pct}%` : "N/A"}</strong></div>
                          <div>Win Rate: <strong style={{ color: "var(--foreground)" }}>{baseM.win_rate_pct ? `${baseM.win_rate_pct}%` : "N/A"}</strong></div>
                          <div>Trades: <strong style={{ color: "var(--foreground)" }}>{baseM.num_trades ?? 0}</strong></div>
                        </div>
                      </div>

                      {/* Proposed Version */}
                      <div style={{ borderLeft: "1px solid var(--card)", paddingLeft: "24px" }}>
                        <div style={{ fontSize: "12px", fontWeight: 700, color: "#38bdf8", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "8px" }}>
                          PROPOSED VERSION
                        </div>
                        <div style={{ fontSize: "20px", fontWeight: 700, color: "#38bdf8", marginBottom: "12px" }}>
                          {rec.proposed_value}
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "13px", color: "var(--muted-foreground)" }}>
                          <div>
                            Profit Factor: <strong style={{ color: "#34d399" }}>{candM.profit_factor ?? "N/A"}</strong>
                            {baseM.profit_factor && candM.profit_factor && (
                              <span style={{ fontSize: "11px", marginLeft: "6px", color: candM.profit_factor >= baseM.profit_factor ? "#34d399" : "#f87171" }}>
                                ({baseM.profit_factor} → {candM.profit_factor})
                              </span>
                            )}
                          </div>
                          <div>
                            Sharpe Ratio: <strong style={{ color: "#34d399" }}>{candM.sharpe ?? "N/A"}</strong>
                            {baseM.sharpe && candM.sharpe && (
                              <span style={{ fontSize: "11px", marginLeft: "6px", color: candM.sharpe >= baseM.sharpe ? "#34d399" : "#f87171" }}>
                                ({baseM.sharpe} → {candM.sharpe})
                              </span>
                            )}
                          </div>
                          <div>
                            Max Drawdown: <strong style={{ color: "#38bdf8" }}>{candM.max_drawdown_pct ? `${candM.max_drawdown_pct}%` : "N/A"}</strong>
                            {baseM.max_drawdown_pct && candM.max_drawdown_pct && (
                              <span style={{ fontSize: "11px", marginLeft: "6px", color: candM.max_drawdown_pct <= baseM.max_drawdown_pct ? "#34d399" : "#f87171" }}>
                                ({baseM.max_drawdown_pct}% → {candM.max_drawdown_pct}%)
                              </span>
                            )}
                          </div>
                          <div>
                            Win Rate: <strong style={{ color: "var(--foreground)" }}>{candM.win_rate_pct ? `${candM.win_rate_pct}%` : "N/A"}</strong>
                          </div>
                          <div>
                            Trades: <strong style={{ color: "var(--foreground)" }}>{candM.num_trades ?? 0}</strong>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* Evidence & Validation Details Grid */}
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "12px", marginBottom: "16px", fontSize: "12px" }}>
                      <div style={{ backgroundColor: "var(--card)", padding: "12px", borderRadius: "6px" }}>
                        <div style={{ color: "var(--muted-foreground)", marginBottom: "4px", fontWeight: 600 }}>FORWARD EVIDENCE</div>
                        <div style={{ color: "var(--foreground)", fontWeight: 500 }}>
                          {rec.sample_size} trades observed
                        </div>
                        <div style={{ color: "var(--muted-foreground)", fontSize: "11px", marginTop: "4px" }}>
                          {rec.source_observations?.statement || rec.source_observations?.kind || "Genuine paper-forward evidence"}
                        </div>
                      </div>

                      <div style={{ backgroundColor: "var(--card)", padding: "12px", borderRadius: "6px" }}>
                        <div style={{ color: "var(--muted-foreground)", marginBottom: "4px", fontWeight: 600 }}>WALK-FORWARD VALIDATION</div>
                        <div style={{ color: wfM.passed ? "#34d399" : "#f87171", fontWeight: 600 }}>
                          {wfM.passed ? "Passed OOS Validation" : "Failed OOS"}
                        </div>
                        <div style={{ color: "var(--muted-foreground)", fontSize: "11px", marginTop: "4px" }}>
                          OOS PF: {wfM.out_of_sample_profit_factor ?? "N/A"} | Eff: {wfM.efficiency_ratio ?? "N/A"}
                        </div>
                      </div>

                      <div style={{ backgroundColor: "var(--card)", padding: "12px", borderRadius: "6px" }}>
                        <div style={{ color: "var(--muted-foreground)", marginBottom: "4px", fontWeight: 600 }}>ROBUSTNESS SCAN</div>
                        <div style={{ color: rob.is_stable ? "#38bdf8" : "#f87171", fontWeight: 600 }}>
                          {rob.is_stable ? "Stable Plateau" : "Isolated Peak (Overfitting Risk)"}
                        </div>
                        <div style={{ color: "var(--muted-foreground)", fontSize: "11px", marginTop: "4px" }}>
                          {rob.sensitivity_verdict || (rob.stable_range ? `Stable between ${rob.stable_range[0]} and ${rob.stable_range[1]}` : "")}
                        </div>
                      </div>
                    </div>

                    {/* Optimization Reason */}
                    <div style={{ fontSize: "13px", color: "var(--foreground)", backgroundColor: "var(--card)50", padding: "10px 14px", borderRadius: "6px", marginBottom: "16px" }}>
                      <span style={{ color: "var(--muted-foreground)", fontWeight: 600 }}>Evaluation Synthesis: </span>
                      {rec.reason}
                    </div>

                    {/* Action Controls */}
                    <div style={{ display: "flex", justifyContent: "flex-end", gap: "12px", alignItems: "center" }}>
                      {isRecommended && (
                        <>
                          <button
                            onClick={() => handleReject(rec.recommendation_id)}
                            disabled={actionLoading === rec.recommendation_id}
                            style={{
                              padding: "7px 16px",
                              backgroundColor: "transparent",
                              color: "#f87171",
                              border: "1px solid #ef444450",
                              borderRadius: "6px",
                              fontSize: "13px",
                              fontWeight: 600,
                              cursor: "pointer",
                            }}
                          >
                            Reject
                          </button>
                          <button
                            onClick={() => handleApprove(rec.recommendation_id)}
                            disabled={actionLoading === rec.recommendation_id}
                            style={{
                              padding: "7px 18px",
                              backgroundColor: "#10b981",
                              color: "#ffffff",
                              border: "none",
                              borderRadius: "6px",
                              fontSize: "13px",
                              fontWeight: 600,
                              cursor: "pointer",
                            }}
                          >
                            {actionLoading === rec.recommendation_id ? "Processing..." : "Approve Recommendation"}
                          </button>
                        </>
                      )}

                      {isApproved && (
                        <button
                          onClick={() => handleApply(rec.recommendation_id)}
                          disabled={actionLoading === rec.recommendation_id}
                          style={{
                            padding: "8px 20px",
                            backgroundColor: "#2563eb",
                            color: "#ffffff",
                            border: "none",
                            borderRadius: "6px",
                            fontSize: "13px",
                            fontWeight: 600,
                            cursor: "pointer",
                          }}
                        >
                          {actionLoading === rec.recommendation_id
                            ? "Applying..."
                            : `Apply & Generate Immutable Version (V${rec.source_strategy_version} → V${rec.source_strategy_version + 1})`}
                        </button>
                      )}

                      {isApplied && (
                        <span style={{ fontSize: "13px", color: "#c084fc", fontWeight: 600 }}>
                          ✓ Applied to Strategy Version V{rec.target_strategy_version}
                        </span>
                      )}

                      {isRejected && (
                        <span style={{ fontSize: "13px", color: "#f87171" }}>
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
