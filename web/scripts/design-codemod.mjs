// One-off codemod: bring every className in src/ onto the design tokens.
// Run:  node scripts/design-codemod.mjs [--write]
// Without --write it only reports how many replacements each rule would make.

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const WRITE = process.argv.includes("--write");
const ROOT = new URL("../src", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

// Files that DEFINE tokens or intentionally name raw values.
const SKIP = [/lib[\\/]tone\.ts$/, /components[\\/]ui[\\/]surface\.ts$/, /index\.css$/];

const PREFIX = "(text|bg|border|ring|stroke|fill|from|via|to|divide|outline|shadow|decoration|accent|caret)";
const tone = (names, out) => [
  // tints and dark-mode backdrops become a soft wash of the tone, not a solid fill
  [new RegExp(`\\b(bg)-(?:${names})-(?:50|100|200)\\b`, "g"), `$1-${out}/10`, `${out}: pale bg`],
  [new RegExp(`\\b(bg)-(?:${names})-(?:900|950)\\b`, "g"), `$1-${out}/15`, `${out}: dark bg`],
  [new RegExp(`\\b${PREFIX}-(?:${names})-\\d{2,3}\\b`, "g"), `$1-${out}`, `${out}: any shade`],
];

const RULES = [
  ...tone("emerald|green", "gain"),
  ...tone("rose|red", "loss"),
  ...tone("amber|yellow|orange", "warning"),

  // type scale: one-off pixel sizes -> named steps
  [/\btext-\[9px\]/g, "text-micro", "type 9px"],
  [/\btext-\[10px\]/g, "text-micro", "type 10px"],
  [/\btext-\[11px\]/g, "text-caption", "type 11px"],
  [/\btext-\[12px\]/g, "text-xs", "type 12px"],
  [/\btext-\[13px\]/g, "text-body", "type 13px"],
  [/\btext-\[14px\]/g, "text-sm", "type 14px"],
  [/\btext-\[15px\]/g, "text-sm", "type 15px"],

  // weights: 400 body, 500 labels and controls, 600 headings and key figures; no bold
  [/\bfont-bold\b/g, "font-semibold", "weight bold"],

  // hex that has a token
  [/-\[#533afd\]/g, "-primary", "hex primary"],
  [/-\[#4434d4\]/g, "-primary-deep", "hex primary-deep"],
  [/-\[#2e2b8c\]/g, "-primary-press", "hex primary-press"],
  [/-\[#665efd\]/g, "-primary-soft", "hex primary-soft"],
  [/-\[#b9b9f9\]/g, "-primary-subdued", "hex primary-subdued"],
  [/-\[#ea2261\]/g, "-loss", "hex ruby"],
  [/-\[#e3e8ee\]/g, "-border", "hex hairline"],
  [/-\[#64748d\]/g, "-muted-foreground", "hex ink-mute"],
  [/-\[#0d253d\]/g, "-foreground", "hex ink"],
  [/-\[#273951\]/g, "-ink-secondary", "hex ink-secondary"],
  [/-\[#1c1e54\]/g, "-brand-dark", "hex brand-dark"],
  [/\bbg-white dark:bg-card\b/g, "bg-card", "bg-white/dark:bg-card"],

  // cards: one radius, one shell
  [/\brounded-2xl\b/g, "rounded-xl", "radius 2xl"],
  [/\brounded(?:-lg)? border border-border bg-card\b/g, "rounded-xl border border-border bg-card", "card shell"],
  [/\brounded border border-border(?! bg-card)/g, "rounded-lg border border-border", "inset tile"],
];

// A bare `rounded` is only a class inside a string literal; elsewhere it is an identifier.
const BARE = /(?<![\w:./-])rounded(?![\w-])/g;
const STRING = /(["'`])((?:\\.|(?!\1)[^\n])*?)\1/g;
function bareRounded(text, count) {
  return text.replace(STRING, (whole, q, body) => {
    BARE.lastIndex = 0;
    if (/[=;{}]/.test(body) || !BARE.test(body)) return whole;
    BARE.lastIndex = 0;
    return q + body.replace(BARE, () => (count(), "rounded-md")) + q;
  });
}

// `X dark:X` is redundant once both sides are the same token.
const DEDUPE = [
  [/(\b[\w/.[\]-]+)\s+dark:\1(?=[\s"'`}])/g, "$1"],
  [/\s+dark:border-white\/10(?=[\s"'`}])/g, ""],
];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(tsx|ts)$/.test(name) && !/\.test\.ts$/.test(name)) out.push(p);
  }
  return out;
}

const totals = new Map();
let changedFiles = 0;
for (const file of walk(ROOT)) {
  if (SKIP.some((re) => re.test(file))) continue;
  const before = readFileSync(file, "utf8");
  let after = before;
  for (const [re, to, label] of RULES) {
    after = after.replace(re, (...m) => {
      totals.set(label, (totals.get(label) ?? 0) + 1);
      return to.replace(/\$(\d)/g, (_, i) => m[Number(i)]);
    });
  }
  after = bareRounded(after, () => totals.set("bare rounded", (totals.get("bare rounded") ?? 0) + 1));
  for (const [re, to] of DEDUPE) after = after.replace(re, to);
  if (after !== before) {
    changedFiles++;
    if (WRITE) writeFileSync(file, after);
  }
}

console.log(`${WRITE ? "wrote" : "would change"} ${changedFiles} files`);
for (const [label, n] of [...totals].sort((a, b) => b[1] - a[1])) console.log(String(n).padStart(5), label);
