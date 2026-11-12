import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The browser only talks to this origin. /api and /idp are forwarded, so the API
// needs no CORS configuration (the nginx container uses the same two rules).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true, // the IdP only redirects to registered URIs (port 5173 or 8080)
    proxy: {
      "/api": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api/, "") },
      "/idp": { target: "http://localhost:8090", rewrite: (p) => p.replace(/^\/idp/, "") },
    },
  },
  test: { environment: "jsdom" },
});
