import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The SPA is served by the same FastAPI process that serves /api, so there is
// one Cloud Run service, one deploy, and no CORS. In dev, proxy through to a
// locally running uvicorn.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/healthz': 'http://127.0.0.1:8080',
    },
  },
})
