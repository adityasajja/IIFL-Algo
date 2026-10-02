/**
 * Form and toolbar styles shared by every dense panel, so a label, a text box and an
 * action button look the same wherever they appear.
 *
 *   fieldLabel    the small uppercase caption above a control
 *   fieldInput    a single-line text box
 *
 * Buttons live in components/ui/button.tsx: <Button size="xs"> is the compact action for a
 * card's toolbar; <Chip> (components/ui/chip.tsx) is a toggle or filter.
 */
export const fieldLabel = "text-micro font-semibold uppercase tracking-wider text-muted-foreground";

export const fieldInput =
  "rounded-md border border-border bg-muted/50 px-2.5 py-1.5 text-xs text-foreground " +
  "placeholder:text-muted-foreground focus:border-primary focus:outline-none";

