import { FlaskConical, Hand, Scale } from "lucide-react";
import type { DataSource, DataStatus } from "./api";
import { Tooltip } from "./components/motion/tooltip";
import { Button } from "./components/ui/button";
import { freshnessView, shortDay } from "./lib/track-view";
import { toneFill } from "./lib/tone";
import { cn } from "./lib/utils";

/**
 * Where the numbers come from, as one line: is the data current, did the nightly check of the stored
 * prices find anything, and the three limits of what the product claims. The path from source to record
 * is in the tooltips.
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
  const overall = status?.sources ? freshnessView(status.overall, null) : null;
  const q = status?.quality;
  const suspect = q ? q.jumps.length + q.gaps.length + q.unreadable.length : 0;
  const detail = q ? [...q.jumps, ...q.gaps, ...q.unreadable.map((s) => `${s} unreadable`)].slice(0, 4).join("; ") : "";
  const sources = (status?.sources ?? []).map((s) => `${s.label}: ${s.as_of ? shortDay(s.as_of) : "unknown"}`).join(" · ");

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-caption text-muted-foreground">
      {overall ? (
        <Tooltip
          content={`${sources}. Prices come from IIFL history${brokerConnected ? "" : " (not logged in)"}, topped up from public bars.`}
          side="top"
          delay={200}
        >
          <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
            <i className={cn("size-2 rounded-full", toneFill[overall.tone])} />
            Data {overall.word.toLowerCase()}
          </span>
        </Tooltip>
      ) : (
        <span className="inline-flex items-center gap-1.5">
          <i className={cn("size-2 rounded-full", toneFill[error ? "bad" : "flat"])} />
          {error ? "Could not check data" : "Checking data"}
        </span>
      )}
      {q && (
        <Tooltip
          content={suspect ? `Likely unadjusted splits or missing days: ${detail}.` : "Stored prices checked nightly for splits and gaps."}
          side="top"
          delay={200}
        >
          <span className="inline-flex items-center gap-1.5">
            <i className={cn("size-2 rounded-full", toneFill[suspect ? "warn" : "good"])} />
            {suspect ? `${suspect} suspect in price history` : "History clean"}
          </span>
        </Tooltip>
      )}
      <Tooltip content="Paper results use simulated fills. They show how the rules behaved, not what a real order would have got." side="top" delay={200}>
        <span className="inline-flex items-center gap-1.5">
          <FlaskConical className="size-3.5" aria-hidden="true" />
          Practice money
        </span>
      </Tooltip>
      <Tooltip content="A tool, not investment advice. Past results do not predict future results." side="top" delay={200}>
        <span className="inline-flex items-center gap-1.5">
          <Scale className="size-3.5" aria-hidden="true" />
          Not advice
        </span>
      </Tooltip>
      <Tooltip content="Nothing trades live unless you connect a broker and switch it on. Any live order is your decision." side="top" delay={200}>
        <span className="inline-flex items-center gap-1.5">
          <Hand className="size-3.5" aria-hidden="true" />
          You place live orders
        </span>
      </Tooltip>
    </div>
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
