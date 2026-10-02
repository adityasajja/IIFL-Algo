/**
 * The maths and wording behind the track-record visuals, kept out of the components so it can be
 * tested: return series, drawdown, axis ticks, which point the pointer is on, and how freshness
 * is worded. Nothing here draws anything.
 */
import type { Tone } from "./tone";

export type TrackPoint = { d: string; equity: number; benchmark: number | null };

/** A point as a return from the starting base, in percent. `bench` is null where unknown. */
export type ReturnPoint = { d: string; ret: number; bench: number | null };

export function returnSeries(points: TrackPoint[], base: number): ReturnPoint[] {
  if (!(base > 0)) return [];
  return points.map((p) => ({
    d: p.d,
    ret: (p.equity / base - 1) * 100,
    bench: p.benchmark == null ? null : (p.benchmark / base - 1) * 100,
  }));
}

/** Depth below the running peak at each point, in percent (0 at a new high, negative below). */
export function drawdownSeries(points: TrackPoint[], base: number): number[] {
  let peak = base;
  return points.map((p) => {
    peak = Math.max(peak, p.equity);
    return peak > 0 ? (p.equity / peak - 1) * 100 : 0;
  });
}

/** Round tick values spanning [min, max], including zero when it is in range. */
export function niceTicks(min: number, max: number, target = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0];
  if (min === max) return [min];
  const raw = (max - min) / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const first = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = first; v <= max + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : Number(v.toFixed(10)));
  return out;
}

/** The y-domain for a set of series: padded so the line never touches the frame, always holding 0. */
export function domain(values: number[], pad = 0.12): [number, number] {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return [-1, 1];
  let lo = Math.min(0, ...finite);
  let hi = Math.max(0, ...finite);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const span = hi - lo;
  return [lo - span * pad, hi + span * pad];
}

/** Index of the point nearest an x position, given the plot's left edge and width. */
export function nearestIndex(x: number, left: number, width: number, n: number): number {
  if (n <= 1 || width <= 0) return 0;
  const t = (x - left) / width;
  return Math.max(0, Math.min(n - 1, Math.round(t * (n - 1))));
}

/** "+2.4%" / "-0.4%" / "0.0%". The sign is always shown; a missing value is a dash, never zero. */
export function signedPct(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const r = Number(v.toFixed(digits));
  if (r === 0) return `${(0).toFixed(digits)}%`;
  return `${r > 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}%`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "3 Jul", or "3 Jul 2025" when the year is not this one. Takes an ISO date. */
export function shortDay(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return "—";
  const [, y, mo, d] = m;
  const day = `${Number(d)} ${MONTHS[Number(mo) - 1]}`;
  return Number(y) === now.getFullYear() ? day : `${day} ${y}`;
}

export type Freshness = "fresh" | "late" | "stale" | "missing";

/** The words and tone for how current a data set is. Plain, and never green for old data. */
export function freshnessView(status: Freshness, behind: number | null): { word: string; tone: Tone } {
  switch (status) {
    case "fresh":
      return { word: "Current", tone: "good" };
    case "late":
      return { word: "1 session behind", tone: "warn" };
    case "stale":
      return { word: behind != null ? `${behind} sessions behind` : "Out of date", tone: "bad" };
    default:
      return { word: "No data", tone: "bad" };
  }
}

/** Compare a result with its yardstick: which side of it, and by how much. */
export function versus(ret: number | null | undefined, bench: number | null | undefined) {
  if (ret == null || bench == null || !Number.isFinite(ret) || !Number.isFinite(bench)) return null;
  const diff = ret - bench;
  return { diff, ahead: diff > 0, level: diff === 0 };
}

/** Whether there are enough days for a record to mean anything. Short ones are shown, but labelled. */
export const MIN_MEANINGFUL_DAYS = 20;
export function isThin(tradingDays: number): boolean {
  return tradingDays < MIN_MEANINGFUL_DAYS;
}
