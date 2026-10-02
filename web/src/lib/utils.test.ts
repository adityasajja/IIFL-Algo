import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("keeps the custom font sizes beside a colour class", () => {
    for (const size of ["text-micro", "text-caption", "text-body"]) {
      expect(cn(size, "text-muted-foreground")).toBe(`${size} text-muted-foreground`);
    }
  });
  it("still lets a later size replace an earlier one", () => {
    expect(cn("text-sm", "text-body")).toBe("text-body");
  });
});
