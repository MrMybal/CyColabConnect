import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = 'http://127.0.0.1:8420'

export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 2000 },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, ws: true, changeOrigin: true },
      '/mcp': { target: backend, changeOrigin: true },
    },
  },
})
