/**
 * Form and toolbar styles shared by every dense panel, so a label, a text box and an
 * action button look the same wherever they appear.
 *
 *   fieldLabel    the small uppercase caption above a control
 *   fieldInput    a single-line text box
 *   toolbarButton the compact action button that sits in a card's toolbar
 *
 * For a page's main call to action use <Button> (components/ui/button.tsx).
 */
export const fieldLabel = "text-micro font-semibold uppercase tracking-wider text-muted-foreground";

export const fieldInput =
  "rounded-md border border-border bg-muted/50 px-2.5 py-1.5 text-xs text-foreground " +
  "placeholder:text-muted-foreground focus:border-primary focus:outline-none";

export const toolbarButton =
  "cursor-pointer rounded-lg border border-primary/40 bg-primary/15 px-3.5 py-1.5 text-xs font-semibold " +
  "text-primary-soft transition-colors hover:bg-primary/25 disabled:cursor-wait disabled:opacity-60";
