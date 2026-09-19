import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { Button } from "./button";
import { Input } from "./input";
import { MorphingModal } from "./modal";

/**
 * In-app replacement for `window.confirm` / `window.prompt`.
 *
 * Native dialogs are silently suppressed in embedded browsers (and by browsers
 * that throttle them), where they return "cancel" without ever showing. For
 * ordinary confirmations that made a button look dead; for the kill switch it
 * meant the control could not be used at all. These render through the app's own
 * modal, so they always appear and behave the same everywhere.
 */

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** `danger` styles the confirm button as destructive. */
  tone?: "default" | "danger";
}

export interface PromptOptions extends ConfirmOptions {
  label?: string;
  placeholder?: string;
  defaultValue?: string;
  /** When true the confirm button stays disabled until something is typed. */
  required?: boolean;
}

interface Pending {
  kind: "confirm" | "prompt";
  opts: PromptOptions;
  cancelValue: boolean | null;
  resolve: (value: boolean | string | null) => void;
}

interface DialogApi {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  /** Resolves with the typed text, or `null` if the user cancelled. */
  prompt: (opts: PromptOptions) => Promise<string | null>;
}

const DialogCtx = createContext<DialogApi | null>(null);

export function useDialog(): DialogApi {
  const ctx = useContext(DialogCtx);
  if (!ctx) throw new Error("useDialog must be used inside <DialogProvider>");
  return ctx;
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  // Kept so the content stays on screen while the modal animates out.
  const lastShown = useRef<Pending | null>(null);
  if (pending) lastShown.current = pending;

  const open = useCallback(
    (kind: Pending["kind"], opts: PromptOptions, cancelValue: boolean | null) =>
      new Promise<boolean | string | null>((resolve) => {
        setText(opts.defaultValue ?? "");
        // A newer dialog cancels an older one still waiting.
        setPending((prev) => {
          prev?.resolve(prev.cancelValue);
          return { kind, opts, cancelValue, resolve };
        });
      }),
    [],
  );

  const api: DialogApi = {
    confirm: (opts) => open("confirm", opts, false) as Promise<boolean>,
    prompt: (opts) => open("prompt", opts, null) as Promise<string | null>,
  };

  function finish(value: boolean | string | null) {
    pending?.resolve(value);
    setPending(null);
  }

  const shown = pending ?? lastShown.current;
  const isPrompt = shown?.kind === "prompt";
  const danger = shown?.opts.tone === "danger";

  return (
    <DialogCtx.Provider value={api}>
      {children}
      <MorphingModal
        viewId={pending ? "dialog" : null}
        onClose={() => finish(pending?.cancelValue ?? null)}
      >
        {shown ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (shown.opts.required && !text.trim()) return;
              finish(isPrompt ? text : true);
            }}
          >
            <h2 className="text-base font-semibold tracking-tight">{shown.opts.title}</h2>
            {shown.opts.description ? (
              <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
                {shown.opts.description}
              </p>
            ) : null}
            {isPrompt ? (
              <div className="mt-4">
                <Input
                  label={shown.opts.label}
                  value={text}
                  onChange={setText}
                  placeholder={shown.opts.placeholder}
                  autoFocus
                />
              </div>
            ) : null}
            <div className="mt-5 flex justify-end gap-2.5">
              <Button type="button" variant="secondary" onClick={() => finish(shown.cancelValue)}>
                {shown.opts.cancelLabel ?? "Cancel"}
              </Button>
              <Button
                type="submit"
                disabled={isPrompt && shown.opts.required === true && !text.trim()}
                className={danger ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
              >
                {shown.opts.confirmLabel ?? (isPrompt ? "Continue" : "Confirm")}
              </Button>
            </div>
          </form>
        ) : null}
      </MorphingModal>
    </DialogCtx.Provider>
  );
}
