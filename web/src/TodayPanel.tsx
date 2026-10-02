import { IconStar } from "./icons";
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
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Badge, Stat } from "./components/ui/stat";
import { AnimatedBadge } from "./components/motion/animated-badge";
import { PageLoader } from "./components/ui/loading";
import { Switch } from "./components/motion/switch";
import { Tooltip } from "./components/motion/tooltip";
import { useToast } from "./components/ui/toast-context";
import { formatIst } from "./lib/format";
import { formatInr as inr, formatPct as signed } from "./lib/theme";
import { cn } from "./lib/utils";

const TONE = {
  good: "bg-gain/[0.08] text-gain border border-gain/20",
  bad: "bg-destructive/[0.08] text-destructive border border-destructive/20",
  warn: "bg-warning/[0.08] text-warning border border-warning/20",
  flat: "bg-muted/50 text-muted-foreground border border-border/60",
};

const FLAG_TONE: Record<HoldingFlag["kind"], string> = {
  slipped: TONE.bad,
  protect: TONE.warn,
  quiet: TONE.flat,
  lagging: TONE.flat,
};

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
      <Card className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <span className={cn("rounded-full px-3 py-1 text-2xl font-semibold tracking-tight", TONE[m.tone as keyof typeof TONE] ?? TONE.flat)}>
                {m.word}
              </span>
              <span className="text-xs text-muted-foreground">market</span>
            </div>
            <p className="mt-3 max-w-xl text-sm" title={m.context}>{m.headline}</p>
          </div>
          {m.nifty_close != null && (
            <div className="text-right">
              <div className="text-xs text-muted-foreground">Nifty 50</div>
              <div className="text-2xl font-semibold tabular-nums">{inr(m.nifty_close)}</div>
              {m.nifty_change_1d_pct != null && (
                <div className={cn("text-xs tabular-nums", m.nifty_change_1d_pct >= 0 ? "text-gain" : "text-loss")}>
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
              <Badge key={c} tone="warn">
                {c}
              </Badge>
            ))}
          </div>
        )}
        {digest.stale && digest.data_as_of && (
          <div className="mt-3 text-xs text-warning">
            Data from {dateLabel(digest.data_as_of)} ({digest.stale_days}d old)
          </div>
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        {/* Things worth considering buying, only setups that held up in history. */}
        <Card>
          <CardHeader title="Worth a look" />
          {marketNote && <div className="px-5 pt-2 text-xs text-warning" title={marketNote}>Market caution</div>}
          {digest.ideas.length === 0 ? (
            <div className="px-5 py-8 text-sm text-muted-foreground">Nothing today</div>
          ) : (
            <div className="mt-3 divide-y divide-border">
              {digest.ideas.map((i) => (
                <IdeaRow key={i.symbol} idea={i} onOpenChart={onOpenChart} />
              ))}
            </div>
          )}
        </Card>

        {/* Your holdings, described honestly. */}
        <Card>
          <CardHeader
            title="Your holdings"
            sub={held.source === "snapshot" && held.as_of ? `as of ${formatIst(held.as_of)}` : undefined}
          />
          {held.items.length === 0 ? (
            <div className="px-5 py-8 text-sm text-muted-foreground">No holdings</div>
          ) : (
            <div className="mt-3 divide-y divide-border">
              {flagged.map((h) => (
                <div key={h.symbol} className="px-5 py-3">
                  <div className="flex items-center justify-between gap-3">
                    <Button
                      size="inline"
                      variant="link"
                      className="font-semibold text-foreground"
                      onClick={() => onOpenChart?.(h.symbol)}
                    >
                      {h.symbol}
                    </Button>
                    {h.pnl_pct != null && (
                      <span className={cn("text-sm tabular-nums", h.pnl_pct >= 0 ? "text-gain" : "text-loss")}>
                        {signed(h.pnl_pct, 0)}
                      </span>
                    )}
                  </div>
                  <div className="mt-2 space-y-1.5">
                    {h.flags.map((f) => (
                      <div key={f.kind} className="flex items-start gap-2 text-xs" title={f.note}>
                      <span className={cn("mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-caption font-medium", FLAG_TONE[f.kind])}>
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
                  {calm} fine
                </div>
              )}
            </div>
          )}
        </Card>
      </div>

      {plan && <GapPlanCard plan={plan} onOpenChart={onOpenChart} />}

      {tracker && <TrackerCard tracker={tracker} onOpenChart={onOpenChart} />}

      {/* What to be told, and when. */}
      {settings && (
        <Card className="flex flex-wrap items-center justify-between gap-4 px-5 py-4">
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
        </Card>
      )}

      <Hint className="px-1 text-caption"><span title={digest.disclaimer}>Not advice</span></Hint>
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
          <Button
            size="inline"
            variant="link"
            className="font-semibold text-foreground"
            onClick={() => onOpenChart?.(idea.symbol)}
          >
            {idea.symbol}
          </Button>
          <AnimatedBadge
          status={idea.setup === "strong_rs" ? "success" : idea.setup === "oversold" ? "warning" : "neutral"}
            size="sm"
          >
            {idea.setup_label}
          </AnimatedBadge>
          {idea.is_new && (
            <AnimatedBadge status="info" size="sm" pulse>
              New
            </AnimatedBadge>
          )}
          {idea.on_watchlist && <span className="text-warning" aria-label="On your watchlist"><IconStar size={12} /></span>}
        </div>
        <div className="text-right">
          <div className="text-sm font-medium tabular-nums">{inr(idea.price)}</div>
          <div className={cn("text-xs tabular-nums", idea.change_1d_pct >= 0 ? "text-gain" : "text-loss")}>
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

/** The forward test: signals written down before the week, graded after it. */
function TrackerCard({ tracker: t, onOpenChart }: { tracker: ForwardTracker; onOpenChart?: (symbol: string) => void }) {
  const pct = Math.min(100, (t.graded / t.needed) * 100);
  const animStatus = t.state === "working" ? "success" : t.state === "not_working" ? "danger" : t.state === "inconclusive" ? "warning" : "neutral";
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold" title={`${t.description}. Each Friday's picks are written down, then graded a week later on whether they gained 2% or more.`}>Live test</span>
        <span title={t.verdict}>
          <AnimatedBadge status={animStatus} pulse={t.state === "working"} size="sm">
            {t.graded < t.needed ? "Too early" : t.verdict}
          </AnimatedBadge>
        </span>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Stat
          label="Hit rate"
          value={t.hit_rate_pct == null ? "—" : `${t.hit_rate_pct}%`}
          sub={t.range_pct ? `${t.range_pct[0]}–${t.range_pct[1]}%` : undefined}
        />
        <Stat
        label="Any stock"
        value={`${t.base_rate_pct}%`}
        
        />
        <Stat
          label="Avg week"
          value={t.avg_net_pct == null ? "—" : signed(t.avg_net_pct, 2)}
          sub="after costs"
          tone={t.avg_net_pct == null ? "neutral" : t.avg_net_pct >= 0 ? "good" : "bad"}
        />
      </div>

      <div className="mt-4">
        <div className="mb-1 flex justify-between text-caption text-muted-foreground">
          <span>{t.graded} / {t.needed} graded</span>
          <span>{t.open.length} open</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {t.open.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">This week</span>
          {t.open.map((s) => (
            <Tooltip key={`${s.entry_date}-${s.symbol}`} content={`Picked ${s.entry_date} at ${inr(s.entry_close)}`} side="top" delay={400}>
            <Button
            size="sm"
            variant="outline"
              onClick={() => onOpenChart?.(s.symbol)}
            >
              {s.symbol}
              </Button>
            </Tooltip>
          ))}
        </div>
      )}
    </Card>
  );
}

const CALL_TONE = { trade: TONE.good, skip: TONE.warn, unknown: TONE.flat };
const EXIT_LABEL = { target: "Hit target", stop: "Stopped out", friday: "Sold Friday" } as const;

function PlanStat({ label, stats }: { label: string; stats: GapPlanStats }) {
  const good = (stats.avg_net_pct ?? 0) > 0;
  return (
    <Stat
      label={label}
      value={stats.graded === 0 ? "No trades" : `${signed(stats.avg_net_pct ?? 0, 2)}`}
      sub={stats.graded === 0 ? undefined : `${stats.win_rate_pct}% hit · ${stats.graded} trades`}
      tone={stats.graded === 0 ? "neutral" : good ? "good" : "bad"}
    />
  );
}

/** The Monday gap plan: this week's call, the results so far, and what is open. */
function GapPlanCard({ plan: p, onOpenChart }: { plan: GapPlan; onOpenChart?: (symbol: string) => void }) {
  const w = p.this_week;
  const pct = Math.min(100, (p.live.graded / p.needed) * 100);
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span
          className="text-sm font-semibold"
          title={`After a rising week, buy stocks that open more than ${Math.abs(p.plan.gap_pct)}% below Friday's close. Sell at +${p.plan.target_pct}%, stop at −${p.plan.stop_pct}%, otherwise sell Friday.`}
        >
          Monday gap plan
        </span>
        <Badge tone="flat">Paper</Badge>
      </div>
      <div className="mt-1 text-xs tabular-nums text-muted-foreground">
        gap −{Math.abs(p.plan.gap_pct)}% · target +{p.plan.target_pct}% · stop −{p.plan.stop_pct}%
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <span className={cn("rounded-full px-3 py-1 text-sm font-semibold", CALL_TONE[w.status])}>
          {w.status === "trade" ? "This week: trade" : w.status === "skip" ? "This week: skip" : "This week: no reading"}
        </span>
        {w.median_pct != null && (
          <span className="text-xs tabular-nums text-muted-foreground">
            Last week {signed(w.median_pct, 2)} · needs +{w.needed_pct}%
          </span>
        )}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <PlanStat label={`Live since ${p.live_from ?? "—"}`} stats={p.live} />
        <PlanStat label="Replay" stats={p.replay} />
      </div>
      <div className="mt-3">
        <div className="mb-1 flex justify-between text-caption text-muted-foreground">
          <span>{p.live.graded} / {p.needed} graded</span>
          <span title={p.verdict}>{p.live.graded < p.needed ? "Too early" : p.verdict}</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {p.open.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 text-xs text-muted-foreground">Open this week</div>
          <div className="divide-y divide-border rounded-lg border border-border">
            {p.open.map((t) => (
              <div key={`${t.entry_date}-${t.symbol}`} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <Button
                  size="inline"
                  variant="link"
                  className="font-semibold text-foreground"
                  onClick={() => onOpenChart?.(t.symbol)}
                >
                  {t.symbol}
                </Button>
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
          <div className="divide-y divide-border rounded-lg border border-border">
            {p.recent.slice(0, 6).map((t) => (
              <div key={`${t.entry_date}-${t.symbol}`} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                <span>
                  <span className="font-semibold">{t.symbol}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{dateLabel(t.entry_date)} · {t.exit_reason ? EXIT_LABEL[t.exit_reason] : ""}{t.source === "replay" ? " · replay" : ""}</span>
                </span>
                <span className={cn("tabular-nums", (t.net_pct ?? 0) >= 0 ? "text-gain" : "text-loss")}>{signed(t.net_pct ?? 0, 2)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
