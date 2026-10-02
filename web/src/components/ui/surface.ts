/**
 * Surfaces: every container in the app is one of these three, so cards look the same on
 * every page.
 *
 *   surface       a card: a panel on the page background
 *   surfaceInset  a tile or row sitting INSIDE a card (one step smaller radius)
 *   surfaceMuted  a quiet filled area inside a card (no border)
 *
 * Padding is not part of the surface. Cards are p-5; compact tiles are p-4; list rows are
 * px-3 py-2. See STYLE_GUIDE.md, "Spacing".
 */
export const surface = "rounded-xl border border-border bg-card";
export const surfaceInset = "rounded-lg border border-border";
export const surfaceMuted = "rounded-lg bg-muted/50";
