import { type DepthLevel } from "../../lib/useLiveTicks";

interface MarketDepthLadderProps {
  symbol: string;
  depth?: DepthLevel[] | null;
  ltp?: number;
  onSelectPrice?: (price: number) => void;
}

export function MarketDepthLadder({
  symbol,
  depth,
  ltp,
  onSelectPrice,
}: MarketDepthLadderProps) {
  const hasDepth = Array.isArray(depth) && depth.length > 0;
  const bids = hasDepth ? depth.filter((d) => d.type === 1 || d.type === 0).slice(0, 5) : [];
  const asks = hasDepth && depth.length > 5
    ? depth.filter((d) => d.type === 2).slice(0, 5)
    : hasDepth
      ? depth.filter((d) => d.type === 2).slice(0, 5)
      : [];
  const live = hasDepth && (bids.length > 0 || asks.length > 0);

  if (!live) {
    return (
      <div className="rounded-xl border border-border/70 bg-card/60 p-4 text-center">
        <div className="flex items-center justify-center gap-2 text-[12px] font-semibold text-muted-foreground">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
          5-Level Market Depth
          <span className="font-normal text-muted-foreground/80">({symbol})</span>
        </div>
        <p className="mt-2 text-[12px] text-muted-foreground">
          {ltp
            ? <>LTP ₹{ltp.toFixed(2)} · awaiting depth feed from the bridge.</>
            : "Awaiting live data from the bridge. Most small-cap symbols do not publish a 5-level feed."}
        </p>
      </div>
    );
  }

  const maxBidQty = Math.max(...bids.map((b) => b.quantity), 1);
  const maxAskQty = Math.max(...asks.map((a) => a.quantity), 1);
  const totalBidQty = bids.reduce((acc, b) => acc + b.quantity, 0);
  const totalAskQty = asks.reduce((acc, a) => acc + a.quantity, 0);
  const totalBoth = totalBidQty + totalAskQty || 1;
  const bidRatio = Math.round((totalBidQty / totalBoth) * 100);
  const askRatio = 100 - bidRatio;

  return (
    <div className="rounded-xl border border-border/70 bg-card/60 p-3 text-[12px]">
      <div className="mb-2 flex items-center justify-between border-b border-border/50 pb-2">
        <div className="flex items-center gap-1.5 font-semibold text-foreground">
          <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
          5-Level Market Depth <span className="font-normal text-muted-foreground">({symbol})</span>
        </div>
        <div className="flex items-center gap-2 text-[11px] font-medium">
          <span className="text-emerald-500">{bidRatio}% Buy</span>
          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-destructive/30">
            <div
              className="h-full bg-emerald-500 transition-all duration-300"
              style={{ width: `${bidRatio}%` }}
            />
          </div>
          <span className="text-destructive">{askRatio}% Sell</span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {/* Bid Side */}
        <div>
          <div className="grid grid-cols-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span>Orders</span>
            <span className="text-right">Qty</span>
            <span className="text-right text-emerald-500">Bid Price</span>
          </div>
          <div className="space-y-1">
            {bids.map((b, i) => {
              const widthPct = Math.round((b.quantity / maxBidQty) * 100);
              return (
                <div
                  key={i}
                  onClick={() => onSelectPrice?.(b.price)}
                  className="group relative grid grid-cols-3 cursor-pointer items-center overflow-hidden rounded py-0.5 text-[11.5px] tabular-nums transition-colors hover:bg-emerald-500/10"
                >
                  <div
                    className="absolute inset-y-0 right-0 bg-emerald-500/10 transition-all"
                    style={{ width: `${widthPct}%` }}
                  />
                  <span className="relative z-10 text-muted-foreground">{b.orders}</span>
                  <span className="relative z-10 text-right font-medium">{b.quantity.toLocaleString("en-IN")}</span>
                  <span className="relative z-10 text-right font-bold text-emerald-500 group-hover:underline">
                    ₹{b.price.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="mt-1.5 flex justify-between border-t border-border/50 pt-1 text-[10.5px] font-semibold text-muted-foreground">
            <span>Total Bid</span>
            <span className="tabular-nums text-foreground">{totalBidQty.toLocaleString("en-IN")}</span>
          </div>
        </div>

        {/* Ask Side */}
        <div>
          <div className="grid grid-cols-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span className="text-destructive">Ask Price</span>
            <span className="text-right">Qty</span>
            <span className="text-right">Orders</span>
          </div>
          <div className="space-y-1">
            {asks.map((a, i) => {
              const widthPct = Math.round((a.quantity / maxAskQty) * 100);
              return (
                <div
                  key={i}
                  onClick={() => onSelectPrice?.(a.price)}
                  className="group relative grid grid-cols-3 cursor-pointer items-center overflow-hidden rounded py-0.5 text-[11.5px] tabular-nums transition-colors hover:bg-destructive/10"
                >
                  <div
                    className="absolute inset-y-0 left-0 bg-destructive/10 transition-all"
                    style={{ width: `${widthPct}%` }}
                  />
                  <span className="relative z-10 font-bold text-destructive group-hover:underline">
                    ₹{a.price.toFixed(2)}
                  </span>
                  <span className="relative z-10 text-right font-medium">{a.quantity.toLocaleString("en-IN")}</span>
                  <span className="relative z-10 text-right text-muted-foreground">{a.orders}</span>
                </div>
              );
            })}
          </div>
          <div className="mt-1.5 flex justify-between border-t border-border/50 pt-1 text-[10.5px] font-semibold text-muted-foreground">
            <span>Total Ask</span>
            <span className="tabular-nums text-foreground">{totalAskQty.toLocaleString("en-IN")}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
