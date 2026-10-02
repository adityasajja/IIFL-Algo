import {
  AnimatePresence,
  type HTMLMotionProps,
  motion,
  useReducedMotion,
} from "motion/react";
import {
  forwardRef,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useRef,
  useState,
} from "react";
import { EASE_OUT, SPRING_PRESS } from "../../lib/ease";
import { useHoverCapable } from "../../lib/hooks/use-hover-capable";
import { cn } from "../../lib/utils";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "outline" | "quiet" | "plain" | "link";
/**
 * `xs` is the compact 32px size for toolbars and dense panels; `icon-sm` is its square twin;
 * `inline` has no box at all, for a text link that has to behave like a button.
 */
export type ButtonSize = "xs" | "sm" | "md" | "lg" | "icon" | "icon-sm" | "inline";

export interface ButtonProps extends Omit<HTMLMotionProps<"button">, "children"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  pressScale?: number;
  /** Spawn a Material-style ripple from the press point. Off by default. */
  ripple?: boolean;
  children?: ReactNode;
}

type Ripple = { id: number; x: number; y: number; size: number };

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "bg-primary text-primary-foreground hover:bg-primary-deep active:bg-primary-press shadow-none",
  secondary: "border border-primary bg-card text-primary hover:bg-primary/[0.06] active:bg-primary/10",
  ghost: "bg-transparent text-primary hover:bg-primary/[0.06]",
  outline: "border border-primary-subdued bg-transparent text-primary hover:border-primary hover:bg-primary/[0.06]",
  /** Neutral and low-key: Close, Cancel, icon-only controls. */
  quiet: "border border-border bg-transparent text-muted-foreground hover:border-primary-subdued hover:text-foreground",
  /** Borderless and muted: icon-only controls (remove, move, collapse). */
  plain: "bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground",
  /** Reads as a link, acts as a button. */
  link: "bg-transparent text-primary hover:underline",
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "h-10 min-h-[40px] px-4 text-sm gap-1.5 rounded-full",
  md: "h-10 min-h-[40px] px-4 text-base gap-2 rounded-full",
  lg: "h-11 min-h-[44px] px-4 text-base gap-2 rounded-full",
  icon: "h-10 w-10 min-h-[40px] rounded-full",
  xs: "h-8 min-h-[32px] px-3 text-xs gap-1.5 rounded-full",
  "icon-sm": "size-8 min-h-[32px] rounded-full [&>svg]:size-3.5",
  inline: "gap-1 rounded-md",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "primary",
      size = "md",
      pressScale = 0.93,
      ripple = false,
      className,
      children,
      onPointerDown,
      ...rest
    },
    ref,
  ) {
    const reduce = useReducedMotion();
    const canHover = useHoverCapable();
    const [ripples, setRipples] = useState<Ripple[]>([]);
    const nextId = useRef(0);

    const handlePointerDown = useCallback(
      (event: PointerEvent<HTMLButtonElement>) => {
        if (ripple && !reduce) {
          const rect = event.currentTarget.getBoundingClientRect();
          const size = Math.max(rect.width, rect.height) * 2;
          const id = nextId.current++;
          setRipples((prev) => [
            ...prev,
            {
              id,
              x: event.clientX - rect.left,
              y: event.clientY - rect.top,
              size,
            },
          ]);
        }
        onPointerDown?.(event);
      },
      [ripple, reduce, onPointerDown],
    );

    return (
      <motion.button
        ref={ref}
        type="button"
        whileTap={reduce || size === "inline" ? undefined : { scale: pressScale }}
        whileHover={reduce || !canHover || size === "inline" ? undefined : { scale: 1.02 }}
        transition={SPRING_PRESS}
        onPointerDown={handlePointerDown}
        className={cn(
          "inline-flex items-center justify-center font-medium select-none",
          "transition-colors",
          "disabled:pointer-events-none disabled:opacity-50",
          ripple && "relative overflow-hidden",
          VARIANT_CLASS[variant],
          SIZE_CLASS[size],
          className,
        )}
        {...rest}
      >
        {ripple && !reduce ? (
          <span className="pointer-events-none absolute inset-0 overflow-hidden rounded-[inherit]">
            <AnimatePresence>
              {ripples.map((r) => (
                <motion.span
                  key={r.id}
                  className="absolute rounded-full bg-current"
                  style={{
                    left: r.x,
                    top: r.y,
                    width: r.size,
                    height: r.size,
                    x: "-50%",
                    y: "-50%",
                  }}
                  initial={{ scale: 0.05, opacity: 0.3 }}
                  animate={{ scale: 1, opacity: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 1.6, ease: EASE_OUT }}
                  onAnimationComplete={() =>
                    setRipples((prev) => prev.filter((x) => x.id !== r.id))
                  }
                />
              ))}
            </AnimatePresence>
          </span>
        ) : null}
        {children}
      </motion.button>
    );
  },
);
