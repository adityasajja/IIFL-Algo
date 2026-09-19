import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * A 0–100 donut gauge. Pass a Tailwind `stroke-*` class as `toneClass` to colour
 * the arc; anything in `children` is centred inside it.
 */
export function Ring({
  value,
  size = 80,
  stroke = 9,
  toneClass = "stroke-primary",
  children,
  className,
}: {
  value: number;
  size?: number;
  stroke?: number;
  toneClass?: string;
  children?: ReactNode;
  className?: string;
}) {
  const r = 50 - stroke / 2;
  const circumference = 2 * Math.PI * r;
  const filled = (circumference * Math.min(Math.max(value, 0), 100)) / 100;
  return (
    <div className={cn("relative shrink-0", className)} style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" className="-rotate-90" style={{ width: size, height: size }}>
        <circle cx="50" cy="50" r={r} fill="none" strokeWidth={stroke} className="stroke-muted" />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${circumference}`}
          className={cn("transition-all duration-500", toneClass)}
        />
      </svg>
      <div className="absolute inset-0 grid place-items-center">{children}</div>
    </div>
  );
}
