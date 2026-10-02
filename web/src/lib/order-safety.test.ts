import { describe, expect, it } from "vitest";

import { checkOrder, newClientOrderId, nseWindowOpen, type OrderIntent, type RiskContext } from "./order-safety";

// 2026-07-06 is a Monday. These are IST wall-clock times written as UTC minus 5:30.
const ist = (day: number, h: number, m = 0) => new Date(Date.UTC(2026, 6, day, h - 5, m - 30));
const OPEN = ist(6, 10, 30);

const order = (over: Partial<OrderIntent> = {}): OrderIntent => ({
  symbol: "RELIANCE-EQ", side: "BUY", quantity: 10, orderType: "LIMIT", price: 2500, ...over,
});
const ctx = (over: Partial<RiskContext> = {}): RiskContext => ({
  live: true, maxOrderNotional: 200_000, capIsDefault: false, warnings: [], ...over,
});

describe("market window", () => {
  it.each([
    [ist(6, 10, 30), true], [ist(6, 9, 0), true], [ist(6, 15, 30), true],
    [ist(6, 8, 59), false], [ist(6, 15, 31), false], [ist(4, 11), false], [ist(5, 11), false],
  ])("%s", (when, open) => expect(nseWindowOpen(when)).toBe(open));
});

describe("blockers", () => {
  it("a sound order in hours has none", () => {
    expect(checkOrder(order(), ctx(), OPEN).blockers).toEqual([]);
  });
  it("no price means it cannot be valued, and the notional is null not zero", () => {
    const r = checkOrder(order({ price: null }), ctx(), OPEN);
    expect(r.notional).toBeNull();
    expect(r.blockers[0]).toMatch(/no price/i);
  });
  it("a price of zero is no price", () => {
    expect(checkOrder(order({ price: 0 }), ctx(), OPEN).blockers.length).toBe(1);
  });
  it("a fractional or zero quantity is refused", () => {
    expect(checkOrder(order({ quantity: 0 }), ctx(), OPEN).blockers.length).toBeGreaterThan(0);
    expect(checkOrder(order({ quantity: 1.5 }), ctx(), OPEN).blockers.length).toBeGreaterThan(0);
  });
  it("paper mode blocks a real order and says how to change it", () => {
    expect(checkOrder(order(), ctx({ live: false }), OPEN).blockers.join(" ")).toMatch(/paper mode/i);
  });
  it("while the mode is still loading nothing is assumed", () => {
    expect(checkOrder(order(), ctx({ live: null }), OPEN).blockers).toEqual([]);
  });
  it("a closed market blocks", () => {
    expect(checkOrder(order(), ctx(), ist(4, 11)).blockers.join(" ")).toMatch(/closed/i);
  });
  it("an order above the cap is blocked, and a default cap says it is a default", () => {
    const r = checkOrder(order({ quantity: 1000 }), ctx({ capIsDefault: true }), OPEN);
    expect(r.blockers.join(" ")).toMatch(/limit per order/);
    expect(r.blockers.join(" ")).toMatch(/default/);
  });
  it("exactly at the cap is allowed", () => {
    expect(checkOrder(order({ quantity: 80, price: 2500 }), ctx(), OPEN).blockers).toEqual([]);
  });
});

describe("warnings", () => {
  it("say the cap is a default, and that no daily loss limit is set", () => {
    const w = checkOrder(order(), ctx({ capIsDefault: true, warnings: ["no_daily_loss_limit"] }), OPEN).warnings.join(" ");
    expect(w).toMatch(/by default/);
    expect(w).toMatch(/daily loss/i);
  });
  it("flag a large order and a cached price", () => {
    const w = checkOrder(order({ quantity: 60, priceSource: "cache" }), ctx(), OPEN).warnings.join(" ");
    expect(w).toMatch(/large order/);
    expect(w).toMatch(/cache/);
  });
  it("a small order with everything set has nothing to say", () => {
    expect(checkOrder(order({ quantity: 1 }), ctx(), OPEN).warnings).toEqual([]);
  });
});

describe("client order id", () => {
  it("is long enough for the server and new each time", () => {
    const a = newClientOrderId();
    expect(a.length).toBeGreaterThanOrEqual(8);
    expect(a).not.toBe(newClientOrderId());
  });
});
