// Design guard. Fails when UI code drifts from the style guide (web/STYLE_GUIDE.md).
//
//   node scripts/check-design.mjs            check against the baseline (runs before `build`)
//   node scripts/check-design.mjs --update   accept the current counts as the new baseline
//
// It is a RATCHET: the baseline records how many violations each file has today, a file may
// never get worse, and the baseline only moves down. New files start at zero. That lets the
// guard land on a live codebase without a big-bang cleanup, and stops regressions from the
// first commit.

import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const SRC = new URL("../src", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const BASELINE = new URL("./design-baseline.json", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const UPDATE = process.argv.includes("--update");

// Files that DEFINE the values the rules forbid elsewhere.
const ALLOW = [/lib[\\/]tone\.ts$/, /lib[\\/]chart-theme\.ts$/, /components[\\/]ui[\\/]surface\.ts$/, /components[\\/]ui[\\/]brand-mark\.tsx$/];

const PALETTE = "emerald|green|rose|red|amber|yellow|orange|slate|gray|zinc|neutral|stone";
const RULES = {
  "hex-colour": {
    why: "Use a token (bg-primary, text-gain, border-border) or lib/chart-theme.ts for canvas.",
    re: /#[0-9a-fA-F]{6}\b/g,
  },
  "raw-palette": {
    why: "Colours carry meaning: use text-gain / text-loss / text-warning / text-info (lib/tone.ts).",
    re: new RegExp(`\\b(?:[a-z]+:)*(?:text|bg|border|ring|fill|stroke|from|via|to|divide)-(?:${PALETTE})-\\d{2,3}\\b`, "g"),
  },
  "pixel-text-size": {
    why: "Use the type scale: text-micro, text-caption, text-xs, text-body, text-sm, text-base...",
    re: /\btext-\[\d+px\]/g,
  },
  "static-inline-style": {
    why: "A style prop with only literal values is a class. Keep style for data-driven geometry.",
    // style={{ a: "x", b: 4 }} with no template, ternary, call or identifier values.
    re: /style=\{\{\s*(?:[A-Za-z]+\s*:\s*(?:"[^"]*"|'[^']*'|-?[\d.]+)\s*,?\s*)+\}\}/g,
  },
  "font-bold": {
    why: "Weights are 400 / 500 / 600. Use font-semibold, not font-bold.",
    re: /\bfont-bold\b/g,
  },
};

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx$/.test(name)) out.push(p);
  }
  return out;
}

/** Strip comments and template strings that are prose (Pine script samples, docs). */
function code(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

const counts = {};
for (const file of walk(SRC)) {
  if (ALLOW.some((re) => re.test(file))) continue;
  const text = code(readFileSync(file, "utf8"));
  const rel = relative(SRC, file).replace(/\\/g, "/");
  for (const [rule, { re }] of Object.entries(RULES)) {
    const n = (text.match(re) ?? []).length;
    if (n) (counts[rel] ??= {})[rule] = n;
  }
}

if (UPDATE) {
  writeFileSync(BASELINE, JSON.stringify(counts, null, 2) + "\n");
  const total = Object.values(counts).flatMap(Object.values).reduce((a, b) => a + b, 0);
  console.log(`baseline written: ${total} known violations in ${Object.keys(counts).length} files`);
  process.exit(0);
}

const base = existsSync(BASELINE) ? JSON.parse(readFileSync(BASELINE, "utf8")) : {};
const failures = [];
let better = 0;
for (const [file, rules] of Object.entries(counts)) {
  for (const [rule, n] of Object.entries(rules)) {
    const allowed = base[file]?.[rule] ?? 0;
    if (n > allowed) failures.push({ file, rule, n, allowed });
  }
}
for (const [file, rules] of Object.entries(base)) {
  for (const [rule, allowed] of Object.entries(rules)) if ((counts[file]?.[rule] ?? 0) < allowed) better++;
}

if (failures.length) {
  console.error("\nDesign check failed. These files got further from the style guide:\n");
  for (const f of failures) {
    console.error(`  ${f.file}  [${f.rule}]  ${f.n} found, ${f.allowed} allowed`);
    console.error(`      ${RULES[f.rule].why}`);
  }
  console.error("\nSee web/STYLE_GUIDE.md. If a violation is genuinely needed, run with --update and say why in the commit.\n");
  process.exit(1);
}
console.log(`design check passed${better ? ` (${better} rule counts improved: run --update to lock them in)` : ""}`);
