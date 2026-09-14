import { useCallback, useEffect, useRef, useState } from "react";
import { getMe, isUnauthorized, type Principal } from "../api";

/**
 * Who is using the dashboard, and what they may do.
 *
 * Four states, and the distinction between the last two is the one that matters:
 *
 *   * `loading`       — we do not know yet. Render nothing decisive.
 *   * `authenticated` — a real account, with its effective permissions.
 *   * `anonymous`     — the server answered 401. Show the login gate.
 *   * `offline`       — the server did not answer at all.
 *
 * `anonymous` and `offline` are deliberately separate. A backend that is down
 * would otherwise present the login screen, the user would type a correct
 * password, and the gate would report "invalid credentials" — blaming the user
 * for an outage. It also matters on a laptop: `atr serve` not running is the
 * normal case, not an error worth a login form.
 */
export type SessionState = "loading" | "anonymous" | "authenticated" | "offline";

export interface Session {
  state: SessionState;
  principal: Principal | null;
  /** Human-readable reason for `offline`. */
  error: string | null;
  refresh: () => Promise<void>;
  /** Adopt a principal obtained by a successful login or bootstrap. */
  adopt: (principal: Principal) => void;
  /** Forget the principal locally. Does not call the server. */
  clear: () => void;
  /**
   * Whether the current principal holds a permission.
   *
   * This mirrors the server's check so a button can be disabled instead of
   * failing on click — but it is a *hint*, not a security boundary. The server
   * re-checks every route; a forged client that flips this to `true` gets a 403.
   */
  can: (permission: string) => boolean;
}

export function useSession(): Session {
  const [state, setState] = useState<SessionState>("loading");
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow first response overwriting a faster later one — which
  // happens in practice when the user signs in while the initial `/auth/me` is
  // still in flight: the stale 401 would land after the login and bounce them
  // straight back to the gate.
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const me = await getMe();
      if (seq !== requestSeq.current) return;
      setPrincipal(me);
      setState("authenticated");
      setError(null);
    } catch (e) {
      if (seq !== requestSeq.current) return;
      if (isUnauthorized(e)) {
        setPrincipal(null);
        setState("anonymous");
        setError(null);
        return;
      }
      setPrincipal(null);
      setState("offline");
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const adopt = useCallback((next: Principal) => {
    // Bump the sequence so an in-flight `/auth/me` from before the login cannot
    // land afterwards and undo it.
    requestSeq.current += 1;
    setPrincipal(next);
    setState("authenticated");
    setError(null);
  }, []);

  const clear = useCallback(() => {
    requestSeq.current += 1;
    setPrincipal(null);
    setState("anonymous");
  }, []);

  const can = useCallback(
    (permission: string) => Boolean(principal?.permissions?.includes(permission)),
    [principal],
  );

  return { state, principal, error, refresh, adopt, clear, can };
}
