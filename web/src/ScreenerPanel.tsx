/**
 * Screener — define conditions, scan the NSE universe, rank, and see *why*.
 *
 * The organising idea is that the reason is the product. A screener that hands
 * back 40 tickers is a list; one that hands back 40 tickers and the arithmetic
 * that put each one there is something you can act on and audit. So the results
 * table is not a table of numbers with a hidden detail pane — every row expands
 * to the actual comparisons that were tested, with the measured values in them:
 *
 *     ✓ Close > EMA 50          Close > EMA 50 = 419.00, satisfies > EMA 50 403.03
 *     ✓ RSI 14 < 70.00          RSI 14 < 70.00 = 59.84, satisfies < 70.00
 *
 * Three things this panel deliberately does differently from the legacy custom
 * scanner:
 *
 *  1. **Nested groups.** `(A AND B) OR (C AND D)` is buildable, because the
 *     backend evaluates a real tree. The legacy panel had one global AND/OR.
 *  2. **Unavailable indicators are shown and disabled.** `market_cap` is in the
 *     dropdown, greyed, with the reason ("requires the fundamentals service").
 *     Hiding it would leave a user wondering if they imagined the field.
 *  3. **"Could not be measured" is never rendered as a failure.** The backend
 *     distinguishes the two; collapsing them here would throw that away.
 */
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Plus, Save, Search, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  screenerColumns,
  screenerDeleteSaved,
  screenerIndicators,
  screenerListSaved,
  screenerRun,
  screenerSave,
  screenerSavedResults,
  screenerUniverses,
  screenerValidate,
  type ScreenerEvidence,
  type ScreenerIndicator,
  type ScreenerNode,
  type ScreenerRow,
  type ScreenerRunResponse,
  type ScreenerSaved,
  type ScreenerUniverse,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { cn } from "./lib/utils";

// ─── operators ────────────────────────────────────────────────────────────────
const OPERATORS: { id: string; label: string; needsValue: boolean; needsUpper: boolean }[] = [
  { id: ">", label: ">", needsValue: true, needsUpper: false },
  { id: ">=", label: "≥", needsValue: true, needsUpper: false },
  { id: "<", label: "<", needsValue: true, needsUpper: false },
  { id: "<=", label: "≤", needsValue: true, needsUpper: false },
  { id: "=", label: "=", needsValue: true, needsUpper: false },
  { id: "!=", label: "≠", needsValue: true, needsUpper: false },
  { id: "between", label: "between", needsValue: true, needsUpper: true },
  { id: "outside", label: "outside", needsValue: true, needsUpper: true },
  { id: "crosses_above", label: "crosses above", needsValue: true, needsUpper: false },
  { id: "crosses_below", label: "crosses below", needsValue: true, needsUpper: false },
];

const RHS_KINDS = [
  { id: "value", label: "value" },
  { id: "indicator", label: "indicator" },
] as const;

/** The default the panel opens with — the user's own worked example. */
const STARTER: ScreenerNode = {
  match: "all",
  conditions: [
    { indicator: "close", op: ">", rhs_indicator: "ema", period: 50 },
    { indicator: "rsi", op: "<", value: 70, period: 14 },
    { indicator: "rel_volume", op: ">", value: 1.2, period: 20 },
  ],
};

// ─── a leaf row ───────────────────────────────────────────────────────────────
interface LeafDraft {
  kind: "leaf";
  indicator: string;
  op: string;
  period: string;
  rhsKind: "value" | "indicator";
  value: string;
  upper: string;
  rhsIndicator: string;
  rhsPeriod: string;
}
/** A nested group. The backend supports arbitrary depth; two is what a human
 *  reads comfortably, which is why the UI offers exactly that. */
interface GroupDraft {
  kind: "group";
  match: "all" | "any";
  children: Draft[];
}
type Draft = LeafDraft | GroupDraft;

function leafFromNode(node: ScreenerNode): LeafDraft {
  return {
    kind: "leaf",
    indicator: node.indicator ?? "close",
    op: node.op ?? ">",
    period: node.period != null ? String(node.period) : "",
    rhsKind: node.rhs_indicator ? "indicator" : "value",
    value: node.value != null ? String(node.value) : "",
    upper: node.upper != null ? String(node.upper) : "",
    rhsIndicator: node.rhs_indicator ?? "ema",
    rhsPeriod: node.rhs_period != null ? String(node.rhs_period) : "",
  };
}

function draftFromNode(node: ScreenerNode): Draft {
  if (node.conditions || node.match) {
    return {
      kind: "group",
      match: node.match ?? "all",
      children: (node.conditions ?? []).map(draftFromNode),
    };
  }
  return leafFromNode(node);
}

function nodeFromDraft(draft: Draft): ScreenerNode {
  if (draft.kind === "group") {
    return { match: draft.match, conditions: draft.children.map(nodeFromDraft) };
  }
  const node: ScreenerNode = { indicator: draft.indicator, op: draft.op };
  if (draft.period.trim()) node.period = Number(draft.period);
  if (draft.op === "between" || draft.op === "outside") {
    if (draft.value.trim()) node.value = Number(draft.value);
    if (draft.upper.trim()) node.upper = Number(draft.upper);
  } else if (draft.rhsKind === "indicator") {
    node.rhs_indicator = draft.rhsIndicator;
    if (draft.rhsPeriod.trim()) node.rhs_period = Number(draft.rhsPeriod);
  } else if (draft.value.trim()) {
    node.value = Number(draft.value);
  }
  return node;
}

// ─── formatting ───────────────────────────────────────────────────────────────
function fmt(value: unknown, unit?: string): string {
  if (value == null) return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  switch (unit) {
    case "percent":
      return `${n.toFixed(2)}%`;
    case "multiple":
      return `${n.toFixed(2)}x`;
    case "integer":
      return n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
    default:
      return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
}

const COL_LABELS: Record<string, string> = {
  symbol: "Symbol",
  ltp: "LTP",
  change_pct: "Change %",
  volume: "Volume",
  rel_volume: "Rel vol",
  rsi14: "RSI",
  ema20: "EMA20",
  ema50: "EMA50",
  atr_pct: "ATR %",
  setup: "Setup",
};

const DEFAULT_COLUMNS = [
  "symbol",
  "ltp",
  "change_pct",
  "volume",
  "rel_volume",
  "rsi14",
  "ema20",
  "ema50",
  "atr_pct",
  "setup",
];

const NUMERIC_UNITS: Record<string, string> = {
  ltp: "number",
  change_pct: "percent",
  volume: "integer",
  rel_volume: "multiple",
  rsi14: "number",
  ema20: "number",
  ema50: "number",
  atr_pct: "percent",
};

// ─── condition row ────────────────────────────────────────────────────────────
function ConditionRow({
  draft,
  indicators,
  onChange,
  onRemove,
}: {
  draft: LeafDraft;
  indicators: ScreenerIndicator[];
  onChange: (next: LeafDraft) => void;
  onRemove: () => void;
}) {
  const byKey = useMemo(() => new Map(indicators.map((i) => [i.key, i])), [indicators]);
  const spec = byKey.get(draft.indicator);
  const isBetween = draft.op === "between" || draft.op === "outside";
  const unavailable = spec && !spec.available;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        value={draft.indicator}
        onChange={(e) => {
          const next = byKey.get(e.target.value);
          onChange({
            ...draft,
            indicator: e.target.value,
            period: next?.takes_period && !draft.period ? String(next.default_period ?? 14) : draft.period,
          });
        }}
        className="min-w-[13rem] rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
      >
        {indicators.map((ind) => (
          <option key={ind.key} value={ind.key} disabled={!ind.available}>
            {ind.label}
            {ind.available ? "" : " — unavailable"}
          </option>
        ))}
      </select>

      {spec?.takes_period ? (
        <input
          value={draft.period}
          onChange={(e) => onChange({ ...draft, period: e.target.value.replace(/[^0-9]/g, "") })}
          placeholder={String(spec.default_period ?? "")}
          className="w-16 rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
          aria-label="period"
        />
      ) : null}

      <select
        value={draft.op}
        onChange={(e) => onChange({ ...draft, op: e.target.value })}
        className="rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
      >
        {OPERATORS.map((op) => (
          <option key={op.id} value={op.id}>
            {op.label}
          </option>
        ))}
      </select>

      {isBetween ? (
        <>
          <input
            value={draft.value}
            onChange={(e) => onChange({ ...draft, value: e.target.value })}
            placeholder="lower"
            className="w-20 rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
          />
          <span className="text-xs text-muted-foreground">to</span>
          <input
            value={draft.upper}
            onChange={(e) => onChange({ ...draft, upper: e.target.value })}
            placeholder="upper"
            className="w-20 rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
          />
        </>
      ) : (
        <>
          <select
            value={draft.rhsKind}
            onChange={(e) => onChange({ ...draft, rhsKind: e.target.value as "value" | "indicator" })}
            className="rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
          >
            {RHS_KINDS.map((k) => (
              <option key={k.id} value={k.id}>
                {k.label}
              </option>
            ))}
          </select>

          {draft.rhsKind === "indicator" ? (
            <>
              <select
                value={draft.rhsIndicator}
                onChange={(e) => onChange({ ...draft, rhsIndicator: e.target.value })}
                className="min-w-[10rem] rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
              >
                {indicators
                  .filter((i) => i.available)
                  .map((ind) => (
                    <option key={ind.key} value={ind.key}>
                      {ind.label}
                    </option>
                  ))}
              </select>
              {byKey.get(draft.rhsIndicator)?.takes_period && !spec?.takes_period ? (
                <input
                  value={draft.rhsPeriod}
                  onChange={(e) => onChange({ ...draft, rhsPeriod: e.target.value.replace(/[^0-9]/g, "") })}
                  placeholder={String(byKey.get(draft.rhsIndicator)?.default_period ?? "")}
                  className="w-16 rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
                  aria-label="rhs period"
                />
              ) : null}
            </>
          ) : (
            <input
              value={draft.value}
              onChange={(e) => onChange({ ...draft, value: e.target.value })}
              placeholder="value"
              className="w-24 rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
            />
          )}
        </>
      )}

      {unavailable ? (
        <span className="text-[11px] text-muted-foreground">requires {spec?.requires}</span>
      ) : null}

      <button
        type="button"
        onClick={onRemove}
        className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive"
        aria-label="remove condition"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

// ─── group editor ─────────────────────────────────────────────────────────────
function GroupEditor({
  group,
  indicators,
  depth,
  onChange,
  onRemove,
}: {
  group: GroupDraft;
  indicators: ScreenerIndicator[];
  depth: number;
  onChange: (next: GroupDraft) => void;
  onRemove?: () => void;
}) {
  const setChild = (index: number, next: Draft) => {
    const children = group.children.slice();
    children[index] = next;
    onChange({ ...group, children });
  };
  const removeChild = (index: number) =>
    onChange({ ...group, children: group.children.filter((_, i) => i !== index) });

  return (
    <div
      className={cn(
        "rounded-xl border p-3",
        depth === 0 ? "border-border bg-muted/20" : "border-primary/30 bg-primary/5",
      )}
    >
      <div className="mb-2 flex items-center gap-2">
        <select
          value={group.match}
          onChange={(e) => onChange({ ...group, match: e.target.value as "all" | "any" })}
          className="rounded-lg border border-border bg-card px-2 py-1 text-[12px] font-semibold uppercase tracking-wide outline-none focus:border-primary"
        >
          <option value="all">Match ALL (AND)</option>
          <option value="any">Match ANY (OR)</option>
        </select>
        {depth > 0 ? (
          <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-medium text-primary">
            nested group
          </span>
        ) : null}
        <span className="text-[11px] text-muted-foreground">
          {group.children.length} condition{group.children.length === 1 ? "" : "s"}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              onChange({
                ...group,
                children: [
                  ...group.children,
                  { kind: "leaf", indicator: "close", op: ">", period: "", rhsKind: "value", value: "", upper: "", rhsIndicator: "ema", rhsPeriod: "" },
                ],
              })
            }
          >
            <Plus className="h-3.5 w-3.5" /> Condition
          </Button>
          {depth < 2 ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                onChange({
                  ...group,
                  children: [
                    ...group.children,
                    {
                      kind: "group",
                      match: "any",
                      children: [
                        { kind: "leaf", indicator: "close", op: ">", period: "", rhsKind: "value", value: "", upper: "", rhsIndicator: "ema", rhsPeriod: "" },
                        { kind: "leaf", indicator: "rsi", op: "<", period: "14", rhsKind: "value", value: "70", upper: "", rhsIndicator: "ema", rhsPeriod: "" },
                      ],
                    },
                  ],
                })
              }
            >
              <Plus className="h-3.5 w-3.5" /> Group
            </Button>
          ) : null}
          {onRemove ? (
            <button
              type="button"
              onClick={onRemove}
              className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted hover:text-destructive"
              aria-label="remove group"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="space-y-2">
        {group.children.length === 0 ? (
          <Hint>
            This group is empty, so it matches nothing. (Not everything — an empty group is a
            no-op, deliberately.)
          </Hint>
        ) : null}
        {group.children.map((child, index) =>
          child.kind === "group" ? (
            <GroupEditor
              key={index}
              group={child}
              indicators={indicators}
              depth={depth + 1}
              onChange={(next) => setChild(index, next)}
              onRemove={() => removeChild(index)}
            />
          ) : (
            <ConditionRow
              key={index}
              draft={child}
              indicators={indicators}
              onChange={(next) => setChild(index, next)}
              onRemove={() => removeChild(index)}
            />
          ),
        )}
      </div>
    </div>
  );
}

// ─── evidence line ────────────────────────────────────────────────────────────
function EvidenceLine({ item, muted }: { item: ScreenerEvidence; muted?: boolean }) {
  const mark = item.unmeasurable ? "?" : item.passed ? "✓" : "✗";
  const tone = item.unmeasurable
    ? "text-muted-foreground"
    : item.passed
      ? "text-success"
      : "text-destructive";
  return (
    <div className={cn("flex items-start gap-2 py-0.5 text-[12px]", muted && "opacity-70")}>
      <span className={cn("mt-[1px] w-3 shrink-0 font-bold", tone)}>{mark}</span>
      <span className="shrink-0 font-medium">{item.label}</span>
      <span className="text-muted-foreground">{item.reason}</span>
      {item.unmeasurable ? (
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          no data
        </span>
      ) : null}
    </div>
  );
}

// ─── the panel ────────────────────────────────────────────────────────────────
export default function ScreenerPanel({ onOpenChart }: { onOpenChart?: (symbol: string) => void }) {
  const [indicators, setIndicators] = useState<ScreenerIndicator[]>([]);
  const [universes, setUniverses] = useState<ScreenerUniverse[]>([]);
  const [columns, setColumns] = useState<string[]>(DEFAULT_COLUMNS);
  const [universe, setUniverse] = useState("all");
  const [exchange] = useState("NSEEQ");
  const [sort, setSort] = useState("rel_volume");
  const [limit, setLimit] = useState(50);

  const [root, setRoot] = useState<GroupDraft>(() => draftFromNode(STARTER) as GroupDraft);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const [result, setResult] = useState<ScreenerRunResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Typed from the API function rather than restated here, so adding a field
  // to the validate response cannot leave this silently behind.
  const [validation, setValidation] = useState<Awaited<ReturnType<typeof screenerValidate>> | null>(
    null,
  );

  const [saved, setSaved] = useState<ScreenerSaved[]>([]);
  const [saveName, setSaveName] = useState("");
  const [saveOpen, setSaveOpen] = useState(false);

  // ── load catalog on mount ──────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [ind, uni, col, sv] = await Promise.all([
          screenerIndicators(),
          screenerUniverses(exchange),
          screenerColumns(),
          screenerListSaved(),
        ]);
        if (!alive) return;
        setIndicators(ind.indicators);
        setUniverses(uni.universes);
        setColumns(col.columns.map((c) => c.key));
        setSaved(sv.scans);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      alive = false;
    };
  }, [exchange]);

  // ── validate as the tree changes, so a typo is caught before a scan ────────
  useEffect(() => {
    const handle = window.setTimeout(async () => {
      try {
        const report = await screenerValidate(nodeFromDraft(root));
        setValidation(report);
      } catch {
        /* a failed validate must not clear the editor */
      }
    }, 400);
    return () => window.clearTimeout(handle);
  }, [root]);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const body = {
        universe,
        exchange,
        conditions: nodeFromDraft(root),
        columns,
        sort,
        limit,
      };
      const response = await screenerRun(body);
      setResult(response);
      // Open the first row by default: the reason is the point, and hiding it
      // behind a click means most users never see the feature.
      setExpanded(response.rows.length ? new Set([response.rows[0].symbol]) : new Set());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }, [columns, exchange, limit, root, sort, universe]);

  const toggle = (symbol: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });

  const visibleColumns = useMemo(() => {
    if (!result?.rows.length) return columns;
    // Only render columns the backend actually returned, so an unavailable
    // column shows as absent rather than as a blank that reads like zero.
    const present = new Set(Object.keys(result.rows[0]));
    return columns.filter((c) => c === "symbol" || present.has(c));
  }, [columns, result]);

  return (
    <div className="space-y-4">
      {/* ── builder ────────────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Screen builder"
          sub="Conditions are evaluated over cached daily bars. The result explains every match."
          action={
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => setSaveOpen((v) => !v)}>
                <Save className="h-3.5 w-3.5" /> Save
              </Button>
              <Button size="sm" onClick={run} disabled={running || validation?.valid === false}>
                {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
                {running ? "Scanning…" : "Run scan"}
              </Button>
            </div>
          }
        />

        <div className="space-y-3 p-5 pt-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-muted-foreground">Universe</span>
              <select
                value={universe}
                onChange={(e) => setUniverse(e.target.value)}
                className="min-w-[12rem] rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
              >
                {universes.map((u) => (
                  <option key={u.name} value={u.name}>
                    {u.label} ({u.size})
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-muted-foreground">Rank by</span>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value)}
                className="rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
              >
                {["rel_volume", "change_pct", "atr_pct", "rsi14", "volume", "ltp", "symbol"].map((s) => (
                  <option key={s} value={s}>
                    {COL_LABELS[s] ?? s}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-muted-foreground">Max rows</span>
              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="rounded-lg border border-border bg-card px-2 py-1.5 text-[13px] outline-none focus:border-primary"
              >
                {[25, 50, 100, 200].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <GroupEditor
            group={root}
            indicators={indicators}
            depth={0}
            onChange={(next) => setRoot(next)}
          />

          {validation && !validation.valid ? (
            <ErrorBox>
              This screen cannot run yet: {validation.error}
            </ErrorBox>
          ) : null}
          {validation?.valid ? (
            <Hint>
              Valid — {validation.summary}
              {validation.warnings?.length ? ` · ${validation.warnings.join(" · ")}` : ""}
            </Hint>
          ) : null}
        </div>
      </Card>

      {/* ── save / saved ───────────────────────────────────────────────── */}
      {saveOpen ? (
        <Card>
          <div className="flex flex-wrap items-end gap-3 p-5">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium text-muted-foreground">Name this scan</span>
              <Input
                value={saveName}
                onChange={setSaveName}
                placeholder="e.g. Breakout with volume"
                className="min-w-[16rem]"
              />
            </label>
            <Button
              size="sm"
              disabled={!saveName.trim() || validation?.valid === false}
              onClick={async () => {
                try {
                  await screenerSave({
                    name: saveName.trim(),
                    definition: { universe, exchange, conditions: nodeFromDraft(root), columns, sort },
                  });
                  setSaveName("");
                  setSaveOpen(false);
                  setSaved((await screenerListSaved()).scans);
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              Save scan
            </Button>
          </div>
        </Card>
      ) : null}

      {saved.length ? (
        <Card>
          <CardHeader title="Saved scans" sub="Re-run a stored screen with one click." />
          <div className="flex flex-wrap gap-2 p-5 pt-3">
            {saved.map((scan) => (
              <div
                key={scan.scan_id}
                className="group flex items-center gap-1 rounded-lg border border-border bg-muted/30 pl-2.5 pr-1 py-1"
              >
                <button
                  type="button"
                  className="text-[13px] font-medium hover:text-primary"
                  onClick={async () => {
                    setRunning(true);
                    setError(null);
                    try {
                      const response = await screenerSavedResults(scan.scan_id, limit);
                      setResult(response);
                      setUniverse(scan.definition.universe ?? "all");
                      if (scan.definition.conditions) {
                        setRoot(draftFromNode(scan.definition.conditions) as GroupDraft);
                      }
                      setExpanded(
                        response.rows.length ? new Set([response.rows[0].symbol]) : new Set(),
                      );
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err));
                    } finally {
                      setRunning(false);
                    }
                  }}
                >
                  {scan.name}
                </button>
                <button
                  type="button"
                  aria-label={`delete ${scan.name}`}
                  className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                  onClick={async () => {
                    try {
                      await screenerDeleteSaved(scan.scan_id);
                      setSaved((await screenerListSaved()).scans);
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err));
                    }
                  }}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {error ? <ErrorBox>{error}</ErrorBox> : null}

      {/* ── results ────────────────────────────────────────────────────── */}
      {result ? (
        <Card>
          <CardHeader
            title={`${result.matched} match${result.matched === 1 ? "" : "es"}`}
            sub={`Scanned ${result.scanned} symbols · as of ${result.as_of ?? "—"} · ${result.elapsed_s}s`}
          />

          {result.warnings?.length ? (
            <div className="mx-5 mt-3 flex items-start gap-2 rounded-xl border border-border bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div>{result.warnings.join(" · ")}</div>
            </div>
          ) : null}

          {result.rows.length === 0 ? (
            <div className="p-5">
              <Hint>
                Nothing matched. That is a result, not a failure — loosen a condition or widen the
                universe and run again.
              </Hint>
            </div>
          ) : (
            <div className="overflow-x-auto p-5 pt-3">
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                    <th className="w-6 py-2" />
                    {visibleColumns.map((col) => (
                      <th key={col} className="whitespace-nowrap px-2 py-2 font-medium">
                        {COL_LABELS[col] ?? col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row: ScreenerRow) => {
                    const open = expanded.has(row.symbol);
                    return (
                      <>
                        <tr
                          key={row.symbol}
                          className="cursor-pointer border-b border-border/50 hover:bg-muted/30"
                          onClick={() => toggle(row.symbol)}
                        >
                          <td className="py-2 pl-1 text-muted-foreground">
                            {open ? (
                              <ChevronDown className="h-3.5 w-3.5" />
                            ) : (
                              <ChevronRight className="h-3.5 w-3.5" />
                            )}
                          </td>
                          {visibleColumns.map((col) => (
                            <td key={col} className="whitespace-nowrap px-2 py-2">
                              {col === "symbol" ? (
                                <button
                                  type="button"
                                  className="font-medium hover:text-primary"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    onOpenChart?.(row.symbol);
                                  }}
                                >
                                  {row.symbol}
                                </button>
                              ) : col === "setup" ? (
                                <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[11px] font-medium text-primary">
                                  {String(row[col] ?? "—")}
                                </span>
                              ) : col === "change_pct" ? (
                                <span
                                  className={cn(
                                    Number(row[col]) >= 0 ? "text-success" : "text-destructive",
                                  )}
                                >
                                  {fmt(row[col], "percent")}
                                </span>
                              ) : (
                                fmt(row[col], NUMERIC_UNITS[col])
                              )}
                            </td>
                          ))}
                        </tr>
                        {open ? (
                          <tr key={`${row.symbol}-why`} className="border-b border-border/50">
                            <td />
                            <td colSpan={visibleColumns.length} className="px-2 pb-3 pt-1">
                              <div className="rounded-xl border border-border bg-muted/30 p-3">
                                <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                                  Why {row.symbol} matched
                                </div>
                                {(row.why ?? []).map((item, i) => (
                                  <EvidenceLine key={i} item={item} />
                                ))}
                                {/* Failed/unmeasurable tests are shown too when the
                                    top-level group is ANY — the row matched on one
                                    branch, and the user needs to see the other. */}
                                {(row.evidence ?? []).some((e) => !e.passed) &&
                                (row.evidence ?? []).length > (row.why ?? []).length ? (
                                  <div className="mt-2 border-t border-border pt-2">
                                    <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                                      Also tested
                                    </div>
                                    {(row.evidence ?? [])
                                      .filter((e) => !e.passed)
                                      .map((item, i) => (
                                        <EvidenceLine key={i} item={item} muted />
                                      ))}
                                  </div>
                                ) : null}
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ) : null}
    </div>
  );
}
