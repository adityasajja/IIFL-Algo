import { useCallback, useEffect, useState } from "react";
import {
  getPortfolio,
  getQuote,
  placeOrder,
  type PortfolioResponse,
  type PortfolioSection,
} from "./api";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, fmtNum } from "./components/ui/stat";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { useToast } from "./components/ui/toast-context";
import { cn } from "./lib/utils";

const SECTIONS: { id: PortfolioSection; label: string; sub: string }[] = [
  { id: "limits", label: "Limits", sub: "Margin, exposure and what the broker will allow" },
  { id: "positions", label: "Positions", sub: "Intraday and carry-forward positions" },
  { id: "holdings", label: "Holdings", sub: "Delivery holdings in the demat account" },
  { id: "orders", label: "Order book", sub: "Every order the broker has seen today" },
  { id: "trades", label: "Trade book", sub: "Executed trades for the day" },
];

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 [&>option]:bg-card";

type Row = Record<string, unknown>;

/** Columns worth showing: scalars only, nested payloads render as noise. */
function columns(rows: Row[]): string[] {
  const keys: string[] = [];
  for (const row of rows) {
    for (const [k, v] of Object.entries(row)) {
      if (v === null || typeof v !== "object") {
        if (!keys.includes(k)) keys.push(k);
      }
    }
  }
  return keys.slice(0, 12);
}

function isNumericKey(key: string, rows: Row[]): boolean {
  const sample = rows.find((r) => r[key] !== null && r[key] !== undefined);
  return sample ? typeof sample[key] === "number" : false;
}

function DataTable({ rows }: { rows: Row[] }) {
  const cols = columns(rows);
  if (rows.length === 0) {
    return <Hint>Empty. That is a normal answer — not every section has content.</Hint>;
  }
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-border bg-muted/40 text-left">
            {cols.map((c) => (
              <th
                key={c}
                className={cn(
                  "whitespace-nowrap px-3 py-2 font-semibold",
                  isNumericKey(c, rows) && "text-right",
                )}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 300).map((row, i) => (
            <tr
              key={i}
              className="border-b border-border/60 transition-colors last:border-0 hover:bg-primary/[0.03]"
            >
              {cols.map((c) => {
                const numeric = isNumericKey(c, rows);
                const value = row[c];
                return (
                  <td
                    key={c}
                    className={cn(
                      "whitespace-nowrap px-3 py-1.5",
                      numeric && "text-right tabular-nums",
                    )}
                  >
                    {numeric && typeof value === "number" ? (
                      <span
                        className={cn(
                          /pnl|profit|gain|loss|netamt|value/i.test(c) &&
                            (value >= 0
                              ? "text-emerald-600 dark:text-emerald-400"
                              : "text-destructive"),
                        )}
                      >
                        {fmtNum(value)}
                      </span>
                    ) : (
                      fmtNum(value)
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 300 && (
        <div className="border-t border-border px-3 py-2 text-[11.5px] text-muted-foreground">
          Showing the first 300 of {rows.length} rows.
        </div>
      )}
    </div>
  );
}

function LimitsView({ rows }: { rows: Row[] }) {
  const limits = rows[0];
  if (!limits) return <Hint>No limits returned.</Hint>;
  const entries = Object.entries(limits).filter(
    ([, v]) => v === null || typeof v !== "object",
  );
  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <table className="w-full border-collapse text-[13px]">
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className="border-b border-border/60 last:border-0">
              <td className="px-3 py-1.5 text-muted-foreground">{k}</td>
              <td className="px-3 py-1.5 text-right font-medium tabular-nums">
                {typeof v === "number"
                  ? v.toLocaleString("en-IN", { maximumFractionDigits: 2 })
                  : fmtNum(v)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PortfolioPanel() {
  const { toast } = useToast();
  const [section, setSection] = useState<PortfolioSection>("limits");
  const [data, setData] = useState<PortfolioResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");

  const [quoteSymbols, setQuoteSymbols] = useState("RELIANCE-EQ,INFY-EQ,TCS-EQ");
  const [quoteExchange, setQuoteExchange] = useState("NSEEQ");
  const [quotes, setQuotes] = useState<Row[] | null>(null);
  const [quoteState, setQuoteState] = useState<ButtonState>("idle");
  const [quoteError, setQuoteError] = useState<string | null>(null);

  const [symbol, setSymbol] = useState("RELIANCE");
  const [exchange, setExchange] = useState("NSEEQ");
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState("MARKET");
  const [price, setPrice] = useState("");
  const [submitState, setSubmitState] = useState<ButtonState>("idle");

  const load = useCallback(async () => {
    setState("loading");
    setError(null);
    try {
      setData(await getPortfolio());
      setState("success");
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onQuote() {
    setQuoteState("loading");
    setQuoteError(null);
    try {
      const res = await getQuote(quoteSymbols, quoteExchange);
      setQuotes(res.quotes);
      setQuoteState("success");
      if (res.failed.length) {
        toast({
          title: `${res.failed.length} symbol(s) not resolved`,
          description: res.failed.map((f) => f.symbol).join(", "),
          status: "error",
        });
      }
    } catch (e) {
      setQuotes(null);
      setQuoteError(e instanceof Error ? e.message : String(e));
      setQuoteState("error");
    }
  }

  async function submit() {
    setSubmitState("loading");
    try {
      const res = await placeOrder({
        symbol: symbol.trim().toUpperCase(),
        exchange: exchange.trim().toUpperCase(),
        quantity: qty,
        order_type: orderType,
        price: price === "" ? null : Number(price),
      });
      setSubmitState("success");
      toast({
        title: `Order ${res.order_id} → ${res.status}`,
        description: res.reject_reason ?? `${symbol.trim().toUpperCase()} × ${qty} ${orderType}`,
        status: res.status === "rejected" ? "error" : "success",
      });
      void load();
    } catch (e) {
      setSubmitState("error");
      toast({
        title: "Order failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    }
  }

  const current = data?.sections?.[section];
  const meta = SECTIONS.find((s) => s.id === section);

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title={meta?.label ?? "Portfolio"}
          sub={meta?.sub}
          action={
            <div className="flex items-center gap-2.5">
              {current?.count !== undefined && (
                <Badge tone={current.error ? "bad" : "flat"}>{current.count} rows</Badge>
              )}
              <StatefulButton
                state={state}
                variant="secondary"
                onClick={() => void load()}
                loadingText="Loading…"
                successText="Loaded"
                errorText="Failed — retry"
              >
                Reload
              </StatefulButton>
            </div>
          }
        />
        <div className="space-y-3.5 p-5 pt-3">
          <Tabs
            value={section}
            onValueChange={(v) => setSection(v as PortfolioSection)}
            variant="segment"
          >
            <TabsList>
              {SECTIONS.map((s) => (
                <TabsTrigger key={s.id} value={s.id}>
                  {s.label}
                  {data?.sections?.[s.id]?.count
                    ? ` (${data.sections[s.id].count})`
                    : ""}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>

          {error && <ErrorBox>{error}</ErrorBox>}
          {!data && !error && <Hint>Loading the book…</Hint>}
          {data && current?.error && (
            <ErrorBox>
              {section} failed: {current.error}
            </ErrorBox>
          )}
          {data && !current?.error && (
            <>
              {section === "limits" ? (
                <LimitsView rows={current?.rows ?? []} />
              ) : (
                <DataTable rows={current?.rows ?? []} />
              )}
              <Hint>
                Pulled {new Date(data.as_of).toLocaleString("en-IN")}. Live sections need
                an active IIFL session — the JWT expires at midnight IST, so this is a
                daily login.
              </Hint>
            </>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Live quotes"
          sub="One bulk call to the market-data endpoint. Needs a session but not paper/live mode."
        />
        <div className="space-y-3.5 p-5 pt-3">
          <div className="grid gap-3.5 sm:grid-cols-[2fr_1fr_auto]">
            <Input
              label="Symbols (comma separated)"
              value={quoteSymbols}
              onChange={setQuoteSymbols}
            />
            <Input label="Exchange" value={quoteExchange} onChange={setQuoteExchange} />
            <div className="flex items-end">
              <StatefulButton
                state={quoteState}
                variant="secondary"
                onClick={() => void onQuote()}
                loadingText="Fetching…"
                successText="Fetched"
                errorText="Failed — retry"
              >
                Get quotes
              </StatefulButton>
            </div>
          </div>
          {quoteError && <ErrorBox>{quoteError}</ErrorBox>}
          {quotes && <DataTable rows={quotes} />}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Place order"
          sub="Signed quantity: negative sells. Blocked unless ENV is paper or live."
        />
        <div className="grid gap-3.5 p-5 sm:grid-cols-2 lg:grid-cols-5">
          <Input label="Symbol" value={symbol} onChange={setSymbol} />
          <Input label="Exchange" value={exchange} onChange={setExchange} />
          <Input
            label="Quantity (signed)"
            type="number"
            value={String(qty)}
            onChange={(v) => setQty(Number(v))}
          />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Type</label>
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className={selectClass}
            >
              <option>MARKET</option>
              <option>LIMIT</option>
              <option>SL</option>
              <option>SLM</option>
            </select>
          </div>
          <Input label="Price (limit)" value={price} onChange={setPrice} placeholder="market" />
        </div>
        <div className="space-y-3 px-5 pb-5">
          <StatefulButton
            state={submitState}
            onClick={() => void submit()}
            loadingText="Submitting…"
            successText="Submitted"
            errorText="Failed — retry"
          >
            Submit
          </StatefulButton>
          <Hint>
            MARKET orders carry a default 0.5% market-protection value — SEBI rejects a
            zero value on API market orders.
          </Hint>
        </div>
      </Card>
    </div>
  );
}
