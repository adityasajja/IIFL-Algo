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
          Market Depth <span className="font-normal text-muted-foreground text-xs">({symbol})</span>
        </div>
        <div className="flex items-center gap-2 text-[11px] font-medium">
          <span className="text-emerald-500">{bidRatio}% Buy</span>
          <div className="h-1.5 w-14 overflow-hidden rounded-full bg-destructive/30">
            <div
              className="h-full bg-emerald-500 transition-all duration-300"
              style={{ width: `${bidRatio}%` }}
            />
          </div>
          <span className="text-destructive">{askRatio}% Sell</span>
        </div>
      </div>

      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center justify-between px-2 pb-1">
        <span>Orders</span>
        <span>Quantity</span>
        <span>Price (Click to copy)</span>
      </div>

      {/* Asks (Sell Orders - descending from higher price down to best ask) */}
      <div className="space-y-0.5">
        {[...asks].reverse().map((a, i) => {
          const widthPct = Math.round((a.quantity / maxAskQty) * 100);
          return (
            <div
              key={`ask-${i}`}
              onClick={() => onSelectPrice?.(a.price)}
              title={`Click to set Limit price ₹${a.price.toFixed(2)}`}
              className="group relative flex items-center justify-between px-2 py-1 rounded text-[11.5px] tabular-nums cursor-pointer overflow-hidden transition-colors hover:bg-destructive/15"
            >
              <div
                className="absolute inset-y-0 right-0 bg-destructive/10 pointer-events-none transition-all duration-300"
                style={{ width: `${widthPct}%` }}
              />
              <span className="relative z-10 text-muted-foreground/75 text-[10.5px]">{a.orders}</span>
              <span className="relative z-10 font-medium text-foreground/85">{a.quantity.toLocaleString("en-IN")}</span>
              <span className="relative z-10 font-semibold text-destructive group-hover:underline">
                ₹{a.price.toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>

      {/* Center LTP Separator Bar */}
      <div className="my-1.5 flex items-center justify-between px-2 py-1 rounded bg-muted/40 border-y border-border/60 text-xs font-semibold">
        <span className="text-[10.5px] text-muted-foreground uppercase tracking-wider">Last Traded Price</span>
        <span className="tabular-nums font-bold text-foreground">
          ₹{ltp ? ltp.toFixed(2) : "—"}
        </span>
      </div>

      {/* Bids (Buy Orders - descending from best bid down to lower prices) */}
      <div className="space-y-0.5">
        {bids.map((b, i) => {
          const widthPct = Math.round((b.quantity / maxBidQty) * 100);
          return (
            <div
              key={`bid-${i}`}
              onClick={() => onSelectPrice?.(b.price)}
              title={`Click to set Limit price ₹${b.price.toFixed(2)}`}
              className="group relative flex items-center justify-between px-2 py-1 rounded text-[11.5px] tabular-nums cursor-pointer overflow-hidden transition-colors hover:bg-emerald-500/15"
            >
              <div
                className="absolute inset-y-0 right-0 bg-emerald-500/10 pointer-events-none transition-all duration-300"
                style={{ width: `${widthPct}%` }}
              />
              <span className="relative z-10 text-muted-foreground/75 text-[10.5px]">{b.orders}</span>
              <span className="relative z-10 font-medium text-foreground/85">{b.quantity.toLocaleString("en-IN")}</span>
              <span className="relative z-10 font-semibold text-emerald-500 group-hover:underline">
                ₹{b.price.toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>

      {/* Totals footer */}
      <div className="mt-2 flex items-center justify-between border-t border-border/50 pt-1.5 text-[10.5px] font-medium text-muted-foreground">
        <span>Total Ask: <strong className="text-foreground tabular-nums">{totalAskQty.toLocaleString("en-IN")}</strong></span>
        <span>Total Bid: <strong className="text-foreground tabular-nums">{totalBidQty.toLocaleString("en-IN")}</strong></span>
      </div>
    </div>
  );
}
