import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"
import { ahBuild } from "./build"

// `npm run dev` serves the page with hot reload and sends /api to `ah view --no-open`.
export default defineConfig({
  plugins: [react(), tailwindcss(), ahBuild(__dirname)],
  base: "/",
  build: {
    outDir: "../agent_history/static",
    emptyOutDir: true,
    target: "es2022",
    reportCompressedSize: false,
    modulePreload: { polyfill: false },
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:4545", changeOrigin: true } },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
})
