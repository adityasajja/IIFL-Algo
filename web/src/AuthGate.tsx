import { AlertTriangle, KeyRound, Loader2, RefreshCw, ShieldCheck, UserRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ApiError,
  bootstrapOwner,
  getBootstrapStatus,
  getMe,
  appLogin,
  registerAccount,
  type BootstrapStatus,
  type Principal,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, ErrorBox } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { StatefulButton, type ButtonState } from "./components/ui/stateful-button";

type View = "setup" | "login" | "register" | "mfa";

interface Props {
  /** Set when the backend could not be reached at all, as opposed to refused. */
  offline?: { error: string | null; onRetry: () => void } | null;
  onAuthenticated: (principal: Principal) => void;
}

/**
 * The password rules this instance enforces.
 *
 * These mirror `atr.auth.passwords.strength_problems`, which is the single
 * authority — this list exists so the user is not left guessing, and the server's
 * `weak_password` code is still surfaced verbatim if the two ever disagree.
 */
const PASSWORD_RULES: { test: (v: string) => boolean; label: string }[] = [
  { test: (v) => v.length >= 10, label: "at least 10 characters" },
  { test: (v) => /[a-z]/.test(v), label: "a lowercase letter" },
  { test: (v) => /[A-Z]/.test(v), label: "an uppercase letter" },
  { test: (v) => /\d/.test(v), label: "a digit" },
];

export default function AuthGate({ offline, onAuthenticated }: Props) {
  const [status, setStatus] = useState<BootstrapStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [view, setView] = useState<View>("login");

  const [identifier, setIdentifier] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [buttonState, setButtonState] = useState<ButtonState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setStatusError(null);
    try {
      const s = await getBootstrapStatus();
      setStatus(s);
      // First run: there is no account to log in with, so the only useful screen
      // is the one that creates one.
      setView(s.needs_setup ? "setup" : "login");
    } catch (e) {
      setStatusError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const unmet = useMemo(
    () => PASSWORD_RULES.filter((r) => !r.test(password)).map((r) => r.label),
    [password],
  );

  function fail(e: unknown) {
    setButtonState("error");
    if (e instanceof ApiError) {
      setError(e.message);
      setErrorCode(e.code);
      return;
    }
    setError(e instanceof Error ? e.message : String(e));
    setErrorCode(null);
  }

  /** A login returns the account; the *permissions* come from `/auth/me`. */
  async function finish() {
    const me = await getMe();
    setButtonState("success");
    onAuthenticated(me);
  }

  async function submit() {
    setError(null);
    setErrorCode(null);
    setButtonState("loading");
    try {
      if (view === "setup") {
        await bootstrapOwner({
          email: email.trim(),
          username: username.trim(),
          password,
          display_name: displayName.trim() || undefined,
        });
        await finish();
        return;
      }
      if (view === "register") {
        await registerAccount({
          email: email.trim(),
          username: username.trim(),
          password,
          display_name: displayName.trim() || undefined,
        });
        // A fresh account is `viewer` and self-registration does not sign you in,
        // so drop the user on the login form with the identifier pre-filled
        // rather than pretending they are authenticated.
        setIdentifier(username.trim());
        setPassword("");
        setView("login");
        setButtonState("idle");
        return;
      }
      const result = await appLogin({
        identifier: identifier.trim(),
        password,
        ...(totp.trim() ? { totp_code: totp.trim() } : {}),
      });
      if ("mfa_required" in result) {
        // The password was right and a second factor is owed. That is not a
        // failure, so it does not get the error styling.
        setView("mfa");
        setButtonState("idle");
        setError(null);
        return;
      }
      await finish();
    } catch (e) {
      fail(e);
    }
  }

  // ── offline: the backend never answered ────────────────────────────────────
  if (offline || statusError) {
    return (
      <Shell>
        <div className="text-center">
          <div className="mx-auto mb-4 grid size-11 place-items-center rounded-xl bg-amber-500/15 text-amber-500">
            <AlertTriangle className="size-5" />
          </div>
          <h2 className="text-lg font-semibold tracking-tight">Backend unreachable</h2>
          <p className="mx-auto mt-1.5 max-w-sm text-[13px] leading-relaxed text-muted-foreground">
            The dashboard could not reach the ATR API, so it cannot tell whether you are
            signed in. This is a connectivity problem, not a rejected login.
          </p>
          <div className="mt-4 text-left">
            <ErrorBox>
              <code className="font-mono text-xs break-all">
                {offline?.error ?? statusError}
              </code>
            </ErrorBox>
          </div>
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            Start it with <code className="font-mono">atr serve</code>, then retry.
          </p>
          <Button
            className="mt-4 w-full"
            onClick={() => {
              offline?.onRetry();
              void loadStatus();
            }}
          >
            <RefreshCw className="size-4" />
            Retry
          </Button>
        </div>
      </Shell>
    );
  }

  if (!status) {
    return (
      <Shell>
        <div className="grid place-items-center py-6 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
        </div>
      </Shell>
    );
  }

  const isSetup = view === "setup";
  const isRegister = view === "register";
  const isMfa = view === "mfa";
  const busy = buttonState === "loading";

  const heading = isSetup
    ? "Set up ATR"
    : isMfa
      ? "Two-factor code"
      : isRegister
        ? "Create an account"
        : "Sign in";

  const blurb = isSetup
    ? "No account exists yet. The first one becomes the owner — full control, including the execution mode."
    : isMfa
      ? "Your password was accepted. Enter the 6-digit code from your authenticator app to finish."
      : isRegister
        ? "New accounts start as viewer: read everything, change nothing."
        : "Your platform account, not your broker session. IIFL is connected separately.";

  const disabled = busy
    ? true
    : isMfa
      ? totp.trim().length < 4
      : isSetup || isRegister
        ? !email.trim() || !username.trim() || password.length === 0
        : !identifier.trim() || password.length === 0;

  return (
    <Shell>
      <div className="text-center">
        <div className="mx-auto mb-4 grid size-11 place-items-center rounded-xl bg-gradient-to-br from-primary to-violet-500 text-lg font-extrabold text-white">
          A
        </div>
        <h2 className="text-lg font-semibold tracking-tight">{heading}</h2>
        <p className="mx-auto mt-1.5 max-w-sm text-[13px] leading-relaxed text-muted-foreground">
          {blurb}
        </p>
      </div>

      <div className="mt-5 grid gap-3">
        {isSetup || isRegister ? (
          <>
            <Input
              label="Email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={setEmail}
              placeholder="you@example.com"
              leftIcon={<UserRound />}
            />
            <Input
              label="Username"
              autoComplete="username"
              value={username}
              onChange={setUsername}
              placeholder="how you sign in"
              leftIcon={<UserRound />}
            />
            <Input
              label="Display name"
              value={displayName}
              onChange={setDisplayName}
              placeholder="optional"
            />
            <div>
              <Input
                label="Password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={setPassword}
                placeholder="choose a strong password"
                leftIcon={<KeyRound />}
                error={errorCode === "weak_password" ? error ?? undefined : undefined}
              />
              {password.length > 0 && unmet.length > 0 ? (
                <p className="mt-1.5 px-1 text-xs text-muted-foreground">
                  Needs {unmet.join(", ")}.
                </p>
              ) : null}
            </div>
          </>
        ) : null}

        {view === "login" ? (
          <>
            <Input
              label="Email or username"
              autoComplete="username"
              value={identifier}
              onChange={setIdentifier}
              placeholder="you@example.com"
              leftIcon={<UserRound />}
            />
            <Input
              label="Password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={setPassword}
              leftIcon={<KeyRound />}
              error={error && !errorCode ? error : undefined}
            />
          </>
        ) : null}

        {isMfa ? (
          <Input
            label="Authenticator code"
            value={totp}
            onChange={setTotp}
            placeholder="123456"
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            leftIcon={<ShieldCheck />}
          />
        ) : null}
      </div>

      {error && errorCode ? (
        <p
          role="alert"
          className="mt-3 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs leading-relaxed text-destructive"
        >
          {error}
          <span className="mt-1 block font-mono text-[10px] opacity-70">{errorCode}</span>
        </p>
      ) : null}

      <div className="mt-4 grid gap-2.5">
        <StatefulButton
          state={buttonState}
          disabled={disabled}
          className="w-full"
          onClick={() => void submit()}
          loadingText={isSetup ? "Creating…" : "Signing in…"}
          successText="Signed in"
          errorText="Try again"
        >
          {isSetup ? "Create the owner account" : isMfa ? "Verify" : isRegister ? "Create account" : "Sign in"}
        </StatefulButton>

        {isMfa ? (
          <button
            type="button"
            className="text-[13px] font-medium text-primary hover:underline"
            onClick={() => {
              setTotp("");
              setView("login");
              setError(null);
              setErrorCode(null);
              setButtonState("idle");
            }}
          >
            ← Use a different account
          </button>
        ) : null}

        {!isSetup && status.allow_signup ? (
          <button
            type="button"
            className="text-[13px] font-medium text-primary hover:underline"
            onClick={() => {
              setView(isRegister ? "login" : "register");
              setError(null);
              setErrorCode(null);
              setButtonState("idle");
            }}
          >
            {isRegister ? "← I already have an account" : "Create an account →"}
          </button>
        ) : null}

        {!isSetup && !status.allow_signup ? (
          <p className="text-center text-[11px] leading-relaxed text-muted-foreground">
            Sign-up is disabled on this instance. Ask the owner to create an account for you.
          </p>
        ) : null}
      </div>

      {status.auth_required ? null : (
        <p className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-600 dark:text-amber-400">
          <strong>Auth is switched off</strong> on this instance, so reads work without
          signing in. Signing in is still the only way to write.
        </p>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center bg-background px-4 py-10">
      <Card className="w-full max-w-sm p-6">{children}</Card>
    </div>
  );
}
