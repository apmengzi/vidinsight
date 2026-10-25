import './App.css'
import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, HashRouter, Navigate, Routes, Route } from 'react-router-dom'
import { ConfigProvider, theme as antdTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useTaskPolling } from '@/hooks/useTaskPolling.ts'
import { useTheme } from '@/hooks/useTheme'
import { useCheckBackend } from '@/hooks/useCheckBackend.ts'
import { systemCheck } from '@/services/system.ts'
import BackendInitDialog from '@/components/BackendInitDialog'
import StartupBanner from '@/components/SystemDiagnostic/StartupBanner'
import BackendHealthIndicator from '@/components/BackendHealth/BackendHealthIndicator'

// 布局
import AppLayout from '@/layouts/AppLayout'

// 首屏页面

// 非首屏页面使用 React.lazy 按需加载
const Onboarding = lazy(() => import('@/pages/Onboarding'))
const ChatPage = lazy(() => import('@/pages/ChatPage'))
const BoardPage = lazy(() => import('@/pages/BoardPage'))
const HomePage = lazy(() => import('@/pages/HomePage/Home.tsx').then(module => ({ default: module.HomePage })))
const HistoryPage = lazy(() => import('@/pages/HistoryPage'))
const SettingPage = lazy(() => import('./pages/SettingPage/index.tsx'))

// 桌面端首启引导守卫
function OnboardingGuard({ children }: { children: React.ReactNode }) {
  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  if (!isTauri) return <>{children}</>
  if (localStorage.getItem('vidinsight-onboarded') !== '1') return <Navigate to="/onboarding" replace />
  return <>{children}</>
}

const Model = lazy(() => import('@/pages/SettingPage/Model.tsx'))
const ProviderForm = lazy(() => import('@/components/Form/modelForm/Form.tsx'))
const AboutPage = lazy(() => import('@/pages/SettingPage/about.tsx'))
const Monitor = lazy(() => import('@/pages/SettingPage/Monitor.tsx'))
const Downloader = lazy(() => import('@/pages/SettingPage/Downloader.tsx'))
const DownloaderForm = lazy(() => import('@/components/Form/DownloaderForm/Form.tsx'))
const TranscriberPage = lazy(() => import('@/pages/SettingPage/transcriber.tsx'))
const NotFoundPage = lazy(() => import('@/pages/NotFoundPage'))

function App() {
  useTaskPolling(3000)
  const { loading, initialized, failed, lastError, retry } = useCheckBackend()

  useEffect(() => {
    if (initialized) {
      systemCheck()
    }
  }, [initialized])

  // 外链统一走系统浏览器：webview 里点 http(s) 链接会把整个应用导航走且回不来
  // （用户实测：点了视频链接后 VidInsight 界面丢失）。capture 阶段拦截所有 <a>。
  useEffect(() => {
    if (!('__TAURI_INTERNALS__' in window)) return
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      const anchor = target?.closest?.('a')
      if (!anchor) return
      const href = anchor.getAttribute('href') || ''
      if (!/^https?:\/\//i.test(href)) return
      if (/^(https?:\/\/)?(localhost|127\.0\.0\.1|tauri\.localhost)([:/]|$)/i.test(href)) return
      e.preventDefault()
      e.stopPropagation()
      import('@tauri-apps/plugin-shell')
        .then(({ open }) => open(href))
        .catch(() => { /* 打不开就放弃，别把应用带走 */ })
    }
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [])

  if (!initialized) {
    return (
      <>
        <StartupBanner />
        <BackendInitDialog
          open={loading}
          failed={failed}
          lastError={lastError}
          onRetry={retry}
        />
      </>
    )
  }

  const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const Router = isTauri ? HashRouter : BrowserRouter

  return (
    <ThemeProvider>
      <StartupBanner />
      <BackendHealthIndicator />
      <Router>
        <Suspense fallback={<div className="flex h-screen items-center justify-center">正在加载 VidInsight…</div>}>
          <Routes>
            <Route path="/onboarding" element={<Onboarding />} />

            {/* 主应用：侧边栏 + 内容区 */}
            <Route path="/" element={<OnboardingGuard><AppLayout /></OnboardingGuard>}>
              {/* 默认跳转聊天页 */}
              <Route index element={<Navigate to="/chat" replace />} />
              <Route path="chat" element={<ChatPage />} />
              <Route path="chat/:conversationId" element={<ChatPage />} />

              {/* 内容分析 */}
              <Route path="generate" element={<HomePage />} />

              {/* 我的收藏（收藏专辑勾选工作台） */}
              <Route path="board" element={<BoardPage />} />

              {/* 生成历史（默认显示最近一条） */}
              <Route path="history" element={<HistoryPage />} />
              <Route path="history/:taskId" element={<HistoryPage />} />

              {/* 设置 */}
              <Route path="settings" element={<SettingPage />}>
                <Route index element={<Navigate to="model" replace />} />
                <Route path="model" element={<Model />}>
                  <Route path="new" element={<ProviderForm isCreate />} />
                  <Route path=":id" element={<ProviderForm />} />
                </Route>
                <Route path="download" element={<Downloader />}>
                  <Route path=":id" element={<DownloaderForm />} />
                </Route>
                <Route path="transcriber" element={<TranscriberPage />} />
                <Route path="monitor" element={<Monitor />} />
                <Route path="about" element={<AboutPage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>

              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </Suspense>
      </Router>
    </ThemeProvider>
  )
}

export default App


/// antd 主题壳：跟随 useTheme 浅/深切换（VS Code 式）
function ThemeProvider({ children }: { children: React.ReactNode }) {
  const { theme } = useTheme()
  const dark = theme === 'dark'
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: dark ? '#E8EAED' : '#1F2328',
          colorLink: '#0E7C66',
          borderRadius: 8,
          colorBgLayout: dark ? '#14161A' : '#F5F6F8',
        },
      }}
    >
      {children}
    </ConfigProvider>
  )
}
