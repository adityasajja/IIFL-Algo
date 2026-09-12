import { useEffect, useState } from "react";
import {
  getHealth,
  getHistoryStatus,
  getInstrumentsStatus,
  getLoginStatus,
  logoutSession,
  searchSymbols,
  type CacheExchange,
  type Health,
  type InstrumentSegment,
  type LoginStatus,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Badge, Stat, fmtNum } from "./components/ui/stat";
import { useToast } from "./components/ui/toast-context";

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hours = (Date.now() - d.getTime()) / 3_600_000;
  const rel = hours < 1 ? "just now" : hours < 48 ? `${Math.round(hours)}h ago` : `${Math.round(hours / 24)}d ago`;
  return `${d.toLocaleString("en-IN")} · ${rel}`;
}

export default function SystemPanel() {
  const { toast } = useToast();
  const [health, setHealth] = useState<Health | null>(null);
  const [auth, setAuth] = useState<LoginStatus | null>(null);
  const [history, setHistory] = useState<{
    cache_root: string;
    exchanges: CacheExchange[];
    total_symbols: number;
  } | null>(null);
  const [instruments, setInstruments] = useState<{
    cache_dir: string;
    segments: InstrumentSegment[];
    total_contracts: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [logoutState, setLogoutState] = useState<ButtonState>("idle");

  const [query, setQuery] = useState("");
  const [exchange, setExchange] = useState("NSEEQ");
  const [hits, setHits] = useState<{ symbol: string; exchange: string; conid: string }[] | null>(
    null,
  );
  const [searchState, setSearchState] = useState<ButtonState>("idle");
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    // Loaded independently rather than with Promise.all: the health probe can
    // lag behind the cache reads, and one slow call should not blank the rest.
    let alive = true;
    const fail = (e: unknown) => {
      if (alive) setError(e instanceof Error ? e.message : String(e));
    };
    getHealth()
      .then((h) => alive && setHealth(h))
      .catch(fail);
    getLoginStatus()
      .then((a) => alive && setAuth(a))
      .catch(fail);
    getHistoryStatus()
      .then((h) => alive && setHistory(h))
      .catch(fail);
    getInstrumentsStatus()
      .then((i) => alive && setInstruments(i))
      .catch(fail);
    return () => {
      alive = false;
    };
  }, []);

  async function onLogout() {
    setLogoutState("loading");
    try {
      await logoutSession();
      setLogoutState("success");
      setAuth(await getLoginStatus());
      toast({ title: "Session cleared", status: "success" });
    } catch (e) {
      setLogoutState("error");
      toast({
        title: "Logout failed",
        description: e instanceof Error ? e.message : String(e),
        status: "error",
      });
    }
  }

  async function onSearch() {
    setSearchState("loading");
    setSearchError(null);
    try {
      const res = await searchSymbols(query, exchange);
      setHits(res.results);
      setSearchState("success");
    } catch (e) {
      setHits(null);
      setSearchError(e instanceof Error ? e.message : String(e));
      setSearchState("error");
    }
  }

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader
          title="Session & engine"
          sub="The IIFL JWT dies at midnight IST, so logging in is a daily step."
          action={
            auth?.session_active ? (
              <StatefulButton
                state={logoutState}
                variant="secondary"
                onClick={() => void onLogout()}
                loadingText="Clearing…"
                successText="Cleared"
                errorText="Failed — retry"
              >
                Log out
              </StatefulButton>
            ) : auth ? (
              <Button
                size="sm"
                onClick={() => window.open(auth.login_url, "_blank", "noopener")}
              >
                Open IIFL login
              </Button>
            ) : null
          }
        />
        <div className="space-y-3.5 p-5 pt-3">
          {error && <ErrorBox>{error}</ErrorBox>}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Environment"
              value={health ? health.env : "…"}
              tone={health ? "good" : "warn"}
              sub={health ? health.status : "probing"}
              hint="Order placement is blocked unless this is paper or live."
            />
            <Stat
              label="Broker session"
              value={auth ? (auth.session_active ? "active" : "expired") : "…"}
              tone={auth ? (auth.session_active ? "good" : "warn") : "warn"}
              sub={auth?.client_id ?? "—"}
            />
            <Stat
              label="Database"
              value={health?.database ? "up" : "off"}
              tone={health?.database ? "good" : "warn"}
              sub="optional — Postgres"
            />
            <Stat
              label="Session expires"
              value={
                auth?.expires_at
                  ? new Date(auth.expires_at).toLocaleTimeString("en-IN", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "—"
              }
              sub={when(auth?.expires_at ?? null)}
            />
          </div>
          {auth?.session_active && auth.expires_at && (
            <Hint>
              Expires at midnight IST. Run{" "}
              <code className="font-mono">atr login</code> after that to get a new code —
              the auth code is single-use.
            </Hint>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Daily history cache"
          sub="Parquet files the scanner, backtester and validator read. No broker session needed."
          action={
            history ? (
              <Badge tone={history.total_symbols ? "good" : "warn"}>
                {history.total_symbols.toLocaleString("en-IN")} symbols
              </Badge>
            ) : null
          }
        />
        <div className="p-5 pt-3">
          {!history ? (
            <Hint>Reading the cache…</Hint>
          ) : history.exchanges.length === 0 ? (
            <Hint>
              Empty. Populate it with{" "}
              <code className="font-mono">atr history sync --exchange NSEEQ</code>.
            </Hint>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-left">
                    <th className="px-3 py-2 font-semibold">Exchange</th>
                    <th className="px-3 py-2 text-right font-semibold">Symbols</th>
                    <th className="px-3 py-2 text-right font-semibold">Size</th>
                    <th className="px-3 py-2 font-semibold">Last sync</th>
                  </tr>
                </thead>
                <tbody>
                  {history.exchanges.map((e) => (
                    <tr key={e.exchange} className="border-b border-border/60 last:border-0">
                      <td className="px-3 py-1.5 font-semibold">{e.exchange}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {e.symbols.toLocaleString("en-IN")}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{e.size_mb} MB</td>
                      <td className="px-3 py-1.5 text-muted-foreground">{when(e.last_sync)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {history && (
            <Hint className="mt-3">
              <code className="font-mono">{history.cache_root}</code> — refresh nightly
              with <code className="font-mono">atr history sync</code>. Files written today
              are skipped, so a cron stays incremental.
            </Hint>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Instrument master"
          sub="Contract files per segment, used to resolve symbols to instrument IDs."
          action={
            instruments ? (
              <Badge>{instruments.total_contracts.toLocaleString("en-IN")} contracts</Badge>
            ) : null
          }
        />
        <div className="p-5 pt-3">
          {!instruments ? (
            <Hint>Reading the contract cache…</Hint>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-left">
                    <th className="px-3 py-2 font-semibold">Segment</th>
                    <th className="px-3 py-2 text-right font-semibold">Contracts</th>
                    <th className="px-3 py-2 text-right font-semibold">Size</th>
                    <th className="px-3 py-2 font-semibold">Synced</th>
                  </tr>
                </thead>
                <tbody>
                  {instruments.segments.map((s) => (
                    <tr key={s.segment} className="border-b border-border/60 last:border-0">
                      <td className="px-3 py-1.5 font-semibold">{s.segment}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">
                        {fmtNum(s.contracts, 0)}
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{s.size_mb} MB</td>
                      <td className="px-3 py-1.5 text-muted-foreground">{when(s.synced_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <Hint className="mt-3">
            Note: <code className="font-mono">marketquotes</code> is not served for BSECURR
            or MCXCOMM — they resolve but return no prices.
          </Hint>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Symbol lookup"
          sub="Served from the cached contract files, so no session is required."
        />
        <div className="space-y-3.5 p-5 pt-3">
          <div className="grid gap-3.5 sm:grid-cols-[2fr_1fr_auto]">
            <Input
              label="Query (blank = all equities)"
              value={query}
              onChange={setQuery}
              placeholder="NIFTY"
            />
            <Input label="Exchange" value={exchange} onChange={setExchange} />
            <div className="flex items-end">
              <StatefulButton
                state={searchState}
                variant="secondary"
                onClick={() => void onSearch()}
                loadingText="Searching…"
                successText="Found"
                errorText="Failed — retry"
              >
                Search
              </StatefulButton>
            </div>
          </div>
          {searchError && <ErrorBox>{searchError}</ErrorBox>}
          {hits && hits.length === 0 && <Hint>No matches.</Hint>}
          {hits && hits.length > 0 && (
            <div className="overflow-hidden rounded-xl border border-border">
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-border bg-muted/40 text-left">
                    <th className="px-3 py-2 font-semibold">Symbol</th>
                    <th className="px-3 py-2 font-semibold">Exchange</th>
                    <th className="px-3 py-2 text-right font-semibold">Instrument ID</th>
                  </tr>
                </thead>
                <tbody>
                  {hits.map((h) => (
                    <tr key={h.conid} className="border-b border-border/60 last:border-0">
                      <td className="px-3 py-1.5 font-semibold">{h.symbol}</td>
                      <td className="px-3 py-1.5 text-muted-foreground">{h.exchange}</td>
                      <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                        {h.conid}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}
