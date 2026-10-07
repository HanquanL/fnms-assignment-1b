import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // Fail instead of silently moving to 5174 -- the backend's CORS
    // allowlist names 5173 exactly, so another port would be blocked.
    strictPort: true,
  },
})
