import { useCallback, useEffect, useRef, useState } from 'react'
import { Maximize2, Minus, MessageSquare, Trash2, X } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import ChatPanel from './ChatPanel'

/* ═══════════════════════════════════════════════════
   洞察问答悬浮窗（P0-2 方案 B，2026-11-05）
   为什么改：旧版 half 模式是 w-1/2 的并排分栏，窗口变窄后
   笔记正文被压到每行只有几个字，"边看边问"变成"没法看"。
   悬浮窗能同时保住正文宽度（max-w-[860px] 不动）和对话可用性：
   - 默认右下角 380px × 60vh，标题栏可拖动（位置持久化）
   - 右下角手柄可缩放（宽高持久化）
   - 最小化收成圆形气泡，点击恢复
   - 关闭回到普通模式
   视觉按 DESIGN.md 浮层规范：surface 白卡 + hairline 边 +
   极浅投影 0 8px 24px rgba(23,25,30,.10)，全部走 --vi-* token。
   ═══════════════════════════════════════════════════ */

const STORAGE_KEY = 'vidinsight-chat-float-geometry'
const DEFAULT_W = 380
const MIN_W = 320
const MIN_H = 280

interface Geometry {
  x: number
  y: number
  w: number
  h: number
}

/** 默认位置：右下角留 24px 呼吸边 */
function defaultGeometry(): Geometry {
  const w = DEFAULT_W
  const h = Math.round(window.innerHeight * 0.6)
  return {
    x: Math.max(8, window.innerWidth - w - 24),
    y: Math.max(8, window.innerHeight - h - 24),
    w,
    h,
  }
}

/** 把窗口几何夹回视口内：恢复持久化位置时窗口尺寸/视口可能已变化 */
function clampToViewport(g: Geometry): Geometry {
  const w = Math.min(Math.max(MIN_W, g.w), Math.max(MIN_W, window.innerWidth - 16))
  const h = Math.min(Math.max(MIN_H, g.h), Math.max(MIN_H, window.innerHeight - 16))
  return {
    w,
    h,
    x: Math.min(Math.max(8, g.x), Math.max(8, window.innerWidth - w - 8)),
    y: Math.min(Math.max(8, g.y), Math.max(8, window.innerHeight - h - 8)),
  }
}

function loadGeometry(): Geometry {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return clampToViewport(JSON.parse(raw) as Geometry)
  } catch {
    /* 损坏数据走默认 */
  }
  return defaultGeometry()
}

interface ChatFloatingWindowProps {
  taskId: string
  /** 切换为全屏问答（交给 MarkdownViewer 的 setShowChat('full')） */
  onExpand: () => void
  /** 关闭悬浮窗，回到普通阅读模式 */
  onClose: () => void
  /** ChatPanel 的模式切换（类型与 MarkdownViewer 的 setShowChat 对齐） */
  onModeChange: (mode: 'half' | 'full' | false) => void
}

export default function ChatFloatingWindow({
  taskId,
  onExpand,
  onClose,
  onModeChange,
}: ChatFloatingWindowProps) {
  const [geo, setGeo] = useState<Geometry>(() =>
    typeof window === 'undefined' ? { x: 24, y: 24, w: DEFAULT_W, h: 480 } : loadGeometry(),
  )
  const [minimized, setMinimized] = useState(false)
  const [dragging, setDragging] = useState(false)

  // 最新几何的 ref：pointerup 落盘时避免读到渲染闭包里的旧 state
  const geoRef = useRef(geo)
  geoRef.current = geo

  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null)
  const resizeRef = useRef<{ startX: number; startY: number; baseW: number; baseH: number } | null>(null)

  const chatMessages = useChatStore(s => s.chatHistory[taskId])
  const clearChat = useChatStore(s => s.clearChat)

  const persist = useCallback((g: Geometry) => {
    // 位置/尺寸持久化：刷新后窗口还在用户调好的地方（P0-2 验收点）
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(g))
    } catch {
      /* 存储不可用则仅内存态 */
    }
  }, [])

  // 视口变化（如窗口缩放）时把悬浮窗夹回可见区
  useEffect(() => {
    const onResize = () => setGeo(g => clampToViewport(g))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // ===== 标题栏拖动：pointer capture + 视口内 clamp =====
  const onHeaderPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // 点在标题栏按钮上时不触发拖动（按钮有自己的 click）
    if ((e.target as HTMLElement).closest('button')) return
    dragRef.current = { startX: e.clientX, startY: e.clientY, baseX: geo.x, baseY: geo.y }
    setDragging(true)
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const onHeaderPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current
    if (!d) return
    const next = clampToViewport({
      ...geoRef.current,
      x: d.baseX + e.clientX - d.startX,
      y: d.baseY + e.clientY - d.startY,
    })
    setGeo(g => (g.x === next.x && g.y === next.y ? g : { ...g, x: next.x, y: next.y }))
  }

  const endDrag = () => {
    if (dragRef.current) {
      dragRef.current = null
      persist(geoRef.current)
    }
    if (resizeRef.current) {
      resizeRef.current = null
      persist(geoRef.current)
    }
    setDragging(false)
  }

  // ===== 右下角缩放手柄 =====
  const onResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    resizeRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      baseW: geoRef.current.w,
      baseH: geoRef.current.h,
    }
    setDragging(true)
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const onResizePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = resizeRef.current
    if (!r) return
    const next = clampToViewport({
      ...geoRef.current,
      w: r.baseW + e.clientX - r.startX,
      h: r.baseH + e.clientY - r.startY,
    })
    setGeo(g =>
      g.w === next.w && g.h === next.h ? g : { ...g, w: next.w, h: next.h },
    )
  }

  // ===== 最小化态：右下角圆形气泡，点击恢复 =====
  if (minimized) {
    return (
      <button
        onClick={() => setMinimized(false)}
        className="fixed bottom-6 right-6 z-50 flex h-12 w-12 items-center justify-center rounded-full border border-[var(--vi-hairline)] bg-[var(--vi-surface)] shadow-[0_8px_24px_rgba(23,25,30,0.18)] transition-transform hover:scale-105"
        title="展开洞察问答"
        aria-label="展开洞察问答"
      >
        <MessageSquare className="h-5 w-5 text-[var(--vi-ink-2)]" />
        {/* REC 红点：提示有一段进行中的对话挂在这里 */}
        <span aria-hidden className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-rec" />
      </button>
    )
  }

  return (
    <section
      style={{ left: geo.x, top: geo.y, width: geo.w, height: geo.h }}
      className={
        'fixed z-50 flex flex-col overflow-hidden rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] shadow-[0_12px_32px_rgba(23,25,30,0.16)] ' +
        // 拖动/缩放过程中关掉过渡，否则窗口滞后跟手；松手后恢复
        (dragging ? '' : 'transition-[width,height]')
      }
      role="dialog"
      aria-label="洞察问答悬浮窗"
    >
      {/* 标题栏 = 拖动把手（按钮区已排除拖动触发） */}
      <div
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        className={
          'flex shrink-0 select-none items-center gap-2 border-b border-[var(--vi-hairline)] px-3 py-1.5 ' +
          (dragging ? 'cursor-grabbing' : 'cursor-grab')
        }
      >
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-rec" />
        <span className="text-sm font-medium text-[var(--vi-ink)]">洞察问答</span>
        <span className="tc text-[10px] uppercase tracking-[0.14em] text-[var(--vi-ink-3)]">
          FLOAT
        </span>

        <div className="ml-auto flex items-center gap-0.5">
          {/* 三个圆形操作键：h-8 w-8 = 32px 点击区（全局按钮口径） */}
          <button
            onClick={onExpand}
            className="flex h-8 w-8 items-center justify-center rounded-full text-[var(--vi-ink-3)] transition-colors hover:bg-[var(--vi-hover)] hover:text-[var(--vi-ink)]"
            title="切换为全屏"
            aria-label="切换为全屏"
          >
            <Maximize2 className="h-4 w-4" />
          </button>
          {(chatMessages?.length ?? 0) > 0 && (
            <button
              onClick={() => clearChat(taskId)}
              className="flex h-8 w-8 items-center justify-center rounded-full text-[var(--vi-ink-3)] transition-colors hover:bg-[var(--vi-hover)] hover:text-rec"
              title="清空对话"
              aria-label="清空对话"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          )}
          <button
            onClick={() => setMinimized(true)}
            className="flex h-8 w-8 items-center justify-center rounded-full text-[var(--vi-ink-3)] transition-colors hover:bg-[var(--vi-hover)] hover:text-[var(--vi-ink)]"
            title="最小化为气泡"
            aria-label="最小化为气泡"
          >
            <Minus className="h-4 w-4" />
          </button>
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-full text-[var(--vi-ink-3)] transition-colors hover:bg-[var(--vi-hover)] hover:text-[var(--vi-ink)]"
            title="关闭（回到普通模式）"
            aria-label="关闭悬浮窗"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 对话面板：hideHeader 避免双标题栏；外壳已带边框，去掉 border-l */}
      <div className="min-h-0 flex-1 overflow-hidden">
        <ChatPanel taskId={taskId} mode="half" hideHeader onModeChange={onModeChange} />
      </div>

      {/* 右下角缩放手柄：斜纹指示可拖 */}
      <div
        onPointerDown={onResizePointerDown}
        onPointerMove={onResizePointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        className="absolute bottom-0 right-0 flex h-4 w-4 cursor-nwse-resize items-end justify-end"
        title="拖动调整大小"
        aria-hidden
      >
        <svg viewBox="0 0 16 16" className="h-3 w-3 text-[var(--vi-ink-3)]">
          <path d="M14 6 L6 14 M14 10 L10 14" stroke="currentColor" strokeWidth="1.5" fill="none" />
        </svg>
      </div>
    </section>
  )
}
