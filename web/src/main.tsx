import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { SmoothScroll } from "./components/motion/smooth-scroll";
import { DialogProvider } from "./components/ui/dialog-context";
import { ToastProvider } from "./components/ui/toast-context";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <SmoothScroll>
      <ToastProvider>
        <DialogProvider>
          <App />
        </DialogProvider>
      </ToastProvider>
    </SmoothScroll>
  </React.StrictMode>,
);
