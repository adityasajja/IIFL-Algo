import { motion } from "motion/react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Banknote,
  BarChart3,
  Briefcase,
  Clock,
  Coins,
  LineChart,
  ListOrdered,
  Send,
  ShieldAlert,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getPortfolio,
  getRiskStatus,
  getQuote,
  placeOrder,
  searchSymbols,
  setKillSwitch,
  type PortfolioResponse,
  type PortfolioSection,
} from "./api";
import { ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/motion/input";
import { Select } from "./components/ui/select";
import { Button } from "./components/ui/button";
import ReconcileCard from "./ReconcileCard";
import { useOrderConfirm } from "./components/ui/order-confirm";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, Stat, fmtNum } from "./components/ui/stat";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/motion/tabs";
import { useToast } from "./components/ui/toast-context";
import { Tooltip } from "./components/motion/tooltip";
import { Table, type TableColumn } from "./components/motion/table";
import { BouncyAccordion, type BouncyAccordionItem } from "./components/motion/bouncy-accordion";
import { MarketDepthLadder } from "./components/ui/market-depth";
import { useLiveTicks, type LiveTick } from "./lib/useLiveTicks";
import { sound } from "./lib/sound";
import { cn } from "./lib/utils";
import { useDialog } from "./components/ui/dialog-context";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type Row = Record<string, unknown>;

const num = (v: unknown): number => {
  const n = typeof v === "string" ? Number(v) : (v as number);
  return typeof n === "number" && Number.isFinite(n) ? n : 0;
};

const INR = (n: number, frac = 0) =>
  `\u20b9${n.toLocaleString("en-IN", { minimumFractionDigits: frac, maximumFractionDigits: frac })}`;

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Empty({ icon: Icon, title, hint }: { icon: React.ElementType; title: string; hint?: string }) {
  return (
    <div className="grid place-items-center gap-2 rounded-md border border-dashed border-border bg-muted/15 px-6 py-10 text-center">
      <div className="grid h-10 w-10 place-items-center rounded-xl border border-border bg-card text-muted-foreground">
        <Icon size={18} />
      </div>
      <div className="text-sm font-semibold text-foreground">{title}</div>
      {hint ? <div className="max-w-xs text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

function PnLCell({ value, pct, size = "sm" }: { value: number; pct?: number; size?: "sm" | "md" | "lg" }) {
  const positive = value >= 0;
  const cls =
  size === "lg" ? "text-lg font-semibold tabular-nums tracking-tight"
  : size === "md" ? "text-sm font-semibold tabular-nums"
        : "text-body font-semibold tabular-nums";
  const colour = positive ? "text-gain" : "text-destructive";
  return (
    <span className={cn("inline-flex items-center gap-1", colour, cls)}>
      {positive ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
      {INR(Math.abs(value))}
      {typeof pct === "number" ? (
        <span className="text-caption font-medium opacity-80">({pct >= 0 ? "+" : ""}{pct.toFixed(2)}%)</span>
      ) : null}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const s = status.toUpperCase();
  const tone =
    s.includes("REJECT") || s.includes("CANCEL") ? "bad"
    : s.includes("COMPLETE") || s.includes("FILLED") || s === "TRADED" ? "good"
    : s.includes("PENDING") || s.includes("OPEN") || s.includes("PARTIAL") ? "warn"
    : "flat";
  return <Badge tone={tone}>{status}</Badge>;
}

// ---------------------------------------------------------------------------
// Inline "stale data" notice used in tab content
// ---------------------------------------------------------------------------

function StaleNotice({ onRetry }: { lastUpdated: Date | null; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-center">
      <Clock size={18} className="text-muted-foreground" />
      <div className="text-sm text-muted-foreground">Your broker data isn't available right now.</div>
      <Button variant="secondary" size="sm" onClick={onRetry}>Try again</Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Hero KPI strip
// ---------------------------------------------------------------------------

function KpiStrip({
  data, error, lastUpdated, getTick,
}: {
  data: PortfolioResponse | null;
  error: string | null;
  lastUpdated: Date | null;
  getTick: (sym: string) => LiveTick | undefined;
}) {
  const kpis = useMemo(() => {
    const limits = data?.sections?.limits?.rows?.[0] ?? null;
    const holdings = data?.sections?.holdings?.rows ?? [];
    const positions = data?.sections?.positions?.rows ?? [];

    const tradingLimit = num(limits?.tradingLimit);
    const utilized = num(limits?.utilizedMargin);
    const spanMargin = num(limits?.utilizedSpanMargin);
    const exposureMargin = num(limits?.utilizedExposureMargin);
    const openingCash = num(limits?.openingCashLimit);
    const intradayPayin = num(limits?.intradayPayin);
    const creditForSell = num(limits?.creditForSell);
    const blockedForPayout = num(limits?.blockedForPayout);

    const cashFunds = openingCash + intradayPayin + creditForSell - utilized - blockedForPayout;

    const holdingsInvested = holdings.reduce((s, h) => s + num(h.totalQuantity) * num(h.averageTradedPrice), 0);
    const holdingsAtClose = holdings.reduce((s, h) => {
      const sym = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "");
      const px = priceOf(h, getTick(sym));
      return s + num(h.totalQuantity) * px;
    }, 0);

    const positionsDailyPnl = positions.reduce((s, p) => {
      const sym = String(p.symbol ?? p.tradingSymbol ?? "");
      const tick = getTick(sym);
      const ltp = tick?.ltp ?? 0;
      const prev = num(p.prev_close ?? p.previous_close ?? p.previousClose ?? p.prevClose ?? p.close ?? 0);
      return s + (ltp - prev) * num(p.quantity);
    }, 0);

    // What the holdings moved today: the price now against yesterday's close. A holding with
    // no live price is valued at that close, so it adds nothing rather than a made-up move.
    const holdingsDailyPnl = holdings.reduce((s, h) => {
      const sym = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "");
      const prev = num(h.previousDayClose);
      return prev > 0 ? s + num(h.totalQuantity) * (priceOf(h, getTick(sym)) - prev) : s;
    }, 0);

    return {
      tradingLimit,
      utilized: utilized + spanMargin + exposureMargin,
      cashFunds,
      blockedForPayout,
      holdingsInvested,
      holdingsAtClose,
      holdingsCount: holdings.length,
      positionsCount: positions.length,
      pnl: positionsDailyPnl + holdingsDailyPnl,
    };
  }, [data, getTick]);

  const noData = !data && !error;

  return (
    <>
      {error && data && lastUpdated && (
        <div className="flex items-center gap-1.5 px-3 pt-1 pb-2 text-caption text-warning/80">
          <Clock size={10} />
          <span>
            Market data delayed · Last updated{" "}
            {lastUpdated.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </span>
        </div>
      )}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat
          label="Available Margin"
          value={INR(noData ? 0 : kpis.tradingLimit, 2)}
          sub={noData ? "Loading\u2026" : `${INR(kpis.utilized, 2)} utilized`}
          hint="Funds and margin available for placing new orders"
        />
        <Stat
          label="Cash Balance"
          value={INR(noData ? 0 : kpis.cashFunds, 2)}
          sub={kpis.blockedForPayout > 0 ? `${INR(kpis.blockedForPayout, 2)} blocked` : "Free funds"}
          hint="Liquid cash in trading account"
        />
        <Stat
          label="Holdings Value"
          value={INR(noData ? 0 : kpis.holdingsAtClose, 2)}
          sub={noData ? "Loading\u2026" : `${INR(kpis.holdingsInvested, 2)} invested`}
          hint="Current market value of demat holdings"
        />
        <Stat
          label="Day P&L"
          value={INR(kpis.pnl, 2)}
          sub={`${kpis.holdingsCount} holdings · ${kpis.positionsCount} open pos`}
          tone={kpis.pnl > 0 ? "good" : kpis.pnl < 0 ? "bad" : "neutral"}
          hint="Day movement — since yesterday's close"
        />
        </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab: Limits
// ---------------------------------------------------------------------------

function LimitRow({ label, value, hint, emphasize, tone }: {
  label: string; value: number; hint?: string; emphasize?: boolean; tone?: "good" | "bad" | "warn" | "neutral";
}) {
  const content = (
    <div className={cn("flex items-center justify-between gap-3 py-2 transition-colors", emphasize && "border-b border-border/60 pb-2.5")}>
      <div className="flex items-center gap-1.5 min-w-0">
        <span className={cn("truncate", emphasize ? "text-sm font-semibold text-foreground" : "text-xs text-muted-foreground")}>{label}</span>
        {hint ? <span className="cursor-help rounded-full bg-muted px-1.5 py-0.2 text-micro text-muted-foreground/70">?</span> : null}
      </div>
      <div className={cn("shrink-0 tabular-nums font-medium", emphasize ? "text-base font-semibold text-foreground" : "text-xs",
          tone === "good" && "text-gain",
        tone === "bad" && "text-destructive",
        tone === "warn" && "text-warning")}>
        {INR(value)}
      </div>
    </div>
  );
  if (hint) return <Tooltip content={hint} wrapperClassName="w-full block">{content}</Tooltip>;
  return content;
}

function LimitsGroup({ title, icon: Icon, badge, children, className }: {
  title: string; icon: React.ElementType; badge?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  return (
    <div className={cn("rounded-xl border border-border bg-card p-4.5", className)}>
      <div className="mb-3 flex items-center justify-between gap-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
        <span className="flex items-center gap-2">
          <span className="grid h-5 w-5 place-items-center rounded-md bg-primary/[0.09] text-primary"><Icon size={11} /></span>
          {title}
        </span>
        {badge}
      </div>
      <div className="divide-y divide-border/40">{children}</div>
    </div>
  );
}

function LimitsView({ row }: { row: Row | null }) {
  if (!row) return <Empty icon={Wallet} title="No limits returned" />;
  const tradingLimit = num(row.tradingLimit);
  const openingCash = num(row.openingCashLimit);
  const collateral = num(row.collateralMargin);
  const adhoc = num(row.adhocMargin);
  const utilized = num(row.utilizedMargin);
  const spanMargin = num(row.utilizedSpanMargin);
  const exposureMargin = num(row.utilizedExposureMargin);
  const intradayPayin = num(row.intradayPayin);
  const blocked = num(row.blockedForPayout);
  const credit = num(row.creditForSell);
  const totalDeployed = utilized + spanMargin + exposureMargin;
  const totalPower = tradingLimit + totalDeployed;
  const deploymentPct = totalPower > 0 ? (totalDeployed / totalPower) * 100 : 0;

  const technicalItems: BouncyAccordionItem[] = [
    {
      id: "fo_margins",
      title: "Derivatives & Exposure Margins",
      icon: <LineChart className="h-4 w-4" />,
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40"><span>SPAN Margin (F&O blocked)</span><span className="font-semibold text-foreground">{INR(spanMargin)}</span></div>
          <div className="flex justify-between py-1"><span>Exposure Margin</span><span className="font-semibold text-foreground">{INR(exposureMargin)}</span></div>
        </div>
      ),
    },
    {
      id: "cash_movements",
      title: "Cash Inflow, Outflow & Payouts",
      icon: <Banknote className="h-4 w-4" />,
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40"><span>Opening Cash</span><span className="font-semibold text-foreground">{INR(openingCash)}</span></div>
          <div className="flex justify-between py-1 border-b border-border/40"><span>Intraday Pay-in</span><span className="font-semibold text-gain">+{INR(intradayPayin)}</span></div>
          <div className="flex justify-between py-1 border-b border-border/40"><span>Credit from Sells</span><span className="font-semibold text-gain">+{INR(credit)}</span></div>
          <div className="flex justify-between py-1"><span>Blocked for Payout</span><span className="font-semibold text-destructive">-{INR(blocked)}</span></div>
        </div>
      ),
    },
    {
      id: "collateral_adhoc",
      title: "Collateral & Ad-hoc Margins",
      icon: <Coins className="h-4 w-4" />,
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40"><span>Pledged Collateral</span><span className="font-semibold text-foreground">{INR(collateral)}</span></div>
          <div className="flex justify-between py-1"><span>Ad-hoc Margin Granted</span><span className="font-semibold text-foreground">{INR(adhoc)}</span></div>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        <LimitsGroup title="Margin & Buying Capacity" icon={LineChart}
          badge={<Badge tone={deploymentPct > 80 ? "warn" : deploymentPct > 0 ? "good" : "flat"}>{deploymentPct.toFixed(1)}% deployed</Badge>}>
          <LimitRow label="Utilized Margin (Active)" value={totalDeployed} emphasize tone={totalDeployed > 0 ? "warn" : "neutral"} hint="Total active margin consumed by equity and derivatives positions." />
          <LimitRow label="Equity Utilized" value={utilized} hint="Margin consumed specifically by equity intraday trades." />
          <LimitRow label="F&O Blocked" value={spanMargin + exposureMargin} hint="SPAN + exposure margin currently blocked for open derivatives." />
        </LimitsGroup>
        <LimitsGroup title="Capital Composition" icon={Wallet}
          badge={<span className="text-xs text-muted-foreground">{collateral > 0 ? "Cash + Pledged" : "100% Cash"}</span>}>
          <LimitRow label="Pledged Collateral" value={collateral} emphasize hint="Margin value obtained from pledged shares/securities." />
          <LimitRow label="Opening Cash" value={openingCash} hint="Starting cash balance at the beginning of the trading day." />
          <LimitRow label="Ad-hoc Margin" value={adhoc} hint="Special discretionary margin extended by the broker." />
        </LimitsGroup>
      </div>
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="mb-3 flex items-center justify-between">
        <div className="text-caption font-semibold uppercase tracking-[0.07em] text-muted-foreground">Granular Breakdown & Ledger Flow</div>
          <span className="text-caption text-muted-foreground/75">Click to inspect</span>
        </div>
        <BouncyAccordion items={technicalItems} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Positions
// ---------------------------------------------------------------------------

function positionPnl(p: Row, tick?: LiveTick): number {
  const ltp = tick?.ltp ?? 0;
  const avg = num(p.avg_price);
  const qty = num(p.quantity);
  const explicit = num(p.unrealized_pnl);
  return tick?.ltp ? (tick.ltp - avg) * qty : explicit !== 0 ? explicit : (ltp - avg) * qty;
}

function PositionsView({ rows, getTick }: { rows: Row[]; getTick: (sym: string) => LiveTick | undefined }) {
  const columns = useMemo<TableColumn<Row>[]>(() => [
    {
      key: "instrument", header: "Instrument", sortable: true, width: "2fr",
      sortValue: (r) => String(r.symbol ?? r.tradingSymbol ?? ""),
      cell: (r) => {
        const sym = String(r.symbol ?? r.tradingSymbol ?? "?");
        const exch = String(r.exchange ?? "NSEEQ");
        const tick = getTick(sym);
        return (
          <div className="flex items-center gap-2 py-1">
            <span className="font-semibold text-foreground hover:text-primary transition-colors">{sym}</span>
              <span className="text-caption text-muted-foreground">{exch}</span>
              {tick && <span className="h-1.5 w-1.5 rounded-full bg-gain animate-pulse" title="Live" />}
          </div>
        );
      },
    },
    {
      key: "side", header: "Side", sortable: true, width: "100px",
      sortValue: (r) => (num(r.quantity) > 0 ? "LONG" : num(r.quantity) < 0 ? "SHORT" : "FLAT"),
      cell: (r) => { const qty = num(r.quantity); return <Badge tone={qty > 0 ? "good" : qty < 0 ? "bad" : "flat"}>{qty === 0 ? "FLAT" : qty > 0 ? "LONG" : "SHORT"}</Badge>; },
    },
    {
      key: "qty", header: "Qty", sortable: true, align: "right", width: "100px",
      sortValue: (r) => num(r.quantity),
      cell: (r) => <span className="tabular-nums font-medium text-foreground">{num(r.quantity).toLocaleString("en-IN")}</span>,
    },
    {
      key: "avg", header: "Avg", sortable: true, align: "right", width: "110px",
      sortValue: (r) => num(r.avg_price),
      cell: (r) => <span className="tabular-nums text-muted-foreground">{INR(num(r.avg_price), 2)}</span>,
    },
    {
      key: "ltp", header: "LTP", sortable: true, align: "right", width: "110px",
      sortValue: (r) => { const sym = String(r.symbol ?? r.tradingSymbol ?? ""); return getTick(sym)?.ltp ?? 0; },
      cell: (r) => {
        const sym = String(r.symbol ?? r.tradingSymbol ?? "");
        const tick = getTick(sym);
        const ltp = tick?.ltp ?? 0;
        return <span className={cn("tabular-nums font-semibold transition-colors duration-300", tick?.flash === "up" && "text-gain", tick?.flash === "down" && "text-destructive", !tick?.flash && "text-foreground")}>{ltp > 0 ? INR(ltp, 2) : <span className="text-muted-foreground/50">—</span>}</span>;
      },
    },
    {
      key: "pnl", header: "Unrealized P&L", sortable: true, align: "right", width: "140px",
      sortValue: (r) => { const sym = String(r.symbol ?? r.tradingSymbol ?? ""); return positionPnl(r, getTick(sym)); },
      cell: (r) => { const sym = String(r.symbol ?? r.tradingSymbol ?? ""); return <PnLCell value={positionPnl(r, getTick(sym))} size="sm" />; },
    },
    {
      key: "pnl_pct", header: "P&L %", sortable: true, align: "right", width: "100px",
      sortValue: (r) => { const sym = String(r.symbol ?? r.tradingSymbol ?? ""); const tick = getTick(sym); const avg = num(r.avg_price); const ltp = tick?.ltp ?? 0; return avg > 0 ? ((ltp - avg) / avg) * 100 : 0; },
      cell: (r) => {
        const sym = String(r.symbol ?? r.tradingSymbol ?? ""); const tick = getTick(sym);
        const avg = num(r.avg_price); const ltp = tick?.ltp ?? 0;
        if (!tick || ltp === 0) return <span className="text-muted-foreground/50 tabular-nums text-[12.5px]">—</span>;
        const pct = avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
        return <span className={cn("inline-flex items-center tabular-nums font-semibold text-[12.5px]", pct >= 0 ? "text-gain" : "text-destructive")}>{pct >= 0 ? "+" : ""}{pct.toFixed(2)}%</span>;
      },
    },
  ], [getTick]);

  if (rows.length === 0) return <Empty icon={Activity} title="No open positions" />;

  return (
    <div className="overflow-hidden rounded-xl border border-border/80 bg-card/20 -xs">
      <Table data={rows} columns={columns} getRowId={(r, i) => String(r.symbol ?? r.tradingSymbol ?? i)} resizable reorderable
      defaultSort={{ key: "pnl", direction: "desc" }} height={Math.min(480, Math.max(160, rows.length * 52 + 48))} rowHeight={52} className="rounded-md border-none" />
    </div>
  );
}

/**
 * The price a holding is valued at: the broker's last traded price, else the live tick, else the
 * last close the broker reported. A holding with neither is not worth zero, and counting it that
 * way while still counting what it cost shows a loss that is not there.
 */
function priceOf(row: Row, tick?: LiveTick): number {
  // The live WebSocket tick is pushed the instant a trade prints — the same
  // mechanism a broker terminal uses — so it's always fresher than `row.ltp`,
  // which only updates on the ~1.5s account-state poll. Preferring `row.ltp`
  // here (the original order) meant every price on this page only ever moved
  // on that poll, silently ignoring the live stream this page already
  // subscribes every held symbol to. Only fall back to the polled snapshot,
  // then yesterday's close, when no live tick has arrived yet.
  const live = tick?.ltp ?? 0;
  if (live > 0) return live;
  const quoted = num(row.ltp);
  return quoted > 0 ? quoted : num(row.previousDayClose);
}

// ---------------------------------------------------------------------------
// Tab: Holdings
// ---------------------------------------------------------------------------

function HoldingCard({ row, tick }: { row: Row; tick?: LiveTick }) {
  const symbol = String(row.nseTradingSymbol ?? row.bseTradingSymbol ?? row.symbol ?? "?");
  const name = String(row.formattedInstrumentName ?? "").trim();
  const qty = num(row.totalQuantity);
  const avg = num(row.averageTradedPrice);
  const lastPrice = priceOf(row, tick);
  const invested = qty * avg;
  const currentVal = qty * lastPrice;
  const delta = currentVal - invested;
  const pct = avg > 0 ? ((lastPrice - avg) / avg) * 100 : 0;
  const product = String(row.product ?? "");

  return (
    <motion.div whileHover={{ y: -1 }} transition={{ type: "spring", stiffness: 380, damping: 28 }}
    className="rounded-xl border border-border bg-card p-4 transition-colors">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="truncate text-sm font-semibold tracking-tight">{symbol}</div>
            {product ? <Badge tone="flat">{product}</Badge> : null}
            {tick && <span className="flex items-center gap-1 text-[10.5px] font-medium text-gain"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gain" />Live</span>}
          </div>
          {name ? <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground" title={name}>{name}</div> : null}
        </div>
        <div className="text-right">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">Qty</div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">{qty.toLocaleString("en-IN")}</div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3 border-t border-border/60 pt-3">
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">Invested</div>
          <div className="mt-0.5 text-body font-semibold tabular-nums">{INR(invested)}</div>
          <div className="text-[10.5px] text-muted-foreground/70">@ {INR(avg, 2)}</div>
        </div>
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">Current</div>
          {lastPrice > 0 ? (
            <>
            <div className={cn("mt-0.5 text-body font-semibold tabular-nums transition-colors duration-300", tick?.flash === "up" && "text-gain", tick?.flash === "down" && "text-destructive")}>{INR(currentVal)}</div>
              <div className="text-[10.5px] text-muted-foreground/70">@ {INR(lastPrice, 2)}</div>
            </>
          ) : (
            <div className="mt-0.5 text-body font-semibold text-muted-foreground/50">—</div>
          )}
        </div>
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">Δ vs avg</div>
          <div className="mt-0.5">{lastPrice > 0 ? <PnLCell value={delta} pct={pct} /> : <span className="text-muted-foreground/50 text-body">—</span>}</div>
        </div>
      </div>
    </motion.div>
  );
}

function HoldingsView({ rows, getTick }: { rows: Row[]; getTick: (sym: string) => LiveTick | undefined }) {
  const [query, setQuery] = useState("");
  const [selectedLots, setSelectedLots] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<"table" | "grid">(() => (localStorage.getItem("atr.holdings.view") as "table" | "grid") || "table");

  const toggleView = (mode: "table" | "grid") => { setViewMode(mode); localStorage.setItem("atr.holdings.view", mode); };

  const totalPortfolioValue = useMemo(() =>
    rows.reduce((s, h) => {
      const sym = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "");
      return s + num(h.totalQuantity) * priceOf(h, getTick(sym));
    }, 0), [rows, getTick]);

  const columns = useMemo<TableColumn<Row>[]>(() => [
    {
      key: "instrument", header: "Instrument", sortable: true, width: "2.2fr",
      sortValue: (r) => String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? ""),
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "?");
        const name = String(r.formattedInstrumentName ?? "");
        const tick = getTick(sym);
        return (
          <div className="flex flex-col justify-center min-w-0 py-1">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-foreground hover:text-primary transition-colors">{sym}</span>
                {tick && <span className="h-1.5 w-1.5 rounded-full bg-gain animate-pulse" />}
            </div>
            {name && <span className="text-caption text-muted-foreground/75 truncate" title={name}>{name}</span>}
          </div>
        );
      },
    },
    {
      key: "qty", header: "Qty", sortable: true, align: "right", width: "90px",
      sortValue: (r) => num(r.totalQuantity),
      cell: (r) => <span className="tabular-nums font-medium text-foreground/90">{num(r.totalQuantity).toLocaleString("en-IN")}</span>,
    },
    {
      key: "avg", header: "Avg Price", sortable: true, align: "right", width: "110px",
      sortValue: (r) => num(r.averageTradedPrice),
      cell: (r) => <span className="tabular-nums text-muted-foreground">{INR(num(r.averageTradedPrice), 2)}</span>,
    },
    {
      key: "ltp", header: "LTP", sortable: true, align: "right", width: "110px",
      sortValue: (r) => { const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? ""); return priceOf(r, getTick(sym)); },
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const tick = getTick(sym);
        const ltp = priceOf(r, tick);
        return <span className={cn("tabular-nums font-semibold transition-colors duration-300", tick?.flash === "up" && "text-gain", tick?.flash === "down" && "text-destructive", !tick?.flash && "text-foreground")}>{ltp > 0 ? INR(ltp, 2) : <span className="text-muted-foreground/50">—</span>}</span>;
      },
    },
    {
      key: "value", header: "Value", sortable: true, align: "right", width: "120px",
      sortValue: (r) => { const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? ""); return num(r.totalQuantity) * priceOf(r, getTick(sym)); },
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const ltp = priceOf(r, getTick(sym));
        return <span className="tabular-nums font-semibold text-foreground">{ltp > 0 ? INR(num(r.totalQuantity) * ltp) : <span className="text-muted-foreground/50">—</span>}</span>;
      },
    },
    {
      key: "alloc_pct", header: "Alloc %", sortable: true, align: "right", width: "90px",
      sortValue: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const val = num(r.totalQuantity) * priceOf(r, getTick(sym));
        return totalPortfolioValue > 0 ? (val / totalPortfolioValue) * 100 : 0;
      },
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const ltp = priceOf(r, getTick(sym));
        if (ltp === 0) return <div className="flex flex-col items-end gap-0.5"><span className="text-muted-foreground/50 text-xs">—</span></div>;
        const val = num(r.totalQuantity) * ltp;
        const pct = totalPortfolioValue > 0 ? (val / totalPortfolioValue) * 100 : 0;
        return (
          <div className="flex flex-col items-end gap-0.5">
          <span className="tabular-nums text-xs font-semibold text-foreground/80">{pct.toFixed(1)}%</span>
            <div className="h-1 w-14 rounded-full bg-border/50 overflow-hidden">
              <div className="h-full rounded-full bg-primary/60 transition-all duration-500" style={{ width: `${Math.min(100, pct)}%` }} />
            </div>
          </div>
        );
      },
    },
    {
      key: "pnl", header: "Unrealized P&L", sortable: true, align: "right", width: "130px",
      sortValue: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const ltp = priceOf(r, getTick(sym));
        return num(r.totalQuantity) * ltp - num(r.totalQuantity) * num(r.averageTradedPrice);
      },
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const ltp = priceOf(r, getTick(sym));
        if (ltp === 0) return <span className="text-muted-foreground/50 tabular-nums text-body">—</span>;
        return <PnLCell value={num(r.totalQuantity) * ltp - num(r.totalQuantity) * num(r.averageTradedPrice)} size="sm" />;
      },
    },
    {
      key: "pnl_pct", header: "P&L %", sortable: true, align: "right", width: "95px",
      sortValue: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const avg = num(r.averageTradedPrice); const ltp = priceOf(r, getTick(sym));
        return avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
      },
      cell: (r) => {
        const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
        const avg = num(r.averageTradedPrice); const ltp = priceOf(r, getTick(sym));
        if (ltp === 0) return <span className="text-muted-foreground/50 tabular-nums text-[12.5px]">—</span>;
        const pct = avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
        return <span className={cn("inline-flex tabular-nums font-semibold text-[12.5px]", pct >= 0 ? "text-gain" : "text-destructive")}>{pct >= 0 ? "+" : ""}{pct.toFixed(2)}%</span>;
      },
    },
  ], [getTick, totalPortfolioValue]);

  if (rows.length === 0) return <Empty icon={Briefcase} title="No holdings" />;

  const totalInvested = rows.reduce((s, h) => s + num(h.totalQuantity) * num(h.averageTradedPrice), 0);
  const totalDelta = totalPortfolioValue - totalInvested;
  const totalPct = totalInvested > 0 ? (totalDelta / totalInvested) * 100 : 0;

  const filtered = rows.filter((r) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "").toLowerCase().includes(q)
      || String(r.formattedInstrumentName ?? "").toLowerCase().includes(q);
  });

  return (
    <div className="space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-3.5 pt-1">
        <div className="flex items-center gap-2.5">
          <span className="text-body font-medium text-muted-foreground">Unrealized:</span>
          <PnLCell value={totalDelta} pct={totalPct} size="md" />
          {selectedLots.length > 0 && (
            <><span className="text-xs text-muted-foreground/60">·</span>
            <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-semibold text-primary">{selectedLots.length} selected</span></>
          )}
        </div>
        <div className="flex items-center gap-2.5">
          <div className="w-52">
            <Input
              value={query}
              onChange={(val) => setQuery(val)}
              placeholder="Search symbols…"
              className="text-sm"
              classNames={{ field: "h-12" }}
            />
          </div>
          <Tabs value={viewMode} onValueChange={(v) => toggleView(v as "table" | "grid")} variant="pill">
            <TabsList>
            <TabsTrigger value="table" className="h-10 px-4 text-xs">Table</TabsTrigger>
            <TabsTrigger value="grid" className="h-10 px-4 text-xs">Cards</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
      </div>

      {viewMode === "table" ? (
        <div className="overflow-hidden rounded-xl border border-border/80 bg-card/20 -xs">
          <Table data={filtered} columns={columns}
            getRowId={(r, i) => String(r.isin ? `${r.isin}-${r.product ?? "DEL"}-${i}` : (r.nseTradingSymbol ?? r.symbol ?? i))}
            selectable resizable reorderable selectedRowIds={selectedLots} onSelectionChange={setSelectedLots}
            defaultSort={{ key: "value", direction: "desc" }}
            height={Math.min(540, Math.max(160, filtered.length * 52 + 48))} rowHeight={52} className="rounded-md border-none" />
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {filtered.map((r, i) => {
            const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
            return <HoldingCard key={i} row={r} tick={getTick(sym)} />;
          })}
        </div>
      )}
      {filtered.length === 0 && <Empty icon={Briefcase} title="No matching holdings" />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Orders / Trades
// ---------------------------------------------------------------------------

function pickString(row: Row, ...keys: string[]): string {
  for (const k of keys) { const v = row[k]; if (v !== null && v !== undefined && v !== "") return String(v); }
  return "\u2014";
}

function OrdersView({ rows }: { rows: Row[] }) {
  if (rows.length === 0) return <Empty icon={ListOrdered} title="No orders today" />;
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-body">
        <thead><tr className="bg-muted/40 text-[10.5px] uppercase tracking-[0.07em] text-muted-foreground">
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Time</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Symbol</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Side</th>
          <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Qty</th>
          <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Price</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Type</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
        </tr></thead>
        <tbody>{rows.map((r, i) => {
          const side = pickString(r, "transactionType", "TransactionType", "side", "Side").toUpperCase();
          return (
            <tr key={i} className="border-t border-border/60 transition-colors hover:bg-primary/[0.03]">
              <td className="px-3 py-1.5 whitespace-nowrap font-mono text-[11.5px] text-muted-foreground">{pickString(r, "orderDateTime", "exchangeTimestamp", "ExchangeTimestamp", "updatedAt", "CreatedAt")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap font-semibold">{pickString(r, "tradingSymbol", "TradingSymbol", "symbol")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap"><Badge tone={side.startsWith("B") ? "good" : "bad"}>{side || "BUY"}</Badge></td>
              <td className="px-3 py-1.5 whitespace-nowrap text-right tabular-nums">{fmtNum(r.quantity ?? r.Quantity)}</td>
              <td className="px-3 py-1.5 whitespace-nowrap text-right tabular-nums">{fmtNum(r.price ?? r.Price)}</td>
              <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground">{pickString(r, "orderComplexity", "OrderComplexity", "product", "Product")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap"><StatusPill status={pickString(r, "orderStatus", "OrderStatus", "status", "Status")} /></td>
            </tr>
          );
        })}</tbody>
      </table>
    </div>
  );
}

function TradesView({ rows }: { rows: Row[] }) {
  if (rows.length === 0) return <Empty icon={BarChart3} title="No trades today" />;
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-body">
        <thead><tr className="bg-muted/40 text-[10.5px] uppercase tracking-[0.07em] text-muted-foreground">
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Time</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Symbol</th>
          <th className="px-3 py-2 text-left text-micro font-semibold uppercase tracking-wider text-muted-foreground">Side</th>
          <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Qty</th>
          <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Price</th>
          <th className="px-3 py-2 text-right text-micro font-semibold uppercase tracking-wider text-muted-foreground">Value</th>
        </tr></thead>
        <tbody>{rows.map((r, i) => {
          const side = pickString(r, "transactionType", "TransactionType", "side", "Side").toUpperCase();
          const qty = num(r.quantity ?? r.Quantity);
          const price = num(r.tradePrice ?? r.TradePrice ?? r.price ?? r.Price);
          return (
            <tr key={i} className="border-t border-border/60 transition-colors hover:bg-primary/[0.03]">
              <td className="px-3 py-1.5 whitespace-nowrap font-mono text-[11.5px] text-muted-foreground">{pickString(r, "exchangeTimestamp", "ExchangeTimestamp", "tradeTime", "TradeTime", "orderDateTime")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap font-semibold">{pickString(r, "tradingSymbol", "TradingSymbol", "symbol")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap"><Badge tone={side.startsWith("B") ? "good" : "bad"}>{side || "BUY"}</Badge></td>
              <td className="px-3 py-1.5 whitespace-nowrap text-right tabular-nums">{qty.toLocaleString("en-IN")}</td>
              <td className="px-3 py-1.5 whitespace-nowrap text-right tabular-nums">{INR(price, 2)}</td>
              <td className="px-3 py-1.5 whitespace-nowrap text-right font-semibold tabular-nums">{INR(qty * price)}</td>
            </tr>
          );
        })}</tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Execute (Order Ticket + Market Depth side by side)
// ---------------------------------------------------------------------------

function ExecuteTab({ availableMargin }: { availableMargin: number }) {
  const { toast } = useToast();
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [symbol, setSymbol] = useState("RELIANCE");
  const [exchange, setExchange] = useState("NSEEQ");
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState("MARKET");
  const [price, setPrice] = useState("");
  const [state, setState] = useState<ButtonState>("idle");

  const formattedSymbol = `${symbol.trim().toUpperCase()}${symbol.includes("-") ? "" : "-EQ"}`;
  const { getTick } = useLiveTicks(useMemo(() => [formattedSymbol], [formattedSymbol]));
  const tick = getTick(formattedSymbol);

  const [matches, setMatches] = useState<{ symbol: string; exchange: string }[]>([]);
  const [quoteBy, setQuoteBy] = useState<Record<string, { ltp: number; chg_pct: number }>>({});
  const [showMatches, setShowMatches] = useState(false);

  useEffect(() => {
    const q = symbol.trim().toUpperCase();
    if (q.length < 2 || q.includes("-")) { setMatches([]); setQuoteBy({}); return; }
    let cancelled = false;
    const t = window.setTimeout(() => {
      searchSymbols(q, exchange || "NSEEQ", 8).then((r) => { if (!cancelled) setMatches(r.results || []); }).catch(() => { if (!cancelled) setMatches([]); });
    }, 180);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [symbol, exchange]);

  useEffect(() => {
    if (matches.length === 0) { setQuoteBy({}); return; }
    let cancelled = false;
    const t = window.setTimeout(async () => {
      try {
        const res = await getQuote(matches.map((m) => m.symbol).join(","), "NSEEQ");
        const map: Record<string, { ltp: number; chg_pct: number }> = {};
        for (const q of res.quotes || []) {
          const sym = String((q as Record<string, unknown>).symbol || "");
          if (!sym) continue;
          map[sym] = { ltp: num(q.ltp), chg_pct: num((q as Record<string, unknown>).chg_pct) };
        }
        if (!cancelled) setQuoteBy(map);
      } catch { if (!cancelled) setQuoteBy({}); }
    }, 120);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [matches]);

  const isLimit = orderType !== "MARKET" && orderType !== "SLM";
  const ltp = tick?.ltp ?? 0;
  const estimatedPrice = (isLimit && price) ? Number(price) : ltp;
  const estimatedValue = estimatedPrice > 0 ? qty * estimatedPrice : 0;
  const marginRequired = estimatedValue * 0.2;
  const marginPct = availableMargin > 0 ? (marginRequired / availableMargin) * 100 : 0;
  const canAfford = marginRequired <= availableMargin || availableMargin === 0;

  // A real order: confirmed first, with an id the server de-duplicates on so a double click cannot place two.
  const confirmOrder = useOrderConfirm();
  async function submit() {
    const ok = await confirmOrder.ask({
      symbol: symbol.trim().toUpperCase(),
      exchange: exchange.trim().toUpperCase(),
      side,
      quantity: Math.abs(qty),
      orderType,
      price: estimatedPrice > 0 ? estimatedPrice : null,
      priceSource: isLimit && price ? "limit" : "last",
    });
    if (!ok) return;
    setState("loading");
    try {
      const res = await placeOrder({
        symbol: symbol.trim().toUpperCase(),
        exchange: exchange.trim().toUpperCase(),
        quantity: side === "SELL" ? -Math.abs(qty) : Math.abs(qty),
        order_type: orderType,
        price: price === "" ? null : Number(price),
        client_order_id: ok.clientOrderId,
      });
      setState("success");
      res.status === "rejected" ? sound.playReject() : sound.playFill();
      toast({ title: `Order ${res.order_id} \u2192 ${res.status}${res.duplicate ? " (already placed)" : ""}`, description: res.reject_reason ?? `${symbol.trim().toUpperCase()} \u00d7 ${qty} ${orderType}`, status: res.status === "rejected" ? "error" : "success" });
    } catch (e) {
      setState("error"); sound.playReject();
      toast({ title: "Order failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_320px] xl:grid-cols-[1fr_360px]">
      {confirmOrder.dialog}
      {/* Left: Order Ticket */}
      <div className="space-y-4">
        {/* BUY / SELL toggle */}
        <div className="flex items-center gap-1.5 rounded-lg border border-border/60 bg-muted/20 p-1">
          <Button
            type="button"
            variant={side === "BUY" ? "primary" : "ghost"}
            size="md"
            onClick={() => setSide("BUY")}
            className={cn("flex-1 font-semibold", side === "BUY" ? "bg-gain hover:bg-gain/90 text-white" : "text-muted-foreground")}
          >
          <TrendingUp size={14} className="mr-1.5 inline" />BUY
          </Button>
          <Button
            type="button"
            variant={side === "SELL" ? "primary" : "ghost"}
            size="md"
            onClick={() => setSide("SELL")}
            className={cn("flex-1 font-semibold", side === "SELL" ? "bg-destructive hover:bg-destructive/90 text-white" : "text-muted-foreground")}
          >
          <TrendingDown size={14} className="mr-1.5 inline" />SELL
          </Button>
        </div>

        {/* Symbol + Exchange */}
        <div className="grid grid-cols-[1fr_140px] gap-3">
          <div className="relative">
            <Input label="Symbol" value={symbol}
              onChange={(v) => { setSymbol(v); setShowMatches(true); }}
              onFocus={() => setShowMatches(matches.length > 0)}
              onBlur={() => window.setTimeout(() => setShowMatches(false), 150)}
              placeholder="RELIANCE" autoComplete="off" />
            {showMatches && matches.length > 0 && (
              <div data-lenis-prevent className="absolute z-30 mt-1 max-h-56 w-full overflow-auto rounded-xl border border-border bg-card ">
                {matches.map((m) => {
                  const q = quoteBy[m.symbol]; const pct = q?.chg_pct; const positive = typeof pct === "number" && pct >= 0;
                  return (
                    <button key={`${m.exchange}:${m.symbol}`} type="button"
                      onMouseDown={(e) => { e.preventDefault(); setSymbol(m.symbol); setShowMatches(false); }}
                      className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-[12.5px] hover:bg-primary/[0.06]">
                      <span className="flex items-center gap-2"><span className="font-semibold text-foreground">{m.symbol}</span><span className="text-[10.5px] text-muted-foreground">{m.exchange}</span></span>
                      <span className="flex items-center gap-2 tabular-nums">
                      {q && q.ltp > 0 ? (<><span className="font-semibold text-foreground">₹{q.ltp.toFixed(2)}</span>{typeof pct === "number" && Number.isFinite(pct) && <span className={positive ? "text-gain" : "text-destructive"}>{positive ? "+" : ""}{pct.toFixed(2)}%</span>}</>) : <span className="text-muted-foreground/70 text-[10.5px]">· · ·</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Exchange</label>
            <Select
              value={exchange}
              onChange={setExchange}
              options={[
                { value: "NSEEQ", label: "NSEEQ" },
                { value: "BSEEQ", label: "BSEEQ" },
                { value: "NSEFO", label: "NSEFO" },
              ]}
            />
          </div>
        </div>

        {/* Qty + Type + Price */}
        <div className="grid grid-cols-3 gap-3">
          <Input label="Quantity" type="number" value={String(qty)} onChange={(v) => setQty(Math.max(0, Number(v) || 0))} />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Order Type</label>
            <Select
              value={orderType}
              onChange={setOrderType}
              options={[
                { value: "MARKET", label: "MARKET" },
                { value: "LIMIT", label: "LIMIT" },
                { value: "SL", label: "SL (stop-loss)" },
                { value: "SLM", label: "SL-MKT" },
              ]}
            />
          </div>
          <Input label={isLimit ? "Limit price" : "Price"} value={price} onChange={setPrice} placeholder={isLimit ? "0.00" : "market"} disabled={!isLimit && orderType === "MARKET"} />
        </div>

        {/* Live LTP strip */}
        {ltp > 0 && (
          <div className="flex items-center gap-3 rounded-lg border border-border/60 bg-muted/20 px-3 py-2 text-[12.5px]">
            <span className="text-muted-foreground">LTP</span>
            <span className={cn("font-semibold tabular-nums transition-colors duration-300", tick?.flash === "up" && "text-gain", tick?.flash === "down" && "text-destructive", !tick?.flash && "text-foreground")}>{INR(ltp, 2)}</span>
            {tick?.flash && <span className="h-1.5 w-1.5 rounded-full bg-gain animate-pulse" />}
            <span className="ml-auto text-muted-foreground/70">{formattedSymbol}</span>
          </div>
        )}

        {/* Risk preview */}
        <div className={cn("rounded-md border px-4 py-3 text-xs space-y-2", canAfford ? "border-border/60 bg-muted/10" : "border border-warning/20 bg-warning/15/10")}>
          <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">Order Preview</div>
          <div className="flex justify-between"><span className="text-muted-foreground">Estimated value</span><span className="font-semibold tabular-nums text-foreground">{estimatedValue > 0 ? INR(estimatedValue) : "\u2014"}</span></div>
          <div className="flex justify-between"><span className="text-muted-foreground">Margin required</span><span className="font-semibold tabular-nums text-foreground">{marginRequired > 0 ? INR(marginRequired) : "\u2014"}</span></div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">Margin usage</span>
            <span className={cn("font-semibold tabular-nums", marginPct > 80 ? "text-warning" : "text-foreground")}>{marginPct > 0 ? `${marginPct.toFixed(1)}% of available` : "\u2014"}</span>
          </div>
          {orderType === "MARKET" && (
            <div className="flex items-start gap-1.5 border-t border-border/40 pt-2 text-caption text-muted-foreground/80">
              <Zap size={10} className="mt-0.5 shrink-0 text-warning" />
              <span>MARKET: 0.5% protection, fill may differ from LTP</span>
            </div>
          )}
          {!canAfford && availableMargin > 0 && (
            <div className="flex items-start gap-1.5 border-t border-warning/30 pt-2 text-caption text-warning">
              <ShieldAlert size={10} className="mt-0.5 shrink-0" />
              <span>Estimated margin ({INR(marginRequired)}) may exceed available ({INR(availableMargin)}).</span>
            </div>
          )}
        </div>

        {/* Submit */}
        <StatefulButton state={state} onClick={() => void submit()}
          loadingText="Submitting\u2026" successText="Submitted \u2713" errorText="Failed \u2014 retry"
          className={cn("w-full h-11 text-sm font-semibold tracking-wide", side === "BUY" ? "bg-gain hover:bg-gain/90 text-white border-gain" : "bg-destructive hover:bg-destructive/90 text-white border-loss")}>
          {side} {qty} {symbol.trim().toUpperCase() || "\u2014"}
        </StatefulButton>
      </div>

      {/* Right: Market Depth */}
      <div className="flex flex-col">
        <div className="mb-2 text-caption font-semibold uppercase tracking-wider text-muted-foreground">
          Market Depth · {formattedSymbol}
        </div>
        <MarketDepthLadder symbol={formattedSymbol} depth={tick?.depth} ltp={tick?.ltp}
          onSelectPrice={(p) => { setPrice(p.toFixed(2)); setOrderType("LIMIT"); }} />
        <p className="mt-2 text-[10.5px] text-muted-foreground/60">Click a price to use as limit</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

type TabId = PortfolioSection | "execute";

const SECTIONS: { id: TabId; label: string; icon: React.ElementType }[] = [
  { id: "limits", label: "Limits", icon: Wallet },
  { id: "positions", label: "Positions", icon: Activity },
  { id: "holdings", label: "Holdings", icon: Briefcase },
  { id: "orders", label: "Orders", icon: ListOrdered },
  { id: "trades", label: "Trades", icon: BarChart3 },
  { id: "execute", label: "Execute", icon: Send },
];

export default function PortfolioPanel() {
  const [section, setSectionState] = useState<TabId>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("atr.portfolio.section") as TabId;
      if (saved && ["limits", "positions", "holdings", "orders", "trades", "execute"].includes(saved)) return saved;
    }
    return "limits";
  });

  const setSection = useCallback((next: TabId) => {
    setSectionState(next);
    if (typeof window !== "undefined") localStorage.setItem("atr.portfolio.section", next);
  }, []);

  const { toast } = useToast();
  const [data, setData] = useState<PortfolioResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [killEngaged, setKillEngaged] = useState<boolean>(false);
  const [killLoading, setKillLoading] = useState<boolean>(false);
  const dialog = useDialog();

  const loadRisk = useCallback(async () => {
    try { const risk = await getRiskStatus(); setKillEngaged(risk?.kill_switch === true); } catch { /* ignore */ }
  }, []);

  // The broker has no push channel for account state (positions/holdings/
  // margin) — only market prices stream over the websocket — so this has to
  // be polled. Two things made that poll visible as a "glitch" every cycle:
  // replacing `data` with a fresh object (even when nothing changed) forced
  // every KPI and table to re-render and any count-up numbers to replay, and
  // a failed poll (e.g. a 429) cleared `error` right before immediately
  // re-setting it, flickering the error banner off and back on.
  const lastPayloadRef = useRef<string | null>(null);
  // Nudges the account poll to run ~800ms from now — used after actions
  // that change account state so the UI catches up without waiting.
  const kickRef = useRef<(() => void) | null>(null);
  const requestPollSoon = useCallback(() => kickRef.current?.(), []);
  // `load` deliberately swallows its own errors (the UI shows stale data plus
  // a banner rather than blanking), so the poll loop below needs its own
  // signal of whether that call actually succeeded, to decide whether to back
  // its interval off.
  const load = useCallback(async (): Promise<boolean> => {
    try {
      const [res] = await Promise.all([getPortfolio(), loadRisk()]);
      // `as_of` is a fresh server timestamp on every call — comparing the
      // whole response against it would never match, defeating the dedup.
      const serialized = JSON.stringify(res.sections);
      if (serialized !== lastPayloadRef.current) {
        lastPayloadRef.current = serialized;
        setData(res);
      }
      setLastUpdated(new Date());
      setError(null);
      return true;
    } catch (e) {
      // Preserve stale data; just show friendly error
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  }, [loadRisk]);

  const handleToggleKillSwitch = async () => {
    const nextState = !killEngaged;
    const reason = await dialog.prompt({
      title: nextState ? "Turn the safety switch on?" : "Turn the safety switch off?",
      description: nextState
        ? "All trading and new orders stop immediately."
        : "Orders can be placed again.",
      label: "Reason",
      placeholder: "Why? This is saved in the audit trail.",
      required: true,
      confirmLabel: nextState ? "Turn on" : "Turn off",
      tone: nextState ? "danger" : "default",
    });
    if (reason === null) return;
    setKillLoading(true);
    try {
      await setKillSwitch(nextState, reason.trim());
      setKillEngaged(nextState);
      requestPollSoon();
      toast({ title: nextState ? "Kill Switch ENGAGED" : "Kill Switch Disarmed", description: nextState ? "All order submissions are blocked." : "Risk engine cleared. New orders may be placed.", status: nextState ? "error" : "success" });
    } catch (e) {
      toast({ title: "Kill switch action failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    } finally { setKillLoading(false); }
  };

  useEffect(() => { void load(); }, [load]);

  // Account state has no push channel — only prices stream — so it polls.
  // Steady 5s while visible: ticks already move prices live, the poll just
  // re-confirms account state. A 429 (or any failed poll) backs off to 15s.
  // Actions that change account state kick an early poll (see requestPollSoon).
  const BASE_POLL_MS = 5000;
  const MAX_POLL_MS = 15000;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    let delay = BASE_POLL_MS;
    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState === "visible") {
        const ok = await load();
        delay = ok ? BASE_POLL_MS : Math.min(delay * 2, MAX_POLL_MS);
      }
      timer = setTimeout(() => void tick(), delay);
    };
    kickRef.current = () => {
      if (cancelled) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void tick(), 800);
    };
    timer = setTimeout(() => void tick(), delay);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      kickRef.current = null;
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const counts = useMemo(() => {
    if (!data) return {} as Record<TabId, number>;
    return Object.fromEntries(SECTIONS.filter(s => s.id !== "execute").map((s) => [s.id, data.sections[s.id as PortfolioSection]?.count ?? 0])) as Record<TabId, number>;
  }, [data]);

  const positions = data?.sections?.positions?.rows ?? [];
  const holdings = data?.sections?.holdings?.rows ?? [];

  const liveSymbols = useMemo(() => {
    const syms = new Set<string>();
    positions.forEach((p) => { const s = String(p.symbol ?? p.tradingSymbol ?? "").trim(); if (s) syms.add(s); });
    holdings.forEach((h) => { const s = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "").trim(); if (s) syms.add(s); });
    return Array.from(syms);
  }, [positions, holdings]);

  const { getTick, connected, bridgeActive } = useLiveTicks(liveSymbols);

  const availableMargin = useMemo(() => num(data?.sections?.limits?.rows?.[0]?.tradingLimit), [data]);
  const currentSection = section !== "execute" ? data?.sections?.[section as PortfolioSection] : null;

  // A plain function, not a component: a component declared here is a new type on every
  // render, so React would rebuild the whole table on every price tick and lose its state.
  const guard = (children: React.ReactNode) => {
    if (error && !data) return <StaleNotice lastUpdated={lastUpdated} onRetry={() => void load()} />;
    if (!data) return <Hint>Loading…</Hint>;
    if (currentSection?.error) return <ErrorBox>{currentSection.error}</ErrorBox>;
    return <>{children}</>;
  };

  return (
    <div className="space-y-6">
      <KpiStrip data={data} error={error} lastUpdated={lastUpdated} getTick={getTick} />

      <ReconcileCard />

      <div className="rounded-lg border border-border/80 bg-card/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-4">
          <Tabs value={section} onValueChange={(v) => setSection(v as TabId)}>
            <TabsList>
              {SECTIONS.map((s) => {
                const Icon = s.icon;
                const c = counts[s.id] ?? 0;
                const active = section === s.id;
                return (
                  <TabsTrigger key={s.id} value={s.id} className="whitespace-nowrap px-3 py-1.5 text-xs font-medium">
                    <span className="inline-flex items-center gap-1.5">
                      <Icon size={13} />
                      {s.label}
                      {c > 0 && <span className={cn("rounded-full px-1.5 py-0.2 text-micro tabular-nums transition-colors", active ? "bg-primary-foreground/20 text-primary-foreground font-medium" : "bg-primary/10 text-primary font-semibold")}>{c}</span>}
                    </span>
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </Tabs>

          <div className="flex items-center gap-2.5">
          <Tooltip content={killEngaged ? "Orders are blocked. Click to allow orders again." : "Block all new orders."} side="bottom" delay={400}>
            <Button
              size="xs"
              variant="quiet"
              className={cn(killEngaged && "border-destructive/40 bg-destructive/10 text-destructive hover:border-destructive hover:text-destructive")}
              disabled={killLoading}
              aria-pressed={killEngaged}
              onClick={() => void handleToggleKillSwitch()}
            >
              {killEngaged ? <ShieldAlert size={12} /> : <ShieldCheck size={12} />}
              {killEngaged ? "Allow orders" : "Stop orders"}
            </Button>
            </Tooltip>

            <Tooltip content={connected ? (bridgeActive ? "Live prices" : "Connecting to prices") : "Prices offline"} side="top" delay={400}>
            <span
            className={cn("h-2 w-2 rounded-full", connected ? (bridgeActive ? "bg-gain" : "bg-warning") : "bg-muted-foreground/40")}
            />
            </Tooltip>
          </div>
        </div>

        <div className="pt-4">
          <Tabs value={section} onValueChange={(v) => setSection(v as TabId)}>
            <TabsContent value="limits">
              {guard(<LimitsView row={currentSection?.rows?.[0] ?? null} />)}
            </TabsContent>
            <TabsContent value="positions">
              {guard(<PositionsView rows={currentSection?.rows ?? []} getTick={getTick} />)}
            </TabsContent>
            <TabsContent value="holdings">
              {guard(<HoldingsView rows={currentSection?.rows ?? []} getTick={getTick} />)}
            </TabsContent>
            <TabsContent value="orders">
              {guard(<OrdersView rows={currentSection?.rows ?? []} />)}
            </TabsContent>
            <TabsContent value="trades">
              {guard(<TradesView rows={currentSection?.rows ?? []} />)}
            </TabsContent>
            <TabsContent value="execute">
              <ExecuteTab availableMargin={availableMargin} />
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  );
}
