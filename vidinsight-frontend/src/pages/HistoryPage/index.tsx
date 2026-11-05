import { lazy, Suspense, useEffect, useState, useMemo, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTaskStore } from '@/store/taskStore'
import { fetchTaskList } from '@/services/note.ts'
import { Clock, FileText, ArrowLeft, Search, ChevronLeft, PanelLeft } from 'lucide-react'
import NoteHistory from '@/pages/HomePage/components/NoteHistory.tsx'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { Button } from '@/components/ui/button.tsx'

type ViewStatus = 'idle' | 'loading' | 'success' | 'failed'

const MarkdownViewer = lazy(() => import('@/pages/HomePage/components/MarkdownViewer.tsx'))

export default function HistoryPage() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const tasks = useTaskStore(state => state.tasks)
  const currentTaskId = useTaskStore(state => state.currentTaskId)
  const setCurrentTask = useTaskStore(state => state.setCurrentTask)
  const hydrateFromServer = useTaskStore(state => state.hydrateFromServer)

  // 后端补全必须在这里做，不能只放在 NoteHistory 里（2026-11-03 修）。
  // 原来只有 NoteHistory 拉 /tasks/list，但本组件在 tasks 为空时会提前 return
  // 「暂无分析记录」，NoteHistory 根本没挂载 → 补全永远跑不到 → 换浏览器/
  // 清缓存后历史全空，而服务器上明明有笔记。
  const [hydrating, setHydrating] = useState(true)
  useEffect(() => {
    let cancelled = false
    fetchTaskList()
      .then(res => {
        if (cancelled) return
        if (res?.items?.length) hydrateFromServer(res.items)
      })
      .finally(() => {
        if (!cancelled) setHydrating(false)
      })
    return () => { cancelled = true }
  }, [hydrateFromServer])

  // 按创建时间倒序
  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()),
    [tasks],
  )

  // 当前选中的任务（直接订阅，非方法引用）
  const currentTask = useMemo(
    () => tasks.find(t => t.id === currentTaskId) || null,
    [tasks, currentTaskId],
  )

  // 初始化：URL param 优先，否则默认第一个（最近一次）
  const [selectedId, setSelectedId] = useState<string | null>(null)
  useEffect(() => {
    if (taskId) {
      setSelectedId(taskId)
      setCurrentTask(taskId)
    } else if (sortedTasks.length > 0 && !selectedId) {
      const firstId = sortedTasks[0].id
      setSelectedId(firstId)
      setCurrentTask(firstId)
    }
  }, [taskId, sortedTasks])

  // 当 sortedTasks 变化且当前 selectedId 不在列表中时，重置
  useEffect(() => {
    if (selectedId && !sortedTasks.find(t => t.id === selectedId)) {
      const firstId = sortedTasks[0]?.id ?? null
      setSelectedId(firstId)
      if (firstId) setCurrentTask(firstId)
    }
  }, [sortedTasks])

  // 同步推导状态（不用 useEffect，避免多余渲染）
  const status: ViewStatus = useMemo(() => {
    if (!currentTask || currentTask.id !== selectedId) return 'idle'
    if (currentTask.status === 'SUCCESS') return 'success'
    if (currentTask.status === 'FAILED') return 'failed'
    return 'loading'
  }, [currentTask?.id, currentTask?.status, selectedId])

  const handleSelect = useCallback(
    (id: string) => {
      setSelectedId(id)
      setCurrentTask(id)
      navigate(`/history/${id}`, { replace: true })
    },
    [navigate, setCurrentTask],
  )

  // ===== 移动端：列表视图 vs 详情视图 =====
  const [showDetail, setShowDetail] = useState(false)

  // ===== 桌面端列表收起（P0-1，2026-11-05） =====
  // 旧版左侧列表恒占 lg:320/xl:360，窄屏下把右侧正文压到不可读。
  // 现在桌面端可收起：列表宽度归零让正文撑满，按钮形态与 AppLayout
  // 侧栏按钮一致（圆形悬浮，贴列表右边缘）；状态持久化到 localStorage。
  const COLLAPSE_KEY = 'history-list-collapsed'
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window !== 'undefined' && window.innerWidth >= 1024,
  )
  const [isXl, setIsXl] = useState(
    () => typeof window !== 'undefined' && window.innerWidth >= 1280,
  )
  const [listCollapsed, setListCollapsed] = useState(() => {
    try { return localStorage.getItem(COLLAPSE_KEY) === '1' } catch { return false }
  })

  useEffect(() => {
    const check = () => {
      setIsDesktop(window.innerWidth >= 1024)
      setIsXl(window.innerWidth >= 1280)
    }
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [])

  const toggleListCollapsed = useCallback(() => {
    setListCollapsed(prev => {
      const next = !prev
      // 状态持久化：刷新/重开后保持用户的收起偏好
      try { localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0') } catch { /* 存储不可用则仅内存态 */ }
      return next
    })
  }, [])

  const listWidth = isXl ? 360 : 320

  // 移动端点击任务后切到详情
  const handleMobileSelect = useCallback(
    (id: string) => {
      handleSelect(id)
      setShowDetail(true)
    },
    [handleSelect],
  )

  const handleBackToList = useCallback(() => {
    setShowDetail(false)
  }, [])

  // ===== 渲染 =====
  if (sortedTasks.length === 0) {
    // 补全请求还没回来时先显示加载态，避免「暂无分析记录」闪一下又出现列表
    return (
      <div className="flex h-full flex-col items-center justify-center bg-[var(--vi-bg)] text-[var(--vi-ink-3)]">
        {hydrating ? (
          <>
            <Clock className="h-12 w-12 animate-pulse" />
            <p className="mt-3 text-sm">正在同步分析记录…</p>
          </>
        ) : (
          <>
            <FileText className="h-12 w-12" />
            <p className="mt-3 text-sm">暂无分析记录</p>
            <p className="mt-1 text-xs">粘贴竞品视频链接到对话窗口即可开始分析</p>
          </>
        )}
      </div>
    )
  }

  const selectedTask = sortedTasks.find(t => t.id === selectedId)

  return (
    <div className="relative flex h-full flex-col bg-[var(--vi-bg)] lg:flex-row">
      {/* ===== 桌面端列表收起按钮（P0-1）=====
          圆形悬浮（h-8 w-8 rounded-full），贴列表右边缘，随收起动画同步移动；
          与 AppLayout 侧栏按钮同形态——全局只允许一种展开按钮形状（P1-2）。 */}
      {isDesktop && (
        <button
          onClick={toggleListCollapsed}
          style={{ left: listCollapsed ? 8 : listWidth + 8 }}
          className="absolute top-3 z-20 flex h-8 w-8 items-center justify-center rounded-full border border-neutral-200 bg-white shadow-sm transition-all duration-300 hover:bg-neutral-50"
          title={listCollapsed ? '展开历史列表' : '收起历史列表'}
          aria-label={listCollapsed ? '展开历史列表' : '收起历史列表'}
        >
          {listCollapsed ? (
            <PanelLeft className="h-4 w-4 text-neutral-500" />
          ) : (
            <ChevronLeft className="h-4 w-4 text-neutral-500" />
          )}
        </button>
      )}

      {/* ===== 左侧：历史列表 ===== */}
      {/* 桌面端：宽度由 JS 控制（收起=0 且去边框），transition 平滑动画；
          移动端：维持原有 showDetail 视图切换，不受收起影响 */}
      <aside
        style={isDesktop ? {
          width: listCollapsed ? 0 : listWidth,
          borderRightWidth: listCollapsed ? 0 : 1,
        } : undefined}
        className={`flex shrink-0 flex-col overflow-hidden border-r border-[var(--vi-hairline)] bg-[var(--vi-bg)] transition-all duration-300 ${
          isDesktop ? '' : `${showDetail ? 'hidden' : 'flex w-full'} h-full`
        }`}
      >
        {/* 列表头部 */}
        <div className="shrink-0 border-b border-[var(--vi-hairline)] px-4 py-3">
          <div className="flex items-center gap-2">
            <Clock className="h-5 w-5 text-[var(--vi-ink-2)]" />
            <h1 className="text-lg font-bold text-[var(--vi-ink)]">分析历史</h1>
          </div>
          <p className="mt-0.5 text-xs text-[var(--vi-ink-3)]">
            共 {sortedTasks.length} 条内容分析记录，点击查看详情
          </p>
        </div>

        {/* 列表内容 */}
        <div className="relative flex-1 min-h-0">
          <ScrollArea className="h-full">
            <div className="p-3">
              <NoteHistory onSelect={handleSelect} selectedId={selectedId} />
            </div>
          </ScrollArea>
          {/* 滚动区底部渐变羽化 */}
          <div
            className="pointer-events-none absolute bottom-0 left-0 right-0 h-12 z-10"
            style={{ background: 'linear-gradient(to bottom, transparent, var(--vi-bg))' }}
          />
        </div>
      </aside>

      {/* ===== 右侧：笔记详情 ===== */}
      {/* 列表收起后 flex-1 自动撑满全宽（P0-1 验收点：CDP 量 clientWidth） */}
      <main
        className={`flex flex-1 flex-col overflow-hidden ${
          isDesktop ? 'flex' : showDetail ? 'flex' : 'hidden'
        }`}
      >
        {/* 顶部导航 */}
        <header className="flex shrink-0 items-center gap-3 border-b border-neutral-100 px-4 py-3">
          {/* 移动端返回按钮 */}
          <Button
            variant="ghost"
            size="sm"
            onClick={handleBackToList}
            className="flex items-center gap-1 text-neutral-500 hover:text-neutral-800 lg:hidden"
          >
            <ArrowLeft className="h-4 w-4" />
            返回列表
          </Button>

          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-neutral-100 lg:flex">
              <FileText className="h-4 w-4 text-[var(--vi-ink-2)]" />
            </div>
            <div className="min-w-0">
              <h2 className="truncate text-sm font-semibold text-[var(--vi-ink)]">
                {selectedTask?.audioMeta?.title || '未命名分析'}
              </h2>
              {selectedTask && (
                <p className="text-xs text-[var(--vi-ink-3)]">
                  {new Date(selectedTask.createdAt).toLocaleString('zh-CN')}
                </p>
              )}
            </div>
          </div>
        </header>

        {/* 分析内容 */}
        <div className="flex-1 overflow-hidden">
          <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-[var(--vi-ink-3)]">加载中...</div>}>
            <MarkdownViewer status={status} />
          </Suspense>
        </div>
      </main>
    </div>
  )
}
