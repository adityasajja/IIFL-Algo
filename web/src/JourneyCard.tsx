import { Check } from "lucide-react";
import { Button } from "./components/ui/button";
import { Card, CardHeader } from "./components/ui/card";
import type { JourneyStep } from "./lib/journey";
import type { Tab } from "./lib/nav";
import { cn } from "./lib/utils";

/**
 * The product in five steps, with the next one marked. The same path as the sidebar's
 * first group, so a new user sees where they are and what to do without reading a manual.
 */
export function JourneyCard({
  steps,
  next,
  onNavigate,
}: {
  steps: JourneyStep[];
  next: JourneyStep | null;
  onNavigate: (tab: Tab, sub?: string) => void;
}) {
  const doneCount = steps.filter((s) => s.done).length;
  return (
    <Card padding="md">
      <CardHeader
        title="Your path to live trading"
        sub={next ? "Each step unlocks the next. Do them in order." : "You are live. Review keeps you honest."}
        action={<span className="text-caption tabular-nums text-muted-foreground">{doneCount} of {steps.length - 1} done</span>}
      />
      <ol className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {steps.map((s, i) => {
          const isNext = next?.id === s.id;
          return (
            <li
              key={s.id}
              className={cn(
                "flex flex-col gap-2 rounded-lg border p-3",
                isNext ? "border-primary/50 bg-primary/5" : "border-border",
              )}
            >
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "grid size-5 shrink-0 place-items-center rounded-full text-micro font-semibold",
                    s.done ? "bg-gain text-primary-foreground" : isNext ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                  )}
                  aria-hidden="true"
                >
                  {s.done ? <Check className="size-3" /> : i + 1}
                </span>
                <span className="text-sm font-semibold text-foreground">{s.title}</span>
                {s.done ? <span className="sr-only">(done)</span> : null}
              </div>
              <p className="text-xs leading-relaxed text-muted-foreground">{s.what}</p>
              <Button
                size="xs"
                variant={isNext ? "primary" : "quiet"}
                className="mt-auto self-start"
                onClick={() => onNavigate(s.tab, s.sub)}
              >
                {s.action}
              </Button>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
