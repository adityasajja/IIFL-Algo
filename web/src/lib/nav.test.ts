import { describe, expect, it } from "vitest";

import { journeySteps, nextStep, type JourneyFacts } from "./journey";
import { NAV_GROUPS, PAGES, SUBS, VALID_TABS, defaultSub, normaliseSub, parseRoute } from "./nav";

describe("navigation map", () => {
  it("lists every page exactly once across the groups", () => {
    const listed = NAV_GROUPS.flatMap((g) => g.tabs);
    expect(new Set(listed).size).toBe(listed.length);
    expect(new Set(listed)).toEqual(VALID_TABS);
  });

  it("gives every page a name, title and one-sentence blurb", () => {
    for (const p of Object.values(PAGES)) {
      expect(p.name.length).toBeGreaterThan(0);
      expect(p.title.length).toBeGreaterThan(0);
      expect(/[.?]$/.test(p.blurb)).toBe(true);
    }
  });

  it("falls back to the default sub-page when the requested one does not exist", () => {
    expect(normaliseSub("evidence", "nope")).toBe("backtest");
    expect(normaliseSub("evidence", "improve")).toBe("improve");
    expect(normaliseSub("dashboard", "anything")).toBeUndefined();
    expect(defaultSub("signals")).toBe("today");
  });
});

describe("old links still land in the right place", () => {
  it.each([
    ["#overview", "dashboard", undefined],
    ["#charts", "markets", "charts"],
    ["#scanner", "markets", "scanner"],
    ["#optimization", "evidence", "improve"],
    ["#analytics", "learning", "attribution"],
    ["#risk", "paper", "risk"],
    ["#trading", "paper", "runs"],
    ["#portfolio", "paper", "runs"],
    ["#trading/control-center", "paper", "risk"],
    ["#alerts", "signals", "alerts"],
    ["#briefing", "signals", "brief"],
    ["#deployments", "paper", "runs"],
    ["#markets/custom", "labs", "custom-scan"],
    ["#trading/mode", "paper", "runs"],
  ])("%s", (hash, tab, sub) => {
    expect(parseRoute(hash)).toEqual({ tab, sub });
  });

  it("opens a tab's default sub-page for a bare hash and rejects unknown ones", () => {
    expect(parseRoute("#markets")).toEqual({ tab: "markets", sub: "intelligence" });
    expect(parseRoute("#signals")).toEqual({ tab: "signals", sub: "today" });
    expect(parseRoute("#nonsense")).toBeNull();
    expect(parseRoute("")).toBeNull();
  });

  it("only ever returns a sub-page that exists", () => {
    for (const tab of Object.keys(SUBS)) {
      for (const s of SUBS[tab as keyof typeof SUBS]!) {
        expect(parseRoute(`#${tab}/${s.id}`)).toEqual({ tab, sub: s.id });
      }
    }
  });
});

describe("journey", () => {
  const none: JourneyFacts = { strategies: 0, deployable: 0, paperRuns: 0 };

  it("starts at build for a new user", () => {
    expect(nextStep(journeySteps(none))?.id).toBe("build");
  });

  it("moves forward as each fact becomes true", () => {
    expect(nextStep(journeySteps({ ...none, strategies: 1 }))?.id).toBe("test");
    expect(nextStep(journeySteps({ ...none, strategies: 1, deployable: 1 }))?.id).toBe("paper");
  });

  it("is finished once a strategy has been paper traded, and Review never blocks", () => {
    expect(nextStep(journeySteps({ strategies: 1, deployable: 1, paperRuns: 1 }))).toBeNull();
  });

  it("has no live step: the product never places a real order", () => {
    expect(journeySteps(none).map((s) => s.id)).toEqual(["build", "test", "paper", "review"]);
    expect(Object.keys(PAGES)).not.toContain("trading");
  });
});
