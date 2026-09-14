import {
  ArrowDown,
  ArrowUp,
  BarChart3,
  Check,
  Columns3,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addWatchlistItems,
  ApiError,
  createWatchlist,
  deleteWatchlist,
  getAvailableColumns,
  getWatchlist,
  getWatchlistQuotes,
  listWatchlists,
  removeWatchlistItem,
  reorderWatchlistItems,
  searchInstruments,
  setWatchlistColumns,
  updateWatchlist,
  type ColumnSpec,
  type InstrumentRecord,
  type WatchlistDetail,
  type WatchlistQuotes,
  type WatchlistSummary,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Switch } from "./components/ui/switch";
import { cn } from "./lib/utils";

interface Props {
  /** Effective permissions of the signed-in principal, from `/auth/me`. */
  permissions: string[];
  onOpenChart?: (symbol: string) => void;
}

/** How often to re-pull quotes while the live toggle is on. */
const LIVE_REFRESH_MS = 20_000;

/** Columns that read as a movement, so they get up/down colouring. */
const SIGNED_COLUMNS = new Set(["change", "change_pct"]);

/**
 * Render one cell.
 *
 * `null` becomes an em dash, never `0`. A column with no data source reports
 * `null` on purpose, and printing `0.00` there would turn "we do not know" into
 * a measurement — the exact failure this platform is built to avoid.
 */
function formatValue(value: unknown, kind: string): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value !== "number") return String(value);
  if (!Number.isFinite(value)) return "—";
  switch (kind) {
    case "currency":
      return value.toLocaleString("en-IN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    case "percent":
      return `${value.toFixed(2)}%`;
    case "integer":
      return Math.round(value).toLocaleString("en-IN");
    default:
      return value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
}

/** Split a pasted blob into tickers. Commas, spaces and newlines all separate. */
function splitSymbols(raw: string): string[] {
  return raw
    .split(/[,\s;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function WatchlistPanel({ permissions, onOpenChart }: Props) {
  const mayWrite = permissions.includes("watchlist:write");

  const [lists, setLists] = useState<WatchlistSummary[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WatchlistDetail | null>(null);
  const [quotes, setQuotes] = useState<WatchlistQuotes | null>(null);
  const [specs, setSpecs] = useState<ColumnSpec[]>([]);

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [live, setLive] = useState(true);
  const [showColumns, setShowColumns] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState("");
  const [renameValue, setRenameValue] = useState("");

  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<InstrumentRecord[]>([]);
  const [highlight, setHighlight] = useState(0);
  const [searching, setSearching] = useState(false);

  const searchSeq = useRef(0);

  const specByKey = useMemo(() => {
    const map = new Map<string, ColumnSpec>();
    for (const s of specs) map.set(s.key, s);
    return map;
  }, [specs]);

  /** Columns in the order the watchlist stores them, resolved to full specs. */
  const activeSpecs = useMemo<ColumnSpec[]>(() => {
    if (!detail) return [];
    return detail.columns.map(
      (key) =>
        specByKey.get(key) ?? {
          key,
          label: key,
          group: "other",
          kind: "text",
          available: true,
          requires: null,
          description: "",
        },
    );
  }, [detail, specByKey]);

  // ── loading ────────────────────────────────────────────────────────────────
  const loadQuotes = useCallback(
    async (id: string, withLive: boolean) => {
      try {
        setQuotes(await getWatchlistQuotes(id, withLive));
      } catch (e) {
        // A quote failure must not blank the table — the list itself is still
        // valid and the user can still edit it.
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  const selectList = useCallback(
    async (id: string, withLive: boolean) => {
      setActiveId(id);
      setError(null);
      setQuotes(null);
      try {
        const d = await getWatchlist(id);
        setDetail(d);
        // Keep the rail's badge in step. `lists` was fetched before this list was
        // edited, so without this the count keeps showing the size it had when the
        // panel mounted — "Core · 0" next to a table with two rows.
        setLists((prev) =>
          prev
            ? prev.map((w) =>
                w.watchlist_id === id ? { ...w, item_count: d.items.length } : w,
              )
            : prev,
        );
        void loadQuotes(id, withLive);
      } catch (e) {
        setDetail(null);
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [loadQuotes],
  );

  const loadLists = useCallback(
    async (preferId?: string) => {
      setLoading(true);
      try {
        const [{ watchlists }, columns] = await Promise.all([
          listWatchlists(),
          getAvailableColumns(),
        ]);
        setLists(watchlists);
        setSpecs(columns.columns);

        const wanted =
          preferId ??
          (activeId && watchlists.some((w) => w.watchlist_id === activeId)
            ? activeId
            : null) ??
          watchlists.find((w) => w.is_default)?.watchlist_id ??
          watchlists[0]?.watchlist_id ??
          null;

        if (wanted) await selectList(wanted, live);
        else {
          setActiveId(null);
          setDetail(null);
          setQuotes(null);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        setLists(null);
      } finally {
        setLoading(false);
      }
    },
    // `live` is read, not depended on: including it would re-fetch the whole
    // list every time the toggle flips, which is a wasted round trip.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeId, selectList],
  );

  useEffect(() => {
    void loadLists();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live polling. Cleared on unmount, on list change, and when the toggle goes
  // off — a leaked interval here would keep hammering the broker for a panel
  // the user has already navigated away from.
  useEffect(() => {
    if (!live || !activeId) return;
    const t = setInterval(() => void loadQuotes(activeId, true), LIVE_REFRESH_MS);
    return () => clearInterval(t);
  }, [live, activeId, loadQuotes]);

  // ── symbol search ──────────────────────────────────────────────────────────
  useEffect(() => {
    const q = draft.trim();
    // A comma or space means the user is pasting a list, not searching.
    if (q.length < 1 || /[,\s;]/.test(q)) {
      setSuggestions([]);
      return;
    }
    const seq = ++searchSeq.current;
    setSearching(true);
    const t = setTimeout(() => {
      searchInstruments(q, { exchange: detail?.exchange, limit: 8 })
        .then((res) => {
          if (seq !== searchSeq.current) return;
          setSuggestions(res.results);
          setHighlight(0);
        })
        .catch(() => {
          if (seq === searchSeq.current) setSuggestions([]);
        })
        .finally(() => {
          if (seq === searchSeq.current) setSearching(false);
        });
    }, 220);
    return () => clearTimeout(t);
  }, [draft, detail?.exchange]);

  // ── mutations ──────────────────────────────────────────────────────────────
  function report(e: unknown) {
    if (e instanceof ApiError) {
      setError(
        e.code === "forbidden" || e.status === 403
          ? "Your role does not allow changing watchlists."
          : e.message,
      );
      return;
    }
    setError(e instanceof Error ? e.message : String(e));
  }

  async function createList() {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createWatchlist({ name, exchange: detail?.exchange ?? "NSEEQ" });
      setNewName("");
      setRenaming(false);
      setNotice(`Created “${created.name}”.`);
      await loadLists(created.watchlist_id);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function renameList() {
    if (!activeId || !renameValue.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateWatchlist(activeId, { name: renameValue.trim() });
      setDetail(updated);
      setRenameValue("");
      setRenaming(false);
      setLists((prev) =>
        prev
          ? prev.map((w) =>
              w.watchlist_id === updated.watchlist_id ? { ...w, name: updated.name } : w,
            )
          : prev,
      );
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function removeList() {
    if (!activeId || !detail) return;
    if (!window.confirm(`Delete “${detail.name}” and its ${detail.items.length} symbols?`)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteWatchlist(activeId);
      setNotice(`Deleted “${detail.name}”.`);
      setActiveId(null);
      setDetail(null);
      setQuotes(null);
      await loadLists();
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function makeDefault() {
    if (!activeId) return;
    setBusy(true);
    try {
      await updateWatchlist(activeId, { is_default: true });
      await loadLists(activeId);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function addSymbols() {
    if (!activeId) return;
    const symbols = splitSymbols(draft);
    if (symbols.length === 0) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await addWatchlistItems(activeId, symbols);
      setDraft("");
      setSuggestions([]);
      const parts: string[] = [];
      if (res.added.length) parts.push(`added ${res.added.join(", ")}`);
      if (res.skipped.length) parts.push(`already present: ${res.skipped.join(", ")}`);
      // Unknown tickers are surfaced, never silently dropped — a typo that
      // vanishes looks like a successful add.
      if (res.unknown.length) parts.push(`not in the instrument master: ${res.unknown.join(", ")}`);
      setNotice(parts.join(" · ") || "Nothing to add.");
      await selectList(activeId, live);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function removeSymbol(symbol: string) {
    if (!activeId) return;
    setBusy(true);
    setError(null);
    try {
      await removeWatchlistItem(activeId, symbol);
      await selectList(activeId, live);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  async function move(index: number, delta: number) {
    if (!activeId || !detail) return;
    const next = [...detail.items];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    // Optimistic: the order is a local concern and the call is idempotent, so a
    // failure is repaired by the reload rather than blocking the interaction.
    setDetail({ ...detail, items: next });
    setBusy(true);
    try {
      const updated = await reorderWatchlistItems(activeId, next);
      setDetail(updated);
    } catch (e) {
      report(e);
      await selectList(activeId, live);
    } finally {
      setBusy(false);
    }
  }

  async function toggleColumn(key: string) {
    if (!activeId || !detail) return;
    const has = detail.columns.includes(key);
    const next = has
      ? detail.columns.filter((k) => k !== key)
      : [...detail.columns, key];
    if (next.length === 0) {
      setError("A watchlist needs at least one column.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await setWatchlistColumns(activeId, next);
      setDetail(updated);
      if (updated.rejected_columns?.length) {
        setNotice(`Ignored unknown columns: ${updated.rejected_columns.join(", ")}`);
      }
      void loadQuotes(activeId, live);
    } catch (e) {
      report(e);
    } finally {
      setBusy(false);
    }
  }

  // ── render ─────────────────────────────────────────────────────────────────
  const staleCount = quotes?.rows.filter((r) => r.stale).length ?? 0;
  const noHistory = quotes?.rows.filter((r) => r.error).length ?? 0;

  if (loading && !lists) {
    return (
      <Card className="grid place-items-center p-10 text-muted-foreground">
        <Loader2 className="size-5 animate-spin" />
      </Card>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
      {/* ── list rail ─────────────────────────────────────────────────────── */}
      <Card className="p-3">
        <div className="mb-2 flex items-center justify-between px-1">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Watchlists
          </span>
          {mayWrite ? (
            <Button
              size="sm"
              variant="ghost"
              title="New watchlist"
              onClick={() => {
                setRenaming(true);
                setNewName("");
              }}
            >
              <Plus className="size-3.5" />
            </Button>
          ) : null}
        </div>

        {renaming ? (
          <div className="mb-2 grid gap-1.5 rounded-lg border border-border p-2">
            <Input
              value={newName}
              onChange={setNewName}
              placeholder="Name"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter") void createList();
                if (e.key === "Escape") setRenaming(false);
              }}
            />
            <div className="flex gap-1.5">
              <Button size="sm" disabled={busy || !newName.trim()} onClick={() => void createList()}>
                Create
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setRenaming(false)}>
                Cancel
              </Button>
            </div>
          </div>
        ) : null}

        {lists && lists.length === 0 && !renaming ? (
          <Hint className="px-1 py-2">
            No watchlists yet. {mayWrite ? "Create one to start tracking symbols." : "Ask an owner to create one."}
          </Hint>
        ) : null}

        <div className="grid gap-1">
          {lists?.map((w) => (
            <button
              key={w.watchlist_id}
              type="button"
              onClick={() => void selectList(w.watchlist_id, live)}
              className={cn(
                "flex items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors",
                w.watchlist_id === activeId
                  ? "bg-primary/10 font-semibold text-foreground"
                  : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
              )}
            >
              <span className="flex min-w-0 items-center gap-1.5">
                {w.is_default ? (
                  <Star className="size-3 shrink-0 fill-amber-500 text-amber-500" />
                ) : null}
                <span className="truncate">{w.name}</span>
              </span>
              <span className="shrink-0 text-[11px] tabular-nums opacity-70">{w.item_count}</span>
            </button>
          ))}
        </div>
      </Card>

      {/* ── the list itself ───────────────────────────────────────────────── */}
      <Card className="min-w-0 p-4">
        {!detail ? (
          <Hint>
            {error
              ? error
              : "Select a watchlist, or create one. Every list is scoped to your account."}
          </Hint>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2.5">
              <div className="min-w-0">
                {renaming && renameValue !== "" ? null : null}
                <h3 className="truncate text-base font-semibold tracking-tight">
                  {detail.name}
                  {detail.is_default ? (
                    <span className="ml-2 align-middle text-[10px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">
                      default
                    </span>
                  ) : null}
                </h3>
                <Hint>
                  {detail.items.length} symbols · {detail.exchange} ·{" "}
                  {quotes ? `source ${quotes.source}` : "loading quotes…"}
                  {staleCount > 0 ? ` · ${staleCount} from cache` : ""}
                  {noHistory > 0 ? ` · ${noHistory} with no local history` : ""}
                </Hint>
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <Switch checked={live} onCheckedChange={setLive} label="Live" />
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy}
                  onClick={() => void selectList(detail.watchlist_id, live)}
                  title="Re-pull quotes"
                >
                  <RefreshCw className={cn("size-3.5", busy && "animate-spin")} />
                </Button>
                <Button
                  size="sm"
                  variant={showColumns ? "primary" : "secondary"}
                  onClick={() => setShowColumns((v) => !v)}
                  title="Choose columns"
                >
                  <Columns3 className="size-3.5" />
                  Columns
                </Button>
                {mayWrite ? (
                  <>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        setRenaming((v) => !v);
                        setRenameValue(detail.name);
                      }}
                      title="Rename"
                    >
                      <Pencil className="size-3.5" />
                    </Button>
                    {!detail.is_default ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={busy}
                        onClick={() => void makeDefault()}
                        title="Make this my default list"
                      >
                        <Star className="size-3.5" />
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy}
                      onClick={() => void removeList()}
                      title="Delete this watchlist"
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </>
                ) : null}
              </div>
            </div>

            {renaming && mayWrite ? (
              <div className="mt-3 flex items-end gap-1.5">
                <div className="w-64 max-w-full">
                  <Input
                    label="New name"
                    value={renameValue}
                    onChange={setRenameValue}
                    autoFocus
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void renameList();
                      if (e.key === "Escape") setRenaming(false);
                    }}
                  />
                </div>
                <Button size="sm" disabled={busy || !renameValue.trim()} onClick={() => void renameList()}>
                  Save
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setRenaming(false)}>
                  Cancel
                </Button>
              </div>
            ) : null}

            {showColumns ? (
              <ColumnPicker
                specs={specs}
                selected={detail.columns}
                disabled={busy || !mayWrite}
                onToggle={(key) => void toggleColumn(key)}
              />
            ) : null}

            {/* ── add symbols ─────────────────────────────────────────────── */}
            {mayWrite ? (
              <div className="relative mt-3.5 flex items-end gap-1.5">
                <div className="w-full max-w-xl">
                  <Input
                    label="Add symbols"
                    value={draft}
                    onChange={setDraft}
                    placeholder="RELIANCE, INFY-EQ — or paste a list"
                    rightIcon={
                      searching ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : draft ? (
                        <button
                          type="button"
                          title="Clear"
                          onClick={() => {
                            setDraft("");
                            setSuggestions([]);
                          }}
                        >
                          <X className="size-4" />
                        </button>
                      ) : undefined
                    }
                    onKeyDown={(e) => {
                      if (e.key === "ArrowDown") {
                        e.preventDefault();
                        setHighlight((h) => Math.min(h + 1, suggestions.length - 1));
                      } else if (e.key === "ArrowUp") {
                        e.preventDefault();
                        setHighlight((h) => Math.max(h - 1, 0));
                      } else if (e.key === "Enter") {
                        e.preventDefault();
                        if (suggestions[highlight]) {
                          // Commit the highlighted match rather than the raw
                          // text, so a half-typed ticker becomes a real one.
                          setDraft(suggestions[highlight].symbol);
                          setSuggestions([]);
                          return;
                        }
                        void addSymbols();
                      } else if (e.key === "Escape") {
                        setSuggestions([]);
                      }
                    }}
                  />
                </div>
                <Button size="md" disabled={busy || !draft.trim()} onClick={() => void addSymbols()}>
                  <Plus className="size-4" />
                  Add
                </Button>

                {suggestions.length > 0 ? (
                  <ul className="absolute left-0 top-full z-20 mt-1 max-h-72 w-full max-w-xl overflow-auto rounded-xl border border-border bg-card py-1 shadow-lg">
                    {suggestions.map((s, i) => (
                      <li key={`${s.exchange}:${s.symbol}`}>
                        <button
                          type="button"
                          onMouseEnter={() => setHighlight(i)}
                          onClick={() => {
                            setDraft(s.symbol);
                            setSuggestions([]);
                          }}
                          className={cn(
                            "flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-[13px]",
                            i === highlight ? "bg-primary/10" : "hover:bg-muted/60",
                          )}
                        >
                          <span className="min-w-0">
                            <span className="font-semibold">{s.symbol}</span>
                            {s.name ? (
                              <span className="ml-2 truncate text-muted-foreground">{s.name}</span>
                            ) : null}
                          </span>
                          <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                            {s.exchange}
                            {s.bars ? ` · ${s.bars} bars` : " · no local data"}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}

            {error ? (
              <div className="mt-3">
                <ErrorBox>{error}</ErrorBox>
              </div>
            ) : null}
            {notice ? (
              <Hint className="mt-3">{notice}</Hint>
            ) : null}

            {/* ── the table ──────────────────────────────────────────────── */}
            {detail.items.length === 0 ? (
              <Hint className="mt-4">
                No symbols yet.{mayWrite ? " Add one above." : ""}
              </Hint>
            ) : (
              <div className="mt-3 overflow-x-auto rounded-xl border border-border">
                <table className="w-full border-collapse text-[13px]">
                  <thead>
                    <tr className="border-b border-border bg-muted/40 text-left">
                      <th className="w-8 px-2 py-2" />
                      <th className="px-3 py-2 font-semibold">Symbol</th>
                      {activeSpecs.map((c) => (
                        <th key={c.key} className="px-3 py-2 text-right font-semibold">
                          {c.label}
                        </th>
                      ))}
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {detail.items.map((symbol, index) => {
                      const row = quotes?.rows.find((r) => r.symbol === symbol);
                      return (
                        <tr
                          key={symbol}
                          className="border-b border-border/60 transition-colors last:border-0 hover:bg-primary/[0.03]"
                        >
                          <td className="px-2 py-1.5">
                            {/* Freshness is per row: a cached close shown next to a
                                live one is the single most misleading thing this
                                table could do. */}
                            <span
                              title={
                                !row
                                  ? "loading"
                                  : row.error
                                    ? row.error
                                    : row.stale
                                      ? "cached close, not a live price"
                                      : "live"
                              }
                              className={cn(
                                "block size-1.5 rounded-full",
                                !row
                                  ? "bg-muted-foreground/30"
                                  : row.error
                                    ? "bg-destructive"
                                    : row.stale
                                      ? "bg-amber-500"
                                      : "animate-pulse bg-emerald-500",
                              )}
                            />
                          </td>
                          <td className="px-3 py-1.5">
                            <button
                              type="button"
                              className="text-left font-semibold hover:underline"
                              onClick={() => onOpenChart?.(symbol)}
                              title="Open chart"
                            >
                              {symbol}
                            </button>
                            {row?.name ? (
                              <span className="ml-2 text-[11px] text-muted-foreground">
                                {row.name}
                              </span>
                            ) : null}
                          </td>
                          {activeSpecs.map((c) => {
                            const raw = row?.[c.key];
                            const numeric = typeof raw === "number" ? raw : null;
                            return (
                              <td
                                key={c.key}
                                className={cn(
                                  "px-3 py-1.5 text-right tabular-nums",
                                  SIGNED_COLUMNS.has(c.key) && numeric !== null
                                    ? numeric >= 0
                                      ? "text-emerald-600 dark:text-emerald-400"
                                      : "text-destructive"
                                    : "",
                                  numeric === null && "text-muted-foreground",
                                )}
                                title={
                                  !c.available && c.requires
                                    ? `Not collected yet — requires ${c.requires}`
                                    : undefined
                                }
                              >
                                {formatValue(raw, c.kind)}
                              </td>
                            );
                          })}
                          <td className="px-3 py-1.5">
                            <div className="flex items-center justify-end gap-0.5">
                              {mayWrite ? (
                                <>
                                  <button
                                    type="button"
                                    title="Move up"
                                    disabled={busy || index === 0}
                                    onClick={() => void move(index, -1)}
                                    className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
                                  >
                                    <ArrowUp className="size-3.5" />
                                  </button>
                                  <button
                                    type="button"
                                    title="Move down"
                                    disabled={busy || index === detail.items.length - 1}
                                    onClick={() => void move(index, 1)}
                                    className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
                                  >
                                    <ArrowDown className="size-3.5" />
                                  </button>
                                  <button
                                    type="button"
                                    title="Remove from this list"
                                    disabled={busy}
                                    onClick={() => void removeSymbol(symbol)}
                                    className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-30"
                                  >
                                    <X className="size-3.5" />
                                  </button>
                                </>
                              ) : null}
                              <button
                                type="button"
                                title="Open chart"
                                onClick={() => onOpenChart?.(symbol)}
                                className="grid size-6 place-items-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
                              >
                                <BarChart3 className="size-3.5" />
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            <Hint className="mt-3">
              Prices come from the local daily cache and are overlaid with live broker
              quotes when a session is active. A row with a green dot is live; amber means
              the cached close; red means there is no local history for that symbol.
              {!mayWrite ? " Your role can read this list but not change it." : ""}
            </Hint>
          </>
        )}
      </Card>
    </div>
  );
}

/**
 * The column picker.
 *
 * Columns the backend cannot populate yet are listed, disabled, and labelled with
 * what they need. Hiding them would leave the user wondering why market cap is
 * missing; showing them as zero would be a lie.
 */
function ColumnPicker({
  specs,
  selected,
  disabled,
  onToggle,
}: {
  specs: ColumnSpec[];
  selected: string[];
  disabled: boolean;
  onToggle: (key: string) => void;
}) {
  const groups = useMemo(() => {
    const map = new Map<string, ColumnSpec[]>();
    for (const s of specs) {
      const list = map.get(s.group) ?? [];
      list.push(s);
      map.set(s.group, list);
    }
    return [...map.entries()];
  }, [specs]);

  if (specs.length === 0) {
    return (
      <div className="mt-3 rounded-xl border border-border p-3">
        <Hint>Loading the column registry…</Hint>
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-xl border border-border bg-muted/20 p-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {groups.map(([group, items]) => (
          <div key={group}>
            <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {group}
            </div>
            <div className="grid gap-0.5">
              {items.map((c) => {
                const on = selected.includes(c.key);
                return (
                  <button
                    key={c.key}
                    type="button"
                    disabled={disabled || !c.available}
                    onClick={() => onToggle(c.key)}
                    title={c.available ? c.description : `Not collected yet — requires ${c.requires}`}
                    className={cn(
                      "flex items-center gap-2 rounded px-1.5 py-1 text-left text-[12.5px] transition-colors",
                      !c.available
                        ? "cursor-not-allowed text-muted-foreground/50"
                        : on
                          ? "text-foreground hover:bg-muted/60"
                          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                    )}
                  >
                    <span
                      className={cn(
                        "grid size-3.5 shrink-0 place-items-center rounded border",
                        on && c.available
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border",
                      )}
                    >
                      {on && c.available ? <Check className="size-2.5" /> : null}
                    </span>
                    <span className="truncate">{c.label}</span>
                    {!c.available ? (
                      <span className="ml-auto shrink-0 text-[10px] italic">needs {c.requires}</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
