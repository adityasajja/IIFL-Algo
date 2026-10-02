import { ChevronLeft, ChevronRight, Landmark } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  getPnlCalendar,
  getPnlDay,
  removeTradingProfit,
  saveTradingProfit,
  type PnlDayDetail,
  type PnlMonth,
} from "../../api";
import { cn } from "../../lib/utils";
import { PageLoader } from "./loading";
import { Card, CardHeader } from "./card";
import { Button } from "./button";
import { Input } from "../motion/input";

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];

const inr = (n: number) => `${n < 0 ? "-" : ""}₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;
const compact = (n: number) => {
  const a = Math.abs(n);
  const s = a >= 1e5 ? `${(a / 1e5).toFixed(a >= 1e6 ? 0 : 1)}L` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : `${Math.round(a)}`;
  return `${n < 0 ? "-" : ""}₹${s}`;
};
const monthLabel = (m: string) =>
  new Date(`${m}-01T00:00:00`).toLocaleDateString("en-IN", { month: "short", year: "numeric" });
const dayLabel = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/** Absolute shade scale: the same rupee P&L renders the same color in every
calendar and every month. Normalizing to each month's biggest day made
identical results look different side by side. */
const FULL_SHADE_AT = 20000;
function shade(pnl: number): string {
  const strength = Math.min(1, Math.abs(pnl) / FULL_SHADE_AT);
  const alpha = 0.15 + 0.55 * strength;
  return pnl >= 0 ? `rgba(16, 185, 129, ${alpha})` : `rgba(244, 63, 94, ${alpha})`;
}

export function PnlCalendar({ scope, title, note }: { scope: "paper" | "real"; title: string; note?: string }) {
  const [month, setMonth] = useState<string | undefined>(undefined);
  const [data, setData] = useState<PnlMonth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let live = true;
    getPnlCalendar(scope, month)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
        setMonth((m) => m ?? d.month);
      })
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [scope, month, version]);

  const byDate = useMemo(() => new Map((data?.days ?? []).map((d) => [d.date, d])), [data]);
  const cells = useMemo(() => {
    if (!data) return [];
    const [y, m] = data.month.split("-").map(Number);
    const first = new Date(y, m - 1, 1).getDay();
    const count = new Date(y, m, 0).getDate();
    return [...Array<null>(first).fill(null), ...Array.from({ length: count }, (_, i) => i + 1)];
  }, [data]);

  if (error) {
    return (
      <Card className="p-5">
      <CardHeader title={<span className="text-base font-semibold tracking-tight">{title}</span>} />
        <div className="py-6 text-sm text-muted-foreground">Couldn't load this calendar.</div>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card className="p-5">
      <CardHeader title={<span className="text-base font-semibold tracking-tight">{title}</span>} />
        <PageLoader label="Loading" />
      </Card>
    );
  }

  const months = data.months_with_data;
  const at = months.indexOf(data.month);
  const step = (by: number) => {
    const next = months[at + by];
    if (next) {
      setMonth(next);
      setPicked(null);
    }
  };

  return (
    <Card className="p-5">
      <CardHeader
        title={<span className="text-base font-semibold tracking-tight">{title}</span>}
        sub={note ? <span className="text-xs text-muted-foreground">{note}</span> : undefined}
      />
      <div className="mt-3">
      {months.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted-foreground">
          {scope === "paper" ? "No paper trades have closed yet." : "No portfolio history yet."}
        </div>
      ) : (
        <>
            {/* Official FY figures, straight from the broker — one strip, no cards. */}
            {data.tax_gl_summary && (
              <div className="mb-4 rounded-xl border border-border/70 bg-muted/20 px-4 py-3">
                <div className="flex items-center gap-2 text-caption font-medium uppercase tracking-[0.08em] text-muted-foreground">
                  <Landmark className="size-3.5 text-primary" />
                  <span>Official FY figures · IIFL</span>
          </div>
                <div className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-4">
                  <div className="min-w-0">
                  <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Net realized</div>
                  <div className={cn("mt-0.5 text-lg font-semibold tracking-tight tabular-nums", (data.tax_gl_summary.totalPnlInclusiveCharges ?? 0) >= 0 ? "text-gain" : "text-loss")}>{compact(data.tax_gl_summary.totalPnlInclusiveCharges ?? 0)}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">after all charges</div>
                  </div>
                  <div className="min-w-0">
                  <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Short-term</div>
                  <div className={cn("mt-0.5 text-lg font-semibold tracking-tight tabular-nums", (data.tax_gl_summary.shortTerm ?? 0) >= 0 ? "text-gain" : "text-loss")}>{compact(data.tax_gl_summary.shortTerm ?? 0)}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">held under 12 months</div>
                  </div>
                  <div className="min-w-0">
                  <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Long-term</div>
                  <div className={cn("mt-0.5 text-lg font-semibold tracking-tight tabular-nums", (data.tax_gl_summary.longTerm ?? 0) >= 0 ? "text-gain" : "text-loss")}>{compact(data.tax_gl_summary.longTerm ?? 0)}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">held 12 months or more</div>
                  </div>
                  <div className="min-w-0">
                  <div className="text-caption font-normal uppercase tracking-[0.08em] text-muted-foreground">Charges</div>
                  <div className="mt-0.5 text-lg font-semibold tracking-tight tabular-nums text-warning">{compact((data.tax_gl_summary.brokerage ?? 0) + (data.tax_gl_summary.chargesTaxes ?? 0))}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">brokerage, STT, GST & stamp</div>
                  </div>
                </div>
              </div>
            )}

            {/* One quiet sentence instead of five shouting pills. */}
            <p className="text-body tabular-nums text-muted-foreground">
            <span className="font-medium text-foreground">{data.traded_days}</span> traded {data.traded_days === 1 ? "day" : "days"} ·{" "}
              <span className="font-medium text-foreground">{data.green_days}</span> up ·{" "}
              <span className="font-medium text-foreground">{data.red_days}</span> down
              {data.best_day ? <> · best <span className="font-medium text-gain">{compact(data.best_day.pnl)}</span></> : null}
              {data.worst_day ? <> · worst <span className="font-medium text-loss">{compact(data.worst_day.pnl)}</span></> : null}
            </p>

          <div className="mt-4 flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <Button
                  variant="outline"
                  size="icon"
                  disabled={at <= 0}
                  onClick={() => step(-1)}
                  aria-label="Earlier month"
                  className="size-7 rounded-lg"
                >
                <ChevronLeft className="size-4" />
                </Button>
                <span className="min-w-24 text-center text-sm font-semibold tabular-nums">{monthLabel(data.month)}</span>
                <Button
                  variant="outline"
                  size="icon"
                  disabled={at < 0 || at >= months.length - 1}
                  onClick={() => step(1)}
                  aria-label="Later month"
                  className="size-7 rounded-lg"
                >
                <ChevronRight className="size-4" />
                </Button>
            </div>
            <span className={cn("text-base font-semibold tabular-nums", data.total >= 0 ? "text-gain" : "text-loss")}>
              {inr(data.total)}
            </span>
          </div>

          <div className="mt-3 grid grid-cols-7 gap-1.5">
            {WEEKDAYS.map((w, i) => (
              <div key={i} className="pb-1 text-center text-micro font-semibold text-muted-foreground">
                {w}
              </div>
            ))}
            {cells.map((n, i) => {
              if (n === null) return <div key={`blank-${i}`} />;
              const iso = `${data.month}-${String(n).padStart(2, "0")}`;
              const d = byDate.get(iso);
              const what = scope === "real" ? "holdings" : "trades";
              const tip = d
                ? `${dayLabel(iso)}: ${inr(d.pnl)}${d.trades ? ` · ${d.trades} ${what}` : ""}${d.estimated ? " · rebuilt from today's holdings" : ""}`
                : `${dayLabel(iso)}: no result`;
              const clickable = !!d || (scope === "real" && iso <= new Date().toISOString().slice(0, 10));
              const cellClass = cn(
                  "flex aspect-square flex-col items-center justify-center rounded-md text-caption tabular-nums font-medium transition-all",
                  d ? "text-foreground" : "bg-muted/30 text-muted-foreground/60",
                  d?.estimated && "ring-1 ring-inset ring-foreground/10",
                  clickable && "cursor-pointer hover:ring-1 hover:ring-foreground/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                  picked === iso && "ring-2 ring-primary ",
              );
              const inner = (
                <>
                    <span className="text-caption font-semibold leading-none">{n}</span>
                    {d && (
                      <span className="mt-0.5 text-micro font-medium leading-tight opacity-90 tracking-tight whitespace-nowrap overflow-hidden text-ellipsis max-w-full px-0.5">
                        {compact(d.pnl)}
                      </span>
                    )}
                </>
              );
              return clickable ? (
                <button
                  key={iso}
                  type="button"
                  title={d ? tip : `${dayLabel(iso)}: no result yet. Click to inspect or record profit.`}
                  aria-pressed={picked === iso}
                  onClick={() => setPicked((cur) => (cur === iso ? null : iso))}
                  className={cellClass}
                    style={d ? { backgroundColor: shade(d.pnl) } : undefined}
                >
                  {inner}
                </button>
              ) : (
                <div key={iso} title={tip} className={cellClass}>
                  {inner}
                </div>
              );
            })}
          </div>

          {picked && (
            <DayDetail
              scope={scope}
              date={picked}
              estimated={byDate.get(picked)?.estimated ?? false}
              onClose={() => setPicked(null)}
              onChanged={() => setVersion((v) => v + 1)}
            />
          )}

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-caption text-muted-foreground">
            <span>
                {data.any_estimated && "Outlined days are rebuilt from today's holdings."}
              {data.unpriced && data.unpriced.length > 0 && ` ${data.unpriced.length} holdings have no price history and are left out.`}
            </span>
              <span className="flex items-center gap-3 font-medium">
                <span className="flex items-center gap-1.5">
                  <i className="size-2 rounded-full bg-loss" />
                loss
              </span>
                <span className="flex items-center gap-1.5">
                  <i className="size-2 rounded-full bg-gain" />
                profit
              </span>
            </span>
          </div>
        </>
      )}
    </div>
    </Card>
  );
}

/** What is behind one day: the trades that closed, or the holdings that moved the book. */
function DayDetail({
  scope,
  date,
  estimated,
  onClose,
  onChanged,
}: {
  scope: "paper" | "real";
  date: string;
  estimated: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<PnlDayDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [saved, setSaved] = useState(0);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setFailed(false);
    getPnlDay(scope, date)
      .then((d) => live && setDetail(d))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [scope, date, saved]);

  const real = scope === "real";
  return (
    <div className="mt-4 rounded-xl border border-border bg-muted/20 p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">{dayLabel(date)}</div>
          {detail && (
            <div className="text-caption text-muted-foreground">
              {detail.rows.length} {real ? "holdings" : detail.rows.length === 1 ? "trade" : "trades"} · {detail.gainers} up, {detail.losers} down
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
        {detail && <span className={cn("text-sm font-semibold tabular-nums", detail.total >= 0 ? "text-gain" : "text-loss")}>{inr(detail.total)}</span>}
          <Button variant="ghost" size="sm" onClick={onClose} className="h-7 px-2 text-xs">
            Close
          </Button>
        </div>
      </div>

      {failed && <div className="mt-3 text-xs text-muted-foreground">Couldn't load this day.</div>}
      {!detail && !failed && <div className="mt-3 text-xs text-muted-foreground">Loading…</div>}
      {detail && detail.rows.length === 0 && <div className="mt-3 text-xs text-muted-foreground">Nothing recorded for this day.</div>}

      {detail && detail.rows.length > 0 && (
        <div className="mt-3 max-h-72 overflow-y-auto rounded-xl border border-border bg-card" data-lenis-prevent>
          <div className="divide-y divide-border">
            {detail.rows.map((r, i) => (
              <div key={`${r.symbol}-${i}`} className="flex items-center justify-between gap-3 px-3 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{r.symbol.replace("-EQ", "")}</div>
                  <div className="truncate text-caption text-muted-foreground">
                    {real
                      ? `${r.quantity} shares · ${inr(r.previous_close ?? 0)} to ${inr(r.close ?? 0)}`
                      : [r.source, r.entry != null && r.exit != null ? `${inr(r.entry)} to ${inr(r.exit)}` : null, r.note].filter(Boolean).join(" · ")}
                  </div>
                </div>
                <div className="shrink-0 text-right tabular-nums">
                <div className={cn("text-sm font-medium", r.pnl >= 0 ? "text-gain" : "text-loss")}>{inr(r.pnl)}</div>
                  <div className="text-caption text-muted-foreground">
                    {(real ? r.change_pct : r.pnl_pct) != null ? `${(real ? r.change_pct : r.pnl_pct)! > 0 ? "+" : ""}${(real ? r.change_pct : r.pnl_pct)!.toFixed(2)}%` : ""}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {detail && detail.unpriced.length > 0 && (
        <div className="mt-2 text-caption text-muted-foreground">Not priced on this day, so left out: {detail.unpriced.map((s) => s.replace("-EQ", "")).join(", ")}.</div>
      )}
      {estimated && <div className="mt-2 text-caption text-muted-foreground">Rebuilt from today's holdings, so the mix on that day may have differed.</div>}
      {real && detail && (
        <TradingProfit
          date={date}
          current={detail.realised ?? null}
          holdings={detail.holdings_total ?? 0}
          onChanged={() => {
            setSaved((n) => n + 1);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

/** Record what buying and selling made on a day, since the broker keeps no history to read it back from. */
function TradingProfit({
  date,
  current,
  holdings,
  onChanged,
}: {
  date: string;
  current: { amount: number; source: string; note: string } | null;
  holdings: number;
  onChanged: () => void;
}) {
  const [amount, setAmount] = useState(current ? String(current.amount) : "");
  const [note, setNote] = useState(current?.note ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    setAmount(current ? String(current.amount) : "");
    setNote(current?.note ?? "");
    setProblem(null);
  }, [date, current]);

  const parsed = Number(amount.replace(/[,₹\s]/g, ""));
  const valid = amount.trim() !== "" && Number.isFinite(parsed);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setProblem(null);
    try {
      await action();
      onChanged();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : "Couldn't save that.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 border-t border-border pt-3">
      <div className="text-xs font-semibold">Realized Trading Profit / Broker Sync</div>
      <div className="mt-0.5 text-caption text-muted-foreground">
        {current
          ? `Recorded ${inr(current.amount)} (${current.source || "sync"}) on top of the ${inr(holdings)} change in your holdings.`
          : "Only for profit that is not already in the holdings figure above, such as intraday trades or F&O settled during the day."}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <div className="w-36">
          <Input
          value={amount}
            onChange={(val) => setAmount(val)}
            placeholder="Amount (₹)"
          aria-label="Profit from trading, in rupees"
            className="h-8 text-xs"
        />
        </div>
        <div className="min-w-40 flex-1">
          <Input
          value={note}
            onChange={(val) => setNote(val)}
          placeholder="Note (optional)"
          aria-label="Note"
            className="h-8 text-xs"
        />
        </div>
        <Button
          size="sm"
          variant="primary"
          disabled={!valid || busy}
          onClick={() => void run(() => saveTradingProfit(date, parsed, note))}
          className="h-8 text-xs"
        >
          {current ? "Update" : "Save"}
        </Button>
        {current && (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void run(() => removeTradingProfit(date))}
            className="h-8 text-xs"
          >
            Remove
          </Button>
        )}
      </div>
      {problem && <div className="mt-1.5 text-caption text-loss">{problem}</div>}
    </div>
  );
}
