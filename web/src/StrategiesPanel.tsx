import { AlertTriangle, CheckCircle2, FlaskConical, HelpCircle, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { getStrategies, type StrategyInfo, type ValidationState } from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Badge, Callout, fmtNum, fmtPct } from "./components/ui/stat";
import { cn } from "./lib/utils";

/**
 * Strategies — decide.
 *
 * The registry is where a strategy earns the right to be run. Each entry shows
 * its walk-forward verdict, because a list that renders a validated strategy
 * and an untested one identically invites the exact mistake this project exists
 * to avoid. There is currently no strategy in the "pass" state, and the page
 * says so rather than hiding it behind a table.
 */
export default function StrategiesPanel({ onOpenResearch }: { onOpenResearch: () => void }) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [control, setControl] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<ValidationState | "all">("all");

  const load = useCallback(async () => {
    try {
      const r = await getStrategies();
      setStrategies(r.strategies);
      setAsOf(r.validation_as_of ?? null);
      setControl(r.control_sharpe ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = {
    pass: strategies.filter((s) => s.validation.state === "pass").length,
    fail: strategies.filter((s) => s.validation.state === "fail").length,
    untested: strategies.filter((s) => s.validation.state === "untested").length,
  };
  const shown = filter === "all" ? strategies : strategies.filter((s) => s.validation.state === filter);

  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}

      <Callout tone={counts.pass > 0 ? "info" : "warn"}>
        <span className="font-medium">
          {counts.pass} of {strategies.length} strategies have cleared out-of-sample validation.
        </span>{" "}
        {counts.fail > 0 && (
          <>
            {counts.fail} were tested and lost
            {control !== null && <> (random selection over the same universe scores {fmtNum(control)} Sharpe)</>}.{" "}
          </>
        )}
        {counts.untested > 0 && (
          <>
            {counts.untested} {counts.untested === 1 ? "has" : "have"} never been tested — treat
            those as unknown, not as working.
          </>
        )}
      </Callout>

      <div className="grid gap-3 sm:grid-cols-3">
        <Tally
          label="Validated"
          value={counts.pass}
          tone="pass"
          active={filter === "pass"}
          onClick={() => setFilter(filter === "pass" ? "all" : "pass")}
        />
        <Tally
          label="Tested, lost"
          value={counts.fail}
          tone="fail"
          active={filter === "fail"}
          onClick={() => setFilter(filter === "fail" ? "all" : "fail")}
        />
        <Tally
          label="Never tested"
          value={counts.untested}
          tone="untested"
          active={filter === "untested"}
          onClick={() => setFilter(filter === "untested" ? "all" : "untested")}
        />
      </div>

      <Card>
        <CardHeader
          title="Registry"
          sub={
            asOf
              ? `Validation last run ${new Date(asOf).toLocaleString()}`
              : "No validation run on disk yet"
          }
        />
        <div className="divide-y divide-border/60">
          {shown.map((s) => (
            <Row key={s.name} s={s} onOpenResearch={onOpenResearch} />
          ))}
          {shown.length === 0 && (
            <div className="px-5 py-6 text-center text-[13px] text-muted-foreground">
              No strategies in this category.
            </div>
          )}
        </div>
      </Card>

      <Hint>
        A strategy that has never been run out of sample is not a candidate — it is an unknown, and
        the UI will not pretend otherwise. To test one, open Evidence → Research and run it against
        full history.
      </Hint>
    </div>
  );
}

function Row({ s, onOpenResearch }: { s: StrategyInfo; onOpenResearch: () => void }) {
  const v = s.validation;
  const beatBench =
    v.oos_sharpe !== null && v.oos_sharpe !== undefined &&
    v.benchmark_sharpe !== null && v.benchmark_sharpe !== undefined
      ? v.oos_sharpe > v.benchmark_sharpe
      : null;

  return (
    <div className="px-5 py-3.5">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <StateIcon state={v.state} />
          <div className="min-w-0">
            <div className="truncate text-[13.5px] font-medium">{pretty(s.name)}</div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11.5px] text-muted-foreground">
              {s.warmup_bars !== null && <span>needs {s.warmup_bars} bars to warm up</span>}
              {s.tunable.length > 0 ? (
                <span>tunable: {s.tunable.join(", ")}</span>
              ) : (
                <span>no tunable parameters</span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <StateBadge state={v.state} />
          {v.state === "untested" && (
            <button
              type="button"
              onClick={onOpenResearch}
              className="inline-flex items-center gap-1 rounded-md border border-border/70 px-2 py-1 text-[11.5px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <FlaskConical className="h-3 w-3" />
              Test it
            </button>
          )}
        </div>
      </div>

      {v.state === "fail" && v.note && (
        <div className="mt-2 pl-7 text-[12px] text-muted-foreground">{v.note}</div>
      )}

      {v.state === "fail" && (v.oos_sharpe !== null || v.deflated_sharpe !== null) && (
        <div className="mt-2 grid gap-x-5 gap-y-1 pl-7 text-[12px] sm:grid-cols-2 lg:grid-cols-4">
          {v.oos_sharpe !== null && v.oos_sharpe !== undefined && (
            <Metric
              label="OOS Sharpe"
              value={fmtNum(v.oos_sharpe)}
              bad={beatBench === false}
              note={beatBench === false ? "behind buy & hold" : undefined}
            />
          )}
          {v.oos_return_pct !== null && v.oos_return_pct !== undefined && (
            <Metric label="OOS return" value={fmtPct(v.oos_return_pct, 1)} bad={v.oos_return_pct < 0} />
          )}
          {v.measured_win_rate !== null && v.measured_win_rate !== undefined && (
            <Metric
              label="Measured win rate"
              value={fmtPct(v.measured_win_rate * 100, 1)}
              note={v.measured_trades ? `${v.measured_trades.toLocaleString()} trades` : undefined}
            />
          )}
          {v.expectancy_r !== null && v.expectancy_r !== undefined && (
            <Metric
              label="Expectancy"
              value={`${v.expectancy_r >= 0 ? "+" : ""}${fmtNum(v.expectancy_r, 3)}R`}
              bad={v.expectancy_r < 0}
            />
          )}
        </div>
      )}

      {v.state === "fail" && v.z_vs_control !== null && v.z_vs_control !== undefined && (
        <div className="mt-2 pl-7 text-[12px]">
          <span className="text-muted-foreground">vs random selection: </span>
          <span
            className={cn(
              "tabular-nums",
              v.z_vs_control >= 2
                ? "font-medium text-emerald-600 dark:text-emerald-400"
                : "text-muted-foreground",
            )}
          >
            {v.z_vs_control >= 0 ? "+" : ""}
            {fmtNum(v.z_vs_control)}σ
          </span>
          {v.z_vs_control < 2 && (
            <span className="ml-2 text-muted-foreground">
              — within range of what random picks produce
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  bad,
  note,
}: {
  label: string;
  value: string;
  bad?: boolean;
  note?: string;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={cn(
          "font-medium tabular-nums",
          bad ? "text-destructive" : undefined,
        )}
      >
        {value}
      </span>
      {note ? <span className="text-[11px] text-muted-foreground">({note})</span> : null}
    </div>
  );
}

function StateIcon({ state }: { state: ValidationState }) {
  if (state === "pass") return <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />;
  if (state === "fail") return <XCircle className="h-4 w-4 shrink-0 text-destructive" />;
  return <HelpCircle className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

function StateBadge({ state }: { state: ValidationState }) {
  if (state === "pass") return <Badge tone="good">validated</Badge>;
  if (state === "fail") return <Badge tone="bad">tested, lost</Badge>;
  return <Badge tone="flat">never tested</Badge>;
}

function Tally({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone: ValidationState;
  active: boolean;
  onClick: () => void;
}) {
  const palette = {
    pass: "text-emerald-600 dark:text-emerald-400",
    fail: "text-destructive",
    untested: "text-muted-foreground",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-xl border p-3.5 text-left transition-colors",
        active ? "border-primary/50 bg-primary/[0.06]" : "border-border/60 hover:bg-muted/40",
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
        {tone === "pass" ? (
          <CheckCircle2 className="h-3.5 w-3.5" />
        ) : tone === "fail" ? (
          <XCircle className="h-3.5 w-3.5" />
        ) : (
          <AlertTriangle className="h-3.5 w-3.5" />
        )}
        {label}
      </div>
      <div className={cn("mt-1 text-2xl font-medium tabular-nums", palette)}>{value}</div>
    </button>
  );
}

const pretty = (s: string) =>
  s
    .replace(/^paper_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
