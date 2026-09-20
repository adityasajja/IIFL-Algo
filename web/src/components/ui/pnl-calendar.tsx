import { ChevronLeft, ChevronRight, Flame, Trophy, TrendingDown } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { getPnlCalendar, type PnlMonth } from "../../api";
import { cn } from "../../lib/utils";
import { PageLoader } from "./loading";

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

/** Shade a day by its size next to the month's biggest, so one huge day does not wash out the rest. */
function shade(pnl: number, biggest: number): string {
  const strength = biggest > 0 ? Math.min(1, Math.abs(pnl) / biggest) : 0;
  const alpha = 0.22 + 0.68 * strength;
  return pnl >= 0 ? `rgba(16, 185, 129, ${alpha})` : `rgba(244, 63, 94, ${alpha})`;
}

export function PnlCalendar({ scope, title, note }: { scope: "paper" | "real"; title: string; note?: string }) {
  const [month, setMonth] = useState<string | undefined>(undefined);
  const [data, setData] = useState<PnlMonth | null>(null);
  const [error, setError] = useState<string | null>(null);

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
  }, [scope, month]);

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
      <Frame title={title}>
        <div className="py-6 text-sm text-muted-foreground">Couldn't load this calendar.</div>
      </Frame>
    );
  }
  if (!data) {
    return (
      <Frame title={title}>
        <PageLoader label="Loading" />
      </Frame>
    );
  }

  const months = data.months_with_data;
  const at = months.indexOf(data.month);
  const biggest = Math.max(0, ...data.days.map((d) => Math.abs(d.pnl)));
  const step = (by: number) => {
    const next = months[at + by];
    if (next) setMonth(next);
  };

  return (
    <Frame title={title}>
      {months.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted-foreground">
          {scope === "paper" ? "No paper trades have closed yet." : "No portfolio history yet."}
        </div>
      ) : (
        <>
          <div className="flex flex-wrap gap-2 text-[11px]">
            <Chip icon={<Flame className="size-3" />}>
              {data.current_green} green {data.current_green === 1 ? "day" : "days"} running
            </Chip>
            <Chip>
              best run {data.best_green} · worst {data.worst_red}
            </Chip>
            {data.best_day && <Chip icon={<Trophy className="size-3 text-emerald-500" />}>best {inr(data.best_day.pnl)}</Chip>}
            {data.worst_day && <Chip icon={<TrendingDown className="size-3 text-rose-500" />}>worst {inr(data.worst_day.pnl)}</Chip>}
            <Chip>
              {data.green_days}G / {data.red_days}R over {data.traded_days} traded {data.traded_days === 1 ? "day" : "days"}
            </Chip>
          </div>

          <div className="mt-4 flex items-center justify-between">
            <div className="flex items-center gap-1">
              <NavButton disabled={at <= 0} onClick={() => step(-1)} label="Earlier month">
                <ChevronLeft className="size-4" />
              </NavButton>
              <span className="min-w-24 text-center text-sm font-medium tabular-nums">{monthLabel(data.month)}</span>
              <NavButton disabled={at < 0 || at >= months.length - 1} onClick={() => step(1)} label="Later month">
                <ChevronRight className="size-4" />
              </NavButton>
            </div>
            <span className={cn("text-sm font-semibold tabular-nums", data.total >= 0 ? "text-emerald-500" : "text-rose-500")}>
              {inr(data.total)}
            </span>
          </div>

          <div className="mt-3 grid grid-cols-7 gap-1.5">
            {WEEKDAYS.map((w, i) => (
              <div key={i} className="pb-1 text-center text-[10px] text-muted-foreground">
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
              return (
                <div
                  key={iso}
                  title={tip}
                  className={cn(
                    "flex aspect-square flex-col items-center justify-center rounded-md text-[11px] tabular-nums",
                    d ? "text-foreground" : "bg-muted/40 text-muted-foreground",
                    d?.estimated && "ring-1 ring-inset ring-foreground/25",
                  )}
                  style={d ? { backgroundColor: shade(d.pnl, biggest) } : undefined}
                >
                  <span className="font-medium">{n}</span>
                  {d && <span className="text-[9px] leading-none opacity-80">{compact(d.pnl)}</span>}
                </div>
              );
            })}
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span>
              {note}
              {data.any_estimated && " Outlined days are rebuilt from today's holdings."}
              {data.unpriced && data.unpriced.length > 0 && ` ${data.unpriced.length} holdings have no price history and are left out.`}
            </span>
            <span className="flex items-center gap-3">
              <span className="flex items-center gap-1">
                <i className="size-2 rounded-full bg-rose-500" />
                loss
              </span>
              <span className="flex items-center gap-1">
                <i className="size-2 rounded-full bg-emerald-500" />
                profit
              </span>
            </span>
          </div>
        </>
      )}
    </Frame>
  );
}

function Frame({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <div className="mb-3 text-sm font-semibold">{title}</div>
      {children}
    </div>
  );
}

function Chip({ icon, children }: { icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-muted-foreground">
      {icon}
      {children}
    </span>
  );
}

function NavButton({ disabled, onClick, label, children }: { disabled: boolean; onClick: () => void; label: string; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="grid size-7 place-items-center rounded-lg border border-border text-muted-foreground transition-colors hover:text-foreground disabled:opacity-30"
    >
      {children}
    </button>
  );
}
