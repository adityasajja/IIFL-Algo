import { useState } from "react";
import { getLoginStatus, loginSubmit, type LoginStatus } from "./api";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { MorphingModal } from "./components/ui/modal";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";

interface Props {
  status: LoginStatus | null;
  onLoggedIn: () => void;
  onClose?: () => void;
}

export default function LoginBanner({ status, onLoggedIn, onClose }: Props) {
  const [view, setView] = useState<"main" | "manual">("main");
  const [clientId, setClientId] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [loginState, setLoginState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);

  async function manualLogin() {
    setLoginState("loading");
    setError(null);
    try {
      await loginSubmit(clientId, authCode);
      setLoginState("success");
      onLoggedIn();
    } catch (e) {
      setLoginState("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  /**
   * Open the broker login.
   *
   * This must never be a dead end. If the status poll failed we have no
   * `login_url` — so fetch it now instead of rendering a disabled button. The
   * tab is opened synchronously (an empty one) and navigated afterwards,
   * because a `window.open` after an `await` is no longer inside the user
   * gesture and gets popup-blocked.
   */
  async function openLogin() {
    setError(null);
    if (status?.login_url) {
      window.open(status.login_url, "_blank", "noopener");
      return;
    }
    const tab = window.open("", "_blank");
    setOpening(true);
    try {
      const fresh = await getLoginStatus();
      if (tab && fresh.login_url) tab.location.href = fresh.login_url;
      else {
        tab?.close();
        setError("The backend did not return a login URL — check IIFL_APP_KEY.");
      }
    } catch {
      tab?.close();
      setError("Can't reach the backend to build the login URL. Is `atr serve` running?");
    } finally {
      setOpening(false);
    }
  }

  return (
    <MorphingModal viewId={view} onClose={() => onClose?.()}>
      {view === "main" ? (
        <div className="text-center">
          <div className="mx-auto mb-4 grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-primary to-violet-500 text-lg font-extrabold text-white">
            A
          </div>
          <h2 className="text-lg font-semibold tracking-tight">IIFL session needed</h2>
          <p className="mx-auto mt-1.5 max-w-xs text-[13px] leading-relaxed text-muted-foreground">
            ATR talks to your broker through a daily IIFL session. It just expired —
            reconnecting takes about a minute.
          </p>
          <div className="mt-5 grid gap-2.5">
            <Button
              size="lg"
              className="w-full"
              disabled={opening}
              onClick={() => void openLogin()}
            >
              {opening ? "Building the login URL…" : "Log in with IIFL"}
            </Button>
            <p className="text-xs leading-relaxed text-muted-foreground">
              Opens markets.iiflcapital.com in a new tab. After you sign in it
              redirects back and the session activates automatically.
            </p>
            {error ? (
              <p className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-left text-xs leading-relaxed text-destructive">
                {error}
              </p>
            ) : !status ? (
              <p className="text-left text-xs leading-relaxed text-muted-foreground">
                Waiting on the backend for the login URL — the button still works,
                it will fetch one when you click.
              </p>
            ) : null}
            <button
              type="button"
              onClick={() => setView("manual")}
              className="text-[13px] font-medium text-primary hover:underline"
            >
              I already have an auth code →
            </button>
          </div>
        </div>
      ) : (
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Paste your auth code</h2>
          <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
            Sign in at the URL first, then copy <code className="font-mono">clientId</code> and{" "}
            <code className="font-mono">authCode</code> from the address bar.
          </p>
          <div className="mt-4 grid gap-3">
            <Input label="Client ID" value={clientId} onChange={setClientId} placeholder="your IIFL client ID" />
            <Input
              label="Auth code"
              value={authCode}
              onChange={setAuthCode}
              placeholder="paste the authCode from the login URL"
              error={error ?? undefined}
            />
          </div>
          <div className="mt-4 flex gap-2.5">
            <StatefulButton
              state={loginState}
              disabled={!clientId || !authCode}
              onClick={() => void manualLogin()}
              loadingText="Connecting…"
              successText="Connected"
              errorText="Failed — retry"
            >
              Connect
            </StatefulButton>
            <Button variant="secondary" onClick={() => setView("main")}>
              Back
            </Button>
          </div>
        </div>
      )}
    </MorphingModal>
  );
}
