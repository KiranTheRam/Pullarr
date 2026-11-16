import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:6997";

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
