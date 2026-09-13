import { useState } from "react";
import AlertsPanel from "./AlertsPanel";
import BriefingPanel from "./BriefingPanel";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";

type Mode = "brief" | "alerts";

/**
 * Two different ways the system reaches you, kept on one page but never
 * mixed together:
 *
 *  - Morning brief: a scheduled batch. Once a day, before the bell, it ranks
 *    the whole universe and Telegram's you the top and bottom names.
 *  - Alerts: condition-driven. Rules you (or the engine) define fire the
 *    moment something happens, with cooldowns.
 *
 * They share a delivery channel and nothing else, so they get separate tabs
 * rather than one long form.
 */
export default function NotificationsPanel({
  onOpenChart,
}: {
  onOpenChart?: (tab: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("brief");

  return (
    <div className="space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={mode} onValueChange={(v) => setMode(v as Mode)} variant="segment">
          <TabsList>
            <TabsTrigger value="brief">Morning brief</TabsTrigger>
            <TabsTrigger value="alerts">Alerts</TabsTrigger>
          </TabsList>
        </Tabs>
        <p className="text-[11.5px] text-muted-foreground">
          {mode === "brief"
            ? "A scheduled batch: ranks the universe once a day and sends the top and bottom names."
            : "Condition-driven: fires the moment a rule matches, with a cooldown between repeats."}
        </p>
      </div>

      {mode === "brief" ? <BriefingPanel /> : <AlertsPanel onOpenChart={onOpenChart} />}
    </div>
  );
}
