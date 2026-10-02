// Convert fully-static inline `style={{ ... }}` blocks to token-based Tailwind classes.
// Data-driven styles (any value that is not a literal) are left alone: a bar's width in %
// or a tooltip's left offset is geometry, not styling, and belongs in a style prop.
//
//   node scripts/inline-to-tailwind.mjs <file...>            report only
//   node scripts/inline-to-tailwind.mjs --write <file...>    rewrite
//
// A block is converted only when EVERY property maps to a class; otherwise it is reported
// untouched, so nothing is half-converted.

import { readFileSync, writeFileSync } from "node:fs";

const WRITE = process.argv.includes("--write");
const FILES = process.argv.slice(2).filter((a) => !a.startsWith("--"));

// ---- value helpers -------------------------------------------------------------------
const SPACE = { 0: "0", 1: "px", 2: "0.5", 4: "1", 6: "1.5", 8: "2", 10: "2.5", 12: "3", 14: "3.5", 16: "4", 20: "5", 24: "6", 28: "7", 32: "8", 40: "10", 48: "12" };

function px(v) {
  if (typeof v === "number") return v;
  const m = /^(-?[\d.]+)(px|rem)?$/.exec(String(v).trim());
  if (!m) return null;
  return m[2] === "rem" ? Number(m[1]) * 16 : Number(m[1]);
}
function space(v) {
  const n = px(v);
  if (n === null) return null;
  const rounded = Math.round(n * 10) / 10;
  if (SPACE[rounded] ?? SPACE[Math.round(rounded)]) return SPACE[rounded] ?? SPACE[Math.round(rounded)];
  // Off-scale values (7px, 1.1rem) snap to the nearest step: that is the point of the scale.
  const steps = Object.keys(SPACE).map(Number);
  const nearest = steps.reduce((a, b) => (Math.abs(b - n) < Math.abs(a - n) ? b : a));
  return SPACE[nearest];
}
/** "0.4rem 0.75rem" -> py/px classes. */
function boxShorthand(prefix, v) {
  const parts = String(v).trim().split(/\s+/);
  const s = parts.map(space);
  if (s.some((x) => x === null)) return null;
  if (parts.length === 1) return [`${prefix}-${s[0]}`];
  if (parts.length === 2) return [`${prefix}y-${s[0]}`, `${prefix}x-${s[1]}`];
  if (parts.length === 4) return [`${prefix}t-${s[0]}`, `${prefix}r-${s[1]}`, `${prefix}b-${s[2]}`, `${prefix}l-${s[3]}`];
  return null;
}

const FONT_SIZE = { 9: "text-micro", 10: "text-micro", 11: "text-caption", 12: "text-xs", 13: "text-body", 14: "text-sm", 15: "text-sm", 16: "text-base", 18: "text-lg", 20: "text-xl", 24: "text-2xl" };
const FONT_WEIGHT = { 300: "font-light", 400: "font-normal", 500: "font-medium", 600: "font-semibold", 700: "font-semibold", 800: "font-semibold" };

// Slate/indigo hexes the old panels used -> the token with the same job.
const COLOR = {
  "#64748b": "muted-foreground", "#475569": "muted-foreground", "#94a3b8": "muted-foreground", "#a1a1aa": "muted-foreground",
  "#e2e8f0": "foreground", "#f1f5f9": "foreground", "#cbd5e1": "foreground",
  "#818cf8": "primary-soft", "#6366f1": "primary", "#a5b4fc": "primary-soft", "#533afd": "primary",
  "#10b981": "gain", "#34d399": "gain", "#22c55e": "gain",
  "#ef4444": "loss", "#f87171": "loss", "#fca5a5": "loss",
  "#eab308": "warning", "#f59e0b": "warning", "#fbbf24": "warning",
  "var(--text-secondary, #94a3b8)": "muted-foreground",
  "var(--muted-foreground)": "muted-foreground", "var(--foreground)": "foreground",
  "#38bdf8": "info", "#60a5fa": "info", "#3b82f6": "info",
};
function color(v) {
  const k = String(v).trim().toLowerCase();
  return COLOR[k] ?? null;
}
/** rgba(r,g,b,a) of a known tone -> token/alpha. */
function rgba(v) {
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/.exec(String(v).trim());
  if (!m) return null;
  const key = `${m[1]},${m[2]},${m[3]}`;
  const alpha = Math.round(parseFloat(m[4] ?? "1") * 100);
  const tone = { "99,102,241": "primary", "239,68,68": "loss", "16,185,129": "gain", "234,179,8": "warning", "245,158,11": "warning", "30,41,59": null }[key];
  if (tone === undefined || tone === null) return null;
  return { token: tone, alpha };
}

const MAP = {
  display: (v) => ({ flex: "flex", grid: "grid", block: "block", none: "hidden", "inline-flex": "inline-flex", inline: "inline", "inline-block": "inline-block" })[v] && [({ flex: "flex", grid: "grid", block: "block", none: "hidden", "inline-flex": "inline-flex", inline: "inline", "inline-block": "inline-block" })[v]],
  flexDirection: (v) => ({ column: ["flex-col"], row: ["flex-row"] })[v],
  flexWrap: (v) => (v === "wrap" ? ["flex-wrap"] : v === "nowrap" ? ["flex-nowrap"] : null),
  flex: (v) => (v === 1 || v === "1" ? ["flex-1"] : null),
  alignItems: (v) => ({ center: ["items-center"], "flex-start": ["items-start"], "flex-end": ["items-end"], baseline: ["items-baseline"], stretch: ["items-stretch"] })[v],
  justifyContent: (v) => ({ center: ["justify-center"], "space-between": ["justify-between"], "flex-end": ["justify-end"], "flex-start": ["justify-start"] })[v],
  gap: (v) => (space(v) ? [`gap-${space(v)}`] : null),
  padding: (v) => boxShorthand("p", v),
  paddingTop: (v) => (space(v) ? [`pt-${space(v)}`] : null),
  paddingBottom: (v) => (space(v) ? [`pb-${space(v)}`] : null),
  paddingLeft: (v) => (space(v) ? [`pl-${space(v)}`] : null),
  paddingRight: (v) => (space(v) ? [`pr-${space(v)}`] : null),
  margin: (v) => boxShorthand("m", v),
  marginTop: (v) => (space(v) ? [`mt-${space(v)}`] : null),
  marginBottom: (v) => (space(v) ? [`mb-${space(v)}`] : null),
  marginLeft: (v) => (v === "auto" ? ["ml-auto"] : space(v) ? [`ml-${space(v)}`] : null),
  marginRight: (v) => (space(v) ? [`mr-${space(v)}`] : null),
  fontSize: (v) => (FONT_SIZE[px(v)] ? [FONT_SIZE[px(v)]] : null),
  fontWeight: (v) => (FONT_WEIGHT[Number(v)] ? [FONT_WEIGHT[Number(v)]] : null),
  color: (v) => (color(v) ? [`text-${color(v)}`] : null),
  textAlign: (v) => ({ left: ["text-left"], right: ["text-right"], center: ["text-center"] })[v],
  textTransform: (v) => (v === "uppercase" ? ["uppercase"] : v === "capitalize" ? ["capitalize"] : null),
  letterSpacing: (v) => (["0.04em", "0.05em", "0.06em"].includes(v) ? ["tracking-wider"] : v === "0.08em" || v === "0.1em" ? ["tracking-widest"] : null),
  whiteSpace: (v) => (v === "nowrap" ? ["whitespace-nowrap"] : null),
  cursor: (v) => (v === "pointer" ? ["cursor-pointer"] : v === "not-allowed" ? ["cursor-not-allowed"] : null),
  overflowX: (v) => (v === "auto" ? ["overflow-x-auto"] : null),
  overflow: (v) => (v === "hidden" ? ["overflow-hidden"] : v === "auto" ? ["overflow-auto"] : null),
  width: (v) => (v === "100%" ? ["w-full"] : null),
  minWidth: (v) => (space(v) ? [`min-w-${space(v)}`] : null),
  borderRadius: (v) => ({ 4: ["rounded"], 6: ["rounded-md"], 8: ["rounded-lg"], 12: ["rounded-xl"], 999: ["rounded-full"] })[px(v)]?.map((c) => (c === "rounded" ? "rounded-md" : c)),
  borderCollapse: (v) => (v === "collapse" ? ["border-collapse"] : null),
  lineHeight: (v) => (Number(v) === 1.5 ? ["leading-normal"] : Number(v) === 1.4 || Number(v) === 1.6 ? ["leading-relaxed"] : null),
  background: (v) => bg(v),
  backgroundColor: (v) => bg(v),
  border: (v) => borderAny("border", v),
  borderBottom: (v) => borderAny("border-b", v),
  borderTop: (v) => borderAny("border-t", v),
  transition: () => ["transition-colors"],
  maxWidth: (v) => ({ 320: ["max-w-xs"], 384: ["max-w-sm"], 448: ["max-w-md"] })[px(v)],
  letterSpacing2: () => null,
  gridTemplateColumns: (v) => {
    const t = String(v).trim();
    if (t === "1fr 1fr") return ["grid-cols-2"];
    if (t === "1fr 1fr 1fr") return ["grid-cols-3"];
    const m = /^repeat\((auto-fit|auto-fill),\s*minmax\((\d+)px,\s*1fr\)\)$/.exec(t);
    return m ? [`grid-cols-[repeat(${m[1]},minmax(${m[2]}px,1fr))]`] : null;
  },
  opacity: (v) => ({ 0.5: ["opacity-50"], 0.6: ["opacity-60"], 0.7: ["opacity-70"], 0.4: ["opacity-40"] })[Number(v)],
};

function bg(v) {
  const s = String(v).trim();
  if (s === "transparent" || s === "none") return ["bg-transparent"];
  if (s === "var(--card)") return ["bg-card"];
  if (s === "var(--border)") return ["bg-border"];
  if (s === "rgba(15,23,42,0.5)" || s === "rgba(15, 23, 42, 0.5)") return ["bg-muted/50"];
  // 8-digit hex: tone + alpha byte (e.g. #ef444420 = 12%)
  const h = /^#([0-9a-f]{6})([0-9a-f]{2})$/i.exec(s);
  if (h) {
    const tone = { ef4444: "loss", "10b981": "gain", "0284c7": "info", eab308: "warning", f59e0b: "warning" }[h[1].toLowerCase()];
    if (tone) return [`bg-${tone}/${Math.round((parseInt(h[2], 16) / 255) * 100)}`];
  }
  const r = rgba(s);
  if (r) return [`bg-${r.token}/${r.alpha}`];
  const c = color(s);
  if (c) return [`bg-${c}`];
  if (s === "rgba(30,41,59,0.7)" || s === "rgba(30, 41, 59, 0.7)") return ["bg-card"];
  return null;
}
function borderAny(prefix, v) {
  const m = /^(\d+)px solid (.+)$/.exec(String(v).trim());
  if (!m) return v === "none" ? [`${prefix}-0`] : null;
  const width = m[1] === "1" ? prefix : `${prefix}-${m[1]}`;
  const r = rgba(m[2]);
  const col = r ? `${prefix.replace(/^border-?/, "border-")}${prefix === "border" ? "" : ""}` : null;
  void col;
  const base = prefix === "border" ? "border" : prefix;
  if (r) return [width, `${base === "border" ? "border" : base}-${r.token}/${r.alpha}`];
  const c = color(m[2]);
  return c ? [width, `${base}-${c}`] : null;
}

// ---- parsing -------------------------------------------------------------------------
/** Parse the inside of `{ a: 1, b: "x" }`; return null if any value is not a literal. */
function parseObject(body) {
  const props = [];
  let i = 0;
  const n = body.length;
  while (i < n) {
    while (i < n && /[\s,]/.test(body[i])) i++;
    if (i >= n) break;
    const key = /^[A-Za-z]+/.exec(body.slice(i));
    if (!key) return null;
    i += key[0].length;
    while (/\s/.test(body[i])) i++;
    if (body[i] !== ":") return null;
    i++;
    while (/\s/.test(body[i])) i++;
    let value;
    if (body[i] === '"' || body[i] === "'") {
      const q = body[i];
      const end = body.indexOf(q, i + 1);
      if (end < 0) return null;
      value = body.slice(i + 1, end);
      i = end + 1;
    } else {
      const m = /^-?[\d.]+/.exec(body.slice(i));
      if (!m) return null; // an identifier, call, template, ternary...: not static
      value = Number(m[0]);
      i += m[0].length;
    }
    while (/\s/.test(body[i] ?? "")) i++;
    if (i < n && body[i] !== ",") return null; // something after the value (e.g. `? :`)
    props.push([key[0], value]);
  }
  return props;
}

function toClasses(props) {
  const out = [];
  for (const [k, v] of props) {
    const fn = MAP[k];
    const cls = fn ? fn(v) : null;
    if (!cls) return { fail: `${k}: ${v}` };
    out.push(...cls);
  }
  return { classes: out };
}

const unmapped = new Map();
let converted = 0;
let skipped = 0;

for (const file of FILES) {
  let text = readFileSync(file, "utf8");
  const crlf = text.includes("\r\n");
  text = text.replace(/\r\n/g, "\n");
  // style={{ ... }} with no nested braces (so no template literals / objects inside).
  const re = /style=\{\{([^{}]*)\}\}/g;
  text = text.replace(re, (whole, body) => {
    if (body.includes("${") || body.includes("?")) return whole;
    const props = parseObject(body);
    if (!props) { skipped++; return whole; }
    const r = toClasses(props);
    if (r.fail) { unmapped.set(r.fail, (unmapped.get(r.fail) ?? 0) + 1); skipped++; return whole; }
    converted++;
    return `className="${r.classes.join(" ")}"`;
  });
  // Two className attributes on one tag (the element already had one): merge them.
  text = text.replace(/className="([^"]*)"((?:\s+[\w-]+=(?:"[^"]*"|\{[^{}]*\}))*?)\s+className="([^"]*)"/g, (_, a, mid, b) => `className="${a} ${b}"${mid}`);
  if (WRITE) writeFileSync(file, crlf ? text.replace(/\n/g, "\r\n") : text);
}

console.log(`${WRITE ? "converted" : "would convert"} ${converted} blocks; left ${skipped} untouched`);
for (const [k, n] of [...unmapped].sort((a, b) => b[1] - a[1]).slice(0, 25)) console.log(String(n).padStart(4), "unmapped", k);
