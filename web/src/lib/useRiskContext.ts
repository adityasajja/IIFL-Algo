import { useEffect, useState } from "react";
import { getRiskState } from "../api";
import type { RiskContext } from "./order-safety";
import { setVisibleInterval } from "./visibleInterval";

const LOADING: RiskContext = { live: null, maxOrderNotional: null, capIsDefault: false, warnings: [] };

/**
 * The server's own answer to "is this a real order, and what limits apply?", kept fresh. While it is
 * unknown nothing is assumed: it never defaults to "live" or to "no cap".
 */
export function useRiskContext(): RiskContext {
  const [ctx, setCtx] = useState<RiskContext>(LOADING);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const s = await getRiskState();
        if (!alive) return;
        const own = s.limits?.max_order_notional;
        const ownCap = typeof own === "number" && Number.isFinite(own) && own > 0 ? own : null;
        const def = s.live_protections?.defaults_applied?.max_order_notional;
        setCtx({
          live: !!s.live,
          maxOrderNotional: ownCap ?? (typeof def === "number" ? def : null),
          capIsDefault: ownCap == null && typeof def === "number",
          warnings: s.live_protections?.warnings ?? [],
        });
      } catch {
        if (alive) setCtx(LOADING);
      }
    };
    void load();
    const t = setVisibleInterval(() => void load(), 20_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return ctx;
}
