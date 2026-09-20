import { Star } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  getForwardTracker,
  getGapPlan,
  getInsightSettings,
  getInsights,
  saveInsightSettings,
  sendInsights,
  type ForwardTracker,
  type GapPlan,
  type GapPlanStats,
  type HoldingFlag,
  type InsightIdea,
  type InsightsDigest,
  type InsightsSettings,
} from "./api";
import { Button } from "./components/ui/button";
import { ErrorBox } from "./components/ui/card";
import { PageLoader } from "./components/ui/loading";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";
import { formatIst } from "./lib/format";
import { cn } from "./lib/utils";

const TONE = {
  good: "bg-emerald-500/10 text-emerald-500",
  bad: "bg-rose-500/10 text-rose-500",
  warn: "bg-amber-500/10 text-amber-500",
  flat: "bg-muted text-muted-foreground",
};

const SETUP_TONE: Record<string, string> = {
  strong_rs: TONE.good,
  oversold: TONE.warn,
  resting_leader: TONE.flat,
};

const FLAG_TONE: Record<HoldingFlag["kind"], string> = {
  slipped: TONE.bad,
  protect: TONE.warn,
  quiet: TONE.flat,
  lagging: TONE.flat,
};

const inr = (n: number) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const signed = (n: number, d = 1) => `${n > 0 ? "+" : ""}${n.toFixed(d)}%`;
const dateLabel = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/** What the system thinks today: the market, what to consider buying, and how your holdings look. */
export default function TodayPanel({ onOpenChart }: { onOpenChart?: (symbol: string) => void }) {
  const { toast } = useToast();
  const [digest, setDigest] = useState<InsightsDigest | null>(null);
  const [settings, setSettings] = useState<InsightsSettings | null>(null);
  const [tracker, setTracker] = useState<ForwardTracker | null>(null);
  const [plan, setPlan] = useState<GapPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const load = useCallback(async (fresh = false) => {
    try {
      const [d, s] = await Promise.all([getInsights(fresh), getInsightSettings()]);
      setDigest(d);
      setSettings(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    getForwardTracker().then(setTracker).catch(() => setTracker(null));
    getGapPlan().then(setPlan).catch(() => setPlan(null));
  }, [load]);

  async function update(patch: Partial<InsightsSettings>) {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    try {
      setSettings(await saveInsightSettings(next));
    } catch (e) {
      setSettings(settings);
      toast({ title: "Couldn't save", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function sendNow() {
    setSending(true);
    try {
      const r = await sendInsights();
      toast({
        title: r.sent ? "Sent" : "Not sent",
        description: r.sent ? `Delivered by ${r.channel ?? "your channel"}.` : "No channel accepted it. Check the Telegram settings.",
        status: r.sent ? "success" : "error",
      });
      if (r.sent) void load(true);
    } catch (e) {
      toast({ title: "Couldn't send", description: e instanceof Error ? e.message : String(e), status: "error" });
    } finally {
      setSending(false);
    }
  }

  if (!digest && !error) return <PageLoader label="Reading the market" />;
  if (!digest) return <ErrorBox>{error}</ErrorBox>;

  const m = digest.market;
  const held = digest.holdings;
  const flagged = held.items.filter((h) => h.flags.length > 0);
  const calm = held.items.length - flagged.length;
  // The same caution applies to every idea, so it is said once.
  const marketNote = digest.ideas.find((i) => i.market_note)?.market_note;

  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}

      {/* The market, in a word, and what changed. */}
      <div className="rounded-2xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <span className={cn("rounded-full px-3 py-1 text-2xl font-semibold tracking-tight", TONE[m.tone as keyof typeof TONE] ?? TONE.flat)}>
                {m.word}
              </span>
              <span className="text-xs text-muted-foreground">market</span>
            </div>
            <p className="mt-3 max-w-xl text-sm">{m.headline}</p>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">{m.context}</p>
          </div>
          {m.nifty_close != null && (
            <div className="text-right">
              <div className="text-xs text-muted-foreground">Nifty 50</div>
              <div className="text-2xl font-semibold tabular-nums">{inr(m.nifty_close)}</div>
              {m.nifty_change_1d_pct != null && (
                <div className={cn("text-xs tabular-nums", m.nifty_change_1d_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
                  {signed(m.nifty_change_1d_pct, 2)} today
                </div>
              )}
            </div>
          )}
        </div>

        {digest.changes.length > 0 && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-4">
            <span className="text-xs text-muted-foreground">Since last time</span>
            {digest.changes.map((c) => (
              <span key={c} className="rounded-full bg-amber-500/10 px-2.5 py-1 text-xs text-amber-500">
                {c}
              </span>
            ))}
          </div>
        )}
        {digest.stale && digest.data_as_of && (
          <div className="mt-3 text-xs text-amber-500">
            Stock data is from {dateLabel(digest.data_as_of)} ({digest.stale_days} days old), so the lists below describe that day.
          </div>
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {/* Things worth considering buying, only setups that held up in history. */}
        <div className="rounded-2xl border border-border bg-card">
          <div className="flex items-baseline justify-between px-5 pt-5">
            <span className="text-sm font-semibold">Worth a look</span>
            <span className="text-xs text-muted-foreground" title="Only setups that beat the average in both halves of the history.">
              backed by history
            </span>
          </div>
          {marketNote && <div className="px-5 pt-2 text-xs text-amber-500">{marketNote}</div>}
          {digest.ideas.length === 0 ? (
            <div className="px-5 py-8 text-sm text-muted-foreground">Nothing matches today.</div>
          ) : (
            <div className="mt-3 divide-y divide-border">
              {digest.ideas.map((i) => (
                <IdeaRow key={i.symbol} idea={i} onOpenChart={onOpenChart} />
              ))}
            </div>
          )}
        </div>

        {/* Your holdings, described honestly. */}
        <div className="rounded-2xl border border-border bg-card">
          <div className="flex items-baseline justify-between px-5 pt-5">
            <span className="text-sm font-semibold">Your holdings</span>
            {held.source === "snapshot" && held.as_of && (
              <span className="text-xs text-muted-foreground">as of {formatIst(held.as_of)}</span>
            )}
          </div>
          {held.items.length === 0 ? (
            <div className="px-5 py-8 text-sm text-muted-foreground">No holdings on record yet.</div>
          ) : (
            <div className="mt-3 divide-y divide-border">
              {flagged.map((h) => (
                <div key={h.symbol} className="px-5 py-3">
                  <div className="flex items-center justify-between gap-3">
                    <button type="button" onClick={() => onOpenChart?.(h.symbol)} className="font-semibold hover:underline">
                      {h.symbol}
                    </button>
                    {h.pnl_pct != null && (
                      <span className={cn("text-sm tabular-nums", h.pnl_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
                        {signed(h.pnl_pct, 0)}
                      </span>
                    )}
                  </div>
                  <div className="mt-2 space-y-1.5">
                    {h.flags.map((f) => (
                      <div key={f.kind} className="flex items-start gap-2 text-xs" title={f.note}>
                        <span className={cn("mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium", FLAG_TONE[f.kind])}>
                          {f.title}
                        </span>
                        <span className="text-muted-foreground">{f.detail}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {calm > 0 && (
                <div className="px-5 py-3 text-xs text-muted-foreground">
                  {calm} {calm === 1 ? "holding looks" : "holdings look"} fine.
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {plan && <GapPlanCard plan={plan} onOpenChart={onOpenChart} />}

      {tracker && <TrackerCard tracker={tracker} onOpenChart={onOpenChart} />}

      {/* What to be told, and when. */}
      {settings && (
        <div className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-border bg-card px-5 py-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <Switch checked={settings.enabled} onCheckedChange={(v) => void update({ enabled: v })} label={`Message me daily at ${settings.send_after}`} />
            <Switch checked={settings.market_changes} onCheckedChange={(v) => void update({ market_changes: v })} label="Market changes" />
            <Switch checked={settings.buy_ideas} onCheckedChange={(v) => void update({ buy_ideas: v })} label="Buy ideas" />
            <Switch checked={settings.holdings_watch} onCheckedChange={(v) => void update({ holdings_watch: v })} label="My holdings" />
          </div>
          <div className="flex items-center gap-3">
            {digest.last_sent && <span className="text-xs text-muted-foreground">Last sent {formatIst(digest.last_sent)}</span>}
            <Button variant="secondary" size="sm" onClick={() => void sendNow()} disabled={sending}>
              {sending ? "Sending…" : "Send now"}
            </Button>
          </div>
        </div>
      )}

      <p className="px-1 text-[11px] text-muted-foreground">{digest.disclaimer}</p>
    </div>
  );
}

function IdeaRow({ idea, onOpenChart }: { idea: InsightIdea; onOpenChart?: (symbol: string) => void }) {
  const fact =
    idea.setup === "oversold"
      ? `Down ${Math.abs(idea.move_20d_pct).toFixed(0)}% in 20 days`
      : `${signed(idea.vs_nifty_20d_pct, 0)} vs the Nifty over 20 days`;
  return (
    <div className="px-5 py-3.5" title={`${idea.evidence}${idea.risk ? ` ${idea.risk}` : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <button type="button" onClick={() => onOpenChart?.(idea.symbol)} className="font-semibold hover:underline">
            {idea.symbol}
          </button>
          <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", SETUP_TONE[idea.setup] ?? TONE.flat)}>
            {idea.setup_label}
          </span>
          {idea.is_new && <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">New</span>}
          {idea.on_watchlist && <Star className="size-3 fill-amber-500 text-amber-500" aria-label="On your watchlist" />}
        </div>
        <div className="text-right">
          <div className="text-sm font-medium tabular-nums">{inr(idea.price)}</div>
          <div className={cn("text-xs tabular-nums", idea.change_1d_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
            {signed(idea.change_1d_pct, 2)}
          </div>
        </div>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
        <span>{fact}</span>
        <span>Stop near {inr(idea.stop_price)}</span>
        {idea.sector && <span className="truncate">{idea.sector}</span>}
      </div>
    </div>
  );
}

const STATE_TONE: Record<ForwardTracker["state"], string> = {
  collecting: TONE.flat,
  working: TONE.good,
  not_working: TONE.bad,
  inconclusive: TONE.warn,
};

/** The forward test: signals written down before the week, graded after it. */
function TrackerCard({ tracker: t, onOpenChart }: { tracker: ForwardTracker; onOpenChart?: (symbol: string) => void }) {
  const pct = Math.min(100, (t.graded / t.needed) * 100);
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">Live test</span>
        <span className={cn("rounded-full px-2.5 py-0.5 text-[11px] font-medium", STATE_TONE[t.state])}>{t.verdict}</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {t.description}. Each Friday's picks are written down, then graded a week later on whether they gained 2% or more.
      </p>

      <div className="mt-4 grid grid-cols-3 gap-3">
        <div>
          <div className="text-xs text-muted-foreground">Hit rate</div>
          <div className="text-2xl font-semibold tabular-nums">{t.hit_rate_pct == null ? "—" : `${t.hit_rate_pct}%`}</div>
          <div className="text-[11px] text-muted-foreground">
            {t.range_pct ? `likely ${t.range_pct[0]}–${t.range_pct[1]}%` : "no graded picks yet"}
          </div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Any stock</div>
          <div className="text-2xl font-semibold tabular-nums">{t.base_rate_pct}%</div>
          <div className="text-[11px] text-muted-foreground">ordinary rate</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Avg week</div>
          <div className={cn("text-2xl font-semibold tabular-nums", t.avg_net_pct == null ? "" : t.avg_net_pct >= 0 ? "text-emerald-500" : "text-rose-500")}>
            {t.avg_net_pct == null ? "—" : signed(t.avg_net_pct, 2)}
          </div>
          <div className="text-[11px] text-muted-foreground">after costs</div>
        </div>
      </div>

      <div className="mt-4">
        <div className="mb-1 flex justify-between text-[11px] text-muted-foreground">
          <span>{t.graded} of {t.needed} picks graded</span>
          <span>{t.open.length} waiting on this week</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {t.open.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">This week's picks</span>
          {t.open.map((s) => (
            <button
              key={`${s.entry_date}-${s.symbol}`}
              type="button"
              onClick={() => onOpenChart?.(s.symbol)}
              className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium hover:bg-accent"
              title={`Picked ${s.entry_date} at ${inr(s.entry_close)}`}
            >
              {s.symbol}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const CALL_TONE = { trade: TONE.good, skip: TONE.warn, unknown: TONE.flat };
const EXIT_LABEL = { target: "Hit target", stop: "Stopped out", friday: "Sold Friday" } as const;

function PlanStat({ label, stats }: { label: string; stats: GapPlanStats }) {
  const good = (stats.avg_net_pct ?? 0) > 0;
  return (
    <div className="rounded-xl bg-muted/40 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      {stats.graded === 0 ? (
        <div className="mt-1 text-sm text-muted-foreground">No graded trades yet</div>
      ) : (
        <>
          <div className={cn("mt-1 text-xl font-semibold tabular-nums", good ? "text-emerald-500" : "text-rose-500")}>
            {signed(stats.avg_net_pct ?? 0, 2)}
            <span className="ml-1 text-xs font-normal text-muted-foreground">per trade</span>
          </div>
          <div className="text-[11px] text-muted-foreground">
            {stats.win_rate_pct}% hit target · {stats.graded} trades · {stats.weeks} {stats.weeks === 1 ? "week" : "weeks"}
          </div>
        </>
      )}
    </div>
  );
}

/** The Monday gap plan: this week's call, the results so far, and what is open. */
function GapPlanCard({ plan: p, onOpenChart }: { plan: GapPlan; onOpenChart?: (symbol: string) => void }) {
  const w = p.this_week;
  const pct = Math.min(100, (p.live.graded / p.needed) * 100);
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">Monday gap plan</span>
        <span className="text-[11px] text-muted-foreground">paper trades, no money at risk</span>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        After a rising week, buy stocks that open more than {Math.abs(p.plan.gap_pct)}% below Friday's close. Sell at +{p.plan.target_pct}%, stop at −{p.plan.stop_pct}%, otherwise sell Friday.
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <span className={cn("rounded-full px-3 py-1 text-sm font-semibold", CALL_TONE[w.status])}>
          {w.status === "trade" ? "This week: trade" : w.status === "skip" ? "This week: skip" : "This week: no reading"}
        </span>
        {w.median_pct != null && (
          <span className="text-xs text-muted-foreground">
            Last week the typical stock moved {signed(w.median_pct, 2)}; the plan needs more than +{w.needed_pct}%.
          </span>
        )}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <PlanStat label={`Live since ${p.live_from ?? "—"}`} stats={p.live} />
        <PlanStat label="Replay of recent weeks" stats={p.replay} />
      </div>
      <div className="mt-3">
        <div className="mb-1 flex justify-between text-[11px] text-muted-foreground">
          <span>{p.live.graded} of {p.needed} live trades graded</span>
          <span>{p.verdict}</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {p.open.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs text-muted-foreground">Open this week</div>
          <div className="divide-y divide-border rounded-xl border border-border">
            {p.open.map((t) => (
              <div key={`${t.entry_date}-${t.symbol}`} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <button type="button" onClick={() => onOpenChart?.(t.symbol)} className="font-semibold hover:underline">{t.symbol}</button>
                <span className="text-xs text-muted-foreground tabular-nums">
                  in at {inr(t.entry)} · target {inr(t.entry * (1 + p.plan.target_pct / 100))} · stop {inr(t.entry * (1 - p.plan.stop_pct / 100))}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {p.recent.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs text-muted-foreground">Latest results</div>
          <div className="divide-y divide-border rounded-xl border border-border">
            {p.recent.slice(0, 6).map((t) => (
              <div key={`${t.entry_date}-${t.symbol}`} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <span>
                  <span className="font-semibold">{t.symbol}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{dateLabel(t.entry_date)} · {t.exit_reason ? EXIT_LABEL[t.exit_reason] : ""}{t.source === "replay" ? " · replay" : ""}</span>
                </span>
                <span className={cn("tabular-nums", (t.net_pct ?? 0) >= 0 ? "text-emerald-500" : "text-rose-500")}>{signed(t.net_pct ?? 0, 2)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
