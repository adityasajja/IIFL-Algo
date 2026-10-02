/**
 * The live style guide (/styleguide.html).
 *
 * It renders the real tokens and the real components, so it cannot drift from the app: if a
 * token changes, this page changes with it. STYLE_GUIDE.md is the written rules; this is the
 * proof. It needs no login and no backend.
 */

import { useEffect, useState, type ReactNode } from "react";
import { Card, CardHeader, EmptyState, ErrorBox, Hint } from "../components/ui/card";
import { Button } from "../components/ui/button";
import { fieldInput, fieldLabel, toolbarButton } from "../components/ui/form-styles";
import { Badge, Callout, Stat, VerdictPill } from "../components/ui/stat";
import { surface, surfaceInset, surfaceMuted } from "../components/ui/surface";
import { Tabs, TabsList, TabsTrigger } from "../components/motion/tabs";
import { fmtMoney, fmtNum, fmtPct } from "../lib/format";
import { toneFill, toneOf, toneText, type Tone } from "../lib/tone";
import { cn } from "../lib/utils";

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Read a CSS variable's resolved value, so swatches always show what the app really uses. */
function useCssVar(name: string, theme: string): string {
  const [v, setV] = useState("");
  useEffect(() => {
    setV(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
  }, [name, theme]);
  return v;
}

function Section({ id, title, lead, children }: { id: string; title: string; lead?: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-20 space-y-4">
      <div>
        <h2 className="text-heading">{title}</h2>
        {lead ? <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{lead}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Sub({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-3">
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </div>
  );
}

function Swatch({ token, utility, use, theme }: { token: string; utility: string; use: string; theme: string }) {
  const value = useCssVar(`--${token}`, theme);
  return (
    <div className={cn(surfaceInset, "flex items-center gap-3 bg-card p-3")}>
      <div className="size-10 shrink-0 rounded-md border border-border" style={{ background: `var(--${token})` }} />
      <div className="min-w-0">
        <div className="truncate font-mono text-xs font-medium">{utility}</div>
        <div className="truncate text-caption text-muted-foreground">{use}</div>
        <div className="truncate font-mono text-micro text-muted-foreground">{value}</div>
      </div>
    </div>
  );
}

const SURFACE_TOKENS = [
  ["background", "bg-background", "The page"],
  ["card", "bg-card", "Panels on the page"],
  ["muted", "bg-muted", "Quiet fills, stripes, tab tracks"],
  ["border", "border-border", "Every hairline"],
  ["foreground", "text-foreground", "Primary text and numbers"],
  ["muted-foreground", "text-muted-foreground", "Labels, captions, units"],
] as const;
const BRAND_TOKENS = [
  ["primary", "bg-primary", "Actions, active states, focus"],
  ["primary-deep", "bg-primary-deep", "Hover"],
  ["primary-press", "bg-primary-press", "Pressed"],
  ["primary-soft", "text-primary-soft", "Brand on dark"],
  ["primary-subdued", "border-primary-subdued", "Secondary control borders"],
] as const;
const TONE_TOKENS = [
  ["gain", "text-gain", "good: profit, rising, healthy, pass"],
  ["loss", "text-loss", "bad: loss, falling, unhealthy, error"],
  ["warning", "text-warning", "warn: caution, thin evidence"],
  ["info", "text-info", "info: neutral highlight"],
] as const;
const CHART_TOKENS = [
  ["chart-up", "bg-chart-up", "Up candle"],
  ["chart-down", "bg-chart-down", "Down candle"],
  ["chart-blue", "bg-chart-blue", "Series"],
  ["chart-orange", "bg-chart-orange", "Series"],
  ["chart-purple", "bg-chart-purple", "Series"],
  ["chart-yellow", "bg-chart-yellow", "Series"],
  ["chart-cyan", "bg-chart-cyan", "Series"],
  ["chart-violet", "bg-chart-violet", "Series"],
] as const;

const TYPE_SCALE: { role: string; cls: string; spec: string; sample: string }[] = [
  { role: "Display", cls: "text-display", spec: "32 / 300", sample: "Know your market" },
  { role: "Page title", cls: "text-heading", spec: "22 / 300", sample: "Markets" },
  { role: "Metric", cls: "text-2xl font-semibold tabular-nums", spec: "24 / 600", sample: "₹22,421.95" },
  { role: "Card title", cls: "text-sm font-semibold", spec: "14 / 600", sample: "Sector strength" },
  { role: "Body", cls: "text-sm", spec: "14 / 400", sample: "Stocks above their 50-day average rose to 62%." },
  { role: "Body, dense", cls: "text-body", spec: "13 / 400", sample: "Stocks above their 50-day average rose to 62%." },
  { role: "Label", cls: "text-xs text-muted-foreground", spec: "12 / 400", sample: "Return over the last month" },
  { role: "Caption", cls: "text-caption text-muted-foreground", spec: "11 / 400", sample: "Data as of 1 Oct" },
  { role: "Micro", cls: "text-micro font-semibold uppercase tracking-wider text-muted-foreground", spec: "10 / 600", sample: "Net return" },
];

const SPACING: [string, string, string][] = [
  ["1.5", "6px", "label to value"],
  ["2", "8px", "dense list gap"],
  ["3", "12px", "items inside a card"],
  ["4", "16px", "between cards (default rhythm)"],
  ["5", "20px", "card padding"],
  ["6", "24px", "between page sections"],
  ["8", "32px", "page breathing room"],
];

const TONES: Tone[] = ["good", "bad", "warn", "info", "flat"];

// ─── page ────────────────────────────────────────────────────────────────────

export default function StyleGuide() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [tab, setTab] = useState("overview");
  const [sub, setSub] = useState("sectors");
  const [under, setUnder] = useState("a");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const nav = ["color", "type", "space", "components", "patterns"];

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b border-border bg-background/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1100px] items-center justify-between gap-4 px-6 py-3">
          <div className="flex items-baseline gap-3">
            <span className="text-sm font-semibold">ATR Style Guide</span>
            <span className="hidden text-caption text-muted-foreground sm:inline">Live tokens and components. Rules: STYLE_GUIDE.md</span>
          </div>
          <nav className="hidden gap-4 text-xs text-muted-foreground md:flex">
            {nav.map((n) => (
              <a key={n} href={`#${n}`} className="capitalize hover:text-foreground">{n}</a>
            ))}
          </nav>
          <button className={toolbarButton} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "Light" : "Dark"} mode
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-[1100px] space-y-16 px-6 py-10">
        <div className="space-y-3">
          <h1 className="text-display">One system, every screen.</h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            ATR is a dense, data-first tool. Numbers lead, colour means something, there is one way to do each
            thing, and the interface is honest about whether data is live. Everything below is rendered from the
            same tokens and components the app uses.
          </p>
          <div className="flex flex-wrap gap-2 pt-1">
            {["Numbers are the hero", "Colour means something", "One way to do each thing", "Calm by default", "Honest about data"].map((p) => (
              <Badge key={p} tone="info">{p}</Badge>
            ))}
          </div>
        </div>

        {/* ─── colour ─── */}
        <Section id="color" title="Colour" lead="Every colour is a token with a light and a dark value. Use the utility, never a hex or a raw palette colour.">
          <Sub title="Surfaces and text">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {SURFACE_TOKENS.map(([t, u, d]) => <Swatch key={t} token={t} utility={u} use={d} theme={theme} />)}
            </div>
          </Sub>
          <Sub title="Brand and action">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {BRAND_TOKENS.map(([t, u, d]) => <Swatch key={t} token={t} utility={u} use={d} theme={theme} />)}
            </div>
          </Sub>
          <Sub title="Tone: what a colour means (the only colours that carry market meaning)">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {TONE_TOKENS.map(([t, u, d]) => <Swatch key={t} token={t} utility={u} use={d} theme={theme} />)}
            </div>
            <p className="max-w-2xl text-xs text-muted-foreground">
              Colour is never the only signal: pair it with a sign, an arrow or a word. Look tones up through{" "}
              <code className="font-mono">lib/tone.ts</code>.
            </p>
          </Sub>
          <Sub title="Charts (TradingView convention)">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {CHART_TOKENS.map(([t, u, d]) => <Swatch key={t} token={t} utility={u} use={d} theme={theme} />)}
            </div>
          </Sub>
        </Section>

        {/* ─── type ─── */}
        <Section id="type" title="Typography" lead="Inter, three weights (400, 500, 600), tabular figures for every number. Headings are light.">
          <div className={cn(surface, "divide-y divide-border")}>
            {TYPE_SCALE.map((t) => (
              <div key={t.role} className="grid items-baseline gap-2 px-5 py-3 sm:grid-cols-[130px_150px_1fr]">
                <div className="text-xs font-medium">{t.role}</div>
                <div className="font-mono text-caption text-muted-foreground">{t.cls.split(" ")[0]} · {t.spec}</div>
                <div className={cn(t.cls, "min-w-0 truncate")}>{t.sample}</div>
              </div>
            ))}
          </div>
        </Section>

        {/* ─── space, radius, elevation ─── */}
        <Section id="space" title="Spacing, radius and elevation" lead="A 4px base. Related things sit closer than unrelated things. Depth comes from borders, not shadows.">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card padding="md">
              <Sub title="Spacing scale">
                <div className="space-y-2">
                  {SPACING.map(([step, px, use]) => (
                    <div key={step} className="flex items-center gap-3">
                      <div className="w-16 font-mono text-xs">p-{step}</div>
                      <div className="h-3 rounded-sm bg-primary/60" style={{ width: px }} />
                      <div className="text-xs text-muted-foreground">{px} · {use}</div>
                    </div>
                  ))}
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Radius">
                <div className="grid grid-cols-2 gap-3 text-center text-caption">
                  {[
                    ["rounded-xl", "Cards, popovers", "rounded-xl"],
                    ["rounded-lg", "Tiles inside cards", "rounded-lg"],
                    ["rounded-md", "Inputs, small buttons", "rounded-md"],
                    ["rounded-full", "Pills, badges, tabs", "rounded-full"],
                  ].map(([cls, use, r]) => (
                    <div key={cls} className="space-y-1.5">
                      <div className={cn("h-14 border border-border bg-muted", r)} />
                      <div className="font-mono">{cls}</div>
                      <div className="text-muted-foreground">{use}</div>
                    </div>
                  ))}
                </div>
              </Sub>
            </Card>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className={cn(surface, "p-5 text-xs")}><div className="font-mono font-medium">surface</div><div className="mt-1 text-muted-foreground">A card. rounded-xl, 1px border, no shadow.</div></div>
            <div className={cn(surfaceInset, "bg-card p-4 text-xs")}><div className="font-mono font-medium">surfaceInset</div><div className="mt-1 text-muted-foreground">A tile inside a card.</div></div>
            <div className={cn(surfaceMuted, "p-4 text-xs")}><div className="font-mono font-medium">surfaceMuted</div><div className="mt-1 text-muted-foreground">A quiet filled area, no border.</div></div>
          </div>
        </Section>

        {/* ─── components ─── */}
        <Section id="components" title="Components" lead="Import these; do not rebuild them.">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card padding="md">
              <Sub title="Card + CardHeader">
                <Card>
                  <CardHeader title="Sector strength" sub="Return over the last month" action={<button className={toolbarButton}>Refresh</button>} />
                  <div className="p-5 pt-3 text-body text-muted-foreground">Card padding is p-5. The header names the card in one line.</div>
                </Card>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Stat (tone colours the value)">
                <div className="grid grid-cols-2 gap-3">
                  <Stat label="Nifty 50" value={fmtMoney(22421.95, 2)} sub={fmtPct(-0.88)} tone="bad" />
                  <Stat label="Win rate" value="58.2%" sub="Over 214 trades" tone="good" />
                  <Stat label="Evidence" value="Thin" sub="Below the floor" tone="warn" />
                  <Stat label="Open positions" value="6" sub="of 10 allowed" />
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Badge and VerdictPill">
                <div className="flex flex-wrap items-center gap-2">
                  {TONES.map((t) => <Badge key={t} tone={t}>{t}</Badge>)}
                </div>
                <div className="flex gap-2 pt-1">
                  <VerdictPill passed>Passed</VerdictPill>
                  <VerdictPill passed={false}>Failed</VerdictPill>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Callout, ErrorBox, Hint">
                <div className="space-y-2">
                  <Callout tone="good" title="Saved">The screen was saved and is being watched.</Callout>
                  <Callout tone="info">Data is as of the last close.</Callout>
                  <Callout tone="warn" title="Thin evidence">Fewer than 30 forward observations.</Callout>
                  <Callout tone="bad">Could not reach the broker.</Callout>
                  <ErrorBox>Could not load signals.</ErrorBox>
                  <Hint>Helper text sits under a control.</Hint>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Buttons">
                <div className="flex flex-wrap items-center gap-3">
                  <Button>Run scan</Button>
                  <Button variant="secondary">Save screen</Button>
                  <Button variant="outline">Reject</Button>
                  <Button variant="ghost">Cancel</Button>
                </div>
                <div className="flex items-center gap-3 pt-1">
                  <button className={toolbarButton}>Toolbar action</button>
                  <span className="text-caption text-muted-foreground">compact, lives in a card toolbar</span>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Form fields">
                <div className="flex flex-wrap items-end gap-4">
                  <div className="flex flex-col gap-1">
                    <label className={fieldLabel}>Strategy</label>
                    <input className={cn(fieldInput, "w-[200px]")} placeholder="e.g. MOMENTUM_BREAKOUT" />
                  </div>
                  <button className={toolbarButton}>Analyze</button>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Tabs: three variants, three jobs">
                <div className="space-y-4">
                  <div>
                    <div className="mb-2 text-caption text-muted-foreground">pill: a whole page's sections</div>
                    <Tabs value={tab} onValueChange={setTab} variant="pill">
                      <TabsList>
                        {["overview", "scan", "screener", "charts"].map((t) => <TabsTrigger key={t} value={t}><span className="capitalize">{t}</span></TabsTrigger>)}
                      </TabsList>
                    </Tabs>
                  </div>
                  <div>
                    <div className="mb-2 text-caption text-muted-foreground">segment: a view within a section</div>
                    <Tabs value={sub} onValueChange={setSub} variant="segment">
                      <TabsList>
                        {["overview", "sectors", "stocks"].map((t) => <TabsTrigger key={t} value={t}><span className="capitalize">{t}</span></TabsTrigger>)}
                      </TabsList>
                    </Tabs>
                  </div>
                  <div>
                    <div className="mb-2 text-caption text-muted-foreground">underline: inside a panel or dialog</div>
                    <Tabs value={under} onValueChange={setUnder} variant="underline">
                      <TabsList>
                        {[["a", "Detail"], ["b", "History"], ["c", "Notes"]].map(([v, l]) => <TabsTrigger key={v} value={v}>{l}</TabsTrigger>)}
                      </TabsList>
                    </Tabs>
                  </div>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="EmptyState">
                <EmptyState>No signals yet. They appear when a paper bar fires.</EmptyState>
              </Sub>
            </Card>
          </div>

          <Card padding="md">
            <Sub title="Table (numbers right-aligned, tabular, tone only where direction matters)">
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-body">
                  <thead>
                    <tr className="border-b border-border">
                      {["Symbol", "Price", "Change", "Rel volume", "Evidence"].map((h, i) => (
                        <th key={h} className={cn("px-3 py-2 text-micro font-semibold uppercase tracking-wider text-muted-foreground", i === 0 || i === 4 ? "text-left" : "text-right")}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["TCS", 3920.5, 1.24, 1.8, "good"],
                      ["INFY", 1488.1, -0.62, 0.9, "warn"],
                      ["RELIANCE", 2915.0, 0, 1.1, "flat"],
                      ["HDFCBANK", 1642.75, -1.9, 2.6, "good"],
                    ].map(([sym, px, chg, rv, ev], i) => (
                      <tr key={sym as string} className={cn("border-b border-border/50", i % 2 === 1 && "bg-muted/20")}>
                        <td className="px-3 py-1.5 font-semibold">{sym}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtMoney(px, 2)}</td>
                        <td className={cn("px-3 py-1.5 text-right tabular-nums", toneText[toneOf(chg as number)])}>{fmtPct(chg)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">{fmtNum(rv, 1)}x</td>
                        <td className="px-3 py-1.5"><Badge tone={ev as Tone}>{ev === "good" ? "Forward" : ev === "warn" ? "Thin" : "In-sample"}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Sub>
          </Card>
        </Section>

        {/* ─── patterns ─── */}
        <Section id="patterns" title="Patterns" lead="How the pieces combine.">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card padding="md">
              <Sub title="Showing market numbers">
                <div className="divide-y divide-border text-body">
                  {[
                    ["Price", fmtMoney(22421.95, 2)],
                    ["Signed change", `${fmtPct(0.88)}   ${fmtPct(-0.88)}`],
                    ["Large amount", "₹1.2 Cr"],
                    ["Ratio", "1.80x"],
                    ["Missing or not measurable", fmtNum(null)],
                  ].map(([k, v]) => (
                    <div key={k} className="flex items-center justify-between py-2">
                      <span className="text-muted-foreground">{k}</span>
                      <span className="font-medium tabular-nums">{v}</span>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">Missing is an em dash, never zero. Zero is a real value.</p>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Live, closed and as-of">
                <div className="space-y-3">
                  <div className="flex items-center gap-3">
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-gain/10 px-2 py-0.5 text-micro font-medium text-gain">
                      <span className="size-1.5 animate-pulse rounded-full bg-gain" />Live
                    </span>
                    <span className="text-xs text-muted-foreground">market open, updating every few seconds</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2 py-0.5 text-micro font-medium text-muted-foreground">
                      <span className="size-1.5 rounded-full bg-muted-foreground/60" />Market closed
                    </span>
                    <span className="text-xs text-muted-foreground">last session's values</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge tone="flat">As of 1 Oct</Badge>
                    <span className="text-xs text-muted-foreground">end-of-day data is dated, never implied current</span>
                  </div>
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Tone dots and bars">
                <div className="space-y-2">
                  {TONES.map((t) => (
                    <div key={t} className="flex items-center gap-3">
                      <span className={cn("size-2 rounded-full", toneFill[t])} />
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                        <div className={cn("h-full rounded-full", toneFill[t])} style={{ width: `${{ good: 78, bad: 34, warn: 52, info: 64, flat: 20 }[t]}%` }} />
                      </div>
                      <span className="w-12 text-right font-mono text-caption text-muted-foreground">{t}</span>
                    </div>
                  ))}
                </div>
              </Sub>
            </Card>
            <Card padding="md">
              <Sub title="Page anatomy">
                <pre className="overflow-x-auto rounded-lg bg-muted/50 p-4 font-mono text-caption leading-relaxed text-muted-foreground">{`Page title (text-heading)           [status chips]
Primary tabs   (Tabs pill)
Secondary tabs (Tabs segment)    only if the section has views
------------------------------------------------------
gap-4 stack
  Card p-5
  Card p-5`}</pre>
              </Sub>
            </Card>
          </div>
        </Section>

        <footer className="border-t border-border pt-6 text-caption text-muted-foreground">
          Rules and rationale: <code className="font-mono">web/STYLE_GUIDE.md</code>. Enforced by{" "}
          <code className="font-mono">bun run check:design</code>.
        </footer>
      </main>
    </div>
  );
}
