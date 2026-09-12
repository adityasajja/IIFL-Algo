import { useId, useMemo } from "react";
import type { EquityPoint } from "../../api";
import { cn } from "../../lib/utils";

export interface Series {
  name: string;
  points: EquityPoint[];
  color: string;
  /** Draw as a thin dashed line — used for the buy-and-hold reference. */
  dashed?: boolean;
  /** Fill the area under this series. Only the first filled series wins. */
  fill?: boolean;
}

/**
 * Equity curves, drawn as SVG.
 *
 * Series are aligned on the union of their timestamps with last-value
 * carry-forward, so a shorter curve still spans the full width instead of
 * being squashed into the left edge.
 */
export function EquityChart({
  series,
  height = 220,
  valueFormat,
  className,
}: {
  series: Series[];
  height?: number;
  valueFormat?: (v: number) => string;
  className?: string;
}) {
  const uid = useId().replace(/:/g, "");
  const usable = series.filter((s) => s.points.length > 1);

  const { stamps, aligned, min, max } = useMemo(() => {
    const allStamps = [
      ...new Set(usable.flatMap((s) => s.points.map((p) => p.ts))),
    ].sort();
    const seriesAligned = usable.map((s) => {
      const byTs = new Map(s.points.map((p) => [p.ts, p.value]));
      let last = s.points[0].value;
      const values = allStamps.map((ts) => {
        const v = byTs.get(ts);
        if (v !== undefined) last = v;
        return last;
      });
      return { ...s, values };
    });
    const flat = seriesAligned.flatMap((s) => s.values);
    return {
      stamps: allStamps,
      aligned: seriesAligned,
      min: flat.length ? Math.min(...flat) : 0,
      max: flat.length ? Math.max(...flat) : 1,
    };
  }, [usable]);

  if (usable.length === 0 || stamps.length < 2) {
    return (
      <div
        style={{ height }}
        className={cn(
          "grid place-items-center rounded-xl border border-dashed border-border text-xs text-muted-foreground",
          className,
        )}
      >
        Nothing to plot yet.
      </div>
    );
  }

  const W = 1000;
  const H = 320;
  const PAD = 10;
  const span = max - min || 1;
  const x = (i: number) => PAD + (i / (stamps.length - 1)) * (W - PAD * 2);
  const y = (v: number) => PAD + (1 - (v - min) / span) * (H - PAD * 2);

  const line = (values: number[]) =>
    values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");

  const first = aligned[0];
  const area = `${line(first.values)} L${x(stamps.length - 1).toFixed(2)},${H - PAD} L${x(0).toFixed(2)},${H - PAD} Z`;
  const fmt = valueFormat ?? ((v: number) => v.toLocaleString("en-IN", { maximumFractionDigits: 0 }));

  return (
    <div className={className}>
      <div className="flex items-baseline justify-between px-1 pb-1.5 text-[11px] tabular-nums text-muted-foreground">
        <span>{fmt(max)}</span>
        <span>{fmt(min)}</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        style={{ height, width: "100%", display: "block" }}
        role="img"
        aria-label={`Equity curves: ${aligned.map((s) => s.name).join(", ")}`}
      >
        <defs>
          <linearGradient id={`fill-${uid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={first.color} stopOpacity="0.22" />
            <stop offset="100%" stopColor={first.color} stopOpacity="0" />
          </linearGradient>
        </defs>

        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <line
            key={t}
            x1={PAD}
            x2={W - PAD}
            y1={PAD + t * (H - PAD * 2)}
            y2={PAD + t * (H - PAD * 2)}
            stroke="var(--border)"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        {first.fill !== false && <path d={area} fill={`url(#fill-${uid})`} />}

        {aligned.map((s) => (
          <path
            key={s.name}
            d={line(s.values)}
            fill="none"
            stroke={s.color}
            strokeWidth={s.dashed ? 1.5 : 2}
            strokeDasharray={s.dashed ? "6 5" : undefined}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <div className="flex flex-wrap items-center gap-3.5 px-1 pt-2 text-[11px] text-muted-foreground">
        {aligned.map((s) => (
          <span key={s.name} className="inline-flex items-center gap-1.5">
            <span
              className="inline-block h-0.5 w-4 rounded-full"
              style={{
                background: s.dashed
                  ? `repeating-linear-gradient(90deg, ${s.color} 0 4px, transparent 4px 7px)`
                  : s.color,
              }}
            />
            {s.name}
          </span>
        ))}
      </div>
    </div>
  );
}
