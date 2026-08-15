import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Docs → Quick start/Concepts (2026-08-14, docs/decisions.md) import
    // `../../../../docs/*.md?raw` -- outside Vite's default served root (`frontend/`, where its
    // own `package-lock.json` sits) -- so the dev server's fs.allow check needs to know the repo
    // root is fine to read from. `vite build` isn't affected either way: fs.allow is dev-server
    // request-serving middleware, not a build-time restriction.
    fs: {
      allow: ['..'],
    },
    host: true,
    // Not 5173: chosen to avoid collisions with other dev stacks on the shared build host
    // (see docs/decisions.md).
    port: 5187,
    // Vite's DNS-rebinding protection rejects any Host header it doesn't recognise, which
    // breaks reaching this server by hostname (`crzydev.home.arpa`) rather than localhost.
    // Default to allowing any host: this is the *dev* server only — in production the SPA
    // is built and served same-origin by FastAPI (§2), so nothing here is reachable by a
    // deployed user, and a dev server that needs config edits before a teammate can open
    // it on the LAN is a worse trade than DNS-rebinding protection is worth locally.
    // Set LFTPWEB_DEV_ALLOWED_HOSTS (comma-separated) to narrow it back down.
    allowedHosts: process.env.LFTPWEB_DEV_ALLOWED_HOSTS
      ? process.env.LFTPWEB_DEV_ALLOWED_HOSTS.split(',')
          .map((h) => h.trim())
          .filter(Boolean)
      : true,
    proxy: {
      // Dev-only: the built SPA is same-origin with the API in production (§2), but the
      // Vite dev server runs on its own port, so /api requests need to be forwarded to
      // the backend service. LFTPWEB_DEV_API_PROXY lets docker-compose.dev.yml point this
      // at the backend container by service name.
      //
      // `ws: true` is load-bearing, not decorative: useLiveModel.ts opens the one
      // WebSocket (§2/§9) at `window.location.host` + `/api/ws`, which in dev is *this*
      // server, not the backend. Without proxying the upgrade, the Files page connects to
      // nothing and renders empty while every REST call still works — a failure that looks
      // like a backend bug and isn't.
      '/api': {
        target: process.env.LFTPWEB_DEV_API_PROXY ?? 'http://localhost:8087',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
