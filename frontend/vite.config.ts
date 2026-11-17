import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Pullarr compares the browser's Origin with the original request Host.
const backend = { target: "http://127.0.0.1:6997", changeOrigin: false };

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/initialize.json": backend,
      "/login": backend,
      "/setup": backend,
      "/auth": backend,
    },
  },
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
});
