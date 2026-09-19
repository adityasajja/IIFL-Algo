import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/** A single labelled number. `tone` colours it; `sub` carries the comparison. */
export function Stat({
  label,
  value,
  sub,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn";
  hint?: string;
}) {
  return (
    <div
      title={hint}
      className="rounded-xl border border-border bg-card px-3.5 py-3"
    >
      <div className="text-[11px] font-medium uppercase tracking-[0.05em] text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "mt-1 text-[19px] font-bold tabular-nums tracking-tight",
          tone === "good" && "text-emerald-600 dark:text-emerald-400",
          tone === "bad" && "text-destructive",
          tone === "warn" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {value}
      </div>
      {sub ? (
        <div className="mt-0.5 text-[11.5px] tabular-nums text-muted-foreground">{sub}</div>
      ) : null}
    </div>
  );
}

/** A pass/fail chip. */
export function VerdictPill({ passed, children }: { passed: boolean; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-bold uppercase tracking-[0.05em]",
        passed
          ? "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400"
          : "bg-destructive/12 text-destructive",
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          passed ? "bg-emerald-500" : "bg-destructive",
        )}
      />
      {children}
    </span>
  );
}

export function Badge({
  tone = "flat",
  className,
  children,
}: {
  tone?: "flat" | "good" | "bad" | "warn" | "info";
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold",
        tone === "good" && "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
        tone === "bad" && "bg-destructive/10 text-destructive",
        tone === "warn" && "bg-amber-500/10 text-amber-600 dark:text-amber-400",
        tone === "info" && "bg-sky-500/10 text-sky-600 dark:text-sky-400",
        tone === "flat" && "bg-primary/[0.07] text-muted-foreground",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Callout({
  tone = "warn",
  title,
  children,
}: {
  tone?: "warn" | "info" | "bad";
  title?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border px-3.5 py-3 text-[13px]",
        tone === "warn" && "border-amber-500/35 bg-amber-500/[0.07] text-amber-700 dark:text-amber-300",
        tone === "bad" && "border-destructive/40 bg-destructive/10 text-destructive",
        tone === "info" && "border-border bg-muted/40 text-muted-foreground",
      )}
    >
      {title ? <div className="mb-0.5 font-semibold">{title}</div> : null}
      <div className="leading-relaxed">{children}</div>
    </div>
  );
}

export function fmtNum(v: unknown, digits = 2): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    return v.toLocaleString("en-IN", { maximumFractionDigits: digits });
  }
  return String(v);
}

export function fmtPct(v: unknown, digits = 2): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function fmtMoney(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}
