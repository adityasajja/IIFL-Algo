import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import {
  domain,
  drawdownSeries,
  nearestIndex,
  niceTicks,
  returnSeries,
  shortDay,
  signedPct,
  type TrackPoint,
} from "../../lib/track-view";
import { cn } from "../../lib/utils";

/**
 * A strategy's return against its yardstick, with the depth of every fall under it.
 *
 * Two lines share ONE axis (both rebased to the same starting capital, so they are directly
 * comparable). The strategy is the accent; the benchmark is the de-emphasis gray, which is the
 * "emphasis" form: one thing is the point, the other is context. The fall-from-peak strip below
 * shares the x axis, so a dip in the line and its depth line up.
 *
 * Every value in the tooltip is also in the table view next to this chart, and the chart is
 * keyboard-reachable (arrow keys move the readout).
 */
export function TrackChart({
  points,
  base,
  strategyLabel = "Paper book",
  benchmarkLabel = "Nifty 50",
  className,
}: {
  points: TrackPoint[];
  base: number;
  strategyLabel?: string;
  benchmarkLabel?: string;
  className?: string;
}) {
  const uid = useId().replace(/:/g, "");
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [active, setActive] = useState<number | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(280, Math.floor(entry.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const series = useMemo(() => returnSeries(points, base), [points, base]);
  const dd = useMemo(() => drawdownSeries(points, base), [points, base]);
  const n = series.length;

  // Layout. Right margin holds the end labels; the strip sits under the main plot.
  const M = { l: 44, r: width < 480 ? 56 : 84, t: 10, b: 22 };
  const H1 = 210;
  const GAP = 26;
  const H2 = 58;
  const W = width - M.l - M.r;
  const total = M.t + H1 + GAP + H2 + M.b;

  const [lo, hi] = useMemo(
    () => domain(series.flatMap((s) => (s.bench == null ? [s.ret] : [s.ret, s.bench]))),
    [series],
  );
  const ticks = useMemo(() => niceTicks(lo, hi, 4), [lo, hi]);
  const ddMin = Math.min(0, ...dd);
  const ddFloor = ddMin < -0.0001 ? ddMin * 1.15 : -1;

  const x = (i: number) => M.l + (n <= 1 ? W / 2 : (i / (n - 1)) * W);
  const y1 = (v: number) => M.t + (1 - (v - lo) / (hi - lo)) * H1;
  const top2 = M.t + H1 + GAP;
  const y2 = (v: number) => top2 + (v / ddFloor) * H2; // 0 at the top of the strip, floor at the bottom

  if (n < 2) {
    return (
      <div className={cn("grid h-48 place-items-center rounded-lg border border-dashed border-border text-sm text-muted-foreground", className)}>
        One day so far
      </div>
    );
  }

  const path = (vals: (number | null)[], f: (v: number) => number) => {
    let d = "";
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${f(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const retPath = path(series.map((s) => s.ret), y1);
  const benchPath = path(series.map((s) => s.bench), y1);
  const wash = `${retPath} L${x(n - 1).toFixed(1)},${y1(0).toFixed(1)} L${x(0).toFixed(1)},${y1(0).toFixed(1)} Z`;
  const ddPath = path(dd, y2);
  const ddArea = `${ddPath} L${x(n - 1).toFixed(1)},${y2(0).toFixed(1)} L${x(0).toFixed(1)},${y2(0).toFixed(1)} Z`;

  const last = series[n - 1];
  const lastBench = [...series].reverse().find((s) => s.bench != null);
  const lastBenchIdx = lastBench ? series.lastIndexOf(lastBench) : -1;
  const worstIdx = dd.indexOf(ddMin);
  const a = active == null ? null : series[active];

  const summary = `${strategyLabel} ${signedPct(last.ret)}${lastBench ? `, ${benchmarkLabel} ${signedPct(lastBench.bench)}` : ""}, from ${shortDay(series[0].d)} to ${shortDay(last.d)}. Deepest fall ${signedPct(ddMin)}.`;

  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    setActive(nearestIndex(e.clientX - r.left + M.l, M.l, W, n));
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? n - 1) - 1));
    else if (e.key === "ArrowRight") setActive((i) => Math.min(n - 1, (i ?? n - 1) + 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(n - 1);
    else if (e.key === "Escape") setActive(null);
    else return;
    e.preventDefault();
  };

  const tipLeft = active == null ? 0 : Math.min(Math.max(x(active) + 12, M.l), width - 215);
  const labelGap = lastBench ? Math.abs(y1(last.ret) - y1(lastBench.bench as number)) : 99;

  return (
    <div className={className}>
      <div className="mb-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-0.5 w-4 rounded-full bg-primary" aria-hidden="true" />
          {strategyLabel}
        </span>
        {lastBench ? (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-0.5 w-4 rounded-full bg-muted-foreground/70" aria-hidden="true" />
            {benchmarkLabel}
          </span>
        ) : null}
      </div>

      <div
        ref={wrap}
        className="relative outline-none focus-visible:ring-2 focus-visible:ring-ring"
        tabIndex={0}
        role="group"
        aria-label={`${summary} Use the arrow keys to read each day.`}
        onKeyDown={onKey}
        onFocus={() => setActive((i) => i ?? n - 1)}
        onBlur={() => setActive(null)}
      >
        <svg width={width} height={total} viewBox={`0 0 ${width} ${total}`} role="img" aria-label={summary} className="block">
          <defs>
            <linearGradient id={`wash-${uid}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.14" />
              <stop offset="100%" stopColor="var(--primary)" stopOpacity="0.02" />
            </linearGradient>
          </defs>

          {/* grid + axis: hairline, solid, recessive */}
          {ticks.map((t) => (
            <g key={t}>
              <line x1={M.l} x2={M.l + W} y1={y1(t)} y2={y1(t)} stroke="var(--border)" strokeWidth={t === 0 ? 1.5 : 1} opacity={t === 0 ? 1 : 0.7} />
              <text x={M.l - 8} y={y1(t) + 3.5} textAnchor="end" className="fill-muted-foreground text-micro tabular-nums">
                {t > 0 ? "+" : t < 0 ? "−" : ""}
                {Math.abs(t)}%
              </text>
            </g>
          ))}

          <path d={wash} fill={`url(#wash-${uid})`} />
          {lastBench ? <path d={benchPath} fill="none" stroke="var(--muted-foreground)" strokeOpacity="0.75" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" /> : null}
          <path d={retPath} fill="none" stroke="var(--primary)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />

          {/* end dots with a surface ring, and direct end labels (the benchmark's only when it fits) */}
          <circle cx={x(n - 1)} cy={y1(last.ret)} r="4" fill="var(--primary)" stroke="var(--card)" strokeWidth="2" />
          <text x={x(n - 1) + 10} y={y1(last.ret) + 4} className="fill-foreground text-xs font-semibold tabular-nums">
            {signedPct(last.ret, 1)}
          </text>
          {lastBench ? (
            <>
              <circle cx={x(lastBenchIdx)} cy={y1(lastBench.bench as number)} r="4" fill="var(--muted-foreground)" stroke="var(--card)" strokeWidth="2" />
              {labelGap >= 15 && lastBenchIdx === n - 1 ? (
                <text x={x(n - 1) + 10} y={y1(lastBench.bench as number) + 4} className="fill-muted-foreground text-xs tabular-nums">
                  {signedPct(lastBench.bench, 1)}
                </text>
              ) : null}
            </>
          ) : null}

          {/* x labels: first, middle, last */}
          {[0, Math.floor((n - 1) / 2), n - 1].map((i, k) => (
            <text key={`${i}-${k}`} x={x(i)} y={total - 6} textAnchor={k === 0 ? "start" : k === 2 ? "end" : "middle"} className="fill-muted-foreground text-micro">
              {shortDay(series[i].d)}
            </text>
          ))}

          {/* fall from peak: same x axis, depth only */}
          <text x={M.l} y={top2 - 8} className="fill-muted-foreground text-micro">
            Fall from peak
          </text>
          <line x1={M.l} x2={M.l + W} y1={y2(0)} y2={y2(0)} stroke="var(--border)" />
          <text x={M.l - 8} y={y2(0) + 3.5} textAnchor="end" className="fill-muted-foreground text-micro tabular-nums">0%</text>
          {ddMin < -0.0001 ? (
            <>
              <path d={ddArea} fill="var(--loss)" fillOpacity="0.14" />
              <path d={ddPath} fill="none" stroke="var(--loss)" strokeWidth="1.5" strokeLinejoin="round" />
              <circle cx={x(worstIdx)} cy={y2(ddMin)} r="4" fill="var(--loss)" stroke="var(--card)" strokeWidth="2" />
              <text
                x={x(worstIdx) + (worstIdx > n * 0.8 ? -10 : 10)}
                y={y2(ddMin) + 4}
                textAnchor={worstIdx > n * 0.8 ? "end" : "start"}
                className="fill-foreground text-xs font-semibold tabular-nums"
              >
                {signedPct(ddMin, 1)}
              </text>
            </>
          ) : (
            <text x={M.l + W / 2} y={top2 + H2 / 2 + 4} textAnchor="middle" className="fill-muted-foreground text-xs">
              No fall from a high yet
            </text>
          )}

          {/* crosshair: finds the day, readers never aim at a 2px line */}
          {active != null && a ? (
            <g pointerEvents="none">
              <line x1={x(active)} x2={x(active)} y1={M.t} y2={top2 + H2} stroke="var(--foreground)" strokeOpacity="0.35" />
              <circle cx={x(active)} cy={y1(a.ret)} r="4" fill="var(--primary)" stroke="var(--card)" strokeWidth="2" />
              {a.bench != null ? <circle cx={x(active)} cy={y1(a.bench)} r="4" fill="var(--muted-foreground)" stroke="var(--card)" strokeWidth="2" /> : null}
              <circle cx={x(active)} cy={y2(dd[active])} r="3.5" fill="var(--loss)" stroke="var(--card)" strokeWidth="2" />
            </g>
          ) : null}

          <rect
            x={M.l}
            y={M.t}
            width={W}
            height={H1 + GAP + H2}
            fill="transparent"
            onPointerMove={onMove}
            onPointerDown={onMove}
            onPointerLeave={() => setActive(null)}
          />
        </svg>

        {active != null && a ? (
          <div
            className="pointer-events-none absolute top-2 z-10 w-52 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-[var(--shadow-level-2)]"
            style={{ left: tipLeft }}
            role="status"
          >
            <div className="mb-1 text-caption text-muted-foreground">{shortDay(a.d)}</div>
            <div className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <span className="h-0.5 w-3 rounded-full bg-primary" aria-hidden="true" />
                {strategyLabel}
              </span>
              <span className="text-sm font-semibold tabular-nums text-foreground">{signedPct(a.ret)}</span>
            </div>
            {a.bench != null ? (
              <div className="mt-0.5 flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                  <span className="h-0.5 w-3 rounded-full bg-muted-foreground/70" aria-hidden="true" />
                  {benchmarkLabel}
                </span>
                <span className="tabular-nums text-foreground">{signedPct(a.bench)}</span>
              </div>
            ) : null}
            <div className="mt-0.5 flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                <span className="h-0.5 w-3 rounded-full bg-loss" aria-hidden="true" />
                Below peak
              </span>
              <span className="tabular-nums text-foreground">{signedPct(dd[active])}</span>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
