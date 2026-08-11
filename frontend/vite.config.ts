import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    // Not 5173: chosen to avoid collisions with other dev stacks on the shared build host
    // (see docs/decisions.md).
    port: 5187,
    proxy: {
      // Dev-only: the built SPA is same-origin with the API in production (§2), but the
      // Vite dev server runs on its own port, so /api requests need to be forwarded to
      // the backend service. LFTPWEB_DEV_API_PROXY lets docker-compose.dev.yml point this
      // at the backend container by service name.
      '/api': process.env.LFTPWEB_DEV_API_PROXY ?? 'http://localhost:8087',
    },
  },
})
