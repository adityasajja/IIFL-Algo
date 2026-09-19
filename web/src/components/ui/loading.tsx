import { Loader } from "../motion/loader";

/**
 * App-wide loading convention, built on the beui `Loader`.
 *
 * - Section/page loading → `PageLoader` (centered `spinner`, 28px).
 * - Button inline loading → `ButtonLoader` (14px, inherits the button's
 *   `currentColor` so it stays visible on every variant).
 *
 * Always `variant="spinner"` unless a panel has a documented reason to differ —
 * seventeen competing animations is how a loading state starts feeling like a
 * slot machine. `Loader` already handles `role="status"`, an `sr-only` label,
 * and reduced-motion (calm opacity pulse, no transforms).
 */
export function PageLoader({ label = "Loading", message }: { label?: string; message?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-8">
      <Loader size={28} label={label} />
      {message ? <div className="text-sm text-muted-foreground">{message}</div> : null}
    </div>
  );
}

/** Inline button spinner — replaces `<Loader2 className="h-4 w-4 animate-spin" />`. */
export function ButtonLoader({ size = 14, label = "Loading" }: { size?: number; label?: string }) {
  return <Loader size={size} label={label} />;
}
