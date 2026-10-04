import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

// Dev: `npm run dev` proxies /api (and the /api/ws websocket) to the API: CLIMATE_API_PROXY
// (scripts/web-dev.sh sets it from DEV_API_PORT), else http://127.0.0.1:8000.
const apiProxy = process.env.CLIMATE_API_PROXY ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon.svg'],
      manifest: {
        name: 'Climate AI',
        short_name: 'Climate',
        description: 'Whole-house comfort and runtime for three ecobees',
        theme_color: '#0f172a',
        background_color: '#0f172a',
        display: 'standalone',
        start_url: '/',
        icons: [{ src: '/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any maskable' }],
      },
      workbox: {
        navigateFallback: '/index.html',
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': { target: apiProxy, changeOrigin: false, ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 1500 },
})
