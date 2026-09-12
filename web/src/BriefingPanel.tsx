import { useEffect, useState } from "react";
import {
  getBriefingConfig,
  previewBriefing,
  saveBriefingConfig,
  sendBriefing,
  type BriefingConfig,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardHeader, ErrorBox, Hint } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";

const SYNC_CMD = `schtasks /create /tn "ATR history sync" /tr "cmd /c cd /d D:\\ALGO && uv run atr history sync" /sc daily /st 16:00 /f`;
const BRIEF_CMD = `schtasks /create /tn "ATR morning brief" /tr "cmd /c cd /d D:\\ALGO && uv run atr brief send" /sc daily /st 08:45 /f`;

const selectClass =
  "h-11 w-full rounded-full border border-border bg-transparent px-3.5 text-sm text-foreground outline-none transition-colors focus:border-foreground/40 [&>option]:bg-card";

export default function BriefingPanel() {
  const { toast } = useToast();
  const [cfg, setCfg] = useState<BriefingConfig | null>(null);
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [previewState, setPreviewState] = useState<ButtonState>("idle");
  const [sendState, setSendState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      setError(null);
      const res = await getBriefingConfig();
      setCfg(res.config);
      setLastSent(
        res.last_sent ? `${res.last_sent.sent_at} via ${res.last_sent.channel}` : null,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  function set<K extends keyof BriefingConfig>(k: K, v: BriefingConfig[K]) {
    if (cfg) setCfg({ ...cfg, [k]: v });
  }

  async function save() {
    if (!cfg) return;
    try {
      setError(null);
      setCfg(await saveBriefingConfig(cfg));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      throw new Error(msg);
    }
  }

  async function preview() {
    setPreviewState("loading");
    try {
      await save();
      setMessage((await previewBriefing()).message);
      setPreviewState("success");
    } catch (e) {
      setPreviewState("error");
      toast({ title: "Preview failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function send() {
    setSendState("loading");
    try {
      await save();
      const res = await sendBriefing();
      setSendState("success");
      toast({
        title:
          res.sent_on === "none"
            ? "Nothing to send with"
            : res.sent_on === "disabled"
              ? "Sending is disabled"
              : `Sent via ${res.sent_on}`,
        description:
          res.sent_on === "none"
            ? "Configure Telegram first."
            : res.sent_on === "disabled"
              ? "Flip the sending toggle below."
              : "Check Telegram.",
        status: res.sent_on !== "none" && res.sent_on !== "disabled" ? "success" : "info",
      });
      await refresh();
    } catch (e) {
      setSendState("error");
      toast({ title: "Send failed", description: e instanceof Error ? e.message : String(e), status: "error" });
    }
  }

  async function saveQuiet() {
    try {
      await save();
      toast({ title: "Saved", status: "success" });
    } catch {
      /* error already surfaced */
    }
  }

  if (!cfg) {
    return (
      <Card className="p-5 text-sm text-muted-foreground">
        {error ? <ErrorBox>{error}</ErrorBox> : "Loading…"}
      </Card>
    );
  }

  return (
    <div className="space-y-3.5">
      <Card>
        <CardHeader title="Briefing config" sub={lastSent ? `Last sent: ${lastSent}` : "Never sent"} />
        <div className="grid gap-3.5 p-5 sm:grid-cols-2 lg:grid-cols-4">
          <Input label="Long ideas" type="number" value={String(cfg.top_n)} onChange={(v) => set("top_n", Number(v))} />
          <Input label="Avoid list" type="number" value={String(cfg.avoid_n)} onChange={(v) => set("avoid_n", Number(v))} />
          <Input label="Min price ₹" type="number" value={String(cfg.min_price)} onChange={(v) => set("min_price", Number(v))} />
          <Input label="Min day value ₹L" type="number" value={String(cfg.min_day_value_lakh)} onChange={(v) => set("min_day_value_lakh", Number(v))} />
          <Input label="Min ATR %" type="number" value={String(cfg.min_atr_pct)} onChange={(v) => set("min_atr_pct", Number(v))} />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Ranking</label>
            <select value={cfg.ranking} onChange={(e) => set("ranking", e.target.value)} className={selectClass}>
              <option value="vs_high">vs 63d high (grid-tested)</option>
              <option value="score">ret_1m + vs_high (old)</option>
            </select>
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Universe</label>
            <select value={cfg.universe} onChange={(e) => set("universe", e.target.value)} className={selectClass}>
              <option value="all">All NSE (cached)</option>
              <option value="watchlist">Watchlist only</option>
            </select>
          </div>
          <div className="flex items-end pb-2">
            <Switch checked={cfg.send_enabled} onCheckedChange={(v) => set("send_enabled", v)} label="Sending enabled" />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2.5 px-5 pb-5">
          <Button variant="secondary" onClick={() => void saveQuiet()}>
            Save
          </Button>
          <StatefulButton state={previewState} variant="secondary" onClick={() => void preview()} loadingText="Working…" successText="Previewed" errorText="Failed — retry">
            Preview now
          </StatefulButton>
          <StatefulButton state={sendState} onClick={() => void send()} loadingText="Sending…" successText="Sent" errorText="Failed — retry">
            Send to Telegram
          </StatefulButton>
        </div>
        {error && (
          <div className="px-5 pb-5">
            <ErrorBox>{error}</ErrorBox>
          </div>
        )}
        {message && (
          <div className="px-5 pb-5">
            <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-xl border border-border bg-background p-4 font-mono text-xs leading-relaxed">
              {message}
            </pre>
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Daily automation"
          sub="Windows Task Scheduler · machine must be on"
        />
        <div className="space-y-3 p-5 text-[13px]">
          <div>
            <Hint>1 · Refresh data after close (4 PM):</Hint>
            <code className="mt-1 block overflow-x-auto rounded-xl border border-border bg-background p-3 font-mono text-xs">
              {SYNC_CMD}
            </code>
          </div>
          <div>
            <Hint>2 · Telegram the brief before open (8:45 AM):</Hint>
            <code className="mt-1 block overflow-x-auto rounded-xl border border-border bg-background p-3 font-mono text-xs">
              {BRIEF_CMD}
            </code>
          </div>
          <Hint>
            Run each once in an admin terminal. The 8:45 AM brief needs no IIFL login (cache +
            Telegram only). The 4 PM sync needs that day&apos;s session — log in once each morning
            and everything downstream works till midnight IST.
          </Hint>
        </div>
      </Card>
    </div>
  );
}
