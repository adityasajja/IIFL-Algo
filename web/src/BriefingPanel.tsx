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
import { Select } from "./components/ui/select";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";
import { Switch } from "./components/ui/switch";
import { useToast } from "./components/ui/toast-context";
import { formatIst } from "./lib/format";
import { ChevronDown } from "lucide-react";
import { cn } from "./lib/utils";

const SYNC_CMD = `schtasks /create /tn "ATR history sync" /tr "cmd /c cd /d D:\\ALGO && uv run atr history sync" /sc daily /st 16:00 /f`;
const BRIEF_CMD = `schtasks /create /tn "ATR morning brief" /tr "cmd /c cd /d D:\\ALGO && uv run atr brief send" /sc daily /st 08:45 /f`;

export default function BriefingPanel() {
  const { toast } = useToast();
  const [cfg, setCfg] = useState<BriefingConfig | null>(null);
  const [lastSent, setLastSent] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [previewState, setPreviewState] = useState<ButtonState>("idle");
  const [sendState, setSendState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [showAutomation, setShowAutomation] = useState(false);

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
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Morning brief"
          sub={
            lastSent
              ? (() => {
                  const [when, via] = lastSent.split(" via ");
                  return `Last sent ${formatIst(when)}${via ? ` on ${via.charAt(0).toUpperCase()}${via.slice(1)}` : ""}`;
                })()
              : "Not sent yet"
          }
        />
        <div className="grid gap-3.5 p-5 sm:grid-cols-2 lg:grid-cols-4">
          <Input label="Stocks to buy" type="number" value={String(cfg.top_n)} onChange={(v) => set("top_n", Number(v))} />
          <Input label="Stocks to avoid" type="number" value={String(cfg.avoid_n)} onChange={(v) => set("avoid_n", Number(v))} />
          <Input label="Minimum price (₹)" type="number" value={String(cfg.min_price)} onChange={(v) => set("min_price", Number(v))} />
          <Input label="Minimum daily trading (₹ lakh)" type="number" value={String(cfg.min_day_value_lakh)} onChange={(v) => set("min_day_value_lakh", Number(v))} />
          <Input label="Minimum daily swing (%)" type="number" value={String(cfg.min_atr_pct)} onChange={(v) => set("min_atr_pct", Number(v))} />
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Ranking</label>
            <Select
              value={cfg.ranking}
              onChange={(v) => set("ranking", v)}
              options={[
                { value: "vs_high", label: "Closest to 3-month high" },
                { value: "score", label: "1-month return (older method)" },
              ]}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="px-1 text-sm font-medium text-foreground">Universe</label>
            <Select
              value={cfg.universe}
              onChange={(v) => set("universe", v)}
              options={[
                { value: "all", label: "All NSE stocks" },
                { value: "watchlist", label: "Watchlist only" },
              ]}
            />
          </div>
          <div className="flex items-end pb-2">
            <Switch checked={cfg.send_enabled} onCheckedChange={(v) => set("send_enabled", v)} label="Send automatically" />
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
        <button
          type="button"
          onClick={() => setShowAutomation((v) => !v)}
          aria-expanded={showAutomation}
          className="flex w-full items-center justify-between px-5 py-4 text-left"
        >
          <span>
            <span className="block text-sm font-semibold tracking-tight">Send it every morning</span>
            <span className="mt-0.5 block text-xs text-muted-foreground">One-time setup on Windows</span>
          </span>
          <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", showAutomation && "rotate-180")} />
        </button>
        {showAutomation ? (
          <div className="space-y-3 border-t border-border p-5 text-[13px]">
            <div>
              <Hint>1 · Refresh data after the market closes (4 PM):</Hint>
              <code className="mt-1 block overflow-x-auto rounded-xl border border-border bg-background p-3 font-mono text-xs">
                {SYNC_CMD}
              </code>
            </div>
            <div>
              <Hint>2 · Send the brief before the market opens (8:45 AM):</Hint>
              <code className="mt-1 block overflow-x-auto rounded-xl border border-border bg-background p-3 font-mono text-xs">
                {BRIEF_CMD}
              </code>
            </div>
            <Hint>
              Run each once in an admin terminal, and keep the computer on. The morning brief needs no
              broker login. The 4 PM refresh does, so log in once each morning.
            </Hint>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
