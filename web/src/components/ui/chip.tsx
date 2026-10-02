import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

/**
 * A toggle chip: one option out of a small set, or a filter that is on or off.
 * `selected` is announced to assistive tech (aria-pressed). Use `Tabs` instead when the choice
 * switches a whole view; use a chip for filters, sort keys and option sets that sit in a toolbar.
 */
export function Chip({
  selected = false,
  className,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { selected?: boolean }) {
  return (
    <button
      type={type}
      aria-pressed={selected}
      className={cn(
        "inline-flex min-h-8 cursor-pointer items-center justify-center gap-1.5 rounded-full border px-3 text-xs font-medium",
        "transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected
          ? "border-primary/40 bg-primary/10 text-foreground"
          : "border-border text-muted-foreground hover:border-foreground/30 hover:text-foreground",
        className,
      )}
      {...rest}
    />
  );
}
