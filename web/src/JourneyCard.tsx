import { Check, ChartPie, FlaskConical, Layers, Radio, type LucideIcon } from "lucide-react";
import { Tooltip } from "./components/motion/tooltip";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";
import type { JourneyStep } from "./lib/journey";
import type { Tab } from "./lib/nav";
import { cn } from "./lib/utils";

const ICON: Record<JourneyStep["id"], LucideIcon> = {
  build: Layers,
  test: FlaskConical,
  paper: Radio,
  review: ChartPie,
};

/**
 * The path from idea to a paper track record as a rail: a node per step, a line that fills as steps are
 * done, and one button on the step that is next. The sentence behind each step is a hover, not
 * a paragraph: the picture carries the order and the progress.
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
  const nextIndex = next ? steps.findIndex((s) => s.id === next.id) : steps.length;
  return (
    <Card padding="md">
      <ol className="flex items-start" aria-label="Path from idea to track record">
        {steps.map((s, i) => {
          const Icon = ICON[s.id];
          const isNext = next?.id === s.id;
          const reached = i < nextIndex || s.done;
          return (
            <li key={s.id} className="flex min-w-0 flex-1 flex-col items-center gap-2">
              <div className="relative flex w-full items-center justify-center">
                {i > 0 ? (
                  <span className={cn("absolute right-1/2 h-0.5 w-full rounded-full", i <= nextIndex ? "bg-gain" : "bg-border")} aria-hidden="true" />
                ) : null}
                <Tooltip content={s.what} side="bottom" delay={200}>
                  <Button
                    size="icon"
                    variant={isNext ? "primary" : "quiet"}
                    className={cn("relative z-10", reached && !isNext && "border-gain/50 text-gain bg-card", !reached && !isNext && "bg-card", isNext && "ring-4 ring-primary/20")}
                    aria-label={`${s.title}${s.done ? " (done)" : isNext ? " (next)" : ""}`}
                    aria-current={isNext ? "step" : undefined}
                    onClick={() => onNavigate(s.tab, s.sub)}
                  >
                    <Icon className="size-4" />
                  </Button>
                </Tooltip>
                {s.done ? (
                  <span className="absolute left-1/2 top-0 z-20 ml-2 grid size-4 -translate-y-1 place-items-center rounded-full bg-gain text-primary-foreground" aria-hidden="true">
                    <Check className="size-2.5" />
                  </span>
                ) : null}
              </div>
              <span className={cn("text-xs", isNext ? "font-semibold text-foreground" : "text-muted-foreground")}>{s.short}</span>
              {isNext ? (
                <Button size="xs" onClick={() => onNavigate(s.tab, s.sub)}>
                  {s.action}
                </Button>
              ) : null}
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
