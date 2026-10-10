import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Dev: the SPA runs on :5173 and proxies the API to the FastAPI server.
// Prod: `npm run build` emits dist/, which web/app.py serves at /app.
export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/accounts": "http://127.0.0.1:8765",
      "/runs": "http://127.0.0.1:8765",
      "/registry": "http://127.0.0.1:8765",
    },
  },
  build: {
    outDir: "dist",
  },
})
