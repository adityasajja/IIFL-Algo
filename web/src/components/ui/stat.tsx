import type { ReactNode } from "react";
import { cn } from "../../lib/utils";
import { toneChip, toneFill, toneText, type Tone } from "../../lib/tone";
import { Tooltip } from "../motion/tooltip";
import { surface } from "./surface";

/** A single labelled number. `tone` colours it; `sub` carries the comparison. */
export function Stat({
  label,
  value,
  sub,
  tone = "neutral",
  hint,
  className,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "neutral" | Tone;
  hint?: string;
  className?: string;
}) {
  const card = (
    <div
      className={cn(
        surface,
        "flex min-w-0 flex-col justify-between overflow-hidden p-4",
        className,
      )}
    >
      <div className="truncate text-micro font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "mt-1.5 text-base sm:text-lg xl:text-xl font-medium leading-tight tracking-tight tabular-nums whitespace-nowrap overflow-hidden text-ellipsis [font-feature-settings:'ss01'_on,'tnum'_on]",
          tone !== "neutral" && toneText[tone],
        )}
      >
        {value}
      </div>
      {sub ? (
        <div className="mt-1 text-caption font-normal tabular-nums text-muted-foreground whitespace-nowrap overflow-hidden text-ellipsis [font-feature-settings:'ss01'_on,'tnum'_on]">
          {sub}
        </div>
      ) : null}
    </div>
  );
  if (!hint) return card;
  return (
    <Tooltip content={hint} side="top" delay={400} wrapperClassName="block min-w-0">
      {card}
    </Tooltip>
  );
}

/** A pass/fail chip. */
export function VerdictPill({ passed, children }: { passed: boolean; children: ReactNode }) {
  const tone: Tone = passed ? "good" : "bad";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.05em]",
        toneChip[tone],
      )}
    >
      <span className={cn("size-1.5 rounded-full", toneFill[tone])} />
      {children}
    </span>
  );
}

export function Badge({
  tone = "flat",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-caption font-semibold tabular-nums",
        toneChip[tone],
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
  tone?: "good" | "warn" | "info" | "bad";
  title?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border px-4 py-3 text-sm", toneChip[tone])}>
      {title ? <div className="mb-0.5 font-semibold">{title}</div> : null}
      <div className="leading-relaxed">{children}</div>
    </div>
  );
}

// Formatters moved to lib/format.ts; re-exported so existing imports keep working.
export { fmtMoney, fmtNum, fmtPct } from "../../lib/format";
