import { useState, useEffect, useCallback } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import {
  MessageSquarePlus,
  MessageSquare,
  FileText,
  History,
  Settings,
  Trash2,
  ChevronLeft,
  PanelLeft,
  Menu,
  X,
  Bookmark,
  Sun,
  Moon,
} from 'lucide-react'
import { useConversationStore } from '@/store/conversationStore'
import { useTheme } from '@/hooks/useTheme'
import logo from '@/assets/vidinsight-logo.png'

const NAV_ITEMS = [
  { id: 'generate', label: '内容分析', icon: FileText, path: '/generate' },
  { id: 'board', label: '我的收藏', icon: Bookmark, path: '/board' },
  { id: 'history', label: '分析历史', icon: History, path: '/history' },
  { id: 'settings', label: '全局配置', icon: Settings, path: '/settings' },
]

const SIDEBAR_WIDTH = 260
const LG_BREAKPOINT = 1024

export default function AppLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { theme, toggle } = useTheme()

  const [desktopCollapsed, setDesktopCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [isMobile, setIsMobile] = useState(() => {
    if (typeof window === 'undefined') return false
    return window.innerWidth < LG_BREAKPOINT
  })

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < LG_BREAKPOINT)
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [])

  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  const {
    conversations,
    activeConversationId,
    createConversation,
    deleteConversation,
    setActiveConversation,
  } = useConversationStore()

  const isChat = location.pathname.startsWith('/chat') || location.pathname === '/'

  const handleNewChat = () => {
    const id = createConversation()
    navigate(`/chat/${id}`)
  }

  const handleSelectConversation = (id: string) => {
    setActiveConversation(id)
    navigate(`/chat/${id}`)
  }

  const handleDeleteConversation = (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    deleteConversation(id)
    if (activeConversationId === id) {
      navigate('/chat')
    }
  }

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    `relative flex items-center gap-3 rounded-lg px-3 py-[9px] text-sm font-medium transition-colors ${
      isActive
        ? 'bg-chrome-2 text-white'
        : 'text-zinc-400 hover:bg-chrome-2/60 hover:text-zinc-200'
    } ${isActive ? "before:absolute before:left-0 before:top-1/2 before:h-5 before:w-[3px] before:-translate-y-1/2 before:rounded-r before:bg-rec" : ""}`

  const closeMobile = useCallback(() => setMobileOpen(false), [])

  const sidebarContent = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-4">
        <div className="flex items-center gap-3">
          <img src={logo} alt="logo" className="h-9 w-9 rounded-xl" />
          <div className="min-w-0">
            <span className="block text-lg font-bold leading-tight text-white">VidInsight</span>
            <span className="tc block text-[11px] text-zinc-500">灵悉 · 短视频洞察</span>
          </div>
        </div>
        {isMobile && (
          <button
            onClick={closeMobile}
            className="rounded p-1 text-neutral-500 hover:bg-neutral-100"
          >
            <X className="h-5 w-5" />
          </button>
        )}
      </div>

      <div className="px-3 pt-4">
        <button
          onClick={handleNewChat}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-white/15 bg-chrome-2 py-[9px] text-sm font-medium text-zinc-200 transition-colors hover:border-white/30 hover:bg-[#262A32]"
        >
          <MessageSquarePlus className="h-4 w-4" />
          新对话
        </button>
      </div>

      <div className="mt-4 flex-1 overflow-hidden">
        <p className="tc px-4 text-[11px] uppercase tracking-wider text-zinc-500">
          对话历史
        </p>
        <div className="mt-2 flex-1 overflow-y-auto px-2">
          {conversations.map(conv => (
            <div
              key={conv.id}
              onClick={() => handleSelectConversation(conv.id)}
              className={`group flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-sm transition-colors ${
                isChat && activeConversationId === conv.id
                  ? 'bg-chrome-2 text-white'
                  : 'text-zinc-400 hover:bg-chrome-2/60 hover:text-zinc-200'
              }`}
            >
              <div className="flex min-w-0 items-center gap-2">
                <MessageSquare className="h-4 w-4 shrink-0 text-zinc-500" />
                <span className="truncate">{conv.title}</span>
              </div>
              <button
                onClick={e => handleDeleteConversation(e, conv.id)}
                className="ml-1 shrink-0 rounded p-0.5 opacity-0 transition-opacity hover:bg-white/10 group-hover:opacity-100"
              >
                <Trash2 className="h-3.5 w-3.5 text-zinc-500 hover:text-rec" />
              </button>
            </div>
          ))}
          {conversations.length === 0 && (
            <p className="px-3 py-4 text-center text-xs text-zinc-600">暂无对话历史</p>
          )}
        </div>
      </div>

      <div className="mt-auto border-t border-white/10 px-3 py-4">
        <button
          onClick={toggle}
          className="mb-3 flex w-full items-center justify-between rounded-lg border border-white/10 bg-chrome-2/50 px-3 py-2 text-sm text-zinc-300 transition-colors hover:border-white/25 hover:text-white"
          title="切换浅色 / 深色主题"
        >
          <span className="flex items-center gap-2">
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            {theme === 'dark' ? '浅色工作面' : '深色剪辑室'}
          </span>
          <span className="tc text-[10px] text-zinc-500">{theme === 'dark' ? 'LIGHT' : 'DARK'}</span>
        </button>
        <nav className="flex flex-col gap-1">
          {NAV_ITEMS.map(item => (
            <NavLink key={item.id} to={item.path} className={navLinkClass}>
              <item.icon className="h-5 w-5" />
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </div>
  )

  const sidebarVisible = !isMobile && !desktopCollapsed

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--vi-bg)]">
      {/* ===== 桌面端侧边栏 ===== */}
      {!isMobile && (
        <aside
          style={{ width: sidebarVisible ? SIDEBAR_WIDTH : 0, borderRightWidth: sidebarVisible ? 1 : 0 }}
          className="flex shrink-0 flex-col overflow-hidden bg-chrome transition-all duration-300"
        >
          {sidebarContent}
        </aside>
      )}

      {/* ===== 移动端遮罩 + 抽屉 ===== */}
      {isMobile && (
        <>
          {mobileOpen && (
            <div
              className="fixed inset-0 z-20 bg-black/40"
              onClick={closeMobile}
            />
          )}
          <aside
            style={{ width: SIDEBAR_WIDTH, transform: mobileOpen ? 'translateX(0)' : `translateX(-${SIDEBAR_WIDTH}px)` }}
            className="fixed inset-y-0 left-0 z-30 flex flex-col bg-chrome shadow-xl transition-transform duration-300"
          >
            {sidebarContent}
          </aside>
        </>
      )}

      {/* ===== 顶部导航栏（移动端 + 桌面端侧栏收起时） ===== */}
      {(isMobile || desktopCollapsed) && (
        <div className="flex shrink-0 items-center gap-3 border-b border-neutral-200 bg-white px-3 py-2">
          <button
            onClick={() => (isMobile ? setMobileOpen(true) : setDesktopCollapsed(false))}
            className="rounded-lg p-1.5 text-neutral-500 hover:bg-neutral-100"
            title="展开侧边栏"
          >
            {isMobile ? <Menu className="h-5 w-5" /> : <PanelLeft className="h-5 w-5" />}
          </button>
          <div className="flex items-center gap-2">
            <img src={logo} alt="logo" className="h-7 w-7 rounded-lg" />
            <span className="text-base font-bold text-neutral-800">VidInsight / 灵悉</span>
          </div>
        </div>
      )}

      {/* ===== 桌面端侧边栏切换按钮 ===== */}
      {!isMobile && (
        <button
          onClick={() => setDesktopCollapsed(!desktopCollapsed)}
          style={{ left: desktopCollapsed ? 0 : SIDEBAR_WIDTH }}
          className="absolute top-3 z-10 rounded-r-lg border border-l-0 border-neutral-200 bg-white p-1.5 shadow-sm transition-all hover:bg-neutral-50"
          title={desktopCollapsed ? '展开侧边栏' : '收起侧边栏'}
        >
          {desktopCollapsed ? (
            <PanelLeft className="h-4 w-4 text-neutral-500" />
          ) : (
            <ChevronLeft className="h-4 w-4 text-neutral-500" />
          )}
        </button>
      )}

      {/* ===== 主内容区域 ===== */}
      <main className="flex flex-1 flex-col overflow-hidden">
        {!isMobile && desktopCollapsed && <div className="h-10 shrink-0" />}
        <div className="flex-1 overflow-hidden">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
