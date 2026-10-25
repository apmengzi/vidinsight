import { lazy, Suspense, useEffect, useState, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, FileText, Clock } from 'lucide-react'
import { useTaskStore } from '@/store/taskStore'
import { Button } from '@/components/ui/button.tsx'
import VisualExportButtons from '@/pages/NoteDetailPage/components/VisualExportButtons.tsx'

type ViewStatus = 'idle' | 'loading' | 'success' | 'failed'

const MarkdownViewer = lazy(() => import('@/pages/HomePage/components/MarkdownViewer.tsx'))

export default function NoteDetailPage() {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const setCurrentTask = useTaskStore(state => state.setCurrentTask)
  const tasks = useTaskStore(state => state.tasks)
  const getCurrentTask = useTaskStore(state => state.getCurrentTask)

  const task = useMemo(() => tasks.find(t => t.id === taskId), [tasks, taskId])
  const currentTask = getCurrentTask()

  const [status, setStatus] = useState<ViewStatus>('idle')

  // 当前展示用的纯文本 markdown：task.markdown 可能是多版本数组
  // （[{ver_id, content}]），也可能直接是字符串——两种都要稳妥取到。
  const markdownText = useMemo(() => {
    const m: any = (task as any)?.markdown
    if (!m) return ''
    if (typeof m === 'string') return m
    if (Array.isArray(m)) {
      const first = m[0]
      if (!first) return ''
      return typeof first === 'string' ? first : (first.content ?? first.markdown ?? '')
    }
    return String(m ?? '')
  }, [task])

  // 设置当前任务
  useEffect(() => {
    if (taskId && task) {
      setCurrentTask(taskId)
    }
  }, [taskId, task, setCurrentTask])

  // 如果没有这个任务，回到历史页
  useEffect(() => {
    if (taskId && !task && tasks.length > 0) {
      navigate('/history', { replace: true })
    }
  }, [taskId, task, tasks.length, navigate])

  // 同步状态
  useEffect(() => {
    if (!currentTask || currentTask.id !== taskId) return
    if (currentTask.status === 'SUCCESS') {
      setStatus('success')
    } else if (currentTask.status === 'FAILED') {
      setStatus('failed')
    } else {
      setStatus('loading')
    }
  }, [currentTask, currentTask?.status, taskId])

  if (!task) {
    return (
      <div className="flex h-full flex-col items-center justify-center text-neutral-400">
        <FileText className="h-12 w-12" />
        <p className="mt-3 text-sm">分析记录不存在</p>
        <Button variant="link" onClick={() => navigate('/history')} className="mt-2">
          返回分析历史
        </Button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col bg-[var(--vi-surface)]">
      {/* 顶部导航栏 */}
      <header className="flex shrink-0 items-center gap-4 border-b border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-4 py-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate('/history')}
          className="flex items-center gap-1 text-[var(--vi-ink-3)] hover:text-[var(--vi-ink)]"
        >
          <ArrowLeft className="h-4 w-4" />
          返回历史
        </Button>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="tc shrink-0 text-[9px] uppercase tracking-[0.28em] text-[var(--vi-ink-3)]">
            REPORT
          </span>
          <span aria-hidden className="h-3 w-px shrink-0 bg-[var(--vi-hairline)]" />
          <h1 className="truncate text-sm font-medium text-[var(--vi-ink)]">
            {task.audioMeta?.title || '未命名分析'}
          </h1>
        </div>
        <div className="tc flex shrink-0 items-center gap-1.5 text-[10px] text-[var(--vi-ink-3)]">
          <Clock className="h-3.5 w-3.5" />
          <span>{new Date(task.createdAt).toLocaleString('zh-CN')}</span>
        </div>
        {status === 'success' && markdownText && (
          <VisualExportButtons
            markdown={markdownText}
            sourceLabel={task.audioMeta?.title || ''}
            coverUrl={task.audioMeta?.cover_url || ''}
          />
        )}
      </header>

      {/* 分析内容区域：全屏阅读容器，双栏（正文 + 胶卷目录）由 MarkdownViewer 内部自适应 */}
      <div className="min-h-0 flex-1 overflow-hidden">
        <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-[var(--vi-ink-3)]">加载中...</div>}>
          <MarkdownViewer status={status} />
        </Suspense>
      </div>
    </div>
  )
}
