import { useId } from "react";
import { cn } from "../../lib/utils";

/**
 * A tiny inline trend line for KPI cards.
 *
 * Deliberately not a chart: no axes, no labels, no tooltip. Its only job is to
 * answer "which way is this going" at a glance, next to a number that carries
 * the actual quantity. Values are normalised to the series' own range, because
 * the shape is the message — an absolute scale would make a quiet series look
 * flat and a noisy one look catastrophic.
 */
export function Sparkline({
  data,
  width = 72,
  height = 22,
  tone = "auto",
  className,
  filled = true,
  ariaLabel,
}: {
  data: number[];
  width?: number;
  height?: number;
  /** `auto` colours by last-vs-first. `up`/`down` force a direction. */
  tone?: "auto" | "up" | "down" | "neutral";
  className?: string;
  filled?: boolean;
  ariaLabel?: string;
}) {
  const gradientId = useId().replace(/:/g, "");

  const clean = (data ?? []).filter((v) => Number.isFinite(v));
  if (clean.length < 2) {
    return (
      <span
        className={cn("inline-flex items-center text-[10px] text-muted-foreground/60", className)}
        style={{ height }}
        aria-label={ariaLabel ?? "no trend data"}
      >
        no data
      </span>
    );
  }

  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const pad = 2;
  const innerH = height - pad * 2;

  const pts = clean.map((v, i) => {
    const x = (i / (clean.length - 1)) * width;
    const y = pad + innerH - ((v - min) / span) * innerH;
    return [x, y] as const;
  });

  const line = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;

  const rising = clean[clean.length - 1] >= clean[0];
  const resolved = tone === "auto" ? (rising ? "up" : "down") : tone;

  const stroke =
    resolved === "up" ? "#10b981" : resolved === "down" ? "#ef4444" : "#94a3b8";
  const fillFrom =
    resolved === "up" ? "rgba(16,185,129,0.28)" : resolved === "down" ? "rgba(239,68,68,0.28)" : "rgba(148,163,184,0.22)";

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cn("overflow-visible", className)}
      role="img"
      aria-label={ariaLabel ?? `${resolved === "up" ? "rising" : resolved === "down" ? "falling" : "flat"} trend`}
    >
      {filled && (
        <>
          <defs>
            <linearGradient id={`spark-${gradientId}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={fillFrom} />
              <stop offset="100%" stopColor="transparent" />
            </linearGradient>
          </defs>
          <path d={area} fill={`url(#spark-${gradientId})`} />
        </>
      )}
      <path
        d={line}
        fill="none"
        stroke={stroke}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Emphasise the latest point — where "now" sits in the range. */}
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={1.9} fill={stroke} />
    </svg>
  );
}

/** Direction arrow paired with a sparkline. */
export function TrendArrow({ rising, className }: { rising: boolean | null; className?: string }) {
  if (rising === null) return null;
  return (
    <span
      className={cn(
        "inline-block text-[10px] leading-none",
        rising ? "text-emerald-500" : "text-destructive",
        className,
      )}
      aria-label={rising ? "expanding" : "contracting"}
    >
      {rising ? "▲" : "▼"}
    </span>
  );
}
