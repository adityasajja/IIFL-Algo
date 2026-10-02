/**
 * Display formatting for identifiers the backend sends as machine keys.
 *
 * A value like `paper_avellaneda_lee` or `PAPER_FORWARD` is a key, not a label.
 * Anything a person reads goes through here; the raw key stays available as a
 * value, a `title`, or a monospace technical detail where reproducibility needs it.
 */

// Words that read wrong when naively capitalised.
const ACRONYMS: Record<string, string> = {
  atr: "ATR",
  rsi: "RSI",
  sma: "SMA",
  ema: "EMA",
  vwap: "VWAP",
  nse: "NSE",
  bse: "BSE",
  oos: "OOS",
  mae: "MAE",
  mfe: "MFE",
  ltp: "LTP",
  oi: "OI",
  iv: "IV",
  pnl: "P&L",
  iima: "IIMA",
  nism: "NISM",
  ipo: "IPO",
  etf: "ETF",
  ohlc: "OHLC",
  eod: "EOD",
};

/** `paper_avellaneda_lee` / `PAPER_FORWARD` / `sma-window` → "Paper Avellaneda Lee". */
export function humanize(key: string): string {
  if (!key) return key;
  return key
    .replace(/[_-]+/g, " ")
    .trim()
    .split(/\s+/)
    .map((w) => ACRONYMS[w.toLowerCase()] ?? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(" ");
}

/** Sentence case: `paper_forward` → "Paper forward". For labels inside a sentence. */
export function humanizeSentence(key: string): string {
  const s = humanize(key);
  return s.charAt(0) + s.slice(1).toLowerCase().replace(/\b(atr|rsi|sma|ema|vwap|nse|bse|oos|mae|mfe|ltp|oi|iv|iima|nism|etf|eod)\b/g, (m) => m.toUpperCase());
}

/** `watchlist.items_add` → "Watchlist · items add". Dotted audit/event codes. */
export function humanizeEvent(code: string): string {
  return code
    .split(".")
    .map((part, i) => (i === 0 ? humanize(part) : humanizeSentence(part)))
    .join(" · ");
}

/**
 * A built-in engine's display name: `paper_avellaneda_lee` → "Avellaneda Lee".
 * The `paper_` prefix marks the module's origin (an academic paper), not part of
 * its name, and showing it on one screen and not another made the same strategy
 * look like two.
 */
export function strategyLabel(key: string): string {
  return humanize(key.replace(/^paper_/, ""));
}

/**
 * An audit `detail` is free text for mode/kill-switch changes but a JSON object
 * for most other events. Render objects as `key: value · key: value` rather
 * than dumping braces and quotes into a table cell.
 */
export function formatDetail(detail: string | null | undefined): string {
  if (!detail) return "—";
  const text = detail.trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return text;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return Object.entries(parsed as Record<string, unknown>)
        .map(([k, v]) => {
          // Values are often identifiers too (`change_pct`), so strip underscores.
          const value = (Array.isArray(v) ? v.join(", ") : String(v)).replace(/_/g, " ");
          return `${humanizeSentence(k).toLowerCase()}: ${value}`;
        })
        .join(" · ");
    }
  } catch {
    // not JSON after all; show it as written
  }
  return text;
}

/** An ISO timestamp as IST wall-clock time, e.g. "18 Sep, 11:42 pm". */
export function formatIst(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "Asia/Kolkata",
  });
}

// ─── Numbers ─────────────────────────────────────────────────────────────────
//
// Every figure the UI shows goes through these, so a price, a signed change and a missing
// value look the same on every page (STYLE_GUIDE.md 4.2):
//
//   price           ₹ + Indian digit grouping          ₹22,421.95
//   signed change   sign always shown, then %          +0.88%  /  -0.88%
//   missing         an em dash, never 0                —
//
// Missing or non-finite input is always "—": a figure that could not be measured must not be
// shown as zero, which is a plausible value.

const MISSING = "—";

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Indian-grouped number, up to `digits` decimals. Booleans read yes/no; other values pass through. */
export function fmtNum(v: unknown, digits = 2): string {
  if (v === null || v === undefined) return MISSING;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isFinite(v) ? v.toLocaleString("en-IN", { maximumFractionDigits: digits }) : MISSING;
  return String(v);
}

/** A signed percentage: `+1.20%`, `-0.40%`. */
export function fmtPct(v: unknown, digits = 2): string {
  if (!finite(v)) return MISSING;
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** Rupees with Indian grouping and a fixed number of decimals: `₹22,421.95`. */
export function fmtMoney(v: unknown, digits = 0): string {
  if (!finite(v)) return MISSING;
  return `₹${v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** Rupees with a leading minus for losses: `-₹1,200`. */
export function formatInr(v: number | null | undefined, frac = 0): string {
  if (!finite(v)) return MISSING;
  const sign = v < 0 ? "-" : "";
  return `${sign}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: frac, maximumFractionDigits: frac })}`;
}

export const formatPct = (v: number | null | undefined, frac = 2): string => fmtPct(v, frac);
