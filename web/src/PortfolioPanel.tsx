import { motion } from "motion/react";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Banknote,
  BarChart3,
  Briefcase,
  Coins,
  LineChart,
  ListOrdered,
  RefreshCw,
  Send,
  ShieldAlert,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
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
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, fmtNum } from "./components/ui/stat";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import { useToast } from "./components/ui/toast-context";
import { Tooltip } from "./components/ui/tooltip";
import { Table, type TableColumn } from "./components/motion/table";
import { BouncyAccordion, type BouncyAccordionItem } from "./components/motion/bouncy-accordion";
import { MarketDepthLadder } from "./components/ui/market-depth";
import { useLiveTicks, type LiveTick } from "./lib/useLiveTicks";
import { sound } from "./lib/sound";
import { cn } from "./lib/utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type Row = Record<string, unknown>;

const num = (v: unknown): number => {
  const n = typeof v === "string" ? Number(v) : (v as number);
  return typeof n === "number" && Number.isFinite(n) ? n : 0;
};

const INR = (n: number, frac = 0) =>
  `₹${n.toLocaleString("en-IN", { maximumFractionDigits: frac })}`;

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Soft empty state. */
function Empty({
  icon: Icon,
  title,
  hint,
}: {
  icon: React.ElementType;
  title: string;
  hint?: string;
}) {
  return (
    <div className="grid place-items-center gap-2 rounded-2xl border border-dashed border-border bg-muted/15 px-6 py-10 text-center">
      <div className="grid h-10 w-10 place-items-center rounded-xl border border-border bg-card text-muted-foreground">
        <Icon size={18} />
      </div>
      <div className="text-sm font-semibold text-foreground">{title}</div>
      {hint ? <div className="max-w-xs text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

/** P&L cell with arrow + colour. Used in card lists and tables. */
function PnLCell({
  value,
  pct,
  size = "sm",
}: {
  value: number;
  pct?: number;
  size?: "sm" | "md" | "lg";
}) {
  const positive = value >= 0;
  const cls =
    size === "lg"
      ? "text-[17px] font-bold tabular-nums tracking-tight"
      : size === "md"
        ? "text-[15px] font-semibold tabular-nums"
        : "text-[13px] font-semibold tabular-nums";
  const colour = positive
    ? "text-emerald-600 dark:text-emerald-400"
    : "text-destructive";
  return (
    <span className={cn("inline-flex items-center gap-1", colour, cls)}>
      {positive ? (
        <ArrowUpRight className="h-3 w-3" />
      ) : (
        <ArrowDownRight className="h-3 w-3" />
      )}
      {INR(Math.abs(value))}
      {typeof pct === "number" ? (
        <span className="text-[11px] font-medium opacity-80">
          ({pct >= 0 ? "+" : ""}
          {pct.toFixed(2)}%)
        </span>
      ) : null}
    </span>
  );
}

/** Status pill for order/trade rows. */
function StatusPill({ status }: { status: string }) {
  const s = status.toUpperCase();
  const tone =
    s.includes("REJECT") || s.includes("CANCEL")
      ? "bad"
      : s.includes("COMPLETE") || s.includes("FILLED") || s === "TRADED"
        ? "good"
        : s.includes("PENDING") || s.includes("OPEN") || s.includes("PARTIAL")
          ? "warn"
          : "flat";
  return <Badge tone={tone}>{status}</Badge>;
}

// ---------------------------------------------------------------------------
// Hero KPI strip
// ---------------------------------------------------------------------------

function KpiStrip({
  data,
  error,
  getTick,
}: {
  data: PortfolioResponse | null;
  error: string | null;
  getTick: (sym: string) => LiveTick | undefined;
}) {
  const kpis = useMemo(() => {
    const limits = data?.sections?.limits?.rows?.[0] ?? null;
    const holdings = data?.sections?.holdings?.rows ?? [];
    const positions = data?.sections?.positions?.rows ?? [];

    const tradingLimit = num(limits?.tradingLimit);
    const collateral = num(limits?.collateralMargin);
    const utilized = num(limits?.utilizedMargin);
    const spanMargin = num(limits?.utilizedSpanMargin);
    const exposureMargin = num(limits?.utilizedExposureMargin);
    const openingCash = num(limits?.openingCashLimit);
    const intradayPayin = num(limits?.intradayPayin);
    const creditForSell = num(limits?.creditForSell);
    const blockedForPayout = num(limits?.blockedForPayout);
    const adhocMargin = num(limits?.adhocMargin);

    const cashFunds =
      openingCash + intradayPayin + creditForSell - utilized - blockedForPayout;

    const holdingsInvested = holdings.reduce(
      (s, h) => s + num(h.totalQuantity) * num(h.averageTradedPrice),
      0,
    );
    const holdingsAtClose = holdings.reduce((s, h) => {
      const sym = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "");
      const tick = getTick(sym);
      const px = tick?.ltp ?? num(h.previousDayClose);
      return s + num(h.totalQuantity) * px;
    }, 0);

    const positionsPnl = positions.reduce((s, p) => {
      const sym = String(p.symbol ?? p.tradingSymbol ?? "");
      const tick = getTick(sym);
      const ltp = tick?.ltp ?? num(p.last_price);
      const avg = num(p.avg_price);
      const qty = num(p.quantity);
      const explicit = p.unrealized_pnl;
      return s + (tick?.ltp ? (tick.ltp - avg) * qty : typeof explicit === "number" ? num(explicit) : (ltp - avg) * qty);
    }, 0);

    return {
      tradingLimit,
      collateral,
      utilized: utilized + spanMargin + exposureMargin,
      cashFunds,
      openingCash,
      intradayPayin,
      creditForSell,
      blockedForPayout,
      adhocMargin,
      holdingsInvested,
      holdingsAtClose,
      holdingsCount: holdings.length,
      positionsCount: positions.length,
      positionsPnl,
    };
  }, [data, getTick]);

  const noData = !data && !error;

  return (
    <div className="rounded-2xl border border-border/80 bg-card/40 p-1">
      <div className="grid grid-cols-2 divide-y divide-border/60 sm:divide-y-0 sm:divide-x sm:grid-cols-4">
        {/* Metric 1: Available Margin */}
        <div className="p-4 sm:p-5">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
            Available Margin
          </div>
          <div className="mt-1 text-xl font-semibold tracking-tight text-foreground tabular-nums">
            {INR(noData ? 0 : kpis.tradingLimit)}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {noData ? "Loading…" : `${INR(kpis.utilized)} utilized`}
          </div>
        </div>

        {/* Metric 2: Cash Funds */}
        <div className="p-4 sm:p-5">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
            Cash Balance
          </div>
          <div className="mt-1 text-xl font-semibold tracking-tight text-foreground tabular-nums">
            {INR(noData ? 0 : kpis.cashFunds)}
          </div>
          <div className="mt-1 text-xs text-muted-foreground truncate">
            {kpis.blockedForPayout > 0 ? `${INR(kpis.blockedForPayout)} blocked` : "Settled free funds"}
          </div>
        </div>

        {/* Metric 3: Total Portfolio / Holdings */}
        <div className="p-4 sm:p-5">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
            Holdings Value
          </div>
          <div className="mt-1 text-xl font-semibold tracking-tight text-foreground tabular-nums">
            {INR(noData ? 0 : kpis.holdingsAtClose)}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {noData ? "Loading…" : `${kpis.holdingsCount} stock lots`}
          </div>
        </div>

        {/* Metric 4: Open Position P&L */}
        <div className="p-4 sm:p-5">
          <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
            Open P&L
          </div>
          <div className={cn(
            "mt-1 text-xl font-semibold tracking-tight tabular-nums",
            kpis.positionsPnl > 0 ? "text-emerald-500" : kpis.positionsPnl < 0 ? "text-destructive" : "text-foreground"
          )}>
            {kpis.positionsPnl === 0 ? "₹0" : INR(kpis.positionsPnl)}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {kpis.positionsCount} active {kpis.positionsCount === 1 ? "position" : "positions"}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab content: Limits (semantic groups)
// ---------------------------------------------------------------------------

function LimitRow({
  label,
  value,
  hint,
  emphasize,
  tone,
}: {
  label: string;
  value: number;
  hint?: string;
  emphasize?: boolean;
  tone?: "good" | "bad" | "warn" | "neutral";
}) {
  const content = (
    <div
      className={cn(
        "flex items-center justify-between gap-3 py-2 transition-colors",
        emphasize && "border-b border-border/60 pb-2.5",
      )}
    >
      <div className="flex items-center gap-1.5 min-w-0">
        <span
          className={cn(
            "truncate",
            emphasize ? "text-sm font-semibold text-foreground" : "text-xs text-muted-foreground",
          )}
        >
          {label}
        </span>
        {hint ? (
          <span className="cursor-help rounded-full bg-muted px-1.5 py-0.2 text-[10px] text-muted-foreground/70">
            ?
          </span>
        ) : null}
      </div>
      <div
        className={cn(
          "shrink-0 tabular-nums font-medium",
          emphasize ? "text-base font-bold text-foreground" : "text-xs",
          tone === "good" && "text-emerald-600 dark:text-emerald-400",
          tone === "bad" && "text-destructive",
          tone === "warn" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {INR(value)}
      </div>
    </div>
  );

  if (hint) {
    return (
      <Tooltip content={hint} wrapperClassName="w-full block">
        {content}
      </Tooltip>
    );
  }

  return content;
}

function LimitsGroup({
  title,
  icon: Icon,
  badge,
  children,
  className,
}: {
  title: string;
  icon: React.ElementType;
  badge?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-2xl border border-border bg-card p-4.5", className)}>
      <div className="mb-3 flex items-center justify-between gap-2 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
        <span className="flex items-center gap-2">
          <span className="grid h-5 w-5 place-items-center rounded-md bg-primary/[0.09] text-primary">
            <Icon size={11} />
          </span>
          {title}
        </span>
        {badge}
      </div>
      <div className="divide-y divide-border/40">{children}</div>
    </div>
  );
}

function LimitsView({ row }: { row: Row | null }) {
  if (!row) {
    return (
      <Empty
        icon={Wallet}
        title="No limits returned"
        hint="The broker returned an empty limits payload. Try Refresh."
      />
    );
  }
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

  // Items for the BouncyAccordion breakdown of technical broker margins
  const technicalItems: BouncyAccordionItem[] = [
    {
      id: "fo_margins",
      title: "Derivatives & Exposure Margins",
      icon: <LineChart className="h-4 w-4" />,
      badge: (
        <span className="tabular-nums text-xs font-semibold">
          {INR(spanMargin + exposureMargin)}
        </span>
      ),
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40">
            <span>SPAN Margin (F&O blocked)</span>
            <span className="font-semibold text-foreground">{INR(spanMargin)}</span>
          </div>
          <div className="flex justify-between py-1">
            <span>Exposure Margin</span>
            <span className="font-semibold text-foreground">{INR(exposureMargin)}</span>
          </div>
        </div>
      ),
    },
    {
      id: "cash_movements",
      title: "Cash Inflow, Outflow & Payouts",
      icon: <Banknote className="h-4 w-4" />,
      badge: (
        <span className="tabular-nums text-xs font-semibold">
          {intradayPayin - blocked > 0 ? "+" : ""}
          {INR(intradayPayin - blocked)}
        </span>
      ),
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40">
            <span>Opening Cash</span>
            <span className="font-semibold text-foreground">{INR(openingCash)}</span>
          </div>
          <div className="flex justify-between py-1 border-b border-border/40">
            <span>Intraday Pay-in</span>
            <span className="font-semibold text-emerald-600 dark:text-emerald-400">+{INR(intradayPayin)}</span>
          </div>
          <div className="flex justify-between py-1 border-b border-border/40">
            <span>Credit from Sells</span>
            <span className="font-semibold text-emerald-600 dark:text-emerald-400">+{INR(credit)}</span>
          </div>
          <div className="flex justify-between py-1">
            <span>Blocked for Payout</span>
            <span className="font-semibold text-destructive">-{INR(blocked)}</span>
          </div>
        </div>
      ),
    },
    {
      id: "collateral_adhoc",
      title: "Collateral & Ad-hoc Margins",
      icon: <Coins className="h-4 w-4" />,
      badge: (
        <span className="tabular-nums text-xs font-semibold">
          {INR(collateral + adhoc)}
        </span>
      ),
      description: (
        <div className="space-y-1.5 pt-1">
          <div className="flex justify-between py-1 border-b border-border/40">
            <span>Pledged Collateral</span>
            <span className="font-semibold text-foreground">{INR(collateral)}</span>
          </div>
          <div className="flex justify-between py-1">
            <span>Ad-hoc Margin Granted</span>
            <span className="font-semibold text-foreground">{INR(adhoc)}</span>
          </div>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2">
        {/* Margin Utilization Meter */}
        <LimitsGroup
          title="Margin & Buying Capacity"
          icon={LineChart}
          badge={
            <Badge tone={deploymentPct > 80 ? "warn" : deploymentPct > 0 ? "good" : "flat"}>
              {deploymentPct.toFixed(1)}% deployed
            </Badge>
          }
        >
          <LimitRow
            label="Utilized Margin (Active)"
            value={totalDeployed}
            emphasize
            tone={totalDeployed > 0 ? "warn" : "neutral"}
            hint="Total active margin consumed by equity and derivatives positions."
          />
          <LimitRow
            label="Equity Utilized"
            value={utilized}
            hint="Margin consumed specifically by equity intraday trades."
          />
          <LimitRow
            label="F&O Blocked"
            value={spanMargin + exposureMargin}
            hint="SPAN + exposure margin currently blocked for open derivatives."
          />
        </LimitsGroup>

        {/* Capital Composition */}
        <LimitsGroup
          title="Capital Composition"
          icon={Wallet}
          badge={
            <span className="text-xs text-muted-foreground">
              {collateral > 0 ? "Cash + Pledged" : "100% Cash"}
            </span>
          }
        >
          <LimitRow
            label="Pledged Collateral"
            value={collateral}
            emphasize
            hint="Margin value obtained from pledged shares/securities."
          />
          <LimitRow
            label="Opening Cash"
            value={openingCash}
            hint="Starting cash balance at the beginning of the trading day."
          />
          <LimitRow
            label="Ad-hoc Margin"
            value={adhoc}
            hint="Special discretionary margin extended by the broker."
          />
        </LimitsGroup>
      </div>

      {/* Accordion for granular line-item breakdowns without cluttering the screen */}
      <div className="rounded-2xl border border-border bg-card p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-[11px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
            Granular Breakdown & Ledger Flow
          </div>
          <span className="text-[11px] text-muted-foreground/75">
            Click to inspect underlying margin accounts
          </span>
        </div>
        <BouncyAccordion items={technicalItems} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab content: Positions
// ---------------------------------------------------------------------------



function positionPnl(p: Row, tick?: LiveTick): number {
  const ltp = tick?.ltp ?? num(p.last_price);
  const avg = num(p.avg_price);
  const qty = num(p.quantity);
  const explicit = num(p.unrealized_pnl);
  return tick?.ltp ? (tick.ltp - avg) * qty : explicit !== 0 ? explicit : (ltp - avg) * qty;
}

function PositionsView({ rows, getTick }: { rows: Row[]; getTick: (sym: string) => LiveTick | undefined }) {
  const columns = useMemo<TableColumn<Row>[]>(
    () => [
      {
        key: "instrument",
        header: "Instrument",
        sortable: true,
        width: "2fr",
        sortValue: (r) => String(r.symbol ?? r.tradingSymbol ?? ""),
        cell: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "?");
          const exch = String(r.exchange ?? "NSEEQ");
          const tick = getTick(sym);
          return (
            <div className="flex items-center gap-2 py-1">
              <span className="font-semibold text-foreground hover:text-primary transition-colors">
                {sym}
              </span>
              <span className="text-[11px] text-muted-foreground">{exch}</span>
              {tick && (
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" title="Live stream active" />
              )}
            </div>
          );
        },
      },
      {
        key: "side",
        header: "Side",
        sortable: true,
        width: "100px",
        sortValue: (r) => (num(r.quantity) > 0 ? "LONG" : num(r.quantity) < 0 ? "SHORT" : "FLAT"),
        cell: (r) => {
          const qty = num(r.quantity);
          const isLong = qty > 0;
          return (
            <Badge tone={isLong ? "good" : qty < 0 ? "bad" : "flat"}>
              {qty === 0 ? "FLAT" : isLong ? "LONG" : "SHORT"}
            </Badge>
          );
        },
      },
      {
        key: "qty",
        header: "Quantity",
        sortable: true,
        align: "right",
        width: "110px",
        sortValue: (r) => num(r.quantity),
        cell: (r) => (
          <span className="tabular-nums font-medium text-foreground">
            {num(r.quantity).toLocaleString("en-IN")}
          </span>
        ),
      },
      {
        key: "avg",
        header: "Avg Price",
        sortable: true,
        align: "right",
        width: "120px",
        sortValue: (r) => num(r.avg_price),
        cell: (r) => (
          <span className="tabular-nums text-muted-foreground">
            {INR(num(r.avg_price), 2)}
          </span>
        ),
      },
      {
        key: "ltp",
        header: "LTP",
        sortable: true,
        align: "right",
        width: "120px",
        sortValue: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          return getTick(sym)?.ltp ?? num(r.last_price);
        },
        cell: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          const tick = getTick(sym);
          const ltp = tick?.ltp ?? num(r.last_price);
          return (
            <span
              className={cn(
                "tabular-nums font-semibold transition-colors duration-300",
                tick?.flash === "up" && "text-emerald-500",
                tick?.flash === "down" && "text-destructive",
                !tick?.flash && "text-foreground"
              )}
            >
              {INR(ltp, 2)}
            </span>
          );
        },
      },
      {
        key: "pnl",
        header: "Unrealized P&L",
        sortable: true,
        align: "right",
        width: "140px",
        sortValue: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          return positionPnl(r, getTick(sym));
        },
        cell: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          const tick = getTick(sym);
          const pnl = positionPnl(r, tick);
          return <PnLCell value={pnl} size="sm" />;
        },
      },
      {
        key: "pnl_pct",
        header: "P&L %",
        sortable: true,
        align: "right",
        width: "115px",
        sortValue: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          const tick = getTick(sym);
          const avg = num(r.avg_price);
          const ltp = tick?.ltp ?? num(r.last_price);
          return avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
        },
        cell: (r) => {
          const sym = String(r.symbol ?? r.tradingSymbol ?? "");
          const tick = getTick(sym);
          const avg = num(r.avg_price);
          const ltp = tick?.ltp ?? num(r.last_price);
          const pct = avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
          const positive = pct >= 0;
          return (
            <span
              className={cn(
                "inline-flex items-center gap-0.5 tabular-nums font-semibold text-[12.5px]",
                positive ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
              )}
            >
              {positive ? "+" : ""}
              {pct.toFixed(2)}%
            </span>
          );
        },
      },
    ],
    [getTick],
  );

  if (rows.length === 0) {
    return (
      <Empty
        icon={Activity}
        title="No open positions"
        hint="Your intraday book is flat. Active positions will appear here with live P&L."
      />
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border/80 bg-card/20 shadow-xs">
      <Table
        data={rows}
        columns={columns}
        getRowId={(r, i) => String(r.symbol ?? r.tradingSymbol ?? i)}
        resizable
        reorderable
        defaultSort={{ key: "pnl", direction: "desc" }}
        height={Math.min(480, Math.max(160, rows.length * 52 + 48))}
        rowHeight={52}
        className="rounded-xl border-none"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab content: Holdings
// ---------------------------------------------------------------------------

function HoldingCard({ row, tick }: { row: Row; tick?: LiveTick }) {
  const symbol = String(row.nseTradingSymbol ?? row.bseTradingSymbol ?? row.symbol ?? "?");
  const name = String(row.formattedInstrumentName ?? "").trim();
  const qty = num(row.totalQuantity);
  const avg = num(row.averageTradedPrice);
  const lastPrice = tick?.ltp ?? num(row.previousDayClose);
  const invested = qty * avg;
  const currentVal = qty * lastPrice;
  const delta = currentVal - invested;
  const pct = avg > 0 ? ((lastPrice - avg) / avg) * 100 : 0;
  const product = String(row.product ?? "");

  return (
    <motion.div
      whileHover={{ y: -1 }}
      transition={{ type: "spring", stiffness: 380, damping: 28 }}
      className="rounded-2xl border border-border bg-card p-4 transition-colors"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div className="truncate text-[15px] font-bold tracking-tight">{symbol}</div>
            {product ? <Badge tone="flat">{product}</Badge> : null}
            {tick && (
              <span className="flex items-center gap-1 text-[10.5px] font-medium text-emerald-500">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
                Live
              </span>
            )}
          </div>
          {name ? (
            <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground" title={name}>
              {name}
            </div>
          ) : null}
        </div>
        <div className="text-right">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
            Qty
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {qty.toLocaleString("en-IN")}
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3 border-t border-border/60 pt-3">
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
            Invested
          </div>
          <div className="mt-0.5 text-[13px] font-semibold tabular-nums">{INR(invested)}</div>
          <div className="text-[10.5px] text-muted-foreground/70">@ {INR(avg, 2)}</div>
        </div>
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
            {tick ? "Current Value" : "At last close"}
          </div>
          <div
            className={cn(
              "mt-0.5 text-[13px] font-semibold tabular-nums transition-colors duration-300",
              tick?.flash === "up" && "text-emerald-500",
              tick?.flash === "down" && "text-destructive"
            )}
          >
            {INR(currentVal)}
          </div>
          <div className="text-[10.5px] text-muted-foreground/70">@ {INR(lastPrice, 2)}</div>
        </div>
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-muted-foreground">
            Δ vs avg
          </div>
          <div className="mt-0.5">
            <PnLCell value={delta} pct={pct} />
          </div>
        </div>
      </div>
    </motion.div>
  );
}

function HoldingsView({ rows, getTick }: { rows: Row[]; getTick: (sym: string) => LiveTick | undefined }) {
  const [query, setQuery] = useState("");
  const [selectedLots, setSelectedLots] = useState<string[]>([]);
  const [viewMode, setViewMode] = useState<"table" | "grid">(() => {
    return (localStorage.getItem("atr.holdings.view") as "table" | "grid") || "table";
  });

  const toggleView = (mode: "table" | "grid") => {
    setViewMode(mode);
    localStorage.setItem("atr.holdings.view", mode);
  };

  // Configure interactive columns for @beui/table (sortable, resizable, reorderable)
  const columns = useMemo<TableColumn<Row>[]>(
    () => [
      {
        key: "instrument",
        header: "Instrument",
        sortable: true,
        width: "2.2fr",
        sortValue: (r) => String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? ""),
        cell: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "?");
          const name = String(r.formattedInstrumentName ?? "");
          const tick = getTick(sym);
          return (
            <div className="flex flex-col justify-center min-w-0 py-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-foreground hover:text-primary transition-colors">
                  {sym}
                </span>
                {tick && (
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" title="Live stream active" />
                )}
              </div>
              {name && (
                <span className="text-[11px] text-muted-foreground/75 truncate" title={name}>
                  {name}
                </span>
              )}
            </div>
          );
        },
      },
      {
        key: "qty",
        header: "Quantity",
        sortable: true,
        align: "right",
        width: "110px",
        sortValue: (r) => num(r.totalQuantity),
        cell: (r) => (
          <span className="tabular-nums font-medium text-foreground/90">
            {num(r.totalQuantity).toLocaleString("en-IN")}
          </span>
        ),
      },
      {
        key: "avg",
        header: "Avg Price",
        sortable: true,
        align: "right",
        width: "120px",
        sortValue: (r) => num(r.averageTradedPrice),
        cell: (r) => (
          <span className="tabular-nums text-muted-foreground">
            {INR(num(r.averageTradedPrice), 2)}
          </span>
        ),
      },
      {
        key: "ltp",
        header: "LTP",
        sortable: true,
        align: "right",
        width: "120px",
        sortValue: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          return getTick(sym)?.ltp ?? num(r.previousDayClose);
        },
        cell: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const tick = getTick(sym);
          const ltp = tick?.ltp ?? num(r.previousDayClose);
          return (
            <span
              className={cn(
                "tabular-nums font-semibold transition-colors duration-300",
                tick?.flash === "up" && "text-emerald-500",
                tick?.flash === "down" && "text-destructive",
                !tick?.flash && "text-foreground"
              )}
            >
              {INR(ltp, 2)}
            </span>
          );
        },
      },
      {
        key: "value",
        header: "Current Value",
        sortable: true,
        align: "right",
        width: "140px",
        sortValue: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const px = getTick(sym)?.ltp ?? num(r.previousDayClose);
          return num(r.totalQuantity) * px;
        },
        cell: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const px = getTick(sym)?.ltp ?? num(r.previousDayClose);
          return (
            <span className="tabular-nums font-semibold text-foreground">
              {INR(num(r.totalQuantity) * px)}
            </span>
          );
        },
      },
      {
        key: "pnl",
        header: "Unrealized P&L",
        sortable: true,
        align: "right",
        width: "140px",
        sortValue: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const px = getTick(sym)?.ltp ?? num(r.previousDayClose);
          const val = num(r.totalQuantity) * px;
          const inv = num(r.totalQuantity) * num(r.averageTradedPrice);
          return val - inv;
        },
        cell: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const avg = num(r.averageTradedPrice);
          const tick = getTick(sym);
          const ltp = tick?.ltp ?? num(r.previousDayClose);
          const val = num(r.totalQuantity) * ltp;
          const inv = num(r.totalQuantity) * avg;
          const delta = val - inv;
          return <PnLCell value={delta} size="sm" />;
        },
      },
      {
        key: "pnl_pct",
        header: "P&L %",
        sortable: true,
        align: "right",
        width: "115px",
        sortValue: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const avg = num(r.averageTradedPrice);
          const tick = getTick(sym);
          const ltp = tick?.ltp ?? num(r.previousDayClose);
          return avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
        },
        cell: (r) => {
          const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
          const avg = num(r.averageTradedPrice);
          const tick = getTick(sym);
          const ltp = tick?.ltp ?? num(r.previousDayClose);
          const pct = avg > 0 ? ((ltp - avg) / avg) * 100 : 0;
          const positive = pct >= 0;
          return (
            <span
              className={cn(
                "inline-flex items-center gap-0.5 tabular-nums font-semibold text-[12.5px]",
                positive ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
              )}
            >
              {positive ? "+" : ""}
              {pct.toFixed(2)}%
            </span>
          );
        },
      },
    ],
    [getTick],
  );

  if (rows.length === 0) {
    return (
      <Empty
        icon={Briefcase}
        title="No holdings"
        hint="Your demat account is empty. Holdings appear here as delivery trades settle."
      />
    );
  }

  const totalInvested = rows.reduce(
    (s, h) => s + num(h.totalQuantity) * num(h.averageTradedPrice),
    0,
  );
  const totalAtClose = rows.reduce((s, h) => {
    const sym = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "");
    const tick = getTick(sym);
    const px = tick?.ltp ?? num(h.previousDayClose);
    return s + num(h.totalQuantity) * px;
  }, 0);
  const totalDelta = totalAtClose - totalInvested;
  const totalPct = totalInvested > 0 ? (totalDelta / totalInvested) * 100 : 0;

  const filtered = rows.filter((r) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "").toLowerCase();
    const name = String(r.formattedInstrumentName ?? "").toLowerCase();
    return sym.includes(q) || name.includes(q);
  });

  return (
    <div className="space-y-3.5">
      {/* Stripe-style Interactive Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-3.5 pt-1">
        <div className="flex items-center gap-2.5">
          <span className="text-[13px] font-medium text-muted-foreground">
            Unrealized:
          </span>
          <PnLCell value={totalDelta} pct={totalPct} size="md" />
          {selectedLots.length > 0 && (
            <>
              <span className="text-xs text-muted-foreground/60">·</span>
              <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-semibold text-primary">
                {selectedLots.length} selected
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-2.5">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search symbols…"
            className="h-8 w-48 rounded-lg border border-border/80 bg-card/60 px-3 text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary/60 focus:outline-none transition-colors"
          />

          {/* View Switcher: Interactive Table vs Cards */}
          <div className="flex items-center rounded-lg border border-border/80 bg-card/50 p-0.5 text-xs">
            <button
              type="button"
              onClick={() => toggleView("table")}
              title="Interactive Virtualized Table"
              className={cn(
                "rounded-md px-2 py-0.5 font-medium transition-colors",
                viewMode === "table" ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:text-foreground"
              )}
            >
              Table
            </button>
            <button
              type="button"
              onClick={() => toggleView("grid")}
              title="Card grid"
              className={cn(
                "rounded-md px-2 py-0.5 font-medium transition-colors",
                viewMode === "grid" ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:text-foreground"
              )}
            >
              Cards
            </button>
          </div>
        </div>
      </div>

      {viewMode === "table" ? (
        /* @beui/table: Virtualized, Sortable, Resizable & Reorderable Data Table */
        <div className="overflow-hidden rounded-xl border border-border/80 bg-card/20 shadow-xs">
          <Table
            data={filtered}
            columns={columns}
            getRowId={(r, i) => String(r.isin ? `${r.isin}-${r.product ?? "DEL"}-${i}` : (r.nseTradingSymbol ?? r.symbol ?? i))}
            selectable
            resizable
            reorderable
            selectedRowIds={selectedLots}
            onSelectionChange={setSelectedLots}
            defaultSort={{ key: "value", direction: "desc" }}
            height={Math.min(540, Math.max(160, filtered.length * 52 + 48))}
            rowHeight={52}
            className="rounded-xl border-none"
          />
        </div>
      ) : (
        /* Compact Card Grid */
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {filtered.map((r, i) => {
            const sym = String(r.nseTradingSymbol ?? r.bseTradingSymbol ?? r.symbol ?? "");
            return <HoldingCard key={i} row={r} tick={getTick(sym)} />;
          })}
        </div>
      )}

      {filtered.length === 0 && (
        <Empty
          icon={Briefcase}
          title="No matching holdings"
          hint={`No holding symbol matches "${query}".`}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab content: Orders / Trades
// ---------------------------------------------------------------------------

function pickString(row: Row, ...keys: string[]): string {
  for (const k of keys) {
    const v = row[k];
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return "—";
}

function OrdersView({ rows }: { rows: Row[] }) {
  if (rows.length === 0) {
    return (
      <Empty
        icon={ListOrdered}
        title="No orders today"
        hint="Every order the broker has seen today appears here. Open positions and the place-order panel are below."
      />
    );
  }
  return (
    <div className="overflow-x-auto rounded-2xl border border-border">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="bg-muted/40 text-[10.5px] uppercase tracking-[0.07em] text-muted-foreground">
            <th className="px-3 py-2 text-left font-semibold">Time</th>
            <th className="px-3 py-2 text-left font-semibold">Symbol</th>
            <th className="px-3 py-2 text-left font-semibold">Side</th>
            <th className="px-3 py-2 text-right font-semibold">Qty</th>
            <th className="px-3 py-2 text-right font-semibold">Price</th>
            <th className="px-3 py-2 text-left font-semibold">Type</th>
            <th className="px-3 py-2 text-left font-semibold">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const side = pickString(r, "transactionType", "TransactionType", "side", "Side").toUpperCase();
            const isBuy = side.startsWith("B");
            return (
              <tr
                key={i}
                className="border-t border-border/60 transition-colors hover:bg-primary/[0.03]"
              >
                <td className="whitespace-nowrap px-3 py-2 font-mono text-[11.5px] text-muted-foreground">
                  {pickString(r, "orderDateTime", "exchangeTimestamp", "ExchangeTimestamp", "updatedAt", "CreatedAt")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 font-semibold">
                  {pickString(r, "tradingSymbol", "TradingSymbol", "symbol")}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <Badge tone={isBuy ? "good" : "bad"}>{side || "BUY"}</Badge>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {fmtNum(r.quantity ?? r.Quantity)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {fmtNum(r.price ?? r.Price)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                  {pickString(r, "orderComplexity", "OrderComplexity", "product", "Product")}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <StatusPill status={pickString(r, "orderStatus", "OrderStatus", "status", "Status")} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TradesView({ rows }: { rows: Row[] }) {
  if (rows.length === 0) {
    return (
      <Empty
        icon={BarChart3}
        title="No trades today"
        hint="Executed trades appear here as they happen. Cost basis, realised P&L, and brokerage all land in this view."
      />
    );
  }
  return (
    <div className="overflow-x-auto rounded-2xl border border-border">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="bg-muted/40 text-[10.5px] uppercase tracking-[0.07em] text-muted-foreground">
            <th className="px-3 py-2 text-left font-semibold">Time</th>
            <th className="px-3 py-2 text-left font-semibold">Symbol</th>
            <th className="px-3 py-2 text-left font-semibold">Side</th>
            <th className="px-3 py-2 text-right font-semibold">Qty</th>
            <th className="px-3 py-2 text-right font-semibold">Price</th>
            <th className="px-3 py-2 text-right font-semibold">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const side = pickString(r, "transactionType", "TransactionType", "side", "Side").toUpperCase();
            const isBuy = side.startsWith("B");
            const qty = num(r.quantity ?? r.Quantity);
            const price = num(r.tradePrice ?? r.TradePrice ?? r.price ?? r.Price);
            return (
              <tr
                key={i}
                className="border-t border-border/60 transition-colors hover:bg-primary/[0.03]"
              >
                <td className="whitespace-nowrap px-3 py-2 font-mono text-[11.5px] text-muted-foreground">
                  {pickString(r, "exchangeTimestamp", "ExchangeTimestamp", "tradeTime", "TradeTime", "orderDateTime")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 font-semibold">
                  {pickString(r, "tradingSymbol", "TradingSymbol", "symbol")}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <Badge tone={isBuy ? "good" : "bad"}>{side || "BUY"}</Badge>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {qty.toLocaleString("en-IN")}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {INR(price, 2)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right font-semibold tabular-nums">
                  {INR(qty * price)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Quick actions: live quotes + place order
// ---------------------------------------------------------------------------

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 [&>option]:bg-card";

function PlaceOrderCard() {
  const { toast } = useToast();
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [symbol, setSymbol] = useState("RELIANCE");
  const [exchange, setExchange] = useState("NSEEQ");
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState("MARKET");
  const [price, setPrice] = useState("");
  const [state, setState] = useState<ButtonState>("idle");
  const [showDepth, setShowDepth] = useState(true);

  const formattedSymbol = `${symbol.trim().toUpperCase()}${symbol.includes("-") ? "" : "-EQ"}`;
  const { getTick } = useLiveTicks(useMemo(() => [formattedSymbol], [formattedSymbol]));
  const tick = getTick(formattedSymbol);

  // ── Symbol autocomplete via /symbols ───────────────────────────────────
  const [matches, setMatches] = useState<{ symbol: string; exchange: string }[]>([]);
  const [quoteBy, setQuoteBy] = useState<Record<string, { ltp: number; chg_pct: number }>>({});
  const [showMatches, setShowMatches] = useState(false);
  useEffect(() => {
    const q = symbol.trim().toUpperCase();
    if (q.length < 2 || q.includes("-")) { setMatches([]); setQuoteBy({}); return; }
    let cancelled = false;
    const t = window.setTimeout(() => {
      searchSymbols(q, exchange || "NSEEQ", 8)
        .then((r) => { if (!cancelled) setMatches(r.results || []); })
        .catch(() => { if (!cancelled) setMatches([]); });
    }, 180);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [symbol, exchange]);

  // Pull LTP + chg% for every match so the user can compare prices before
  // committing. Only one /quote call per keystroke (debounced).
  useEffect(() => {
    if (matches.length === 0) { setQuoteBy({}); return; }
    let cancelled = false;
    const syms = matches.map((m) => m.symbol).join(",");
    const t = window.setTimeout(async () => {
      try {
        const res = await getQuote(syms, "NSEEQ");
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

  const signedQty = side === "SELL" ? -Math.abs(qty) : Math.abs(qty);
  const isLimit = orderType !== "MARKET" && orderType !== "SLM";

  async function submit() {
    setState("loading");
    try {
      const res = await placeOrder({
        symbol: symbol.trim().toUpperCase(),
        exchange: exchange.trim().toUpperCase(),
        quantity: signedQty,
        order_type: orderType,
        price: price === "" ? null : Number(price),
      });
      setState("success");
      if (res.status === "rejected") {
        sound.playReject();
      } else {
        sound.playFill();
      }
      toast({
        title: `Order ${res.order_id} → ${res.status}`,
        description:
          res.reject_reason ?? `${symbol.trim().toUpperCase()} × ${qty} ${orderType}`,
        status: res.status === "rejected" ? "error" : "success",
      });
    } catch (e) {
      setState("error");
      sound.playReject();
      toast({
        title: "Order failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    }
  }

  return (
    <Card>
      <CardHeader
        title={
          <span className="inline-flex items-center gap-2">
            <span className="grid h-5 w-5 place-items-center rounded-md bg-primary/[0.09] text-primary">
              <Send size={11} />
            </span>
            Quick Order Entry
          </span>
        }
        sub="Signed quantity: negative sells. Click any depth price to copy to Limit price."
        action={
          <button
            type="button"
            onClick={() => setShowDepth(!showDepth)}
            className="rounded-full border border-border px-2.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground"
          >
            {showDepth ? "Hide Depth" : "Show Depth"}
          </button>
        }
      />
      <div className="space-y-3.5 p-5">
        <Tabs value={side} onValueChange={(v) => setSide(v as "BUY" | "SELL")} variant="segment">
          <TabsList className="w-full">
            <TabsTrigger value="BUY" className="flex-1">
              <span className="inline-flex items-center gap-1.5 font-semibold">
                <TrendingUp size={12} className="text-emerald-500" /> BUY
              </span>
            </TabsTrigger>
            <TabsTrigger value="SELL" className="flex-1">
              <span className="inline-flex items-center gap-1.5 font-semibold">
                <TrendingDown size={12} className="text-destructive" /> SELL
              </span>
            </TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="relative">
            <Input
              label="Symbol"
              value={symbol}
              onChange={(v) => { setSymbol(v); setShowMatches(true); }}
              onFocus={() => setShowMatches(matches.length > 0)}
              onBlur={() => window.setTimeout(() => setShowMatches(false), 150)}
              placeholder="RELIANCE"
              autoComplete="off"
            />
            {showMatches && matches.length > 0 && (
              <div className="absolute z-30 mt-1 max-h-56 w-full overflow-auto rounded-xl border border-border bg-card shadow-lg">
                {matches.map((m) => {
                  const q = quoteBy[m.symbol];
                  const pct = q?.chg_pct;
                  const positive = typeof pct === "number" && pct >= 0;
                  return (
                    <button
                      key={`${m.exchange}:${m.symbol}`}
                      type="button"
                      onMouseDown={(e) => { e.preventDefault(); setSymbol(m.symbol); setShowMatches(false); }}
                      className="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-[12.5px] hover:bg-primary/[0.06]"
                    >
                      <span className="flex items-center gap-2">
                        <span className="font-semibold text-foreground">{m.symbol}</span>
                        <span className="text-[10.5px] text-muted-foreground">{m.exchange}</span>
                      </span>
                      <span className="flex items-center gap-2 tabular-nums">
                        {q && q.ltp > 0 ? (
                          <>
                            <span className="font-semibold text-foreground">₹{q.ltp.toFixed(2)}</span>
                            {typeof pct === "number" && Number.isFinite(pct) ? (
                              <span className={positive ? "text-emerald-500" : "text-destructive"}>
                                {positive ? "+" : ""}{pct.toFixed(2)}%
                              </span>
                            ) : null}
                          </>
                        ) : (
                          <span className="text-[10.5px] text-muted-foreground/70">· · ·</span>
                        )}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <Input label="Exchange" value={exchange} onChange={setExchange} placeholder="NSEEQ" />
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Input
            label="Quantity"
            type="number"
            value={String(qty)}
            onChange={(v) => setQty(Math.max(0, Number(v) || 0))}
          />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Type</label>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className={selectClass}
            >
              <option value="MARKET">MARKET</option>
              <option value="LIMIT">LIMIT</option>
              <option value="SL">SL (stop-loss)</option>
              <option value="SLM">SL-MKT</option>
            </select>
          </div>
          <Input
            label={isLimit ? "Limit price" : "Price (optional)"}
            value={price}
            onChange={setPrice}
            placeholder={isLimit ? "0.00" : "market"}
            disabled={!isLimit && orderType === "MARKET"}
          />
        </div>

        {showDepth && (
          <div className="pt-1">
            <MarketDepthLadder
              symbol={formattedSymbol}
              depth={tick?.depth}
              ltp={tick?.ltp}
              onSelectPrice={(p) => {
                setPrice(p.toFixed(2));
                setOrderType("LIMIT");
              }}
            />
          </div>
        )}

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          <div className="text-[12px] text-muted-foreground">
            <span className="font-medium text-foreground">
              {side === "BUY" ? "Buy" : "Sell"} {qty}
            </span>{" "}
            × {symbol || "—"} {exchange ? `@ ${exchange}` : ""}{" "}
            {isLimit && price ? `@ ${INR(Number(price) || 0, 2)}` : "@ market"}
          </div>
          <StatefulButton
            state={state}
            onClick={() => void submit()}
            loadingText="Submitting…"
            successText="Submitted"
            errorText="Failed — retry"
          >
            Submit order
          </StatefulButton>
        </div>
        <Hint>
          MARKET orders carry a default 0.5% market-protection value — SEBI rejects a zero
          value on API market orders.
        </Hint>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const SECTIONS: { id: PortfolioSection; label: string; icon: React.ElementType }[] = [
  { id: "limits", label: "Limits", icon: Wallet },
  { id: "positions", label: "Positions", icon: Activity },
  { id: "holdings", label: "Holdings", icon: Briefcase },
  { id: "orders", label: "Orders", icon: ListOrdered },
  { id: "trades", label: "Trades", icon: BarChart3 },
];

export default function PortfolioPanel() {
  const [section, setSectionState] = useState<PortfolioSection>(() => {
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("atr.portfolio.section") as PortfolioSection;
      if (saved && ["limits", "positions", "holdings", "orders", "trades"].includes(saved)) {
        return saved;
      }
    }
    return "limits";
  });

  const setSection = useCallback((next: PortfolioSection) => {
    setSectionState(next);
    if (typeof window !== "undefined") {
      localStorage.setItem("atr.portfolio.section", next);
    }
  }, []);

  const { toast } = useToast();
  const [data, setData] = useState<PortfolioResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const [killEngaged, setKillEngaged] = useState<boolean>(false);
  const [killLoading, setKillLoading] = useState<boolean>(false);

  const loadRisk = useCallback(async () => {
    try {
      const risk = await getRiskStatus();
      setKillEngaged(risk?.kill_switch === true);
    } catch {
      // ignore
    }
  }, []);

  const load = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      const [res] = await Promise.all([getPortfolio(), loadRisk()]);
      setData(res);
      setState("success");
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }, [loadRisk]);

  const handleToggleKillSwitch = async () => {
    const nextState = !killEngaged;
    if (nextState) {
      const confirm = window.confirm(
        "EMERGENCY KILL SWITCH: Are you sure you want to halt all algorithmic trading and new order submissions immediately?"
      );
      if (!confirm) return;
    }
    setKillLoading(true);
    try {
      await setKillSwitch(nextState);
      setKillEngaged(nextState);
      toast({
        title: nextState ? "Kill Switch ENGAGED" : "Kill Switch Disarmed",
        description: nextState
          ? "All order submissions are blocked by the risk engine."
          : "Risk engine cleared. New orders may be placed.",
        status: nextState ? "error" : "success",
      });
    } catch (e) {
      toast({
        title: "Kill switch action failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    } finally {
      setKillLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [load]);

  const current = data?.sections?.[section];
  const counts = useMemo(() => {
    if (!data) return {} as Record<PortfolioSection, number>;
    return Object.fromEntries(
      SECTIONS.map((s) => [s.id, data.sections[s.id]?.count ?? 0]),
    ) as Record<PortfolioSection, number>;
  }, [data]);

  const positions = data?.sections?.positions?.rows ?? [];
  const holdings = data?.sections?.holdings?.rows ?? [];

  const liveSymbols = useMemo(() => {
    const syms = new Set<string>();
    positions.forEach((p) => {
      const s = String(p.symbol ?? p.tradingSymbol ?? "").trim();
      if (s) syms.add(s);
    });
    holdings.forEach((h) => {
      const s = String(h.nseTradingSymbol ?? h.bseTradingSymbol ?? h.symbol ?? "").trim();
      if (s) syms.add(s);
    });
    return Array.from(syms);
  }, [positions, holdings]);

  const { getTick, connected, bridgeActive } = useLiveTicks(liveSymbols);

  return (
    <div className="space-y-6">
      <KpiStrip data={data} error={error} getTick={getTick} />

      {/* Main Tabbed Area */}
      <div className="rounded-2xl border border-border/80 bg-card/40 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-4">
          <Tabs value={section} onValueChange={(v) => setSection(v as PortfolioSection)}>
            <TabsList className="bg-muted/40 p-1 gap-1 rounded-xl border border-border/60">
              {SECTIONS.map((s) => {
                const Icon = s.icon;
                const c = counts[s.id] ?? 0;
                const active = section === s.id;
                return (
                  <TabsTrigger
                    key={s.id}
                    value={s.id}
                    className="whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium"
                  >
                    <span className="inline-flex items-center gap-1.5">
                      <Icon size={13} /> {s.label}
                      {c > 0 ? (
                        <span
                          className={cn(
                            "rounded-full px-1.5 py-0.2 text-[10px] font-semibold tabular-nums transition-colors",
                            active
                              ? "bg-primary-foreground/20 text-primary-foreground font-bold"
                              : "bg-primary/10 text-primary"
                          )}
                        >
                          {c}
                        </span>
                      ) : null}
                    </span>
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </Tabs>

          <div className="flex items-center gap-2.5">
            <button
              type="button"
              onClick={() => void handleToggleKillSwitch()}
              disabled={killLoading}
              title={killEngaged ? "Kill switch is active. Click to disarm." : "Engage emergency kill switch to halt orders."}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-semibold transition-all border",
                killEngaged
                  ? "bg-rose-500/20 border-rose-500 text-rose-400 animate-pulse hover:bg-rose-500/30"
                  : "bg-muted/30 border-border/70 text-muted-foreground hover:text-foreground hover:border-border hover:bg-muted/60"
              )}
            >
              {killEngaged ? (
                <>
                  <ShieldAlert size={12} className="text-rose-400" />
                  <span>KILL SWITCH ENGAGED</span>
                </>
              ) : (
                <>
                  <ShieldCheck size={12} className="text-muted-foreground/70" />
                  <span>Kill Switch</span>
                </>
              )}
            </button>

            <div className="flex items-center gap-1.5 rounded-full border border-border/60 bg-muted/20 px-2.5 py-1 text-[11px] font-medium text-muted-foreground">
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  connected
                    ? bridgeActive
                      ? "bg-emerald-500 animate-pulse"
                      : "bg-amber-500"
                    : "bg-muted-foreground/50"
                )}
              />
              {connected ? (bridgeActive ? "Live Stream" : "Connecting") : "Offline"}
            </div>
            {data?.as_of && !error ? (
              <span className="hidden text-[11px] text-muted-foreground/75 sm:inline">
                {new Date(data.as_of).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}
              </span>
            ) : null}
            <StatefulButton
              state={state}
              variant="secondary"
              size="sm"
              onClick={() => void load()}
              loadingText="…"
              successText="Done"
              errorText="Retry"
              icon={<RefreshCw size={11} />}
              className="h-7 px-2.5 text-xs"
            >
              Refresh
            </StatefulButton>
          </div>
        </div>

        {killEngaged && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-950/30 px-4 py-2.5 text-xs text-rose-300">
            <div className="flex items-center gap-2 font-medium">
              <ShieldAlert size={15} className="text-rose-400 shrink-0" />
              <span>
                <strong>EMERGENCY KILL SWITCH ACTIVE:</strong> The risk engine is rejecting all order submissions and algo trades.
              </span>
            </div>
            <button
              type="button"
              disabled={killLoading}
              onClick={() => void handleToggleKillSwitch()}
              className="shrink-0 font-semibold underline underline-offset-2 hover:text-white"
            >
              Disarm Kill Switch
            </button>
          </div>
        )}

        <div className="pt-2">
          <Tabs value={section} onValueChange={(v) => setSection(v as PortfolioSection)}>

            <TabsContent value="limits">
              {error ? (
                <ErrorBox>{error}</ErrorBox>
              ) : !data ? (
                <Hint>Loading limits…</Hint>
              ) : current?.error ? (
                <ErrorBox>{current.error}</ErrorBox>
              ) : (
                <LimitsView row={current?.rows?.[0] ?? null} />
              )}
            </TabsContent>

            <TabsContent value="positions">
              {error ? (
                <ErrorBox>{error}</ErrorBox>
              ) : !data ? (
                <Hint>Loading positions…</Hint>
              ) : current?.error ? (
                <ErrorBox>{current.error}</ErrorBox>
              ) : (
                <PositionsView rows={current?.rows ?? []} getTick={getTick} />
              )}
            </TabsContent>

            <TabsContent value="holdings">
              {error ? (
                <ErrorBox>{error}</ErrorBox>
              ) : !data ? (
                <Hint>Loading holdings…</Hint>
              ) : current?.error ? (
                <ErrorBox>{current.error}</ErrorBox>
              ) : (
                <HoldingsView rows={current?.rows ?? []} getTick={getTick} />
              )}
            </TabsContent>

            <TabsContent value="orders">
              {error ? (
                <ErrorBox>{error}</ErrorBox>
              ) : !data ? (
                <Hint>Loading orders…</Hint>
              ) : current?.error ? (
                <ErrorBox>{current.error}</ErrorBox>
              ) : (
                <OrdersView rows={current?.rows ?? []} />
              )}
            </TabsContent>

            <TabsContent value="trades" className="mt-4">
              {error ? (
                <ErrorBox>{error}</ErrorBox>
              ) : !data ? (
                <Hint>Loading trades…</Hint>
              ) : current?.error ? (
                <ErrorBox>{current.error}</ErrorBox>
              ) : (
                <TradesView rows={current?.rows ?? []} />
              )}
            </TabsContent>
          </Tabs>
        </div>
      </div>

      <PlaceOrderCard />
    </div>
  );
}