/**
 * Chart colours for canvas code.
 *
 * lightweight-charts draws on a canvas, which cannot read CSS variables, so canvas options
 * need real colour strings. They come from the same tokens the legends use (--chart-* in
 * index.css), read once per call, so a swatch and its line can never disagree.
 */

const FALLBACK = {
  up: "#089981",
  down: "#f23645",
  blue: "#2962ff",
  orange: "#ff6d00",
  purple: "#9c27b0",
  yellow: "#ffeb3b",
  cyan: "#00e5ff",
  violet: "#7e57c2",
  sky: "#2196f3",
  pink: "#ff007f",
} as const;

export type ChartColor = keyof typeof FALLBACK;

export function chartColor(name: ChartColor): string {
  if (typeof document === "undefined") return FALLBACK[name];
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--chart-${name}`).trim();
  return v || FALLBACK[name];
}

/** The chart canvas surface. Charts keep their own dark/light pair (not the app's
 *  surface tokens) so a candle chart looks like every other candle chart. */
export function chartSurface(isDark: boolean) {
  return isDark
    ? { bg: "#131722", text: "#787b86", border: "#2a2e39", grid: "#1e222d", crosshair: "#50535e" }
    : { bg: "#ffffff", text: "#6a6d78", border: "#e0e3eb", grid: "#f0f3fa", crosshair: "#b2b5be" };
}
