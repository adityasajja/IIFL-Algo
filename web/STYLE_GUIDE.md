# ATR Style Guide

How the app looks, and the rules that keep it looking like one app.

This is the working guide for the dashboard. `DESIGN.md` is the *inspiration* (a Stripe-derived
language: indigo brand, hairline borders, light headings). This file is the *system*: the exact
tokens, components and patterns we build with. When they disagree, this file wins.

There is a live version at **`/styleguide.html`** (run `bun run dev`, open
`http://localhost:5173/styleguide.html`). It renders every token and component in light and dark,
from the same source as the app, so it cannot drift from this document.

---

## 1. Principles

ATR is a dense, data-first tool used by one person making decisions with money. Five principles
follow from that.

1. **Numbers are the hero.** Chrome recedes; figures lead. A figure is large, tabular and
   unambiguous, and always shows its unit.
2. **Colour means something.** Green is gain, red is loss, amber is caution, indigo is the brand and
   action. We never use a colour just because it looks nice, so the user can trust a glance.
3. **One way to do each thing.** One card, one badge, one tab style, one way to show a signed
   percentage. If two screens do the same job differently, one of them is a bug.
4. **Calm by default.** No decoration that does not carry information. Motion is short and
   explains a state change; it never performs.
5. **Honest about data.** Say when a number is live, delayed, or last session's. Show missing data
   as missing (`-`), never as zero.

**The rule of three.** Reach for these in order: a **token** (a colour, size or spacing step), then a
**component** (`Card`, `Stat`, `Badge`...), then a **pattern** (this guide). If you are typing a hex
code, a pixel size or a colour name, stop: there is a token for it. `bun run check:design` enforces
this (section 9).

---

## 2. Foundations

### 2.1 Colour

All colour comes from CSS variables in `src/index.css`, with a light and a dark value each. Tailwind
exposes them as utilities (`bg-card`, `text-gain`). Never use a raw palette colour
(`text-emerald-500`) or a hex.

**Surfaces and text**

| Token | Use |
|---|---|
| `background` | The page. |
| `card` | Panels sitting on the page (see 3.1). |
| `muted` | Quiet fills: table stripes, inset areas, tab tracks. Use at low opacity (`bg-muted/50`). |
| `border` | Every hairline. 1px, never heavier. |
| `foreground` | Primary text and numbers. |
| `muted-foreground` | Labels, captions, secondary text, units. |

**Brand and action**

| Token | Use |
|---|---|
| `primary` | Primary actions, active states, links, focus. |
| `primary-deep` / `primary-press` | Hover and pressed states of `primary`. |
| `primary-soft` | Brand on dark backgrounds, subtle emphasis. |
| `primary-subdued` | Borders of secondary and outline controls. |

**Tone: what a colour means.** These four are the only colours that carry market meaning. In UI props
they are written as tones: `good`, `bad`, `warn`, `info`, `flat`.

| Tone | Token | Means | Never use it for |
|---|---|---|---|
| `good` | `gain` | Profit, rising, healthy, pass. | Decoration, "success" of an unrelated action. |
| `bad` | `loss` | Loss, falling, unhealthy, fail, error. | Emphasis. |
| `warn` | `warning` | Caution, thin evidence, needs attention. | Errors. |
| `info` | `info` | Neutral highlight, system notes. | Gain/loss. |
| `flat` | `muted-foreground` | No change, unknown, inactive. | |

Look them up through `src/lib/tone.ts` (`toneText`, `toneChip`, `toneFill`, `toneOf(value)`). Do not
re-decide in a component which green "good" is.

**Colour is never the only signal.** Pair it with a sign (`+1.2%`), an arrow, or a word, because
roughly 1 in 12 men cannot tell red from green. Light-mode tone values are the darker steps so text
meets 4.5:1 contrast on white.

**Charts** have their own palette (`--chart-*`, TradingView's convention, so traders read it without
a key). Legends use `bg-chart-yellow`; canvas code uses `chartColor("yellow")` from
`src/lib/chart-theme.ts`. Candles are `chart-up` / `chart-down`.

### 2.2 Typography

Font: **Inter**. Numbers use tabular figures (`tabular-nums`) everywhere they align or update.

| Role | Class | Size / weight | Use |
|---|---|---|---|
| Display | `text-display` | 32 / 300 | Landing hero only. |
| Page title | `text-heading` | 22 / 300 | One per page, in the page header. |
| Metric | `text-2xl` or `text-xl` + `font-semibold` | 24 / 20, 600 | The big number in a stat or card. |
| Card title | `text-sm font-semibold` | 14 / 600 | Names a card (`CardHeader`). |
| Body | `text-sm` | 14 / 400 | Reading text, form fields. |
| Body, dense | `text-body` | 13 / 400 | Default inside tables and dense panels. |
| Label | `text-xs text-muted-foreground` | 12 / 400-500 | Section labels, card captions. |
| Caption | `text-caption` | 11 / 400-600 | Table meta, chips, helper text. |
| Micro | `text-micro` | 10 / 600 | Axis ticks, uppercase field labels. Use sparingly. |

Rules:

- **Three weights only:** 400 body, 500 labels and controls, 600 headings and key figures. Never
  `font-bold`.
- **Never `text-[13px]`.** If a size is not on the scale, the scale is wrong or you want the nearest
  step. Ask, do not improvise.
- **Uppercase** is only for micro field labels and column headers, with `tracking-wider`.
- Sentence case everywhere else.

### 2.3 Spacing

A **4px** base. Use the Tailwind steps; the ones we actually use:

| Step | px | Use |
|---|---|---|
| `1` | 4 | Icon-to-text in a chip. |
| `1.5` / `2` | 6 / 8 | Between a label and its value; dense list gaps. |
| `3` | 12 | Inside a card between related items. |
| `4` | 16 | **Between cards** and between blocks on a page. The default rhythm. |
| `5` | 20 | **Card padding.** |
| `6` | 24 | Between page sections; large internal gaps. |
| `8` | 32 | Page top and bottom breathing room. |

Rules:

- **Card padding is `p-5`.** A compact tile (a stat, a small box) is `p-4`. A list or table row is
  `px-3 py-2`.
- **Vertical rhythm is `gap-4`** (16px) between sibling cards. Use `space-y-4` or `flex flex-col gap-4`.
- Related things sit closer than unrelated things: 8px label-to-value, 12px item-to-item, 16px
  card-to-card, 24px section-to-section.
- Never mix margin and gap in one stack. The parent owns the spacing (`gap`/`space-y`), children
  carry none.

### 2.4 Radius

| Where | Class |
|---|---|
| Cards, popovers, modals | `rounded-xl` (12px) |
| A tile or box inside a card | `rounded-lg` (8px) |
| Inputs, small buttons, code chips | `rounded-md` (6px) |
| Pills, badges, tabs, round buttons | `rounded-full` |

Bare `rounded` (4px) is not used. Nested radii step down one level (card 12, tile 8).

### 2.5 Elevation

Depth comes from **borders, not shadows**. Cards are flat with a 1px `border`. Shadows exist only for
things that float above the page: menus, tooltips, modals (`--shadow-level-2`). Do not put a shadow on
a card.

### 2.6 Iconography

Icons are **lucide-react**. 16px in controls and headings, 14px inside dense rows and chips. Icons
inherit text colour (`currentColor`). An icon-only button needs an `aria-label`.

### 2.7 Motion

Motion explains a state change; it does not decorate.

- Hover and press feedback: **150ms**, colour or opacity only.
- Layout and tab transitions: the shared spring in `components/motion/tabs.tsx` (no overshoot).
- Respect `prefers-reduced-motion` (the motion components already do; keep it that way).
- Live data pulses once per update at most. Nothing loops except a loading spinner and the "Live"
  dot.

---

## 3. Components

Import these. Do not rebuild them. All live in `src/components/ui/` unless noted.

### 3.1 Surface and Card (`card.tsx`, `surface.ts`)

Every container is one of three surfaces:

| Surface | Class | Use |
|---|---|---|
| `surface` | `rounded-xl border border-border bg-card` | A card. |
| `surfaceInset` | `rounded-lg border border-border` | A tile sitting inside a card. |
| `surfaceMuted` | `rounded-lg bg-muted/50` | A quiet filled area, no border. |

Use `<Card padding="md">` for a card (p-5) or `padding="sm"` for a compact one (p-4). Never write the
card classes by hand.

```tsx
<Card padding="md">
  <CardHeader title="Sector strength" sub="Return over the last month" />
  ...
</Card>
```

### 3.2 Section labels

A card or section is named in one of two ways:

- `CardHeader` (title + optional one-line sub + optional action): a titled card.
- `<h3 className="text-xs font-medium text-muted-foreground">`: a quiet label inside a card.

Do not invent a third (uppercase tracking labels are for **field** labels, 3.7).

### 3.3 Stat (`stat.tsx`)

One labelled number. `tone` colours the value; `sub` carries the comparison. Use it for any
headline figure so they all align and read alike.

### 3.4 Badge, VerdictPill, Callout (`stat.tsx`)

- **Badge**: a small label or status. `tone` sets the colour.
- **VerdictPill**: a pass/fail result.
- **Callout**: a message block. `tone` is `good`, `info`, `warn` or `bad`. Use it for banners and
  inline notices. Do not hand-style a bordered coloured box.

### 3.5 ErrorBox, Hint, EmptyState (`card.tsx`)

- `ErrorBox`: an inline error inside a form or card.
- `Hint`: helper text under a control.
- `EmptyState`: nothing here yet. Say what is missing and what to do next ("No signals yet. They
  appear when a paper bar fires.").

### 3.6 Button (`button.tsx`) and Chip (`chip.tsx`)

`<Button>` is the one button. Pick a **variant** for how loud it is and a **size** for where it sits.

| Variant | Use |
|---|---|
| `primary` | The call to action. One per view. |
| `secondary` / `outline` | Alternatives next to it; destructive actions use `outline` plus a confirmation, never red fill. |
| `quiet` | Neutral and low-key: Close, Cancel, Refresh, filters. |
| `ghost` | Text action in the brand colour. |
| `plain` | Borderless and muted: icon-only controls (remove, move, collapse). |
| `link` | Reads as a link, acts as a button (Retry, Show all, a symbol that opens a chart). |

| Size | Height | Use |
|---|---|---|
| `md` / `sm` / `lg` | 40 / 40 / 44 | A page's or a dialog's main actions. |
| `xs` | 32 | **Toolbars and dense panels**: a card's Refresh / Run / Close, table-adjacent actions. |
| `icon-sm` | 32 square | Icon-only. Needs an `aria-label`; the icon is sized for you, so pass no size. |
| `inline` | text height | `link` variant inside a sentence or a table cell. |

`<Chip selected>` is a toggle: a filter, a sort key, one option of a small set in a toolbar. It sets
`aria-pressed`. If the choice switches a whole view, use `Tabs` (3.8), not chips.

Do not size or colour a `Button` with a `className` (`h-7 text-xs bg-...`): that is what `size` and
`variant` are for. A className may add layout (`ml-auto`, `w-full`) or a tone hover (`hover:text-loss`).
A raw `<button>` is only for a row or a tile that is clickable as a whole (a list row, a disclosure
header, a heatmap cell), and then it uses tokens.

### 3.7 Form fields (`form-styles.ts`, `select.tsx`, `motion/input.tsx`, `motion/switch.tsx`)

- Label above the control: `fieldLabel` (uppercase, micro, muted).
- Text box: `fieldInput`. Dropdown: `Select`. Toggle: `Switch`.
- A filter row is `flex flex-wrap items-end gap-4`, each field a `flex flex-col gap-1` of label +
  control.

### 3.8 Tabs (`motion/tabs.tsx`)

One component, three variants, each with one job:

| Variant | Job | Example |
|---|---|---|
| `pill` | Switching a **whole page's** sections. The top level. | Markets: Intelligence / Scan / Charts. |
| `segment` | Switching a **view within a section**, or a small option set. Second level. | Overview / Sectors / Stocks; Today / Week / Month. |
| `underline` | Switching inside a **panel or dialog**. | Detail tabs in a drawer. |

The track (border and fill) belongs to the variant, so do not restyle `TabsList`. Never write tabs from buttons, including a two-option AND/OR switch or a timeframe row. A page has at most two tab levels.

### 3.9 Tables

Hand-built tables use the same classes so they look identical:

- Header cell: `px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground`
- Body cell: `px-3 py-1.5 text-body` (keep it even when the table sets its own text size; an empty or
  expanded-detail row may use more padding)
- No other cell padding (`p-3`, `px-2`, `pr-3`): the columns of two tables should line up.
- **Right-align numbers**; left-align text. All numbers `tabular-nums`.
- Row: `border-b border-border/50`; stripe alternate rows with `bg-muted/20`; a muted/suppressed row is
  `opacity-60`.
- Tone-colour a number only when direction or health is the point (a return, a win rate), not for
  every figure.

For large or interactive tables use `components/motion/table`.

### 3.10 Loading, error, empty

Every data panel has all three states, in the same form everywhere:

- Loading: `PageLoader` (full panel) or a skeleton row. Never a blank.
- Error: `ErrorBox` or `Callout tone="bad"`, with a Retry. Say what failed in plain words.
- Empty: `EmptyState`.

### 3.11 Overlays

Tooltips use `Tooltip`; menus and popovers use the motion `select-morph`/`tooltip` components.
Floating layers are the only things with a shadow.

---

## 4. Patterns

### 4.1 Page anatomy

```
Page title (text-heading)                              [status chips]
Primary tabs (Tabs pill)
Secondary tabs (Tabs segment)               <- only if the section has views
-------------------------------------------------------------------
gap-4 stack of cards
  Card p-5
  Card p-5
```

One title, at most two tab rows, then a `gap-4` stack. Content is never wider than the page
container; do not add your own max-width.

### 4.1a Navigation: one story

The product is **paper trading and information only**: it never places a real order. It tells one
story, **prove a strategy on paper**, and the sidebar is that story:

```
PROVE IT ON PAPER   Home -> Strategies -> Test -> Paper -> Performance
MARKET              Markets, Signals, Watchlist
EXPERIMENTAL        Labs
```

- The map (names, groups, sub-pages, old links) lives in `src/lib/nav.ts` and is tested. Add a page
  there first; the sidebar, command palette and routing follow from it.
- Every page has a **name** (sidebar, one word where possible), a **title** and **one plain sentence**
  under it saying what it is for. The path pages start with "Step N".
- Sidebar names are what the user would say, not what the code calls it: Test, not Evidence or
  Validate; Performance, not Learning.
- **No live-trading surface.** No order buttons, no live/real toggle, no broker account page, no
  real-portfolio figures on Home. Old links to them land on Paper. If the server reports a live mode
  the header says so in red rather than showing a calm "paper" badge.
- A tool that is not part of the tested path goes in **Labs**, and Labs says so. Do not add a top-level page
  for an experiment.
- Home always shows where the user is on the path and the one next step (`lib/journey.ts`).
- Renaming or moving a page means adding the old id to `LEGACY` in `nav.ts`, so bookmarks keep working.

### 4.1b Show, then say

The first answer to "is this working?" is a picture, not a paragraph.

- **Lead with the evidence.** Home opens on the track record: one hero figure, the curve against
  the Nifty 50, the depth of every fall, then four small facts. Words are labels.
- **A picture before a number.** A meter, a ring, a rail or a dot says it before the digits do. A
  sentence behind a control is a hover (`Tooltip`), not text on the page.
- **Every figure carries its provenance.** Simulated or real, where the prices came from, and how
  many trading sessions old they are, as a chip or a freshness dot. Never green unless current.
- **A missing thing is shown as missing.** No data is a ghost of the chart and one action, never a
  flat line; an unpriced day is flagged, never smoothed.
- **Charts follow the dataviz rules:** one axis, thin marks, the accent for the thing that matters
  and gray for its yardstick, direct end labels, a crosshair readout, a table view, keyboard access.
- The limits (practice money, not advice, no real orders) are icons with a hover, always on
  Home. Legal wording is for a lawyer; the product's plain version lives in `DataTrust`.

### 4.2 Showing market numbers

| Thing | Format | Example |
|---|---|---|
| Price | `₹` + Indian grouping, 2 decimals | `₹22,421.95` |
| Signed change | sign always shown, 2 decimals, `%` | `+0.88%` / `-0.88%` |
| Large amounts | lakh / crore, not millions | `₹1.2 Cr` |
| Ratios | 2 decimals, `x` suffix | `1.80x` |
| Missing / not measurable | an em dash, never `0` | `-` |
| Dates | day + short month; add year only if not this year | `2 Oct` |

Use `fmtMoney`, `fmtPct` and `fmtNum` from `lib/format.ts` so formats cannot drift. Colour a change by tone
(`toneOf(value)`), never by hand.

### 4.3 Live and delayed data

If a number can move, say whether it is moving. Use a `Live` tag (green, pulsing dot) while the market
is open and `Market closed` (muted) otherwise. Anything computed from end-of-day data is labelled
"as of <date>". A user must never have to guess whether a figure is current.

### 4.4 Filters and toolbars

A row of `fieldLabel` + control pairs, wrapping, aligned to the bottom (`items-end`), with the action
(`<Button size="xs">`) last. Changing a filter re-runs the view; if it is expensive, the action is explicit.

### 4.5 Lists with status

A list row is `px-3 py-2` with the name left, the figure right, and an optional `Badge`. A dense list
inside a card uses `divide-y divide-border`.

### 4.6 Writing the interface

- **Plain words first.** "Stocks above their 50-day average", not "EMA50 breadth".
- **Say the unit.** `%`, `₹`, `x`, `days`. A bare number is a bug.
- **Say what to do** in empty and error states.
- Buttons are verbs: "Run scan", "Save screen". Not "Submit".
- No exclamation marks. Emoji only inside user-written content, not in controls or labels.

---

## 5. Accessibility

- Text contrast **4.5:1** (3:1 for large text and UI borders). The tone tokens are chosen to pass.
- **Focus is always visible** (`ring` token). Never `outline-none` without a replacement.
- Interactive targets are at least **32px** in the long dimension; dense table actions get a larger
  hit area than their visible size.
- Colour is never the only carrier of meaning (2.1).
- Everything reachable by keyboard; tabs and menus support arrow keys (the shared components do).
- Icon-only controls have an `aria-label`. Charts and gauges get a text equivalent (`aria-label`).

---

## 6. Theming

Light and dark are both first-class. Every colour is a token with a value for each, so a component
written with tokens works in both with no `dark:` variants. If you find yourself writing `dark:`, you
probably used a raw colour. The dashboard is dark by default; the marketing/landing surfaces are
light.

---

## 7. Adding to the system

**Adding a token.** Add the variable in both `:root` and `[data-theme="dark"]` in `src/index.css`,
expose it in `@theme inline`, add it to the table above, and add a swatch to the live style guide. Do
not add a token for a single use.

**Adding a component.** First check 3.x: does an existing one do 80% of it? Extend it. A new
component needs: a name that says its job, props for `tone`/`size` rather than colours, all states
(loading, empty, error, disabled), light and dark, and a spot in the live style guide.

**Changing a rule.** Change it here first, then the code, in the same change.

---

## 8. Migrating old code

| Old | New |
|---|---|
| `text-emerald-400/500/600` | `text-gain` |
| `text-rose-*` / `text-red-*` | `text-loss` |
| `text-amber-*` | `text-warning` |
| `bg-emerald-500/10` | `bg-gain/10` |
| `text-[10px]` / `[11px]` / `[13px]` | `text-micro` / `text-caption` / `text-body` |
| `text-[22px] font-light ...` | `text-heading` |
| `font-bold` | `font-semibold` |
| `rounded` (bare) | `rounded-md` |
| `rounded border border-border bg-card` | `<Card>` or `surface` |
| `bg-white dark:bg-card` | `bg-card` |
| `border-[#e3e8ee] dark:border-white/10` | `border-border` |
| `style={{ display: "flex", gap: 8 }}` | `className="flex gap-2"` |
| `style={{ color: "#10b981" }}` | `className="text-gain"` |
| `trend(v)` returning a hex | `toneText[toneOf(v)]` |

`style={{}}` is still right for **data-driven geometry**: a bar's width in %, a tooltip's left offset,
a gauge angle.

---

## 9. Enforcement

`bun run check:design` (also run by `bun run build`) fails when a file adds any of: a hex colour, a raw
palette colour (`text-emerald-500`), a pixel text size (`text-[13px]`), a static inline `style`,
`font-bold`, a raw `<button>` (outside the shared components), or non-standard table-cell padding.

It is a **ratchet**: `scripts/design-baseline.json` records the handful of known exceptions (canvas
drawing code and the like). A file may not exceed its baseline, so the system only gets stricter. If a
violation is genuinely required, run `node scripts/check-design.mjs --update` and explain why in the
commit message.

Reviewing a UI change? Ask: does it use tokens, an existing component, and the spacing scale? Does it
look right in light and dark? Does it handle loading, empty and error? Is anything colour-only?
