import React, { useState, useEffect } from "react";
import {
  StrategyExperiment,
  SavedStrategy,
  OptimizationRecommendation,
  AdaptiveParameterSpec,
  MetricComparisonRow,
  listSavedStrategies,
  listExperiments,
  getExperiment,
  createExperiment,
  runExperiment,
  approveExperiment,
  rejectExperiment,
  applyExperiment,
  listOptimizationRecommendations,
  getAdaptiveParameters,
} from "./api";
import { Select } from "./components/ui/select";
import {
  CheckCircle2,
  XCircle,
  Play,
  ShieldCheck,
  History,
  AlertTriangle,
  FlaskConical,
  BarChart3,
  Layers,
  Sparkles,
  GitBranch,
  RefreshCw,
  PlusCircle,
} from "lucide-react";
import { useDialog } from "./components/ui/dialog-context";

export const StrategyExperimentLab: React.FC = () => {
  const [strategies, setStrategies] = useState<SavedStrategy[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string>("");
  const [experiments, setExperiments] = useState<StrategyExperiment[]>([]);
  const [selectedExperimentId, setSelectedExperimentId] = useState<string | null>(null);
  const [currentExperiment, setCurrentExperiment] = useState<StrategyExperiment | null>(null);

  // Recommendations and adaptive params for new experiment creation
  const [recommendations, setRecommendations] = useState<OptimizationRecommendation[]>([]);
  const [adaptiveParams, setAdaptiveParams] = useState<Record<string, AdaptiveParameterSpec>>({});

  // UI state
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const dialog = useDialog();
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [activeVisualTab, setActiveVisualTab] = useState<"equity" | "drawdown" | "monthly" | "distribution" | "regime" | "robustness">("equity");

  // Create modal form state
  const [creationMode, setCreationMode] = useState<"recommendation" | "manual">("recommendation");
  const [selectedRecId, setSelectedRecId] = useState<string>("");
  const [manualParamValues, setManualParamValues] = useState<Record<string, number>>({});
  const [experimentReason, setExperimentReason] = useState<string>("");

  // 1. Initial load of strategies
  useEffect(() => {
    listSavedStrategies()
      .then((res) => {
        const list = res.strategies || [];
        setStrategies(list);
        if (list.length > 0 && !selectedStrategyId) {
          setSelectedStrategyId(list[0].strategy_id);
        }
      })
      .catch((err) => {
        console.error("Failed to load strategies:", err);
      });
  }, []);

  // 2. Load experiments & strategy context when selected strategy changes
  useEffect(() => {
    if (!selectedStrategyId) return;
    setLoading(true);
    setError(null);

    Promise.all([
      listExperiments(selectedStrategyId).catch(() => ({ experiments: [], count: 0 })),
      listOptimizationRecommendations(selectedStrategyId).catch(() => ({ recommendations: [], count: 0 })),
      getAdaptiveParameters(selectedStrategyId).catch(() => ({ adaptive_parameters: {} })),
    ])
      .then(([expRes, recRes, paramRes]) => {
        const expList = expRes.experiments || [];
        setExperiments(expList);
        if (expList.length > 0) {
          setSelectedExperimentId(expList[0].experiment_id);
        } else {
          setSelectedExperimentId(null);
          setCurrentExperiment(null);
        }

        const recs = recRes.recommendations || [];
        setRecommendations(recs);
        if (recs.length > 0) {
          setSelectedRecId(recs[0].recommendation_id);
        }

        const params = (paramRes as any).adaptive_parameters || {};
        setAdaptiveParams(params);
        const initialManual: Record<string, number> = {};
        Object.entries(params).forEach(([k, spec]: [string, any]) => {
          initialManual[k] = spec.current ?? 0;
        });
        setManualParamValues(initialManual);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load experiment data");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [selectedStrategyId]);

  // 3. Load full experiment details when selected experiment changes
  useEffect(() => {
    if (!selectedExperimentId) {
      setCurrentExperiment(null);
      return;
    }

    getExperiment(selectedExperimentId)
      .then((exp) => {
        setCurrentExperiment(exp);
      })
      .catch((err) => {
        console.error("Failed to load experiment details:", err);
      });
  }, [selectedExperimentId]);

  // Refresh list helper
  const refreshExperimentList = async (selectId?: string) => {
    if (!selectedStrategyId) return;
    try {
      const expRes = await listExperiments(selectedStrategyId);
      const list = expRes.experiments || [];
      setExperiments(list);
      if (selectId) {
        setSelectedExperimentId(selectId);
      } else if (list.length > 0 && !selectedExperimentId) {
        setSelectedExperimentId(list[0].experiment_id);
      }
    } catch (err) {
      console.error("Failed to refresh experiment list:", err);
    }
  };

  // Actions
  const handleCreateExperiment = async () => {
    if (!selectedStrategyId) return;
    setActionLoading("create");
    setError(null);
    setSuccessMessage(null);

    try {
      let recIdToUse: string | undefined = undefined;
      let paramChanges: Record<string, any> | undefined = undefined;
      let reasonToUse = experimentReason.trim();

      if (creationMode === "recommendation") {
        if (!selectedRecId) {
          throw new Error("Please select an optimization recommendation.");
        }
        recIdToUse = selectedRecId;
        const rec = recommendations.find((r) => r.recommendation_id === selectedRecId);
        if (!reasonToUse && rec) {
          reasonToUse = `Generated from recommendation ${rec.recommendation_id.slice(0, 8)}: ${rec.reason}`;
        }
      } else {
        // Manual changes
        paramChanges = manualParamValues;
        if (!reasonToUse) {
          reasonToUse = "Manual adaptive parameter experiment hypothesis";
        }
      }

      const exp = await createExperiment({
        strategy_id: selectedStrategyId,
        recommendation_id: recIdToUse,
        parameter_changes: paramChanges,
        reason: reasonToUse,
      });

      setSuccessMessage(`Experiment created successfully (${exp.experiment_id.slice(0, 8)})`);
      setShowCreateModal(false);
      await refreshExperimentList(exp.experiment_id);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to create experiment");
    } finally {
      setActionLoading(null);
    }
  };

  const handleRunExperiment = async (experimentId: string) => {
    setActionLoading("run");
    setError(null);
    setSuccessMessage(null);

    try {
      const exp = await runExperiment(experimentId);
      setCurrentExperiment(exp);
      setSuccessMessage("Experiment execution completed across Backtest, Walk-Forward, and Robustness scans.");
      await refreshExperimentList(experimentId);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Experiment execution failed");
    } finally {
      setActionLoading(null);
    }
  };

  const handleApproveExperiment = async (experimentId: string) => {
    setActionLoading("approve");
    setError(null);
    setSuccessMessage(null);

    try {
      const exp = await approveExperiment(experimentId);
      setCurrentExperiment(exp);
      setSuccessMessage("Experiment approved. You may now apply it to create immutable version V(N+1).");
      await refreshExperimentList(experimentId);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to approve experiment");
    } finally {
      setActionLoading(null);
    }
  };

  const handleRejectExperiment = async (experimentId: string) => {
    const reason = await dialog.prompt({
      title: "Reject this experiment?",
      label: "Reason",
      placeholder: "Why? This is saved in the history.",
      required: true,
      confirmLabel: "Reject",
      tone: "danger",
    });
    if (reason === null) return;

    setActionLoading("reject");
    setError(null);
    setSuccessMessage(null);

    try {
      const exp = await rejectExperiment(experimentId, reason);
      setCurrentExperiment(exp);
      setSuccessMessage("Experiment rejected and recorded in history.");
      await refreshExperimentList(experimentId);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to reject experiment");
    } finally {
      setActionLoading(null);
    }
  };

  const handleApplyExperiment = async (experimentId: string) => {
    const ok = await dialog.confirm({
      title: "Create a new version?",
      description: "This saves the change as a new version. The current version stays as it is.",
      confirmLabel: "Create version",
    });
    if (!ok) return;

    setActionLoading("apply");
    setError(null);
    setSuccessMessage(null);

    try {
      const res = await applyExperiment(experimentId);
      setCurrentExperiment(res.experiment);
      setSuccessMessage(`Success! New immutable Strategy Version V${res.new_version.version} created. Source version remained untouched.`);
      await refreshExperimentList(experimentId);
    } catch (err: any) {
      setError(err instanceof Error ? err.message : "Failed to apply experiment");
    } finally {
      setActionLoading(null);
    }
  };

  const currentStrategy = strategies.find((s) => s.strategy_id === selectedStrategyId);

  // Status color helper
  const getStatusBadge = (status: string) => {
    switch (status) {
      case "CREATED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-muted/60 text-foreground border border-border">CREATED</span>;
      case "RUNNING":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse">RUNNING</span>;
      case "COMPLETED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-sky-500/20 text-sky-300 border border-sky-500/40">COMPLETED</span>;
      case "APPROVED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">APPROVED</span>;
      case "REJECTED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-rose-500/20 text-rose-300 border border-rose-500/40">REJECTED</span>;
      case "APPLIED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/40">APPLIED</span>;
      case "FAILED":
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-rose-600/30 text-rose-200 border border-rose-600">FAILED</span>;
      default:
        return <span className="px-2.5 py-1 text-xs font-semibold rounded-full bg-muted text-foreground">{status}</span>;
    }
  };

  // SVG Chart Helper for Equity Curve
  const renderEquityChart = () => {
    if (!currentExperiment?.results?.equity_curves) return null;
    const { baseline, candidate } = currentExperiment.results.equity_curves;
    if (!baseline || baseline.length < 2) return <div className="text-muted-foreground py-8 text-center text-sm">Insufficient data points to plot curve.</div>;

    const baseVals = baseline.map((p) => p.value);
    const candVals = candidate.map((p) => p.value);
    const allVals = [...baseVals, ...candVals];
    const minVal = Math.min(...allVals) * 0.995;
    const maxVal = Math.max(...allVals) * 1.005;
    const valRange = maxVal - minVal || 1;

    const width = 760;
    const height = 240;
    const padX = 50;
    const padY = 20;

    const getX = (idx: number, total: number) => padX + (idx / Math.max(1, total - 1)) * (width - padX - 20);
    const getY = (val: number) => height - padY - ((val - minVal) / valRange) * (height - 2 * padY);

    const baselinePoints = baseVals.map((v: number, i: number) => `${getX(i, baseVals.length)},${getY(v)}`).join(" ");
    const candidatePoints = candVals.map((v: number, i: number) => `${getX(i, candVals.length)},${getY(v)}`).join(" ");

    return (
      <div className="w-full overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-64 bg-muted/40 rounded-lg border border-border p-2">
          {[0, 0.25, 0.5, 0.75, 1].map((pct) => {
            const y = height - padY - pct * (height - 2 * padY);
            const val = minVal + pct * valRange;
            return (
              <g key={pct}>
                <line x1={padX} y1={y} x2={width - 20} y2={y} stroke="#1e293b" strokeDasharray="3 3" />
                <text x={padX - 8} y={y + 4} fill="#64748b" fontSize="10" textAnchor="end">
                  ₹{(val / 1000).toFixed(0)}k
                </text>
              </g>
            );
          })}

          <polyline fill="none" stroke="#94a3b8" strokeWidth="2" points={baselinePoints} strokeDasharray="4 2" />
          <polyline fill="none" stroke="#10b981" strokeWidth="2.5" points={candidatePoints} />

          <text x={padX} y={height - 4} fill="#64748b" fontSize="10" textAnchor="start">
            {baseline[0]?.ts || "Start"}
          </text>
          <text x={width - 20} y={height - 4} fill="#64748b" fontSize="10" textAnchor="end">
            {baseline[baseline.length - 1]?.ts || "End"}
          </text>
        </svg>

        <div className="flex items-center justify-end gap-6 mt-2 text-xs">
          <div className="flex items-center gap-2">
            <span className="w-4 h-0.5 border-b-2 border-dashed border-muted-foreground"></span>
            <span className="text-muted-foreground">Current Strategy (Baseline)</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-4 h-0.5 bg-emerald-500 rounded"></span>
            <span className="text-emerald-400 font-semibold">Learned Candidate</span>
          </div>
        </div>
      </div>
    );
  };

  // SVG Chart Helper for Drawdown Curve
  const renderDrawdownChart = () => {
    if (!currentExperiment?.results?.drawdown_curves) return null;
    const { baseline, candidate } = currentExperiment.results.drawdown_curves;
    if (!baseline || baseline.length < 2) return <div className="text-muted-foreground py-8 text-center text-sm">Insufficient data points to plot curve.</div>;

    const baseVals = baseline.map((p) => p.value);
    const candVals = candidate.map((p) => p.value);
    const allVals = [...baseVals, ...candVals];
    const minVal = Math.min(...allVals, -0.01);
    const maxVal = 0;
    const valRange = maxVal - minVal || 0.01;

    const width = 760;
    const height = 200;
    const padX = 50;
    const padY = 20;

    const getX = (idx: number, total: number) => padX + (idx / Math.max(1, total - 1)) * (width - padX - 20);
    const getY = (val: number) => padY + ((val - maxVal) / -valRange) * (height - 2 * padY);

    const baselinePoints = baseVals.map((v: number, i: number) => `${getX(i, baseVals.length)},${getY(v)}`).join(" ");
    const candidatePoints = candVals.map((v: number, i: number) => `${getX(i, candVals.length)},${getY(v)}`).join(" ");

    return (
      <div className="w-full overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-56 bg-muted/40 rounded-lg border border-border p-2">
          {[0, -0.05, -0.10, -0.15, -0.20].filter((v) => v >= minVal).map((val) => {
            const y = getY(val);
            return (
              <g key={val}>
                <line x1={padX} y1={y} x2={width - 20} y2={y} stroke="#1e293b" strokeDasharray="3 3" />
                <text x={padX - 8} y={y + 4} fill="#64748b" fontSize="10" textAnchor="end">
                  {(val * 100).toFixed(0)}%
                </text>
              </g>
            );
          })}

          <polyline fill="none" stroke="#f43f5e" strokeWidth="1.5" strokeDasharray="4 2" points={baselinePoints} />
          <polyline fill="none" stroke="#38bdf8" strokeWidth="2" points={candidatePoints} />
        </svg>

        <div className="flex items-center justify-end gap-6 mt-2 text-xs">
          <div className="flex items-center gap-2">
            <span className="w-4 h-0.5 border-b-2 border-dashed border-rose-400"></span>
            <span className="text-rose-400">Current Strategy DD</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-4 h-0.5 bg-sky-400 rounded"></span>
            <span className="text-sky-400 font-semibold">Candidate DD</span>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="text-foreground">
      {/* The page title is in the app bar, so no second heading here. */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 mb-4 border-b border-border">
        <div className="flex items-center gap-2.5">
          <FlaskConical className="size-4 text-primary" />
          <p className="text-[13px] text-muted-foreground">
            Current strategy vs learned candidate vs baseline &bull; statistical gating &bull; immutable versioning
          </p>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          {/* Strategy Picker */}
          <Select
            value={selectedStrategyId}
            onChange={setSelectedStrategyId}
            options={strategies.map((s) => ({
              value: s.strategy_id,
              label: `${s.name} (${s.strategy_id.slice(0, 8)})`,
            }))}
          />

          <button
            onClick={() => setShowCreateModal(true)}
            disabled={!selectedStrategyId}
            className="flex items-center gap-2 px-4 py-2 bg-primary hover:bg-primary/90 text-primary-foreground text-sm font-semibold rounded-lg shadow transition"
          >
            <PlusCircle className="w-4 h-4" />
            Create Experiment
          </button>
        </div>
      </div>

      {/* Safety Notice Banner */}
      <div className="my-4 p-3.5 bg-card border border-border rounded-lg flex items-center justify-between gap-4 text-xs text-foreground">
        <div className="flex items-center gap-3">
          <ShieldCheck className="w-5 h-5 text-emerald-400 shrink-0" />
          <div>
            <span className="font-semibold text-foreground">Strict Verification Protocol:</span> Candidate strategies run under identical dates, universe, capital, slippage, and costs. No orders are placed; strategy versions are strictly immutable.
          </div>
        </div>
        <div className="text-muted-foreground hidden sm:block">
          Active Strategy: <span className="font-mono text-emerald-400">{currentStrategy?.name || selectedStrategyId.slice(0, 8)}</span>
        </div>
      </div>

      {/* Alerts */}
      {error && (
        <div className="mb-4 p-3.5 bg-rose-950/60 border border-rose-800 text-rose-200 text-sm rounded-lg flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}
      {successMessage && (
        <div className="mb-4 p-3.5 bg-emerald-950/60 border border-emerald-800 text-emerald-200 text-sm rounded-lg flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4 shrink-0" />
          {successMessage}
        </div>
      )}

      {/* Main Grid: Experiments History Sidebar + Active Experiment Detail */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 mt-6">
        {/* Left Col: Experiment History (4 cols) */}
        <div className="lg:col-span-4 flex flex-col gap-4">
          <div className="p-4 bg-card border border-border rounded-xl">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
                <History className="w-4 h-4 text-muted-foreground" />
                Experiment History ({experiments.length})
              </h3>
              <button
                onClick={() => refreshExperimentList()}
                className="p-1 hover:bg-muted rounded text-muted-foreground hover:text-foreground"
                title="Refresh list"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>

            {loading ? (
              <div className="py-12 text-center text-muted-foreground text-sm">Loading experiments...</div>
            ) : experiments.length === 0 ? (
              <div className="py-10 text-center text-muted-foreground text-sm">
                No experiments found for this strategy.
                <div className="mt-2">
                  <button
                    onClick={() => setShowCreateModal(true)}
                    className="text-emerald-400 hover:underline text-xs"
                  >
                    + Create your first experiment
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col gap-2 max-h-[640px] overflow-y-auto pr-1">
                {experiments.map((exp) => {
                  const isSelected = exp.experiment_id === selectedExperimentId;
                  const paramKeys = Object.keys(exp.parameter_changes || {});
                  return (
                    <div
                      key={exp.experiment_id}
                      onClick={() => setSelectedExperimentId(exp.experiment_id)}
                      className={`p-3 rounded-lg border cursor-pointer transition ${
                        isSelected
                          ? "bg-muted/90 border-emerald-500/60 shadow-sm"
                          : "bg-card border-border hover:bg-muted/50 hover:border-border"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2 mb-1.5">
                        <span className="font-mono text-xs font-semibold text-foreground">
                          Exp #{exp.experiment_id.slice(0, 8)}
                        </span>
                        {getStatusBadge(exp.status)}
                      </div>

                      <div className="text-xs text-foreground font-medium truncate mb-1">
                        {paramKeys.length > 0
                          ? paramKeys.map((p) => `${p}: ${exp.parameter_changes[p]?.baseline ?? ""} → ${exp.parameter_changes[p]?.candidate ?? ""}`).join(", ")
                          : exp.name || "Baseline calibration"}
                      </div>

                      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                        <span>Source V{exp.source_version} {exp.target_version ? `→ V${exp.target_version}` : ""}</span>
                        <span>{new Date(exp.created_at).toLocaleDateString()}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* Right Col: Active Experiment Workbench (8 cols) */}
        <div className="lg:col-span-8 flex flex-col gap-6">
          {!currentExperiment ? (
            <div className="p-12 text-center bg-card border border-border rounded-xl text-muted-foreground">
              <FlaskConical className="w-12 h-12 text-muted-foreground mx-auto mb-3" />
              <div className="text-base font-medium text-foreground">No Experiment Selected</div>
              <p className="text-sm mt-1 text-muted-foreground">
                Select an experiment from the history list or create a new experiment to begin side-by-side evaluation.
              </p>
            </div>
          ) : (
            <>
              {/* Header Card with Provenance & Action Workflow */}
              <div className="p-5 bg-card border border-border rounded-xl shadow-sm">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-4 pb-4 border-b border-border">
                  <div>
                    <div className="flex items-center gap-3">
                      <h2 className="text-lg font-bold text-foreground font-mono">
                        Experiment #{currentExperiment.experiment_id.slice(0, 10)}
                      </h2>
                      {getStatusBadge(currentExperiment.status)}
                    </div>
                    <div className="text-xs text-muted-foreground mt-1 flex items-center gap-4 flex-wrap">
                      <span>Source Version: <strong className="text-foreground">V{currentExperiment.source_version}</strong></span>
                      <span>Creator: <strong className="text-foreground">{currentExperiment.creator_user_id || "system"}</strong></span>
                      <span>Created: <strong className="text-foreground">{new Date(currentExperiment.created_at).toLocaleString()}</strong></span>
                      {currentExperiment.target_version && (
                        <span className="text-purple-400 font-semibold">Applied To: V{currentExperiment.target_version}</span>
                      )}
                    </div>
                  </div>

                  {/* Actions Bar based on Status */}
                  <div className="flex items-center gap-2 flex-wrap">
                    {currentExperiment.status === "CREATED" && (
                      <button
                        onClick={() => handleRunExperiment(currentExperiment.experiment_id)}
                        disabled={actionLoading === "run"}
                        className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-foreground text-xs font-semibold rounded-lg shadow transition"
                      >
                        <Play className="w-3.5 h-3.5" />
                        {actionLoading === "run" ? "Running Evaluation..." : "Run Experiment"}
                      </button>
                    )}

                    {currentExperiment.status === "RUNNING" && (
                      <span className="text-xs text-amber-400 flex items-center gap-2">
                        <RefreshCw className="w-4 h-4 animate-spin" />
                        Running Backtest, OOS, & Robustness...
                      </span>
                    )}

                    {currentExperiment.status === "COMPLETED" && (
                      <>
                        <button
                          onClick={() => handleApproveExperiment(currentExperiment.experiment_id)}
                          disabled={actionLoading === "approve"}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-primary hover:bg-primary/90 text-primary-foreground text-xs font-semibold rounded-lg shadow transition"
                        >
                          <CheckCircle2 className="w-3.5 h-3.5" />
                          Approve Experiment
                        </button>
                        <button
                          onClick={() => handleRejectExperiment(currentExperiment.experiment_id)}
                          disabled={actionLoading === "reject"}
                          className="flex items-center gap-1.5 px-3 py-1.5 bg-rose-700/80 hover:bg-rose-600 text-foreground text-xs font-semibold rounded-lg transition"
                        >
                          <XCircle className="w-3.5 h-3.5" />
                          Reject
                        </button>
                      </>
                    )}

                    {currentExperiment.status === "APPROVED" && (
                      <button
                        onClick={() => handleApplyExperiment(currentExperiment.experiment_id)}
                        disabled={actionLoading === "apply"}
                        className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-500 text-foreground text-xs font-semibold rounded-lg shadow transition"
                      >
                        <GitBranch className="w-3.5 h-3.5" />
                        {actionLoading === "apply" ? "Creating V(N+1)..." : "Apply to New Immutable V(N+1)"}
                      </button>
                    )}

                    {currentExperiment.status === "REJECTED" && (
                      <div className="text-xs text-rose-300 bg-rose-950/50 px-3 py-1.5 rounded border border-rose-800">
                        Rejected: {currentExperiment.rejection_reason || "Sensitivity / Risk constraint"}
                      </div>
                    )}

                    {currentExperiment.status === "APPLIED" && (
                      <div className="text-xs text-purple-300 bg-purple-950/50 px-3 py-1.5 rounded border border-purple-800 flex items-center gap-1.5">
                        <CheckCircle2 className="w-3.5 h-3.5 text-purple-400" />
                        Immutable Version V{currentExperiment.target_version} Active
                      </div>
                    )}
                  </div>
                </div>

                {/* Reason Note */}
                <div className="text-xs text-foreground">
                  <span className="font-semibold text-muted-foreground">Hypothesis / Trigger Reason:</span> {currentExperiment.reason}
                </div>
              </div>

              {/* SECTION 4: EXPLAIN THE CHANGE (Derived, Non-Hallucinated) */}
              {currentExperiment.explanation && (
                <div className="p-5 bg-gradient-to-br from-card via-card to-muted/40 border border-border rounded-xl shadow-sm">
                  <div className="flex items-center gap-2 mb-4 pb-2 border-b border-border text-emerald-400">
                    <Sparkles className="w-4 h-4" />
                    <h3 className="text-sm font-bold uppercase tracking-wider text-foreground">
                      Structured Change Explanation
                    </h3>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    {/* WHAT CHANGED */}
                    <div className="p-3.5 bg-muted/40 border border-border rounded-lg">
                      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                        1. What Changed
                      </div>
                      <div className="font-mono text-xs text-foreground whitespace-pre-wrap leading-relaxed">
                        {currentExperiment.explanation.what_changed || "No parameters modified"}
                      </div>
                    </div>

                    {/* WHY IT WAS PROPOSED */}
                    <div className="p-3.5 bg-muted/40 border border-border rounded-lg">
                      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                        2. Why It Was Proposed
                      </div>
                      <div className="text-xs text-foreground leading-relaxed whitespace-pre-wrap">
                        {currentExperiment.explanation.why_proposed}
                      </div>
                    </div>

                    {/* WHAT THE EXPERIMENT FOUND */}
                    <div className="p-3.5 bg-muted/40 border border-border rounded-lg">
                      <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                        3. What The Experiment Found
                      </div>
                      <div className="text-xs text-foreground leading-relaxed whitespace-pre-wrap font-mono">
                        {currentExperiment.explanation.what_experiment_found}
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* SECTION 3: SIDE-BY-SIDE METRICS TABLE */}
              {currentExperiment.results?.metrics_table ? (
                <div className="p-5 bg-card border border-border rounded-xl shadow-sm">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-bold text-foreground flex items-center gap-2">
                      <BarChart3 className="w-4 h-4 text-emerald-400" />
                      Side-by-Side Performance Comparison
                    </h3>
                    <span className="text-xs text-muted-foreground">Identical backtest parameters & costs</span>
                  </div>

                  <div className="overflow-x-auto">
                    <table className="w-full text-left border-collapse text-xs">
                      <thead>
                        <tr className="border-b border-border bg-muted/40 text-muted-foreground font-semibold">
                          <th className="py-2.5 px-3">Metric</th>
                          <th className="py-2.5 px-3 text-right">Current ($V_N$)</th>
                          <th className="py-2.5 px-3 text-right">Learned Candidate</th>
                          <th className="py-2.5 px-3 text-right">Difference (Δ)</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border font-mono">
                        {currentExperiment.results.metrics_table.map((row: MetricComparisonRow) => {
                          const isBetter = (row.difference ?? 0) > 0;
                          const isWorse = (row.difference ?? 0) < 0;
                          const deltaGood = row.higher_is_better ? isBetter : isWorse;

                          const formatVal = (val: number | null) => {
                            if (val === null || val === undefined) return "—";
                            if (row.unit === "%") return `${val.toFixed(2)}%`;
                            if (Number.isInteger(val)) return val.toString();
                            return val.toFixed(2);
                          };

                          return (
                            <tr key={row.metric} className="hover:bg-muted/30 transition">
                              <td className="py-2.5 px-3 font-sans font-medium text-foreground">
                                {row.label}
                              </td>
                              <td className="py-2.5 px-3 text-right text-muted-foreground">
                                {formatVal(row.current)}
                              </td>
                              <td className="py-2.5 px-3 text-right font-bold text-foreground">
                                {formatVal(row.candidate)}
                              </td>
                              <td
                                className={`py-2.5 px-3 text-right font-semibold ${
                                  row.difference === 0 || row.difference === null
                                    ? "text-muted-foreground"
                                    : deltaGood
                                    ? "text-emerald-400"
                                    : "text-rose-400"
                                }`}
                              >
                                {row.difference !== null && row.difference !== 0 ? (
                                  <>
                                    {row.difference > 0 ? "+" : ""}
                                    {row.unit === "%" ? `${row.difference.toFixed(2)}%` : row.difference.toFixed(2)}
                                  </>
                                ) : (
                                  "—"
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : (
                <div className="p-8 text-center bg-card border border-border rounded-xl text-muted-foreground text-xs">
                  Evaluation results not available. Click &quot;Run Experiment&quot; to execute identical baseline and candidate evaluations.
                </div>
              )}

              {/* SECTION 3 (cont): COMPARISON VISUALIZATIONS TABS */}
              {currentExperiment.results && (
                <div className="p-5 bg-card border border-border rounded-xl shadow-sm">
                  <div className="flex items-center justify-between gap-4 pb-3 border-b border-border flex-wrap">
                    <h3 className="text-sm font-bold text-foreground flex items-center gap-2">
                      <Layers className="w-4 h-4 text-emerald-400" />
                      Visual Performance Curves & Distributions
                    </h3>

                    {/* Subtabs */}
                    <div className="flex items-center gap-1 bg-background p-1 rounded-lg border border-border text-xs">
                      {(
                        [
                          { id: "equity", label: "Equity Curve" },
                          { id: "drawdown", label: "Drawdown" },
                          { id: "monthly", label: "Monthly Matrix" },
                          { id: "distribution", label: "Trade Returns" },
                          { id: "regime", label: "Regime Breakdown" },
                          { id: "robustness", label: "Walk-Forward & Robustness" },
                        ] as const
                      ).map((tab) => (
                        <button
                          key={tab.id}
                          onClick={() => setActiveVisualTab(tab.id)}
                          className={`px-2.5 py-1 rounded font-medium transition ${
                            activeVisualTab === tab.id
                              ? "bg-muted text-foreground shadow-sm"
                              : "text-muted-foreground hover:text-foreground"
                          }`}
                        >
                          {tab.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="mt-4">
                    {/* Equity Curve Tab */}
                    {activeVisualTab === "equity" && renderEquityChart()}

                    {/* Drawdown Tab */}
                    {activeVisualTab === "drawdown" && renderDrawdownChart()}

                    {/* Monthly Returns Tab */}
                    {activeVisualTab === "monthly" && (
                      <div className="overflow-x-auto">
                        {currentExperiment.results.monthly_returns?.candidate && currentExperiment.results.monthly_returns.candidate.length > 0 ? (
                          <table className="w-full text-left border-collapse text-xs font-mono">
                            <thead>
                              <tr className="border-b border-border text-muted-foreground">
                                <th className="py-2 px-3 font-sans">Year / Month</th>
                                <th className="py-2 px-3 text-right">Current Return</th>
                                <th className="py-2 px-3 text-right">Candidate Return</th>
                                <th className="py-2 px-3 text-right">Diff (Δ)</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-border">
                              {currentExperiment.results.monthly_returns.candidate.map((candRow, idx) => {
                                const baseRow = currentExperiment.results?.monthly_returns?.baseline?.[idx];
                                const diff = candRow.return_pct - (baseRow?.return_pct ?? 0);
                                return (
                                  <tr key={`${candRow.year}-${candRow.month}`} className="hover:bg-muted/20">
                                    <td className="py-2 px-3 font-sans text-foreground">
                                      {candRow.year}-{String(candRow.month).padStart(2, "0")}
                                    </td>
                                    <td className="py-2 px-3 text-right text-muted-foreground">
                                      {baseRow ? `${baseRow.return_pct.toFixed(2)}%` : "—"}
                                    </td>
                                    <td className="py-2 px-3 text-right font-bold text-foreground">
                                      {candRow.return_pct.toFixed(2)}%
                                    </td>
                                    <td className={`py-2 px-3 text-right font-semibold ${diff >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                                      {diff >= 0 ? "+" : ""}{diff.toFixed(2)}%
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        ) : (
                          <div className="py-6 text-center text-muted-foreground text-xs">No monthly return data recorded.</div>
                        )}
                      </div>
                    )}

                    {/* Trade Distributions Tab */}
                    {activeVisualTab === "distribution" && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="p-4 bg-muted/40 border border-border rounded-lg">
                          <h4 className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-3">
                            Current Strategy Return Distribution
                          </h4>
                          {currentExperiment.results.return_distributions?.baseline ? (
                            <div className="flex flex-col gap-2 font-mono text-xs">
                              <div className="flex justify-between text-foreground">
                                <span>Mean Return:</span>
                                <strong>{currentExperiment.results.return_distributions.baseline.mean.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-foreground">
                                <span>Median Return:</span>
                                <strong>{currentExperiment.results.return_distributions.baseline.median.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-foreground">
                                <span>Std Dev:</span>
                                <strong>{currentExperiment.results.return_distributions.baseline.std.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-muted-foreground">
                                <span>Skewness / Kurtosis:</span>
                                <span>
                                  {currentExperiment.results.return_distributions.baseline.skew.toFixed(2)} / {currentExperiment.results.return_distributions.baseline.kurtosis.toFixed(2)}
                                </span>
                              </div>
                            </div>
                          ) : (
                            <span className="text-xs text-muted-foreground">No trade distribution available</span>
                          )}
                        </div>

                        <div className="p-4 bg-muted/40 border border-border rounded-lg">
                          <h4 className="text-xs font-bold text-emerald-400 uppercase tracking-wider mb-3">
                            Learned Candidate Return Distribution
                          </h4>
                          {currentExperiment.results.return_distributions?.candidate ? (
                            <div className="flex flex-col gap-2 font-mono text-xs">
                              <div className="flex justify-between text-foreground">
                                <span>Mean Return:</span>
                                <strong className="text-emerald-400">{currentExperiment.results.return_distributions.candidate.mean.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-foreground">
                                <span>Median Return:</span>
                                <strong className="text-foreground">{currentExperiment.results.return_distributions.candidate.median.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-foreground">
                                <span>Std Dev:</span>
                                <strong className="text-foreground">{currentExperiment.results.return_distributions.candidate.std.toFixed(2)}%</strong>
                              </div>
                              <div className="flex justify-between text-muted-foreground">
                                <span>Skewness / Kurtosis:</span>
                                <span>
                                  {currentExperiment.results.return_distributions.candidate.skew.toFixed(2)} / {currentExperiment.results.return_distributions.candidate.kurtosis.toFixed(2)}
                                </span>
                              </div>
                            </div>
                          ) : (
                            <span className="text-xs text-muted-foreground">No trade distribution available</span>
                          )}
                        </div>
                      </div>
                    )}

                    {/* Regime Breakdown Tab */}
                    {activeVisualTab === "regime" && (
                      <div className="flex flex-col gap-3">
                        {currentExperiment.results.regime_performance?.candidate && currentExperiment.results.regime_performance.candidate.length > 0 ? (
                          currentExperiment.results.regime_performance.candidate.map((candRegime, idx) => {
                            const baseRegime = currentExperiment.results?.regime_performance?.baseline?.[idx];
                            const diff = candRegime.mean_return_pct - (baseRegime?.mean_return_pct ?? 0);
                            return (
                              <div key={candRegime.regime} className="p-3 bg-muted/40 border border-border rounded-lg flex items-center justify-between text-xs">
                                <div>
                                  <span className="font-semibold text-foreground">{candRegime.regime}</span>
                                  <span className="text-muted-foreground ml-2">({candRegime.trades} forward trades)</span>
                                </div>
                                <div className="flex items-center gap-4 font-mono">
                                  <span className="text-muted-foreground">Base: {baseRegime?.mean_return_pct.toFixed(2) ?? "—"}%</span>
                                  <span className="text-foreground font-bold">Cand: {candRegime.mean_return_pct.toFixed(2)}%</span>
                                  <span className={`font-semibold ${diff >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                                    {diff >= 0 ? "+" : ""}{diff.toFixed(2)}%
                                  </span>
                                </div>
                              </div>
                            );
                          })
                        ) : (
                          <div className="py-6 text-center text-muted-foreground text-xs">No regime breakdown available for this evaluation.</div>
                        )}
                      </div>
                    )}

                    {/* Walk-Forward & Robustness Tab */}
                    {activeVisualTab === "robustness" && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                        <div className="p-4 bg-muted/40 border border-border rounded-lg">
                          <h4 className="font-bold text-foreground uppercase tracking-wider mb-2">
                            Walk-Forward Out-Of-Sample Validation
                          </h4>
                          {currentExperiment.results.walk_forward_metrics ? (
                            <div className="flex flex-col gap-2 font-mono">
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">In-Sample PF:</span>
                                <span>{currentExperiment.results.walk_forward_metrics.in_sample_profit_factor.toFixed(2)}</span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">Out-of-Sample PF:</span>
                                <span className="font-bold text-foreground">{currentExperiment.results.walk_forward_metrics.out_of_sample_profit_factor.toFixed(2)}</span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">OOS Efficiency Ratio:</span>
                                <span className="font-bold text-emerald-400">
                                  {(currentExperiment.results.walk_forward_metrics.efficiency_ratio * 100).toFixed(1)}%
                                </span>
                              </div>
                              <div className="flex justify-between pt-1 border-t border-border text-foreground font-sans">
                                <span>Gating Verdict:</span>
                                <span className={currentExperiment.results.walk_forward_metrics.passed ? "text-emerald-400 font-bold" : "text-amber-400"}>
                                  {currentExperiment.results.walk_forward_metrics.verdict}
                                </span>
                              </div>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">No walk-forward data</span>
                          )}
                        </div>

                        <div className="p-4 bg-muted/40 border border-border rounded-lg">
                          <h4 className="font-bold text-foreground uppercase tracking-wider mb-2">
                            Parameter Neighborhood Robustness
                          </h4>
                          {currentExperiment.results.robustness_results ? (
                            <div className="flex flex-col gap-2 font-mono">
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">Is Plateau:</span>
                                <span className={currentExperiment.results.robustness_results.is_stable ? "text-emerald-400 font-bold" : "text-amber-400"}>
                                  {currentExperiment.results.robustness_results.is_stable ? "YES (Stable Plateau)" : "NO (Spike Warning)"}
                                </span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">Stable Parameter Range:</span>
                                <span className="font-bold text-foreground">
                                  [{currentExperiment.results.robustness_results.stable_range?.[0]} – {currentExperiment.results.robustness_results.stable_range?.[1]}]
                                </span>
                              </div>
                              <div className="flex justify-between pt-1 border-t border-border text-foreground font-sans">
                                <span>Sensitivity Verdict:</span>
                                <span className="text-foreground">
                                  {currentExperiment.results.robustness_results.sensitivity_verdict}
                                </span>
                              </div>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">No robustness scan data</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* CREATE EXPERIMENT MODAL */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4 backdrop-blur-sm">
          <div className="bg-card border border-border rounded-xl max-w-xl w-full p-6 shadow-2xl">
            <div className="flex items-center justify-between pb-4 mb-4 border-b border-border">
              <h3 className="text-base font-bold text-foreground flex items-center gap-2">
                <FlaskConical className="w-5 h-5 text-emerald-400" />
                Create Strategy Experiment
              </h3>
              <button
                onClick={() => setShowCreateModal(false)}
                className="text-muted-foreground hover:text-foreground p-1"
              >
                ✕
              </button>
            </div>

            <div className="flex flex-col gap-4 text-xs">
              {/* Creation Source Toggle */}
              <div>
                <label className="block text-muted-foreground font-semibold mb-1">Experiment Creation Source</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setCreationMode("recommendation")}
                    className={`py-2 px-3 rounded-lg border text-center font-medium transition ${
                      creationMode === "recommendation"
                        ? "bg-emerald-600/20 border-emerald-500 text-emerald-300"
                        : "bg-background border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    From Optimization Rec
                  </button>
                  <button
                    type="button"
                    onClick={() => setCreationMode("manual")}
                    className={`py-2 px-3 rounded-lg border text-center font-medium transition ${
                      creationMode === "manual"
                        ? "bg-emerald-600/20 border-emerald-500 text-emerald-300"
                        : "bg-background border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    Manual Adaptive Param
                  </button>
                </div>
              </div>

              {/* Recommendation Selector */}
              {creationMode === "recommendation" ? (
                <div>
                  <label className="block text-muted-foreground font-semibold mb-1">Select Candidate Recommendation</label>
                  {recommendations.length === 0 ? (
                    <div className="p-3 bg-background border border-border rounded-lg text-muted-foreground text-xs">
                      No active optimization recommendations for this strategy. You can switch to &quot;Manual Adaptive Param&quot; or run the optimization cycle first.
                    </div>
                  ) : (
                    <Select
                      className="w-full"
                      value={selectedRecId}
                      onChange={setSelectedRecId}
                      options={recommendations.map((r) => ({
                        value: r.recommendation_id,
                        label: `#${r.recommendation_id.slice(0, 8)}: ${r.parameter} (${r.current_value} → ${r.proposed_value})`,
                      }))}
                    />
                  )}
                </div>
              ) : (
                /* Manual Parameter Inputs */
                <div>
                  <label className="block text-muted-foreground font-semibold mb-1">Adaptive Parameters</label>
                  {Object.keys(adaptiveParams).length === 0 ? (
                    <div className="p-3 bg-background border border-border rounded-lg text-muted-foreground text-xs">
                      No adaptive parameters registered for this strategy.
                    </div>
                  ) : (
                    <div className="flex flex-col gap-2 max-h-48 overflow-y-auto p-2 bg-background rounded-lg border border-border">
                      {Object.entries(adaptiveParams).map(([paramName, spec]) => (
                        <div key={paramName} className="flex items-center justify-between gap-3 text-xs">
                          <div>
                            <span className="font-semibold text-foreground">{paramName}</span>
                            <span className="text-muted-foreground ml-2">
                              [{spec.minimum} – {spec.maximum}, step {spec.step}]
                            </span>
                          </div>
                          <input
                            type="number"
                            min={spec.minimum}
                            max={spec.maximum}
                            step={spec.step}
                            value={manualParamValues[paramName] ?? spec.current}
                            onChange={(e) =>
                              setManualParamValues({
                                ...manualParamValues,
                                [paramName]: parseFloat(e.target.value),
                              })
                            }
                            className="w-24 px-2 py-1 bg-card border border-border rounded text-right text-foreground font-mono text-xs focus:ring-1 focus:ring-emerald-500"
                          />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Hypothesis / Reason */}
              <div>
                <label className="block text-muted-foreground font-semibold mb-1">Reason / Source Observation</label>
                <textarea
                  rows={2}
                  value={experimentReason}
                  onChange={(e) => setExperimentReason(e.target.value)}
                  placeholder="e.g. 84 forward trades observed with RVOL >= 2.0x showed mean return diff +0.82%"
                  className="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground text-xs focus:outline-none focus:ring-1 focus:ring-emerald-500"
                />
              </div>

              <div className="pt-3 border-t border-border flex justify-end gap-3 mt-2">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="px-4 py-2 bg-muted hover:bg-muted text-foreground rounded-lg font-medium"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleCreateExperiment}
                  disabled={actionLoading === "create"}
                  className="px-4 py-2 bg-primary hover:bg-primary/90 text-primary-foreground font-semibold rounded-lg shadow"
                >
                  {actionLoading === "create" ? "Creating..." : "Create Experiment"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
