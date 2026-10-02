import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// The design tokens add three font sizes (index.css: micro, caption, body). Without telling the merger,
// it reads `text-micro` as a text *colour* and drops it whenever a real colour class sits beside it,
// which silently turned every 10px label into the inherited 15px.
const twMerge = extendTailwindMerge({
  extend: { classGroups: { "font-size": [{ text: ["micro", "caption", "body"] }] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
