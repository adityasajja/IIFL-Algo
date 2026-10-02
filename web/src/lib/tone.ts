/**
 * Tone: the one place that decides what a colour means.
 *
 * A tone names a MEANING (good / bad / warn / info / flat), never a colour. Components take
 * a `tone` prop and look their classes up here, so "good" is the same green in a stat, a
 * badge, a table cell and a chart legend, in light and in dark, and changing it is a
 * one-line edit in index.css (--gain, --loss, --warning, --info).
 */

export type Tone = "good" | "bad" | "warn" | "info" | "flat";

/** Text colour for a value. */
export const toneText: Record<Tone, string> = {
  good: "text-gain",
  bad: "text-loss",
  warn: "text-warning",
  info: "text-info",
  flat: "text-muted-foreground",
};

/** A bordered, softly tinted chip: badges, pills, status labels. */
export const toneChip: Record<Tone, string> = {
  good: "border-gain/25 bg-gain/10 text-gain",
  bad: "border-loss/25 bg-loss/10 text-loss",
  warn: "border-warning/30 bg-warning/10 text-warning",
  info: "border-info/25 bg-info/10 text-info",
  flat: "border-border bg-muted/50 text-muted-foreground",
};

/** A bar or dot fill. */
export const toneFill: Record<Tone, string> = {
  good: "bg-gain",
  bad: "bg-loss",
  warn: "bg-warning",
  info: "bg-info",
  flat: "bg-muted-foreground/40",
};

/** Tone for a signed number: positive is good, negative is bad, zero or missing is flat. */
export function toneOf(value: number | null | undefined): Tone {
  if (value == null || !Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "good" : "bad";
}

/** Tone for a share of something healthy (breadth, win rate): high is good, low is bad. */
export function toneOfShare(pct: number, goodAt = 55, warnAt = 40): Tone {
  return pct >= goodAt ? "good" : pct >= warnAt ? "warn" : "bad";
}
