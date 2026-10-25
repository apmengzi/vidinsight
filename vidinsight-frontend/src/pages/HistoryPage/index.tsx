import { lazy, Suspense, useEffect, useState, useMemo, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTaskStore } from '@/store/taskStore'
import { Clock, FileText, ArrowLeft, Search } from 'lucide-react'
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
    return (
      <div className="flex h-full flex-col items-center justify-center bg-[var(--vi-bg)] text-[var(--vi-ink-3)]">
        <FileText className="h-12 w-12" />
        <p className="mt-3 text-sm">暂无分析记录</p>
        <p className="mt-1 text-xs">粘贴竞品视频链接到对话窗口即可开始分析</p>
      </div>
    )
  }

  const selectedTask = sortedTasks.find(t => t.id === selectedId)

  return (
    <div className="flex h-full flex-col bg-[var(--vi-bg)] lg:flex-row">
      {/* ===== 左侧：历史列表 ===== */}
      <aside
        className={`flex shrink-0 flex-col border-r border-[var(--vi-hairline)] bg-[var(--vi-bg)] lg:w-[320px] xl:w-[360px] ${
          showDetail ? 'hidden lg:flex' : 'flex'
        } h-full`}
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
      <main
        className={`flex flex-1 flex-col overflow-hidden ${
          !showDetail ? 'hidden lg:flex' : 'flex'
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
