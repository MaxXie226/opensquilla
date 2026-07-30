import vue from '@vitejs/plugin-vue'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const webuiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const packageRoot = path.resolve(webuiRoot, '..', 'packages', 'ui-primitives')

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: path.join(packageRoot, 'dist'),
    emptyOutDir: true,
    lib: {
      entry: path.join(packageRoot, 'src', 'index.ts'),
      formats: ['es'],
      fileName: 'index',
      cssFileName: 'styles',
    },
    rollupOptions: {
      external: ['vue'],
    },
    sourcemap: false,
  },
})
