import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The workbench talks to the FastAPI control plane through the dev server so
// that the browser never needs cross-origin access or a CORS allowlist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
