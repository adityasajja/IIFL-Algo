import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  // The test environment is `node` by default, which is right: the tests target
  // the pure derivations in `src/lib`, not the rendering. A DOM environment
  // would let a test pass by mounting a component that renders nothing useful.
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  server: {
    port: 5173,
    // `atr dev` runs Vite and FastAPI on separate ports; proxy the API's known
    // root paths to the backend so api.ts's relative URLs work in both dev and
    // `atr serve` (single-port, no proxy needed there) without hardcoding a host.
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/history": "http://127.0.0.1:8000",
      "/login": "http://127.0.0.1:8000",
      "/alerts": "http://127.0.0.1:8000",
      "/audit": "http://127.0.0.1:8000",
      "/analytics": "http://127.0.0.1:8000",
      "/backtests": "http://127.0.0.1:8000",
      "/backtest": "http://127.0.0.1:8000",
      "/briefing": "http://127.0.0.1:8000",
      "/candles": "http://127.0.0.1:8000",
      "/dashboard": "http://127.0.0.1:8000",
      "/evidence": "http://127.0.0.1:8000",
      "/experiments": "http://127.0.0.1:8000",
      "/instruments": "http://127.0.0.1:8000",
      "/learning": "http://127.0.0.1:8000",
      "/market-intel": "http://127.0.0.1:8000",
      "/monitor": "http://127.0.0.1:8000",
      "/optimization": "http://127.0.0.1:8000",
      "/orders": "http://127.0.0.1:8000",
      "/paper": "http://127.0.0.1:8000",
      "/portfolio": "http://127.0.0.1:8000",
      "/positions": "http://127.0.0.1:8000",
      "/quote": "http://127.0.0.1:8000",
      "/research": "http://127.0.0.1:8000",
      "/tracker": "http://127.0.0.1:8000",
      "/risk": "http://127.0.0.1:8000",
      "/scan-all": "http://127.0.0.1:8000",
      "/scan": "http://127.0.0.1:8000",
      "/scanner": "http://127.0.0.1:8000",
      "/self-learning": "http://127.0.0.1:8000",
      "/signal-context": "http://127.0.0.1:8000",
      "/strategies": "http://127.0.0.1:8000",
      "/symbols": "http://127.0.0.1:8000",
      "/ticks": "http://127.0.0.1:8000",
      "/trade-signals": "http://127.0.0.1:8000",
      "/validation": "http://127.0.0.1:8000",
      "/watchlists": "http://127.0.0.1:8000",
    },
  },
  preview: {
    port: 5173,
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom"],
          "vendor-charts": ["lightweight-charts"],
          "vendor-motion": ["motion/react"],
          "vendor-icons": ["lucide-react"],
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
});
