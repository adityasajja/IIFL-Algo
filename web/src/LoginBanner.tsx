import { BrandMark } from "./components/ui/brand-mark";
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
          <BrandMark className="mx-auto mb-4 size-11" />
          <h2 className="text-lg font-semibold tracking-tight">Log in to IIFL</h2>
          <p className="mt-1.5 text-[13px] text-muted-foreground">Your session has expired.</p>
          <div className="mt-5 grid gap-2.5">
            <Button
              size="lg"
              className="w-full"
              disabled={opening}
              onClick={() => void openLogin()}
            >
              {opening ? "Opening…" : "Log in"}
            </Button>
            {error ? (
              <p className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-left text-xs leading-relaxed text-destructive">
                {error}
              </p>
            ) : null}
            <button
              type="button"
              onClick={() => setView("manual")}
              className="text-[13px] font-medium text-primary hover:underline"
            >
              Use an auth code
            </button>
            {onClose ? (
              <button
                type="button"
                onClick={onClose}
                className="text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                Not now
              </button>
            ) : null}
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
