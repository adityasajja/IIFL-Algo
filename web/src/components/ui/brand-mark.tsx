import { useId } from "react";
import { cn } from "../../lib/utils";

/** The Forward mark: a choppy past line that turns into a clean arrow. */
export function BrandMark({ className }: { className?: string }) {
  const id = useId();
  return (
    <svg viewBox="0 0 64 64" role="img" aria-label="Forward" className={cn("shrink-0", className)}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#7c6cff" />
          <stop offset="1" stopColor="#4f46e5" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill={`url(#${id})`} />
      <path d="M12 42 L21 34 L27 39 L35 28" fill="none" stroke="#fff" strokeOpacity=".55" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M35 28 L50 28 M43 20 L51 28 L43 36" fill="none" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
