import type { ReactNode } from "react";
import {
  MorphSelect,
  MorphSelectContent,
  MorphSelectItem,
  MorphSelectTrigger,
  MorphSelectValue,
} from "../motion/select-morph";

export interface SelectOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
}

export interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  /** "sm" fits dense inline rows (e.g. a condition builder); default "md". */
  size?: "sm" | "md";
}

/**
 * Drop-in replacement for a native `<select>` themed with our own tokens —
 * the native listbox popup is OS-rendered and can't be styled, which is why
 * it always looked out of place against the dark theme.
 */
export function Select({
  value,
  onChange,
  options,
  placeholder,
  className,
  disabled,
  size,
}: SelectProps) {
  return (
    <MorphSelect
      value={value}
      onValueChange={onChange}
      disabled={disabled}
      size={size}
      className={className}
    >
      <MorphSelectTrigger>
        <MorphSelectValue placeholder={placeholder} />
      </MorphSelectTrigger>
      <MorphSelectContent>
        {options.map((o) => (
          <MorphSelectItem key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </MorphSelectItem>
        ))}
      </MorphSelectContent>
    </MorphSelect>
  );
}
