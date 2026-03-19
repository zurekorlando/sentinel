import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Target for the API proxy — overridable via env var when running in Docker.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  // Use a user-writable cache dir so Vite works when node_modules/.vite
  // was previously created by root (e.g. after running docker compose).
  cacheDir: '/tmp/sentinel-vite-cache',
  server: {
    port: 5173,
    proxy: {
      // REST API
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
      // WebSocket progress stream
      '/ws': {
        target: API_TARGET.replace(/^http/, 'ws'),
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          // Heavy charting library — separate chunk loaded only on Storage page
          recharts: ['recharts'],
          // Radix UI primitives — loaded on all pages but large
          radix: [
            '@radix-ui/react-dialog',
            '@radix-ui/react-select',
            '@radix-ui/react-tabs',
            '@radix-ui/react-tooltip',
            '@radix-ui/react-progress',
            '@radix-ui/react-slider',
            '@radix-ui/react-switch',
            '@radix-ui/react-label',
            '@radix-ui/react-separator',
          ],
          // React + router + query — shared runtime
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
        },
      },
    },
  },
})
