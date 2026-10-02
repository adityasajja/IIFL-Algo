import { ArrowRight, Database, FlaskConical, Globe, Hand, LineChart, Plug, Scale, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { DataSource, DataStatus } from "./api";
import { Tooltip } from "./components/motion/tooltip";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";
import { freshnessView, shortDay, type Freshness } from "./lib/track-view";
import { toneChip, toneFill, type Tone } from "./lib/tone";
import { cn } from "./lib/utils";

/**
 * Where the numbers come from, drawn as a path with a freshness light on every stop:
 *
 *   sources  ->  stored prices  ->  paper venue  ->  your record
 *
 * Each stop says how current it is in a word and a colour, so a stale cache is visible before
 * anyone reads a number built on it. The limits of what the product claims sit beneath, as icons.
 */
export function DataTrust({
  status,
  brokerConnected,
  error,
}: {
  status: DataStatus | null;
  brokerConnected: boolean;
  error?: boolean;
}) {
  const usable = !!status?.sources;
  const overall = usable ? freshnessView(status!.overall, null) : null;
  const prices = status?.sources?.find((s) => s.id === "prices");
  const index = status?.sources?.find((s) => s.id === "index");

  return (
    <Card padding="md" className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Database className="size-4 text-primary" aria-hidden="true" />
          Where the numbers come from
        </div>
        {overall ? (
          <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-caption font-semibold", toneChip[overall.tone])}>
            <i className={cn("size-1.5 rounded-full", toneFill[overall.tone])} />
            Data: {overall.word.toLowerCase()}
          </span>
        ) : error ? (
          <span className={cn("rounded-full border px-2.5 py-0.5 text-caption font-semibold", toneChip.bad)}>Could not check</span>
        ) : null}
      </div>

      <ol className="grid items-stretch gap-3 md:grid-cols-[1fr_auto_1.2fr_auto_1fr_auto_1fr]">
        <Stop icon={<Plug className="size-4" />} title="Sources">
          <Row dot={brokerConnected ? "good" : "flat"} label="IIFL history" note={brokerConnected ? "connected" : "not logged in"} />
          <Row dot="flat" icon={<Globe className="size-3" />} label="Public bars" note="top-up" />
        </Stop>
        <Link />
        <Stop icon={<Database className="size-4" />} title="Stored prices">
          {prices ? <SourceRow s={prices} /> : <Row dot="flat" label="Stock prices" note="checking" />}
          {index ? <SourceRow s={index} /> : null}
          <QualityRow quality={status?.quality} />
        </Stop>
        <Link />
        <Stop icon={<FlaskConical className="size-4" />} title="Paper venue">
          <Row dot="warn" label="Simulated fills" note="no real orders" />
          <Row dot="flat" label="Costs included" note="modelled" />
        </Stop>
        <Link />
        <Stop icon={<LineChart className="size-4" />} title="Your record">
          <Row dot={status ? freshnessView(status.overall, null).tone : "flat"} label="Equity curve" note={index ? `to ${shortDay(index.as_of)}` : "—"} />
          <Row dot="flat" label="Versus Nifty 50" note="same start" />
        </Stop>
      </ol>

      <ul className="grid gap-2 sm:grid-cols-3">
        <Limit icon={<FlaskConical className="size-4" />} short="Practice money" long="Paper results use simulated fills. They show how the rules behaved on past prices, not what a real order would have got." />
        <Limit icon={<Scale className="size-4" />} short="Not advice" long="This is a tool, not investment advice. Past results do not predict future results." />
        <Limit icon={<Hand className="size-4" />} short="You place live orders" long="Nothing trades live unless you connect a broker and switch it on. Any live order is your decision and your responsibility." />
      </ul>
    </Card>
  );
}

function Stop({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <li className="flex min-w-0 flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex items-center gap-2 text-xs font-semibold text-foreground">
        <span className="text-muted-foreground">{icon}</span>
        {title}
      </div>
      <div className="space-y-1.5">{children}</div>
    </li>
  );
}

function Link() {
  return (
    <li aria-hidden="true" className="hidden items-center text-muted-foreground/60 md:flex">
      <ArrowRight className="size-4" />
    </li>
  );
}

function Row({ dot, label, note, icon }: { dot: Tone; label: string; note: string; icon?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-caption">
      {icon ?? <i className={cn("size-2 shrink-0 rounded-full", toneFill[dot])} />}
      <span className="min-w-0 truncate text-foreground">{label}</span>
      <span className="ml-auto shrink-0 text-muted-foreground">{note}</span>
    </div>
  );
}

function QualityRow({ quality }: { quality: DataStatus["quality"] }) {
  if (!quality) return <Row dot="flat" label="History check" note="not run yet" />;
  const suspect = quality.jumps.length + quality.gaps.length + quality.unreadable.length;
  const detail = [...quality.jumps, ...quality.gaps, ...quality.unreadable.map((s) => `${s} unreadable`)].slice(0, 4).join("; ");
  const row = <Row dot={suspect ? "warn" : "good"} label="History check" note={suspect ? `${suspect} suspect` : "clean"} />;
  return suspect ? (
    <Tooltip content={`Likely unadjusted splits or missing days: ${detail}. Results on these names may be wrong.`} side="top" delay={300} wrapperClassName="flex w-full">
      <div className="w-full">{row}</div>
    </Tooltip>
  ) : (
    row
  );
}

function SourceRow({ s }: { s: DataSource }) {
  const f = freshnessView(s.status as Freshness, s.sessions_behind);
  return (
    <div>
      <Tooltip content={`${s.origin}. ${s.role}.`} side="top" delay={300} wrapperClassName="flex w-full">
        <div className="flex w-full items-center gap-2 text-caption">
          <i className={cn("size-2 shrink-0 rounded-full", toneFill[f.tone])} />
          <span className="min-w-0 truncate text-foreground">{s.label}</span>
          <span className="ml-auto shrink-0 text-muted-foreground tabular-nums">{s.as_of ? shortDay(s.as_of) : f.word}</span>
        </div>
      </Tooltip>
    </div>
  );
}

function Limit({ icon, short, long }: { icon: ReactNode; short: string; long: string }) {
  return (
    <li className="flex">
      <Tooltip content={long} side="top" delay={200} wrapperClassName="flex w-full">
        <div className="flex w-full items-center gap-2 rounded-lg bg-muted/50 px-3 py-2 text-xs font-medium text-foreground">
          <span className="text-muted-foreground">{icon}</span>
          {short}
          <ShieldCheck className="ml-auto size-3.5 text-muted-foreground/60" aria-hidden="true" />
        </div>
      </Tooltip>
    </li>
  );
}

/** One dot and a few words, for the app bar: is the data behind what I am looking at current? */
export function DataPill({ status, onClick }: { status: DataStatus | null; onClick?: () => void }) {
  if (!status?.sources) return null;
  const f = freshnessView(status.overall, null);
  const worst = status.sources.reduce<DataSource | null>(
    (acc, s) => (acc == null || (s.sessions_behind ?? 99) > (acc.sessions_behind ?? 99) ? s : acc),
    null,
  );
  return (
    <Tooltip
      content={`${f.word}. Latest prices: ${shortDay(status.sources.find((s) => s.id === "prices")?.as_of)}. ${worst && status.overall !== "fresh" ? `${worst.label} is the oldest.` : "Open Home for the details."}`}
      side="bottom"
      delay={300}
    >
      <Button size="xs" variant="quiet" onClick={onClick} aria-label={`Data ${f.word}`}>
        <i className={cn("size-2 rounded-full", toneFill[f.tone])} />
        <span className="tabular-nums">{shortDay(status.sources.find((s) => s.id === "prices")?.as_of)}</span>
      </Button>
    </Tooltip>
  );
}
