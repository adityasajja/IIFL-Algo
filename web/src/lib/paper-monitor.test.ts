/**
 * Tests for the pure derivations behind the paper deployment screen.
 *
 * These are not decoration. Each case below pins a *decision* that has a
 * plausible wrong answer which would look right on screen:
 *
 * - `controlGates` mirrors the repository's `WHERE` clauses. Getting it wrong
 *   offers the user a button the server rejects, which teaches them the button
 *   is broken rather than that the state is terminal.
 * - `fmtMoneyOrDash` / `fmtPctOrDash` decide how a *null* P&L prints. Printing
 *   `null` as `₹0` turns "we have not measured this yet" into "you made
 *   nothing", which is the exact confusion the API's nullable field exists to
 *   prevent.
 * - `toChain` collapses duplicate stage events. A deployment emits an `order`
 *   entry for both `SUBMITTED` and `ACKNOWLEDGED`; drawing two arrows for one
 *   link in the chain misrepresents the pipeline.
 * - `deployReadiness` refuses a deployment with no symbols. The runner resolves
 *   its universe from `config.symbols` and nowhere else, so a deployment created
 *   without them is RUNNING and evaluating an empty list forever — running, and
 *   doing nothing, with no error anywhere.
 */
import { describe, expect, it } from "vitest";

import type { MonitorOverview, TimelineEvent } from "../api";
import {
  STAGES,
  ago,
  comparisonVerdictBlurb,
  comparisonVerdictTone,
  controlGates,
  deltaSample,
  deployReadiness,
  fmtDelta,
  fmtDiffValue,
  fmtMoneyOrDash,
  fmtPctOrDash,
  idleReason,
  istClock,
  newestVersion,
  openPositions,
  outcomeTone,
  parseSymbols,
  toChain,
  unpricedOf,
} from "./paper-monitor";

const event = (over: Partial<TimelineEvent> = {}): TimelineEvent => ({
  ts: "2026-09-14T04:45:00+00:00",
  stage: "signal",
  symbol: "RELIANCE-EQ",
  summary: "RELIANCE-EQ BUY signal",
  outcome: "observed",
  reason: null,
  order_id: "o1",
  detail: {},
  ...over,
});

describe("controlGates", () => {
  it("allows start from PENDING and PAUSED only", () => {
    expect(controlGates("PENDING").canStart).toBe(true);
    expect(controlGates("PAUSED").canStart).toBe(true);
    expect(controlGates("RUNNING").canStart).toBe(false);
  });

  it("refuses start on a STOPPED deployment and says why", () => {
    // The backend's `start` accepts only PENDING and PAUSED. A stopped
    // deployment is terminal by design: resuming it would mean the account's
    // history continues after a decision to end it.
    const gates = controlGates("STOPPED");
    expect(gates.canStart).toBe(false);
    expect(gates.resetInstead).toBe(true);
    expect(gates.startBlocked).toContain("terminal");
  });

  it("only pauses a running deployment", () => {
    expect(controlGates("RUNNING").canPause).toBe(true);
    expect(controlGates("PAUSED").canPause).toBe(false);
    expect(controlGates("PENDING").canPause).toBe(false);
  });

  it("stops a running or paused deployment, but not a stopped one", () => {
    expect(controlGates("RUNNING").canStop).toBe(true);
    expect(controlGates("PAUSED").canStop).toBe(true);
    expect(controlGates("STOPPED").canStop).toBe(false);
  });

  it("always allows reset — it is the only way out of the terminal state", () => {
    for (const state of ["PENDING", "RUNNING", "PAUSED", "STOPPED"]) {
      expect(controlGates(state).canReset).toBe(true);
    }
  });

  it("treats an unknown or missing state as not startable", () => {
    expect(controlGates(null).canStart).toBe(false);
    expect(controlGates(undefined).canStart).toBe(false);
    expect(controlGates("WEIRD").canStart).toBe(false);
  });
});

describe("fmtMoneyOrDash", () => {
  it("renders null as an em dash, never as zero", () => {
    // The whole reason `today_pnl` is nullable. `null` is "not measured yet".
    expect(fmtMoneyOrDash(null)).toBe("—");
    expect(fmtMoneyOrDash(undefined)).toBe("—");
  });

  it("renders zero as a real zero", () => {
    expect(fmtMoneyOrDash(0)).toBe("₹0");
  });

  it("puts the minus sign before the rupee symbol", () => {
    expect(fmtMoneyOrDash(-1500)).toBe("-₹1,500");
  });

  it("uses Indian digit grouping", () => {
    // 10 crore rupees groups as 10,00,00,000, not 100,000,000.
    expect(fmtMoneyOrDash(100000000)).toBe("₹10,00,00,000");
  });

  it("refuses non-finite numbers rather than printing Infinity", () => {
    // `Infinity` is not valid JSON; the API collapses its `inf` limit sentinels
    // to null, and anything that leaked through must not render as a figure.
    expect(fmtMoneyOrDash(Number.POSITIVE_INFINITY)).toBe("—");
    expect(fmtMoneyOrDash(Number.NaN)).toBe("—");
  });
});

describe("fmtPctOrDash", () => {
  it("signs positive values and never signs an unknown", () => {
    expect(fmtPctOrDash(1.234)).toBe("+1.23%");
    expect(fmtPctOrDash(-1.234)).toBe("-1.23%");
    expect(fmtPctOrDash(null)).toBe("—");
  });

  it("does not print +0.00% for a null", () => {
    // The failure this guards: a UI that coerces null to 0 shows a flat day
    // where the truth is "no baseline exists to compare against".
    expect(fmtPctOrDash(null)).not.toContain("0.00");
  });
});

describe("outcomeTone", () => {
  it("flags a rejection as bad and an approval as good", () => {
    expect(outcomeTone("rejected")).toBe("bad");
    expect(outcomeTone("approved")).toBe("good");
    expect(outcomeTone("filled")).toBe("good");
  });

  it("treats a placement as neither good nor bad yet", () => {
    expect(outcomeTone("placed")).toBe("warn");
  });

  it("falls back to flat for an unrecognised outcome", () => {
    expect(outcomeTone("something_new")).toBe("flat");
  });
});

describe("toChain", () => {
  it("emits the stages that occurred, in causal order", () => {
    const chain = toChain([
      event({ stage: "position", ts: "2026-09-14T04:45:02+00:00" }),
      event({ stage: "signal", ts: "2026-09-14T04:45:00+00:00" }),
      event({ stage: "fill", ts: "2026-09-14T04:45:02+00:00" }),
    ]);
    expect(chain.map((c) => c.stage)).toEqual(["signal", "fill", "position"]);
  });

  it("omits stages that have not happened rather than inventing them", () => {
    const chain = toChain([event({ stage: "signal" })]);
    expect(chain).toHaveLength(1);
    expect(chain[0].stage).toBe("signal");
  });

  it("collapses repeated events for one stage into a count", () => {
    // SUBMITTED and ACKNOWLEDGED are both `order`. Two facts, one link.
    const chain = toChain([
      event({ stage: "order", summary: "placed (188)", ts: "2026-09-14T04:45:01+00:00" }),
      event({ stage: "order", summary: "acknowledged (188)", ts: "2026-09-14T04:45:01+00:00" }),
    ]);
    expect(chain).toHaveLength(1);
    expect(chain[0].count).toBe(2);
    // Nothing is hidden: both lines survive, the rail is just readable.
    expect(chain[0].summaries).toHaveLength(2);
  });

  it("keeps the newest occurrence of a stage, since the screen answers 'what just happened'", () => {
    const chain = toChain([
      event({ stage: "fill", ts: "2026-09-14T04:40:00+00:00", summary: "older" }),
      event({ stage: "fill", ts: "2026-09-14T04:45:00+00:00", summary: "newer" }),
    ]);
    expect(chain[0].ts).toBe("2026-09-14T04:45:00+00:00");
    expect(chain[0].summaries[0]).toBe("newer");
  });

  it("ignores an unknown stage rather than rendering a stage the rail cannot place", () => {
    const chain = toChain([event({ stage: "teleport" as TimelineEvent["stage"] })]);
    expect(chain).toHaveLength(0);
  });

  it("knows all five stages in the right order", () => {
    expect(STAGES).toEqual(["signal", "risk", "order", "fill", "position"]);
  });
});

describe("idleReason", () => {
  const status = (over: Partial<MonitorOverview["status"]> = {}): MonitorOverview["status"] =>
    ({
      deployment_id: "d1",
      status: "RUNNING",
      mode: "PAPER",
      strategy_id: "s1",
      strategy_version: 1,
      capital: 500000,
      symbols: ["RELIANCE-EQ"],
      exchange: "NSEEQ",
      timeframe: "1d",
      started_at: null,
      stopped_at: null,
      stop_reason: null,
      created_at: "2026-09-14T04:00:00+00:00",
      trading: true,
      not_trading_because: null,
      blocked_reason: null,
      in_market_hours: true,
      runner_running: true,
      loop_attached: true,
      last_pass: null,
      ...over,
    }) as MonitorOverview["status"];

  it("returns nothing when the deployment is trading", () => {
    expect(idleReason(status())).toBeNull();
  });

  it("prefers the server's own explanation when it gives one", () => {
    expect(
      idleReason(status({ trading: false, not_trading_because: "the cash session is closed" })),
    ).toBe("the cash session is closed");
  });

  it("surfaces a blocked rule set in preference to a closed session", () => {
    // A blocked version is a configuration problem the operator can fix; a
    // closed session is not. Leading with the session would send them to the
    // wrong thing.
    const reason = idleReason(
      status({
        trading: false,
        blocked_reason: "strategy version s1#7 defines no entry/exit rules",
        in_market_hours: false,
      }),
    );
    expect(reason).toContain("no entry/exit rules");
  });

  it("falls back through the flags when the server sent no sentence", () => {
    expect(idleReason(status({ trading: false, loop_attached: false }))).toBe(
      "the runner has not attached a loop yet",
    );
    expect(idleReason(status({ trading: false, in_market_hours: false }))).toBe(
      "the cash session is closed",
    );
    expect(idleReason(status({ trading: false, status: "PAUSED" }))).toBe(
      "deployment is PAUSED",
    );
  });

  it("says nothing at all when there is no status to read", () => {
    expect(idleReason(null)).toBeNull();
  });
});

describe("parseSymbols", () => {
  it("splits on commas and whitespace", () => {
    expect(parseSymbols("RELIANCE-EQ, INFY-EQ TCS-EQ")).toEqual([
      "RELIANCE-EQ",
      "INFY-EQ",
      "TCS-EQ",
    ]);
  });

  it("upper-cases, because the runner matches symbols in upper case", () => {
    // A deployment whose symbols never resolve is silently inert.
    expect(parseSymbols("reliance-eq")).toEqual(["RELIANCE-EQ"]);
  });

  it("de-duplicates and drops empties", () => {
    expect(parseSymbols("INFY-EQ, INFY-EQ,,  ,")).toEqual(["INFY-EQ"]);
  });

  it("returns nothing for blank input rather than a list with an empty string", () => {
    expect(parseSymbols("   ")).toEqual([]);
    expect(parseSymbols("")).toEqual([]);
  });
});

describe("deployReadiness", () => {
  const good = {
    strategyId: "s1",
    version: "3",
    capital: "500000",
    symbols: "RELIANCE-EQ",
    universe: "",
  };

  it("passes a complete form", () => {
    expect(deployReadiness(good).ready).toBe(true);
  });

  it("refuses a built-in strategy, which has no version to pin", () => {
    const r = deployReadiness({ ...good, strategyId: "" });
    expect(r.ready).toBe(false);
    expect(r.problem).toContain("saved strategy");
  });

  it("refuses a missing version", () => {
    const r = deployReadiness({ ...good, version: "" });
    expect(r.ready).toBe(false);
    expect(r.problem).toContain("version");
  });

  it("refuses a non-positive capital", () => {
    expect(deployReadiness({ ...good, capital: "0" }).ready).toBe(false);
    expect(deployReadiness({ ...good, capital: "-5" }).ready).toBe(false);
    expect(deployReadiness({ ...good, capital: "abc" }).ready).toBe(false);
  });

  it("refuses a deployment with no symbols and no universe", () => {
    // The runner reads its universe from config.symbols only. Creating one
    // without symbols yields a deployment that is RUNNING and evaluating an
    // empty list forever, with no error shown anywhere.
    const r = deployReadiness({ ...good, symbols: "", universe: "" });
    expect(r.ready).toBe(false);
    expect(r.problem).toContain("symbol");
  });

  it("accepts a universe standing in for typed symbols", () => {
    expect(deployReadiness({ ...good, symbols: "", universe: "nifty50" }).ready).toBe(true);
  });
});

describe("time formatting", () => {
  it("renders a timestamp in IST, not the browser's zone", () => {
    // 04:45 UTC is 10:15 IST. A timeline drawn in local time would put an
    // intraday fill outside the session for anyone reading from Europe and make
    // the session boundary impossible to see.
    expect(istClock("2026-09-14T04:45:00+00:00")).toBe("10:15:00");
  });

  it("returns a dash for a missing or unparseable timestamp", () => {
    expect(istClock(null)).toBe("—");
    expect(istClock("not a date")).toBe("—");
  });

  it("describes recency coarsely, since the exact second is in the tooltip", () => {
    const now = Date.parse("2026-09-14T05:00:00+00:00");
    expect(ago("2026-09-14T04:59:58+00:00", now)).toBe("just now");
    expect(ago("2026-09-14T04:59:30+00:00", now)).toBe("30s ago");
    expect(ago("2026-09-14T04:30:00+00:00", now)).toBe("30m ago");
    expect(ago("2026-09-14T02:00:00+00:00", now)).toBe("3h ago");
    expect(ago("2026-09-12T05:00:00+00:00", now)).toBe("2d ago");
    expect(ago(null, now)).toBe("—");
  });
});

describe("overview readers", () => {
  const overview = {
    pnl: { unpriced_symbols: ["TCS-EQ"], positions: [] },
    positions: [
      { symbol: "RELIANCE-EQ", quantity: 188 },
      { symbol: "INFY-EQ", quantity: 0 },
      { symbol: "TCS-EQ", quantity: -50 },
    ],
  } as unknown as MonitorOverview;

  it("counts held positions only, excluding flat rows", () => {
    // The ledger returns flat rows too. Counting them would inflate the
    // position count above the number of things actually held.
    const open = openPositions(overview);
    expect(open.map((p) => p.symbol)).toEqual(["RELIANCE-EQ", "TCS-EQ"]);
  });

  it("names unpriced symbols rather than dropping them", () => {
    expect(unpricedOf(overview)).toEqual(["TCS-EQ"]);
  });

  it("returns empty collections for a missing overview rather than throwing", () => {
    expect(openPositions(null)).toEqual([]);
    expect(unpricedOf(null)).toEqual([]);
  });
});

describe("newestVersion", () => {
  // The deploy form preselects a version, and that version is what the
  // deployment is *pinned* to — the rules it runs for months. So the cost of
  // getting this wrong is not a wrong pixel: it is silently deploying the wrong
  // ruleset, under a dropdown that shows a perfectly valid version number.
  //
  // The panel used to read `versions[versions.length - 1]`. The service serves
  // the list `ORDER BY version DESC`, so that returned the OLDEST while the
  // comment above it said "newest". These tests exist so the direction is
  // asserted rather than assumed.

  it("picks the highest number from a newest-first list (the real order)", () => {
    expect(newestVersion([{ version: 2 }, { version: 1 }])).toBe("2");
  });

  it("picks the highest number from an oldest-first list too", () => {
    // Not the order the API sends today, but the function must not depend on
    // which end the newest happens to be at — that coupling is the bug.
    expect(newestVersion([{ version: 1 }, { version: 2 }])).toBe("2");
  });

  it("handles a list that is not sorted at all", () => {
    expect(newestVersion([{ version: 3 }, { version: 11 }, { version: 7 }])).toBe("11");
  });

  it("compares numerically, not lexically", () => {
    // `"11" > "9"` is false as a string comparison, and a ten-version strategy
    // is ordinary. A lexical maximum would pick 9 and look entirely normal.
    expect(newestVersion([{ version: 9 }, { version: 11 }])).toBe("11");
  });

  it("returns a string, because the select's value is one", () => {
    expect(newestVersion([{ version: 4 }])).toBe("4");
  });

  it("returns an empty string for no versions, so the form stays submittable-invalid", () => {
    // `deployReadiness` refuses an empty version, which is what should happen:
    // a strategy with no versions cannot be pinned and cannot be deployed.
    expect(newestVersion([])).toBe("");
    expect(newestVersion(null)).toBe("");
    expect(newestVersion(undefined)).toBe("");
  });

  it("preselects something deployReadiness then accepts", () => {
    const version = newestVersion([{ version: 5 }, { version: 4 }]);
    expect(typeof version).toBe("string");
    expect(version.length).toBeGreaterThan(0);
  });
});

describe("newestVersion", () => {
  // The deploy form preselects a version, and that version is what the
  // deployment is *pinned* to — the rules it runs for months. So the cost of
  // getting this wrong is not a wrong pixel: it is silently deploying the wrong
  // ruleset, under a dropdown that shows a perfectly valid version number.
  //
  // The panel used to read `versions[versions.length - 1]`. The service serves
  // the list `ORDER BY version DESC`, so that returned the OLDEST while the
  // comment above it said "newest". These tests exist so the direction is
  // asserted rather than assumed.

  it("picks the highest number from a newest-first list (the real order)", () => {
    expect(newestVersion([{ version: 2 }, { version: 1 }])).toBe("2");
  });

  it("picks the highest number from an oldest-first list too", () => {
    // Not the order the API sends today, but the function must not depend on
    // which end the newest happens to be at — that coupling is the bug.
    expect(newestVersion([{ version: 1 }, { version: 2 }])).toBe("2");
  });

  it("handles a list that is not sorted at all", () => {
    expect(newestVersion([{ version: 3 }, { version: 11 }, { version: 7 }])).toBe("11");
  });

  it("compares numerically, not lexically", () => {
    // `"11" > "9"` is false as a string comparison, and a ten-version strategy
    // is ordinary. A lexical maximum would pick 9 and look entirely normal.
    expect(newestVersion([{ version: 9 }, { version: 11 }])).toBe("11");
  });

  it("returns a string, because the select's value is one", () => {
    expect(newestVersion([{ version: 4 }])).toBe("4");
  });

  it("returns an empty string for no versions, so the form stays submittable-invalid", () => {
    // `deployReadiness` refuses an empty version, which is what should happen:
    // a strategy with no versions cannot be pinned and cannot be deployed.
    expect(newestVersion([])).toBe("");
    expect(newestVersion(null)).toBe("");
    expect(newestVersion(undefined)).toBe("");
  });

  it("preselects something deployReadiness then accepts", () => {
    const version = newestVersion([{ version: 5 }, { version: 4 }]);
    expect(typeof version).toBe("string");
    expect(version.length).toBeGreaterThan(0);
  });
});

describe("champion vs challenger views", () => {
  it("tones readiness, never superiority", () => {
    expect(comparisonVerdictTone("INSUFFICIENT EVIDENCE")).toBe("muted");
    expect(comparisonVerdictTone("EARLY EVIDENCE")).toBe("warn");
    expect(comparisonVerdictTone("COMPARISON READY")).toBe("good");
    expect(comparisonVerdictBlurb("COMPARISON READY")).toContain("not promoting");
    expect(comparisonVerdictBlurb("INSUFFICIENT EVIDENCE")).toContain("10-trade minimum");
  });

  it("formats deltas in the metric unit, with both sizes beside them", () => {
    expect(fmtDelta(1.25, "return_pct")).toBe("+1.25%");
    expect(fmtDelta(-0.5, "return_pct")).toBe("-0.50%");
    expect(fmtDelta(12.5, "net_pnl")).toBe("+12.50");
    expect(fmtDelta(null, "return_pct")).toBe("—");
    expect(deltaSample(12, 9)).toBe("12 vs 9 trades");
  });

  it("prints diff values without collapsing objects", () => {
    expect(fmtDiffValue(1.9)).toBe("1.9");
    expect(fmtDiffValue(null)).toBe("—");
    expect(fmtDiffValue({ a: 1 })).toBe(`{"a":1}`);
  });
});
