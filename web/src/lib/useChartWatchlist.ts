import { useCallback, useEffect, useRef, useState } from "react";
import {
  addWatchlistItems,
  createWatchlist,
  getWatchlistQuotes,
  listWatchlists,
  removeWatchlistItem,
} from "../api";
import { setVisibleInterval } from "./visibleInterval";

/** A row in the chart's side list. A price the server could not give is null, never a made-up number. */
export type WatchRow = {
  symbol: string;
  name: string | null;
  last: number | null;
  chg: number | null;
  /** The price came from the local cache, not the broker. Shown, never passed off as live. */
  stale: boolean;
};

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/**
 * The chart's side list is the user's real default watchlist with its real quotes.
 * It used to be six hard-coded stocks with invented prices, saved in the browser, shown beside a
 * "live" label. Now there is nothing in it until the user has something on a watchlist.
 */
export function useChartWatchlist() {
  const [id, setId] = useState<string | null>(null);
  const [rows, setRows] = useState<WatchRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const idRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    try {
      let wid = idRef.current;
      if (!wid) {
        const lists = (await listWatchlists()).watchlists;
        const pick = lists.find((w) => w.is_default) ?? lists[0];
        wid = pick?.watchlist_id ?? null;
        idRef.current = wid;
        setId(wid);
      }
      if (!wid) {
        setRows([]);
      } else {
        const q = await getWatchlistQuotes(wid, true);
        setRows(
          (q.rows ?? []).map((r) => ({
            symbol: r.symbol,
            name: r.name ?? null,
            last: num(r.ltp),
            chg: num(r.change_pct),
            stale: !!r.stale || !!r.error,
          })),
        );
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the watchlist");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setVisibleInterval(() => void load(), 20_000);
    return () => clearInterval(t);
  }, [load]);

  const add = useCallback(
    async (symbol: string) => {
      let wid = idRef.current;
      if (!wid) {
        const made = await createWatchlist({ name: "My watchlist" });
        wid = made.watchlist_id;
        idRef.current = wid;
        setId(wid);
      }
      await addWatchlistItems(wid, [symbol]);
      await load();
    },
    [load],
  );

  const remove = useCallback(
    async (symbol: string) => {
      const wid = idRef.current;
      if (!wid) return;
      await removeWatchlistItem(wid, symbol);
      await load();
    },
    [load],
  );

  return { id, rows, error, loaded, add, remove, reload: load };
}
