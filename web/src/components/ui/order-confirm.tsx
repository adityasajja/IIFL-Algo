import { ShieldAlert } from "lucide-react";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { checkOrder, inr, newClientOrderId, type OrderIntent, type RiskContext } from "../../lib/order-safety";
import { useRiskContext } from "../../lib/useRiskContext";
import { cn } from "../../lib/utils";
import { Button } from "./button";
import { MorphingModal } from "./modal";

type Pending = { order: OrderIntent; clientOrderId: string; resolve: (v: { clientOrderId: string } | null) => void };

/**
 * Every real order goes through this: it says plainly what is about to be sent, what it is worth, and
 * what is wrong with it, and nothing is sent until a person confirms.
 *
 *   const confirm = useOrderConfirm();
 *   const ok = await confirm.ask({ ... });      // null if they cancelled
 *   if (ok) await placeOrder({ ..., client_order_id: ok.clientOrderId });
 *   ... render {confirm.dialog}
 *
 * The id it returns is made once per confirmation, so a double click or a retry cannot place a second
 * order. Cancel has the focus and Escape cancels: the safe answer is the easy one.
 */
export function useOrderConfirm() {
  const risk = useRiskContext();
  const [pending, setPending] = useState<Pending | null>(null);

  const ask = useCallback(
    (order: OrderIntent) =>
      new Promise<{ clientOrderId: string } | null>((resolve) => {
        setPending({ order, clientOrderId: newClientOrderId(), resolve });
      }),
    [],
  );
  const finish = useCallback((p: Pending, ok: boolean) => {
    p.resolve(ok ? { clientOrderId: p.clientOrderId } : null);
    setPending(null);
  }, []);

  const dialog: ReactNode = (
    <OrderConfirmDialog pending={pending} risk={risk} onCancel={() => pending && finish(pending, false)} onConfirm={() => pending && finish(pending, true)} />
  );
  return { ask, dialog, risk };
}

function OrderConfirmDialog({
  pending,
  risk,
  onCancel,
  onConfirm,
}: {
  pending: Pending | null;
  risk: RiskContext;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const order = pending?.order ?? null;
  // Evaluated when the dialog opens and whenever the risk state changes, never cached across orders.
  const check = useMemo(() => (order ? checkOrder(order, risk) : null), [order, risk]);

  return (
    <MorphingModal viewId={pending ? "order" : null} onClose={onCancel}>
      {order && check ? (
        <div className="w-full max-w-md text-left">
          <div className="mb-3 flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-destructive/30 bg-destructive/[0.08] px-2.5 py-0.5 text-caption font-semibold text-destructive">
              <ShieldAlert className="size-3" aria-hidden="true" />
              REAL ORDER
            </span>
            <span className="text-xs text-muted-foreground">sent to your broker</span>
          </div>

          <div className={cn("text-2xl font-semibold tracking-tight", order.side === "BUY" ? "text-gain" : "text-loss")}>
            {order.side === "BUY" ? "Buy" : "Sell"} {order.quantity} {order.symbol.replace("-EQ", "")}
          </div>

          <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
            <dt className="text-muted-foreground">Type</dt>
            <dd className="text-right">{order.orderType}</dd>
            <dt className="text-muted-foreground">{order.orderType === "MARKET" ? "Last price" : "Price"}</dt>
            <dd className="text-right tabular-nums">{order.price != null && order.price > 0 ? `₹${order.price.toLocaleString("en-IN")}` : "—"}</dd>
            <dt className="text-muted-foreground">Worth</dt>
            <dd className="text-right font-semibold tabular-nums">{check.notional == null ? "—" : inr(check.notional)}</dd>
            {risk.maxOrderNotional != null ? (
              <>
                <dt className="text-muted-foreground">Limit per order</dt>
                <dd className="text-right tabular-nums">{inr(risk.maxOrderNotional)}</dd>
              </>
            ) : null}
          </dl>

          {order.alsoDoes?.length ? (
            <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
              {order.alsoDoes.map((t) => (
                <li key={t}>+ {t}</li>
              ))}
            </ul>
          ) : null}

          {check.blockers.length ? (
            <ul className="mt-4 space-y-1 rounded-lg border border-loss/25 bg-loss/10 px-3 py-2 text-xs text-loss" role="alert">
              {check.blockers.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          ) : null}
          {check.warnings.length ? (
            <ul className="mt-3 space-y-1 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              {check.warnings.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          ) : null}

          <div className="mt-5 flex items-center justify-end gap-2">
            <Button variant="quiet" autoFocus onClick={onCancel}>
              Cancel
            </Button>
            <Button
              variant="primary"
              className={order.side === "BUY" ? "bg-gain hover:bg-gain/90" : "bg-destructive hover:bg-destructive/90"}
              disabled={check.blockers.length > 0}
              onClick={onConfirm}
            >
              Place real {order.side === "BUY" ? "buy" : "sell"} order
            </Button>
          </div>
        </div>
      ) : null}
    </MorphingModal>
  );
}
