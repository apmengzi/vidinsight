import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import tailwindcss from '@tailwindcss/vite'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

function readAppVersion() {
  const fallbackVersion = '0.0.0'

  try {
    const tauriConfigPath = path.resolve(__dirname, 'src-tauri/tauri.conf.json')
    const tauriConfig = JSON.parse(fs.readFileSync(tauriConfigPath, 'utf-8')) as { version?: string }
    return tauriConfig.version || fallbackVersion
  }
  catch {
    return fallbackVersion
  }
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 在 Docker 环境中，父目录可能没有 .env 文件，使用当前目录
  const envDir = process.env.DOCKER_BUILD ? __dirname : path.resolve(__dirname, '../')
  const env = {
    ...loadEnv(mode, envDir),
    ...loadEnv(mode, __dirname),
  }

  // 两个变量的语义不同，不能混用：
  // - VITE_PROXY_TARGET  ：仅 dev 代理用，裸后端地址（如 http://127.0.0.1:8484，不带 /api）
  // - VITE_API_BASE_URL  ：仅浏览器运行时直连用（Tauri 打包场景，必须带 /api 后缀）；
  //                        不设时浏览器走相对路径 /api 经 dev 代理转发
  const apiBaseUrl = env.VITE_PROXY_TARGET || env.VITE_API_BASE_URL || 'http://127.0.0.1:8483'
  const port = parseInt(env.VITE_FRONTEND_PORT || '3015', 10)
  const appVersion = env.VITE_APP_VERSION || process.env.VITE_APP_VERSION || readAppVersion()

  return {
    base: './',
    define: {
      __APP_VERSION__: JSON.stringify(appVersion),
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            markdown: ['react-markdown', 'react-syntax-highlighter', 'remark-gfm', 'remark-math', 'rehype-katex'],
            markmap: ['markmap-lib', 'markmap-view', 'markmap-toolbar', 'markmap-common'],
            vendor: ['react', 'react-dom', 'react-router-dom'],
          },
        },
      },
    },
    server: {
      host: '0.0.0.0',
      port: port,
      allowedHosts: true, // 允许任意域名访问
      proxy: {
        '/api': {
          target: apiBaseUrl,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/api/, '/api'),
        },
        '/static': {
          target: apiBaseUrl,
          changeOrigin: true,
          rewrite: path => path.replace(/^\/static/, '/static'),
        },
      },
    },
  }
})
