/**
 * Where the user is on the path from idea to live trading, and the one thing to do next.
 * Derived from facts the app already has, so it never needs its own state to drift.
 */
import type { Tab } from "./nav";

export type JourneyFacts = {
  /** Saved strategies the user has. */
  strategies: number;
  /** Strategies with a version that can be deployed (it passed the structural checks). */
  deployable: number;
  /** Paper deployments that exist (running or not). */
  paperRuns: number;
  /** The execution mode the platform is in. */
  executionMode: "paper" | "live" | null;
  brokerConnected: boolean;
};

export type JourneyStep = {
  id: "build" | "test" | "paper" | "live" | "review";
  title: string;
  /** One plain sentence: what this step is and why it comes here. */
  what: string;
  done: boolean;
  /** Where the step's action goes. */
  tab: Tab;
  sub?: string;
  action: string;
};

export function journeySteps(f: JourneyFacts): JourneyStep[] {
  const live = f.executionMode === "live";
  return [
    {
      id: "build",
      title: "Build a strategy",
      what: "Write the rules for when to buy and when to sell.",
      done: f.strategies > 0,
      tab: "strategies",
      action: f.strategies > 0 ? "Open strategies" : "Build a strategy",
    },
    {
      id: "test",
      title: "Test it",
      what: "Check it on prices it has never seen. Most ideas fail here, and that is the point.",
      done: f.deployable > 0,
      tab: "evidence",
      sub: "backtest",
      action: "Run a test",
    },
    {
      id: "paper",
      title: "Paper trade it",
      what: "Run it on live prices with practice money for a few weeks.",
      done: f.paperRuns > 0,
      tab: "paper",
      action: f.paperRuns > 0 ? "Open paper runs" : "Start paper trading",
    },
    {
      id: "live",
      title: "Go live",
      what: "Connect your broker, set your limits, and let it place real orders.",
      done: live && f.brokerConnected,
      tab: "trading",
      sub: f.brokerConnected ? "control-center" : "portfolio",
      action: f.brokerConnected ? "Set limits" : "Connect broker",
    },
    {
      id: "review",
      title: "Review",
      what: "See what it did, what it cost, and where the results came from.",
      done: false,
      tab: "learning",
      action: "Open performance",
    },
  ];
}

/** The first step that is not done, in order; `null` once every step before Review is done. */
export function nextStep(steps: JourneyStep[]): JourneyStep | null {
  return steps.find((s) => !s.done && s.id !== "review") ?? null;
}
