import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /api is proxied to FastAPI, so the browser talks to one origin and no CORS setup is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    // 0.0.0.0: reachable from other devices on the home Wi-Fi (http://<this PC's IP>:5173). Anyone who can open the
    // page can use the logged-in Kite session; set HOST=127.0.0.1 to keep it on this PC only.
    host: process.env.HOST || "0.0.0.0",
    port: Number(process.env.PORT) || 5173,
    proxy: { "/api": `http://127.0.0.1:${process.env.API_PORT || 8000}` },
  },
});
