import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `--host` in the dev script binds 0.0.0.0 so phones on the same Wi-Fi can
// load the storefront -- that is the whole point of this frontend (several
// real people racing each other on their own devices).
//
// /api is proxied to the FastAPI app so the browser sees one origin in dev.
// Set VITE_API_BASE to hit the backend directly instead (e.g. from a phone
// pointed at your laptop's LAN IP).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8010',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
