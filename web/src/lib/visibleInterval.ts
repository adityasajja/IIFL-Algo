/**
 * `setInterval` that skips ticks while the tab is hidden. Drop-in: the returned
 * id works with `clearInterval`. A background tab polling the API every few
 * seconds is pure load, and the next visible tick catches the UI up.
 */
export function setVisibleInterval(fn: () => void, ms: number): number {
  return window.setInterval(() => {
    if (document.visibilityState === "visible") fn();
  }, ms);
}
