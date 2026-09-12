import { createContext, useContext, type ReactNode } from "react";
import {
  AnimatedToastStack,
  useAnimatedToastStack,
  type ToastInput,
} from "./toast";

type ToastApi = {
  toast: (input: ToastInput) => string;
  dismiss: (id: string) => void;
};

const ToastCtx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { toasts, showToast, dismissToast } = useAnimatedToastStack({ limit: 5 });
  return (
    <ToastCtx.Provider value={{ toast: showToast, dismiss: dismissToast }}>
      {children}
      <AnimatedToastStack toasts={toasts} onDismiss={dismissToast} position="bottom-right" />
    </ToastCtx.Provider>
  );
}
