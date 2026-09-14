import { cn } from "./utils";

/**
 * Relative time, in the register people actually use when scanning a dashboard.
 *
 * The absolute date is *also* wanted — "4 days ago" tells you the data is
 * stale, "09-09" tells you which day. Return both and let the caller decide
 * which to lead with; never make the reader do the subtraction.
 */
export function relativeTime(input: string | Date | null | undefined): string | null {
  if (!input) return null;
  const then = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(then.getTime())) return null;

  const secs = Math.round((Date.now() - then.getTime()) / 1000);
  const future = secs < 0;
  const s = Math.abs(secs);

  const fmt = (n: number, unit: string) =>
    future ? `in ${n} ${unit}${n === 1 ? "" : "s"}` : `${n} ${unit}${n === 1 ? "" : "s"} ago`;

  if (s < 45) return future ? "in a moment" : "just now";
  if (s < 90) return fmt(1, "minute");
  if (s < 3600) return fmt(Math.round(s / 60), "minute");
  if (s < 5400) return fmt(1, "hour");
  if (s < 86400) return fmt(Math.round(s / 3600), "hour");
  if (s < 172800) return fmt(1, "day");
  if (s < 2592000) return fmt(Math.round(s / 86400), "day");
  if (s < 5184000) return fmt(1, "month");
  if (s < 31536000) return fmt(Math.round(s / 2592000), "month");
  return fmt(Math.round(s / 31536000), "year");
}

/** `09-09 09:08` — compact, unambiguous, no locale surprises. */
export function shortDate(input: string | Date | null | undefined): string | null {
  if (!input) return null;
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}-${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * Staleness band. A timestamp is not a neutral fact: 20 minutes old and 4 days
 * old mean different things, and the reader should not have to remember what
 * "normal" is for each field.
 */
export function staleness(
  input: string | Date | null | undefined,
  freshMins = 60,
  staleHours = 24,
): "fresh" | "aging" | "stale" | "unknown" {
  if (!input) return "unknown";
  const then = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(then.getTime())) return "unknown";
  const mins = (Date.now() - then.getTime()) / 60000;
  if (mins < 0) return "fresh";
  if (mins <= freshMins) return "fresh";
  if (mins <= staleHours * 60) return "aging";
  return "stale";
}

export const STALENESS_TONE: Record<string, string> = {
  fresh: "text-muted-foreground",
  aging: "text-amber-600 dark:text-amber-400",
  stale: "text-destructive",
  unknown: "text-muted-foreground/70",
};

/**
 * A timestamp rendered as "4 days ago · 09-09 09:08", tinted by age.
 * Pass `lead="absolute"` when the exact instant matters more than the age.
 */
export function RelativeTime({
  value,
  lead = "relative",
  freshMins,
  staleHours,
  className,
  absolute = true,
}: {
  value: string | Date | null | undefined;
  lead?: "relative" | "absolute";
  freshMins?: number;
  staleHours?: number;
  className?: string;
  absolute?: boolean;
}) {
  const rel = relativeTime(value);
  const abs = shortDate(value);
  if (!rel && !abs) return <span className={cn("text-muted-foreground/70", className)}>no record</span>;

  const tone = STALENESS_TONE[staleness(value, freshMins, staleHours)];
  const order =
    lead === "relative" ? [rel, absolute ? abs : null] : [absolute ? abs : null, rel];

  return (
    <span className={cn(tone, className)}>
      {order.filter(Boolean).join(" · ")}
    </span>
  );
}
