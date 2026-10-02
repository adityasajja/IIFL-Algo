/**
 * What stands between a click and a real order, as pure functions so it can be tested.
 *
 * An order is refused here ("blockers") for things that make it unsafe or meaningless: no price to
 * value it, a size above the cap, a closed market, a server that is in paper mode. Things worth
 * knowing but not worth blocking ("warnings") are shown in the confirmation. The server enforces
 * the same rules independently; this is the early, plain-language version so a person is told
 * before they send, not after.
 */

export type OrderSide = "BUY" | "SELL";

export type OrderIntent = {
  symbol: string;
  exchange?: string;
  side: OrderSide;
  quantity: number;
  /** MARKET, LIMIT, ... */
  orderType: string;
  /** The limit price, or for a market order the last price the person saw. Null when there is none. */
  price: number | null;
  /** Where `price` came from, so a stale one is not presented as live. */
  priceSource?: "limit" | "last" | "cache";
  /** Extra things this order also does, e.g. "Places a stop-loss at ₹2,400". */
  alsoDoes?: string[];
};

export type RiskContext = {
  /** Null while it is still loading: nothing is assumed. */
  live: boolean | null;
  /** The per-order cap in force: the operator's, else the platform default. Null when there is none. */
  maxOrderNotional: number | null;
  /** True when that cap is the platform default, not one the operator chose. */
  capIsDefault: boolean;
  /** `no_daily_loss_limit` and the like, straight from the server. */
  warnings: string[];
};

export type OrderCheck = {
  /** Quantity times price, or null when it cannot be valued. Never zero standing in for unknown. */
  notional: number | null;
  blockers: string[];
  warnings: string[];
};

const IST_OFFSET_MIN = 330;

/** Whether NSE is inside its order window (09:00-15:30 IST, weekdays). Holidays are the server's call. */
export function nseWindowOpen(now: Date): boolean {
  const ist = new Date(now.getTime() + (IST_OFFSET_MIN + now.getTimezoneOffset()) * 60_000);
  const day = ist.getDay();
  if (day === 0 || day === 6) return false;
  const minutes = ist.getHours() * 60 + ist.getMinutes();
  return minutes >= 9 * 60 && minutes <= 15 * 60 + 30;
}

export function inr(v: number): string {
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
}

export function checkOrder(order: OrderIntent, ctx: RiskContext, now: Date = new Date()): OrderCheck {
  const blockers: string[] = [];
  const warnings: string[] = [];

  if (!Number.isInteger(order.quantity) || order.quantity <= 0) {
    blockers.push("Quantity must be a whole number above zero.");
  }
  const priced = order.price != null && Number.isFinite(order.price) && order.price > 0;
  if (!priced) blockers.push("There is no price yet, so the order cannot be valued. Wait for a quote.");
  const notional = priced && order.quantity > 0 ? order.quantity * (order.price as number) : null;

  if (ctx.live === false) {
    blockers.push("The server is in paper mode, so real orders are switched off. Switch to live under Live to place one.");
  }
  if (!nseWindowOpen(now)) {
    blockers.push("NSE is closed. It takes orders on trading days from 09:00 to 15:30 IST.");
  }
  if (notional != null && ctx.maxOrderNotional != null && notional > ctx.maxOrderNotional) {
    blockers.push(
      `This order is worth ${inr(notional)}, above the ${inr(ctx.maxOrderNotional)} limit per order` +
        (ctx.capIsDefault ? " (the default; set your own under Risk & limits)." : "."),
    );
  }

  if (ctx.capIsDefault && ctx.maxOrderNotional != null) {
    warnings.push(`Each order is capped at ${inr(ctx.maxOrderNotional)} by default. Set your own limit under Risk & limits.`);
  }
  if (ctx.warnings.includes("no_daily_loss_limit")) {
    warnings.push("No daily loss limit is set, so nothing stops a bad day at the account level.");
  }
  if (notional != null && ctx.maxOrderNotional != null && notional <= ctx.maxOrderNotional && notional >= ctx.maxOrderNotional * 0.5) {
    warnings.push("This is a large order for your limit.");
  }
  if (order.priceSource === "cache") warnings.push("The price is from the local cache, not live.");

  return { notional, blockers, warnings };
}

/**
 * An id made once per confirmation. Sent with the order, it makes a double click or a retried request
 * return the first order instead of placing a second; a second deliberate order gets a new id.
 */
export function newClientOrderId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}
