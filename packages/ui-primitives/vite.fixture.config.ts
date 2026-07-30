import vue from '@vitejs/plugin-vue'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const packageRoot = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  root: path.join(packageRoot, 'fixtures'),
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 4187,
    strictPort: true,
  },
})
