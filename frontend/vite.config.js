import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: 'build',
  },
  server: {
    port: 9500,
    proxy: {
      '^/(na-parts|collision-parts)/': {
        target: 'http://localhost:8001',
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
      },
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  }
})
