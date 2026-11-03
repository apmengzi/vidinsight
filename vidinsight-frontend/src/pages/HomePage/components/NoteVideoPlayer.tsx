import { useEffect, useRef, useState } from 'react'
import { Play, Download, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { getOwnerId } from '@/utils/request.ts'

/**
 * 笔记页内嵌播放器（2026-11-03 离线回看）。
 *
 * 设计目标：像刷小红书一样，点开笔记就能看原片。
 * - 视频已归档在本地（note_results/{task_id}/video.mp4），断网也能播
 * - 默认收起只显示一行「播放原片」，点击展开播放器
 * - 支持 Range 请求，进度条可拖动
 * - 没有归档视频时（老笔记），显示「保存原片」让用户就地补下
 */
interface Props {
  /** 任务 ID，用来拼 /api/task_video/{taskId} */
  taskId?: string
  /** 封面图，播放器 poster */
  poster?: string
  /** 视频时长（秒） */
  duration?: number
  /** 标题 */
  title?: string
}

function fmt(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return ''
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')

/** 视频地址：owner 走 query 参数（<video src> 无法设自定义请求头）。 */
const videoUrl = (taskId: string) =>
  `${API_BASE}/task_video/${taskId}?owner=${encodeURIComponent(getOwnerId())}`

export default function NoteVideoPlayer({ taskId, poster, duration, title }: Props) {
  const [open, setOpen] = useState(false)
  // null = 探测中，true/false = 有无归档原片
  const [available, setAvailable] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)

  // 探测视频是否已归档
  // 用 Range GET 而不是 HEAD：后端若只挂了 GET，HEAD 会 405；
  // Range GET 只取 2 字节，代价和 HEAD 一样，但任何实现都能过。
  // 必须带 owner（裸 fetch 不走 axios 拦截器）：否则后端 owner 隔离判成
  // 「别人的笔记」返回 404，视频明明在也探测不到。
  const probe = () => {
    if (!taskId) {
      setAvailable(false)
      return () => {}
    }
    let cancelled = false
    fetch(videoUrl(taskId), {
      headers: { Range: 'bytes=0-1', 'X-VidInsight-Owner': getOwnerId() },
      cache: 'no-store',
    })
      .then(r => {
        if (!cancelled) setAvailable(r.ok)
      })
      .catch(() => {
        if (!cancelled) setAvailable(false)
      })
    return () => {
      cancelled = true
    }
  }

  useEffect(probe, [taskId])

  const handleSave = async () => {
    if (!taskId || saving) return
    setSaving(true)
    try {
      const r = await fetch(`${API_BASE}/note/save_video`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-VidInsight-Owner': getOwnerId(),
        },
        body: JSON.stringify({ task_id: taskId }),
      })
      const j = await r.json().catch(() => null)
      if (!r.ok || (j && j.code && j.code !== 200)) {
        toast.error(j?.msg || j?.message || '保存原片失败')
        setSaving(false)
        return
      }
      toast.success('正在后台下载原片，稍后刷新即可离线观看')
      // 下载 + faststart 需要几秒到几十秒，轮询探测直到出现
      let tries = 0
      const timer = setInterval(() => {
        tries += 1
        fetch(videoUrl(taskId), {
          headers: { Range: 'bytes=0-1', 'X-VidInsight-Owner': getOwnerId() },
          cache: 'no-store',
        })
          .then(r2 => {
            if (r2.ok) {
              clearInterval(timer)
              setAvailable(true)
              setSaving(false)
              toast.success('原片已保存到本地')
            } else if (tries >= 40) {
              clearInterval(timer)
              setSaving(false)
            }
          })
          .catch(() => {
            if (tries >= 40) {
              clearInterval(timer)
              setSaving(false)
            }
          })
      }, 3000)
    } catch (e: any) {
      toast.error(`保存原片失败：${e?.message || e}`)
      setSaving(false)
    }
  }

  if (!taskId) return null

  // 探测中：不占位（避免闪烁），但保存中要显示进度
  if (available === null && !saving) return null

  const src = videoUrl(taskId)

  // 没有归档原片：给一行「保存原片」，点了就地补下
  if (available === false) {
    return (
      <div className="my-4 overflow-hidden rounded-lg border border-dashed border-[var(--vi-hairline)] bg-[var(--vi-surface)]">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[var(--vi-hover)] disabled:cursor-default disabled:hover:bg-transparent"
        >
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[var(--vi-hairline)] text-[var(--vi-ink-2)]">
            {saving ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Download className="h-4 w-4" />
            )}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-[var(--vi-ink)]">
              {saving ? '正在保存原片…' : '保存原片'}
            </span>
            <span className="mt-0.5 block text-xs text-[var(--vi-ink-3)]">
              {saving ? '下载完成后即可离线观看' : '存到本地后，无网络也能回看这条视频'}
            </span>
          </span>
        </button>
      </div>
    )
  }

  return (
    <div className="my-4 overflow-hidden rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)]">
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="group relative flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[var(--vi-hover)]"
        >
          <span className="relative flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[var(--vi-ink)] text-[var(--vi-surface)] transition-transform group-hover:scale-105">
            <Play className="ml-0.5 h-4 w-4" fill="currentColor" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-[var(--vi-ink)]">
              播放原片
            </span>
            <span className="mt-0.5 block text-xs text-[var(--vi-ink-3)]">
              已归档到本地，离线可看
              {duration ? ` · ${fmt(duration)}` : ''}
            </span>
          </span>
          <span className="shrink-0 rounded bg-[var(--vi-hover)] px-1.5 py-0.5 text-[10px] text-[var(--vi-ink-2)]">
            离线
          </span>
        </button>
      ) : (
        <div className="relative bg-black">
          <video
            ref={videoRef}
            src={src}
            poster={poster}
            controls
            preload="metadata"
            playsInline
            title={title || '原片'}
            className="block max-h-[70vh] w-full bg-black"
          />
          <button
            type="button"
            onClick={() => {
              videoRef.current?.pause()
              setOpen(false)
            }}
            className="absolute right-2 top-2 rounded-md bg-black/55 px-2 py-1 text-xs text-white backdrop-blur-sm transition-colors hover:bg-black/75"
          >
            收起
          </button>
        </div>
      )}
    </div>
  )
}
