import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard is served as static files by the same host as the API, so the
// dev proxy points at the FastAPI process to keep paths identical in both.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
