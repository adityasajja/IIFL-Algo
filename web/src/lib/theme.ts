/**
 * DEPRECATED as a source of truth: the system is STYLE_GUIDE.md, with tokens in index.css and
 * components in components/ui. The tables below remain so existing imports keep working; new code
 * should use the tokens, `lib/tone.ts` and `lib/format.ts` directly.
 *
 * Stripi design tokens — mirrors web/DESIGN.md frontmatter.
 * Single source of truth for Tailwind class strings.
 * Display tiers are always weight 300 with negative tracking + ss01.
 * Money / numeric cells add tabular-nums (tnum).
 */

// ── Typography Tokens (DESIGN.md hierarchy) ──────────────────────────────
export const TYPOGRAPHY = {
  // Display — thin weight is the brand, never bump above 300
  displayXxl:
    "text-[56px] max-md:text-4xl font-light leading-[1.03] tracking-[-1.4px] max-md:tracking-[-0.64px] text-foreground",
  displayXl: "text-5xl font-light leading-[1.15] tracking-[-0.96px] text-foreground",
  displayLg: "text-[32px] font-light leading-[1.1] tracking-[-0.64px] text-foreground",
  displayMd: "text-[26px] font-light leading-[1.12] tracking-[-0.26px] text-foreground",
  headingLg: "text-[22px] font-light leading-[1.1] tracking-[-0.22px] text-foreground",
  headingMd: "text-xl font-light leading-[1.4] tracking-[-0.2px] text-foreground",
  headingSm: "text-lg font-light leading-[1.4] text-foreground",

  // Legacy aliases used across panels
  h1: "text-[32px] font-light leading-[1.1] tracking-[-0.64px] text-foreground",
  h2: "text-[22px] font-light leading-[1.1] tracking-[-0.22px] text-foreground",
  h3: "text-lg font-light leading-[1.4] text-foreground",
  h4: "text-micro font-normal uppercase tracking-[0.1px] text-muted-foreground",

  // Metric callouts — whisper-weight monument, always tabular
  metricHero:
    "text-4xl sm:text-5xl font-light tracking-[-0.96px] tabular-nums [font-feature-settings:'ss01'_on,'tnum'_on]",
  metricLg:
    "text-[32px] font-light tracking-[-0.64px] tabular-nums [font-feature-settings:'ss01'_on,'tnum'_on]",
  metricMd:
    "text-[26px] font-light tracking-[-0.26px] tabular-nums [font-feature-settings:'ss01'_on,'tnum'_on]",
  metricSm:
    "text-sm font-light tracking-tight tabular-nums [font-feature-settings:'ss01'_on,'tnum'_on]",

  // Body — default UI body is 15px weight 300
  body: "text-sm leading-[1.4] text-foreground font-light",
  bodyLg: "text-base font-light leading-[1.4] text-foreground",
  bodyMuted: "text-sm leading-[1.4] text-muted-foreground font-light",
  bodyTabular:
    "text-sm font-light leading-[1.4] tracking-[-0.42px] tabular-nums [font-feature-settings:'ss01'_on,'tnum'_on]",
  caption:
    "text-body leading-[1.4] tracking-[-0.39px] tabular-nums text-muted-foreground font-normal [font-feature-settings:'ss01'_on,'tnum'_on]",
  sub: "text-sm text-muted-foreground mt-0.5 font-light",
  micro: "text-caption leading-[1.4] text-muted-foreground font-light",
  eyebrow:
    "text-micro font-normal uppercase tracking-[0.1px] text-muted-foreground [font-feature-settings:'ss01'_on]",
  mono: "font-mono text-xs tabular-nums",
} as const;

// ── Semantic Color Tokens ────────────────────────────────────────────────
// Primary is reserved for filled CTAs + link emphasis, one per band.
// pill-tag-soft: bg #b9b9f9, text #4434d4. Error/success live in dashboard UI.
export const TONES = {
  profit: "text-gain font-normal",
  loss: "text-loss font-normal",
  warn: "text-warning font-normal",
  info: "text-primary font-normal",
  muted: "text-muted-foreground",
  foreground: "text-foreground",

  profitBg:
    "bg-gain/[0.08] text-gain border border-gain/20",
  lossBg: "bg-loss/[0.08] text-loss border border-loss/20",
  warnBg: "bg-warning/[0.08] text-warning border border-warning/20",
  infoBg: "bg-primary-subdued text-primary-deep border border-primary-subdued",
  mutedBg: "bg-muted text-muted-foreground border border-border",
} as const;

// ── Container & Card Tokens ──────────────────────────────────────────────
export const SURFACES = {
  card: "rounded-xl border border-border bg-card", // = `surface` (components/ui/surface.ts); cards are flat
  cardHeader:
    "flex items-start justify-between gap-3 p-8 pb-6 border-b border-border",
  panel: "space-y-8 pb-12",
  container: "mx-auto w-full max-w-[1200px] px-6",
  gridHero: "grid gap-6 md:grid-cols-2 xl:grid-cols-3",
  gridKpi: "grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6",
} as const;

// ── Currency & Percent Formatters (moved to lib/format.ts; re-exported for old imports) ──
export { formatInr, formatPct } from "./format";

export function getPnlTone(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return TONES.muted;
  if (v > 0) return TONES.profit;
  if (v < 0) return TONES.loss;
  return TONES.foreground;
}
