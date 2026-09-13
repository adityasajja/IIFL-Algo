import { useState } from "react";
import { TrendingDown, TrendingUp, Volume2, VolumeX } from "lucide-react";
import { useLiveTicks } from "../../lib/useLiveTicks";
import { sound } from "../../lib/sound";
import { cn } from "../../lib/utils";

const TICKER_SYMBOLS = [
  "NIFTYBEES-EQ",
  "RELIANCE-EQ",
  "HDFCBANK-EQ",
  "ICICIBANK-EQ",
  "INFY-EQ",
  "TCS-EQ",
  "SBIN-EQ",
  "BHARTIARTL-EQ",
  "LT-EQ",
  "TATASTEEL-EQ",
];

export function GlobalTickerBar({ onSelectSymbol }: { onSelectSymbol?: (sym: string) => void }) {
  const { getTick, connected, bridgeActive } = useLiveTicks(TICKER_SYMBOLS);
  const [audioEnabled, setAudioEnabled] = useState(sound.isEnabled());
  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem("atr.ticker.collapsed") === "true";
  });

  const toggleSound = () => {
    const next = sound.toggle();
    setAudioEnabled(next);
  };

  const toggleCollapse = () => {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem("atr.ticker.collapsed", String(next));
      return next;
    });
  };

  if (collapsed) {
    return (
      <div className="flex h-6 w-full items-center justify-between border-b border-border/40 bg-card/40 px-4 text-[10.5px] text-muted-foreground transition-colors">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connected
                ? bridgeActive
                  ? "bg-emerald-500 animate-pulse"
                  : "bg-amber-500"
                : "bg-muted-foreground/40"
            )}
          />
          <span className="font-medium">Live Feed</span>
        </div>
        <button
          type="button"
          onClick={toggleCollapse}
          className="text-muted-foreground hover:text-foreground font-medium transition-colors cursor-pointer"
        >
          Show Ticker Bar
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-8 w-full items-center justify-between border-b border-border/50 bg-card/60 px-4 text-xs backdrop-blur-md">
      {/* Live indices / marquee items */}
      <div className="flex min-w-0 flex-1 items-center gap-5 overflow-x-auto no-scrollbar">
        <div className="flex items-center gap-1.5 shrink-0 font-medium uppercase tracking-wider text-muted-foreground text-[10px]">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connected
                ? bridgeActive
                  ? "bg-emerald-500 animate-pulse"
                  : "bg-amber-500"
                : "bg-muted-foreground/40"
            )}
          />
          Live
        </div>

        <div className="flex items-center gap-5">
          {TICKER_SYMBOLS.map((sym) => {
            const tick = getTick(sym);
            const ltp = tick?.ltp ?? null;
            const chg = tick?.chg ?? 0;
            const chgPct = tick?.chg_pct ?? 0;
            const isUp = chg >= 0;

            return (
              <div
                key={sym}
                onClick={() => onSelectSymbol?.(sym)}
                className="group flex cursor-pointer items-center gap-1.5 whitespace-nowrap transition-transform hover:scale-[1.02]"
                title={`Click to view ${sym}`}
              >
                <span className="font-semibold text-foreground/90 group-hover:text-primary transition-colors">
                  {sym.replace("-EQ", "")}
                </span>
                {ltp !== null ? (
                  <>
                    <span
                      className={cn(
                        "tabular-nums font-semibold transition-colors duration-300",
                        tick?.flash === "up" && "text-emerald-500 font-bold",
                        tick?.flash === "down" && "text-destructive font-bold"
                      )}
                    >
                      ₹{ltp.toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}
                    </span>
                    <span
                      className={cn(
                        "inline-flex items-center text-[10.5px] tabular-nums font-medium",
                        isUp ? "text-emerald-500" : "text-destructive"
                      )}
                    >
                      {isUp ? <TrendingUp size={11} className="mr-0.5" /> : <TrendingDown size={11} className="mr-0.5" />}
                      {isUp ? "+" : ""}{chgPct.toFixed(2)}%
                    </span>
                  </>
                ) : (
                  <span className="text-muted-foreground/60 tabular-nums">Loading…</span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Audio chimes toggle & collapse buttons */}
      <div className="flex shrink-0 items-center gap-2 pl-3 border-l border-border/50">
        <button
          type="button"
          onClick={toggleSound}
          title={audioEnabled ? "Sound Chimes Active (click to mute)" : "Sound Chimes Muted (click to enable)"}
          className={cn(
            "flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10.5px] font-medium transition-colors outline-none",
            audioEnabled
              ? "bg-primary/10 text-primary hover:bg-primary/15"
              : "bg-muted text-muted-foreground hover:text-foreground"
          )}
        >
          {audioEnabled ? <Volume2 size={11} /> : <VolumeX size={11} />}
          <span className="hidden sm:inline">{audioEnabled ? "Sound" : "Muted"}</span>
        </button>
        <button
          type="button"
          onClick={toggleCollapse}
          title="Minimize live ticker bar"
          className="text-[10px] text-muted-foreground hover:text-foreground px-1.5 py-0.5 rounded transition-colors"
        >
          Hide
        </button>
      </div>
    </div>
  );
}
