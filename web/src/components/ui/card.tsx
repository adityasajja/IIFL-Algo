import type { ReactNode } from "react";
import { cn } from "../../lib/utils";
import { surface } from "./surface";

const PADDING = { none: "", sm: "p-4", md: "p-5" } as const;

/** The one card. `padding` is "none" by default so existing callers keep their own. */
export function Card({
  className,
  children,
  padding = "none",
}: {
  className?: string;
  children: ReactNode;
  padding?: keyof typeof PADDING;
}) {
  return <div className={cn(surface, PADDING[padding], className)}>{children}</div>;
}

/** A card's title row: what it shows, an optional one-line description, an optional action. */
export function CardHeader({
  title,
  sub,
  action,
}: {
  title: ReactNode;
  sub?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 px-5 pt-4">
      <div className="min-w-0">
        <h3 className="text-sm font-semibold leading-tight">{title}</h3>
        {sub ? <p className="mt-1 text-xs text-muted-foreground">{sub}</p> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-loss/40 bg-loss/[0.07] px-3 py-2 text-body text-loss">
      {children}
    </div>
  );
}

export function Hint({ className, children }: { className?: string; children: ReactNode }) {
  return <p className={cn("text-body text-muted-foreground", className)}>{children}</p>;
}

/** The placeholder for a list or panel with nothing in it yet. Say what is missing and what to do. */
export function EmptyState({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "rounded-xl border border-dashed border-border bg-card px-6 py-12 text-center text-sm text-muted-foreground",
        className,
      )}
    >
      {children}
    </div>
  );
}
