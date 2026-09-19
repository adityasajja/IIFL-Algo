import { useMemo } from "react";
import { cn } from "../../lib/utils";

export interface NumberTickerProps {
  value: number;
  pad?: number;
  prefix?: string;
  suffix?: string;
  className?: string;
  locale?: boolean;
  format?: (value: number) => string;
}

/**
 * A whole number, shown plainly. It used to roll digit by digit, but the roll could
 * stop part-way (a blank or half-drawn digit) and moved on every refresh; a steady
 * number is calmer and always correct.
 */
export function NumberTicker({ value, pad, prefix, suffix, className, locale, format }: NumberTickerProps) {
  const text = useMemo(() => {
    const rounded = Math.round(value);
    const formatted = format ? format(rounded) : locale ? rounded.toLocaleString() : rounded.toString();
    return pad ? formatted.padStart(pad, "0") : formatted;
  }, [value, pad, format, locale]);

  return (
    <span className={cn("tabular-nums", className)}>
      {prefix}
      {text}
      {suffix}
    </span>
  );
}
