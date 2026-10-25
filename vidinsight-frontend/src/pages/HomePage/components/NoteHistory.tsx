import { useTaskStore } from '@/store/taskStore'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { Badge } from '@/components/ui/badge.tsx'
import { cn } from '@/lib/utils.ts'
import { Trash } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'
import PinyinMatch from 'pinyin-match'
import Fuse from 'fuse.js'

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import LazyImage from "@/components/LazyImage.tsx";
import {FC, useState, useEffect, useMemo} from 'react'
import { proxiedImageUrl, resolveAssetUrl } from '@/utils/image-url'
import { fetchTaskList } from '@/services/note.ts'

interface NoteHistoryProps {
  onSelect: (taskId: string) => void
  selectedId: string | null
}

function getTaskDisplayTitle(task: any): string {
  const title = task?.audioMeta?.title?.trim()
  if (title) return title

  const url = task?.formData?.video_url || ''
  if (url) {
    try {
      const host = new URL(url).hostname.replace(/^www\./, '')
      return `未命名竞品视频 · ${host}`
    } catch {
      return '未命名竞品视频'
    }
  }

  return '未命名竞品视频'
}

/** 秒 → 时间码（TC 03:45 / TC 1:02:03） */
function formatTimecode(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  const ss = String(r).padStart(2, '0')
  return h > 0 ? `TC ${h}:${mm}:${ss}` : `TC ${mm}:${ss}`
}

// 就绪面板底纹：16px 极淡点阵（只用既有 hairline token，不引入新色相）
const DOT_GRID = {
  backgroundImage: 'radial-gradient(var(--vi-hairline) 1px, transparent 1px)',
  backgroundSize: '16px 16px',
} as const

const NoteHistory: FC<NoteHistoryProps> = ({ onSelect, selectedId }) => {
  const tasks = useTaskStore(state => state.tasks)
  const removeTask = useTaskStore(state => state.removeTask)
  const hydrateFromServer = useTaskStore(state => state.hydrateFromServer)
  // 后端兜底：IndexedDB 只有本浏览器提交过的任务，挂载时把 note_results 里
  // 本地缺的任务补进来（换浏览器/清缓存后历史不再凭空消失）
  useEffect(() => {
    let cancelled = false
    fetchTaskList().then(res => {
      if (!cancelled && res?.items?.length) hydrateFromServer(res.items)
    })
    return () => { cancelled = true }
  }, [hydrateFromServer])
  // 确保baseURL没有尾部斜杠
  const [rawSearch, setRawSearch] = useState('')
  const [search, setSearch] = useState('')
  const searchableTasks = useMemo(() => tasks.map(task => ({
    ...task,
    displayTitle: getTaskDisplayTitle(task),
  })), [tasks])

  const fuse = useMemo(() => new Fuse(searchableTasks, {
    keys: ['audioMeta.title', 'displayTitle', 'formData.video_url'],
    threshold: 0.4 // 匹配精度（越低越严格）
  }), [searchableTasks])
  useEffect(() => {
    const timer = setTimeout(() => {
      if (rawSearch === '') return
      setSearch(rawSearch)
    }, 300) // 300ms 防抖

    return () => clearTimeout(timer)
  }, [rawSearch])
  const filteredTasks = search.trim()
      ? fuse.search(search).map(result => result.item)
      : searchableTasks
  if (filteredTasks.length === 0) {
    return (
        <>
          <div className="mb-2">
            <input
                type="text"
                placeholder="搜索分析标题..."
                className="tc w-full rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-3 py-1.5 text-xs text-[var(--vi-ink)] outline-none placeholder:text-[var(--vi-ink-3)] focus:border-[var(--vi-ink-3)]"
                value={search}
                onChange={e => setSearch(e.target.value)}
            />
          </div>
          <div
            className="rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] py-8 text-center"
            style={DOT_GRID}
          >
            <p className="tc text-[11px] tracking-[0.18em] text-[var(--vi-ink-3)]">READY · 监视器待命</p>
            <p className="mt-2 text-xs text-[var(--vi-ink-2)]">粘贴链接开始第一条分析</p>
          </div>
        </>

    )
  }


  return (
    <>
      <div className="mb-2">
        <input
            type="text"
            placeholder="搜索分析标题..."
            className="tc w-full rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-3 py-1.5 text-xs text-[var(--vi-ink)] outline-none placeholder:text-[var(--vi-ink-3)] focus:border-[var(--vi-ink-3)]"
            value={search}
            onChange={e => setSearch(e.target.value)}
        />
      </div>
      {/* 列表 + 上下缘 32px 渐进羽化（sticky 跟随父级滚动视口，pointer-events-none 不挡点击） */}
      <div className="relative">
        <div
          className="pointer-events-none sticky top-0 z-10 -mb-8 h-8"
          style={{ background: 'linear-gradient(to bottom, var(--vi-bg), transparent)' }}
        />
        <div className="flex flex-col gap-2">
        {filteredTasks.map(task => {
          const displayTitle = getTaskDisplayTitle(task)
          return (
          <div
            key={task.id}
            onClick={() => onSelect(task.id)}
            className={cn(
              'group cursor-pointer overflow-hidden rounded-xl border bg-[var(--vi-surface)] transition-all',
              selectedId === task.id
                ? 'border-[var(--vi-ink-3)] shadow-[0_0_0_1px_var(--vi-ink-3)]'
                : 'border-[var(--vi-hairline)] hover:border-[var(--vi-ink-3)]',
            )}
          >
            {/* 16:9 横版缩略：B 站 / YouTube 封面原生比例（LazyImage 需要 wrapper 控制比例） */}
            <div className="relative aspect-video w-full overflow-hidden bg-[var(--vi-hover)]">
              {!task.audioMeta.cover_url ? (
                // 无封面：就绪面板底纹占位（16px 极淡点阵），不留空灰块
                <div
                  className="flex h-full w-full items-center justify-center"
                  style={DOT_GRID}
                >
                  <span className="tc text-[10px] tracking-[0.18em] text-[var(--vi-ink-3)]">NO SIGNAL</span>
                </div>
              ) : task.platform === 'local' ? (
                <img
                  src={resolveAssetUrl(task.audioMeta.cover_url)}
                  alt="封面"
                  className="h-full w-full object-cover transition-transform duration-200 ease-out group-hover:scale-[1.02]"
                />
              ) : (
                <div className="h-full w-full">
                  <LazyImage
                    src={proxiedImageUrl(task.audioMeta.cover_url)}
                    alt="封面"
                    className="h-full w-full transition-transform duration-200 ease-out group-hover:scale-[1.02]"
                  />
                </div>
              )}
              {/* 时长徽标：有 duration 数据才显示 */}
              {typeof task.audioMeta.duration === 'number' && task.audioMeta.duration > 0 && (
                <span className="tc absolute bottom-2 left-2 rounded bg-black/55 px-1.5 py-0.5 text-[10px] tabular-nums text-white">
                  {formatTimecode(task.audioMeta.duration)}
                </span>
              )}
              {/* 状态点：右上 mono chip */}
              {task.status === 'SUCCESS' && (
                <span className="tc absolute right-2 top-2 rounded bg-[#0E7C66]/90 px-1.5 py-0.5 text-[10px] text-white">DONE</span>
              )}
              {task.status === 'FAILED' && (
                <span className="tc absolute right-2 top-2 rounded bg-[#C0392B]/90 px-1.5 py-0.5 text-[10px] text-white">FAILED</span>
              )}
              {task.status !== 'SUCCESS' && task.status !== 'FAILED' && (
                <span className="tc absolute right-2 top-2 rounded bg-[#E5484D]/90 px-1.5 py-0.5 text-[10px] text-white">REC</span>
              )}
            </div>
            {/* 标题行 */}
            <div className="flex items-start justify-between gap-1 px-3 py-2.5">
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div className="line-clamp-2 flex-1 text-xs leading-snug text-[var(--vi-ink)]">
                      {displayTitle}
                    </div>
                  </TooltipTrigger>
                  <TooltipContent>
                    <p>{displayTitle}</p>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
              <Button
                type="button"
                size="small"
                variant="ghost"
                onClick={e => {
                  e.stopPropagation()
                  removeTask(task.id)
                }}
                className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
              >
                <Trash className="h-3.5 w-3.5 text-[var(--vi-ink-3)] hover:text-[#E5484D]" />
              </Button>
            </div>
          </div>
        )})}
        </div>
        <div
          className="pointer-events-none sticky bottom-0 z-10 -mt-8 h-8"
          style={{ background: 'linear-gradient(to bottom, transparent, var(--vi-bg))' }}
        />
      </div>
    </>
  )
}

export default NoteHistory
