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
};

export type JourneyStep = {
  id: "build" | "test" | "paper" | "review";
  title: string;
  /** One word for the rail. */
  short: string;
  /** One plain sentence: what this step is and why it comes here. */
  what: string;
  done: boolean;
  /** Where the step's action goes. */
  tab: Tab;
  sub?: string;
  action: string;
};

export function journeySteps(f: JourneyFacts): JourneyStep[] {
  return [
    {
      id: "build",
      title: "Build a strategy",
      short: "Build",
      what: "Write the rules for when to buy and when to sell.",
      done: f.strategies > 0,
      tab: "strategies",
      action: f.strategies > 0 ? "Open strategies" : "Build a strategy",
    },
    {
      id: "test",
      title: "Test it",
      short: "Test",
      what: "Check it on prices it has never seen. Most ideas fail here, and that is the point.",
      done: f.deployable > 0,
      tab: "evidence",
      sub: "backtest",
      action: "Run a test",
    },
    {
      id: "paper",
      title: "Paper trade it",
      short: "Paper",
      what: "Run it on live prices with practice money. No real order is ever sent.",
      done: f.paperRuns > 0,
      tab: "paper",
      action: f.paperRuns > 0 ? "Open paper runs" : "Start paper trading",
    },
    {
      id: "review",
      title: "Review",
      short: "Review",
      what: "See what it did, what it cost, and where the results came from.",
      done: false,
      tab: "learning",
      action: "Open performance",
    },
  ];
}

/** The first step that is not done, in order; `null` once a strategy has been paper traded. */
export function nextStep(steps: JourneyStep[]): JourneyStep | null {
  return steps.find((s) => !s.done && s.id !== "review") ?? null;
}
