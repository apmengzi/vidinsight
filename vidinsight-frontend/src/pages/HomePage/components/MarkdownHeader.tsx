'use client'

import { useEffect, useState } from 'react'
import { Copy, Download, BrainCircuit, MessageSquare } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { videoPlatforms } from '@/constant/note'

interface VersionNote {
  ver_id: string
  model_name?: string
  style?: string
  created_at?: string
}

/** 封头/工具栏只读取这几个字段，保持与 Task 结构兼容 */
interface ReportTaskLike {
  id?: string
  markdown?: VersionNote[] | string
  audioMeta?: {
    title?: string
    platform?: string
    video_id?: string
    duration?: number
    raw_info?: any
  }
  formData?: {
    video_url?: string
    model_name?: string
    platform?: string
  }
  createdAt?: string
}

interface NoteHeaderProps {
  currentTask?: ReportTaskLike | null
  isMultiVersion: boolean
  currentVerId: string
  setCurrentVerId: (id: string) => void
  modelName: string
  style: string
  noteStyles: readonly { value: string; label: string }[]
  onCopy: () => void
  onDownload: () => void
  createAt?: string | Date
  showTranscribe: boolean
  setShowTranscribe: (show: boolean) => void
  showChat?: false | 'half' | 'full'
  setShowChat?: (mode: false | 'half' | 'full') => void
  viewMode: 'map' | 'preview'
  setViewMode: (mode: 'map' | 'preview') => void
}

/* ═══════════════════════════════════════════════════
   体验面③ · 报告封头（Masthead）
   衬线大标题 + 下方 mono 元数据阵列。
   只渲染真实存在的字段：没有数据就不出该列，绝不造值。
   ═══════════════════════════════════════════════════ */

export interface ReportMastheadProps {
  title?: string
  platform?: string
  videoId?: string
  durationSeconds?: number
  modelName?: string
  styleLabel?: string
  createdAt?: string | Date
  versionLabel?: string
  taskId?: string
}

const serifStyle = { fontFamily: '"Noto Serif SC", "Source Han Serif SC", serif' }

function formatDuration(totalSeconds?: number): string {
  if (!totalSeconds || totalSeconds <= 0) return ''
  const s = Math.floor(totalSeconds)
  const hh = Math.floor(s / 3600)
  const mm = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`
}

function formatMastheadDate(date?: string | Date): string {
  if (!date) return ''
  const d = typeof date === 'string' ? new Date(date) : date
  if (isNaN(d.getTime())) return ''
  return d
    .toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    .replace(/\//g, '-')
}

/** 元数据阵列中的单列：mono 小标 + 正文值 */
function MetaCell({ label, value }: { label: string; value?: string }) {
  if (!value) return null
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="tc text-[9px] uppercase tracking-[0.22em] text-[var(--vi-ink-3)]">
        {label}
      </span>
      <span className="tc max-w-[220px] truncate text-[11px] text-[var(--vi-ink-2)]" title={value}>
        {value}
      </span>
    </span>
  )
}

function MetaDivider() {
  return <span aria-hidden className="h-2.5 w-px shrink-0 bg-[var(--vi-hairline)]" />
}

export function ReportMasthead({
  title,
  platform,
  videoId,
  durationSeconds,
  modelName,
  styleLabel,
  createdAt,
  versionLabel,
  taskId,
}: ReportMastheadProps) {
  const platformLabel =
    videoPlatforms.find(p => p.value === platform)?.label || platform || ''
  const sourceLabel = [platformLabel, videoId].filter(Boolean).join(' · ')
  const durationLabel = formatDuration(durationSeconds)
  const dateLabel = formatMastheadDate(createdAt)

  const metaItems: { label: string; value?: string }[] = [
    { label: 'SOURCE', value: sourceLabel },
    { label: 'MODEL', value: modelName },
    { label: 'STYLE', value: styleLabel },
    { label: 'DURATION', value: durationLabel },
    { label: 'DATE', value: dateLabel },
    { label: 'VER', value: versionLabel },
  ].filter(item => !!item.value)

  if (!title && metaItems.length === 0) return null

  return (
    <header className="mb-6 border-b border-[var(--vi-hairline)] pb-5">
      {/* 刊头小标：mono 机构行 */}
      <div className="flex items-center gap-2">
        <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-rec" />
        <span className="tc text-[9px] uppercase tracking-[0.3em] text-[var(--vi-ink-3)]">
          VIDINSIGHT · 内容洞察简报
        </span>
        <span aria-hidden className="h-px flex-1 bg-[var(--vi-hairline)]" />
        {taskId && (
          <span className="tc shrink-0 text-[9px] uppercase tracking-[0.16em] text-[var(--vi-ink-3)]">
            NO.{taskId.slice(-8)}
          </span>
        )}
      </div>

      {/* 衬线大标题 */}
      {title && (
        <h1
          className="mt-3 text-[32px] font-semibold leading-[1.22] tracking-[-0.01em] text-[var(--vi-ink)]"
          style={serifStyle}
        >
          {title}
        </h1>
      )}

      {/* mono 元数据阵列 */}
      {metaItems.length > 0 && (
        <div className="mt-3.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          {metaItems.map((item, idx) => (
            <span key={item.label} className="inline-flex items-center gap-3">
              {idx > 0 && <MetaDivider />}
              <MetaCell label={item.label} value={item.value} />
            </span>
          ))}
        </div>
      )}
    </header>
  )
}

/* ═══════════════════════════════════════════════════
   阅读区顶部工具栏（版本切换 / 视图切换 / 导出）
   交互与数据流保持原样，仅统一为剪辑室视觉语言。
   ═══════════════════════════════════════════════════ */

export function MarkdownHeader({
  currentTask,
  isMultiVersion,
  currentVerId,
  setCurrentVerId,
  modelName,
  style,
  noteStyles,
  onCopy,
  onDownload,
  createAt,
  showTranscribe,
  setShowTranscribe,
  showChat,
  setShowChat,
  viewMode,
  setViewMode,
}: NoteHeaderProps) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let timer: NodeJS.Timeout
    if (copied) {
      timer = setTimeout(() => setCopied(false), 2000)
    }
    return () => clearTimeout(timer)
  }, [copied])

  const handleCopy = () => {
    onCopy()
    setCopied(true)
  }

  const styleName = noteStyles.find(v => v.value === style)?.label || style

  const versions: VersionNote[] = Array.isArray(currentTask?.markdown)
    ? currentTask.markdown
    : []

  const formatDate = (date: string | Date | undefined) => {
    if (!date) return ''
    const d = typeof date === 'string' ? new Date(date) : date
    if (isNaN(d.getTime())) return ''
    return d
      .toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
      .replace(/\//g, '-')
  }

  return (
    <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--vi-hairline)] bg-[var(--vi-surface)]/95 px-4 py-2 backdrop-blur-sm">
      {/* 左侧区域：版本 + 标签 + 创建时间 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {isMultiVersion && (
          <Select value={currentVerId} onValueChange={setCurrentVerId}>
            <SelectTrigger className="h-7 w-[132px] border-[var(--vi-hairline)] text-xs">
              <div className="tc flex items-center text-[11px] text-[var(--vi-ink-2)]">
                {(() => {
                  const idx = versions.findIndex(v => v.ver_id === currentVerId)
                  return idx !== -1 ? `版本（${currentVerId.slice(-6)}）` : ''
                })()}
              </div>
            </SelectTrigger>

            <SelectContent>
              {versions.map(v => {
                const shortId = v.ver_id.slice(-6)
                return (
                  <SelectItem key={v.ver_id} value={v.ver_id}>
                    {`版本（${shortId}）`}
                  </SelectItem>
                )
              })}
            </SelectContent>
          </Select>
        )}

        <span className="inline-flex items-baseline gap-1.5">
          <span className="tc text-[9px] uppercase tracking-[0.22em] text-[var(--vi-ink-3)]">
            MODEL
          </span>
          <span className="tc max-w-[200px] truncate text-[11px] text-[var(--vi-ink-2)]" title={modelName}>
            {modelName || '—'}
          </span>
        </span>

        <span aria-hidden className="h-2.5 w-px bg-[var(--vi-hairline)]" />

        <span className="inline-flex items-baseline gap-1.5">
          <span className="tc text-[9px] uppercase tracking-[0.22em] text-[var(--vi-ink-3)]">
            STYLE
          </span>
          <span className="tc text-[11px] text-[var(--vi-ink-2)]">{styleName || '—'}</span>
        </span>

        {createAt && (
          <>
            <span aria-hidden className="h-2.5 w-px bg-[var(--vi-hairline)]" />
            <span className="tc text-[10px] text-[var(--vi-ink-3)]">{formatDate(createAt)}</span>
          </>
        )}
      </div>

      {/* 右侧操作按钮 */}
      <div className="flex items-center gap-1">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                onClick={() => {
                  setViewMode(viewMode == 'preview' ? 'map' : 'preview')
                }}
                variant="ghost"
                size="sm"
                className="h-8 px-2"
              >
                <BrainCircuit className="mr-1.5 h-4 w-4" />
                <span className="text-sm">{viewMode == 'preview' ? '思维导图' : 'markdown'}</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>思维导图</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button onClick={handleCopy} variant="ghost" size="sm" className="h-8 px-2">
                <Copy className="mr-1.5 h-4 w-4" />
                <span className="text-sm">{copied ? '已复制' : '复制'}</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>复制内容</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button onClick={onDownload} variant="ghost" size="sm" className="h-8 px-2">
                <Download className="mr-1.5 h-4 w-4" />
                <span className="text-sm">导出分析报告</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>下载为 Markdown 分析报告</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                onClick={() => {
                  setShowTranscribe(!showTranscribe)
                }}
                variant="ghost"
                size="sm"
                className="h-8 px-2"
              >
                <span className="text-sm">原文参照</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>原文参照</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        {setShowChat && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  onClick={() => setShowChat(showChat ? false : 'half')}
                  variant={showChat ? 'default' : 'ghost'}
                  size="sm"
                  className="h-8 px-2"
                >
                  <MessageSquare className="mr-1.5 h-4 w-4" />
                  <span className="text-sm">洞察问答</span>
                </Button>
              </TooltipTrigger>
              <TooltipContent>基于竞品内容的洞察问答</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>
    </div>
  )
}
