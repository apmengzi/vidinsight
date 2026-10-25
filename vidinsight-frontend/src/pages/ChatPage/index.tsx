import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { Bubble, Sender } from '@ant-design/x'
import { useParams, useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import {
  UserRound, Bot, Loader2, Sparkles, CheckCircle2, XCircle,
  ExternalLink, BookOpen, Brain, Zap,
} from 'lucide-react'
import { useConversationStore, type Conversation, type ConversationTaskProgress } from '@/store/conversationStore'
import { useTaskStore } from '@/store/taskStore'
import { useModelStore } from '@/store/modelStore'
import { useProviderStore } from '@/store/providerStore'
import { cancel_task, expandCollection, fetchXhsCollectionViaWebview, generateNote, get_task_status } from '@/services/note'
import { askAgent, askHarness, askHarnessStream, routeIntent, type ChatSource, type KbStats, type RouteIntentResponse } from '@/services/chat'
import StepBar from '@/pages/HomePage/components/StepBar.tsx'
import AgentTracePanel from '@/pages/ChatPage/components/AgentTracePanel.tsx'
import { preprocessLatex } from '@/utils/latex-preprocessor'
import toast from 'react-hot-toast'

// 批量粘贴兼容：逗号（半/全角）、顿号、分号、空白、换行都是分隔符；
// URL 字符类里排除这些分隔符，避免把多条链接粘成一条脏 URL
const URL_REGEX = /(https?:\/\/[^\s，,、；;。！？（）()【】<>"]+[\w/=&?%.\-~#:])/g

// ====== 平台检测 ======
function detectPlatform(url: string): string {
  if (/bilibili\.com/i.test(url)) return 'bilibili'
  if (/youtube\.com|youtu\.be/i.test(url)) return 'youtube'
  if (/douyin\.com/i.test(url)) return 'douyin'
  if (/kuaishou\.com/i.test(url)) return 'kuaishou'
  if (/xiaohongshu\.com|xhslink\.(?:com|cn)/i.test(url)) return 'xiaohongshu'
  if (/ixigua\.com/i.test(url)) return 'xigua'
  return 'douyin'
}

// 状态行展示用平台名（纯展示映射，不参与业务判定）
const PLATFORM_LABEL: Record<string, string> = {
  bilibili: 'Bilibili',
  youtube: 'YouTube',
  douyin: 'Douyin',
  kuaishou: 'Kuaishou',
  xiaohongshu: 'XHS',
  xigua: 'Xigua',
}

// 小红书合集/收藏夹链接：需要先展开成逐条笔记再批量提交
const isXhsCollection = (url: string) => /xiaohongshu\.com\/collection\/item\//i.test(url)

// 各平台合集链接形态（合集解析的统一入口判定）
const isYoutubePlaylist = (url: string) => /youtube(-nocookie)?\.com\/playlist\?|music\.youtube\.com\/playlist\?/i.test(url)
const isBilibiliCollection = (url: string) => /bilibili\.com\/lists?\/|bilibili\.com\/medialist|space\.bilibili\.com\/\d+\/channel\/collectiondetail|b23\.tv.*medialist/i.test(url)
const isCollectionLink = (url: string) => isXhsCollection(url) || isYoutubePlaylist(url) || isBilibiliCollection(url)

const detectCollectionPlatform = (url: string) =>
  isXhsCollection(url) ? 'xiaohongshu'
  : isYoutubePlaylist(url) ? 'youtube'
  : isBilibiliCollection(url) ? 'bilibili'
  : /douyin\.com/i.test(url) ? 'douyin'
  : /kuaishou\.com/i.test(url) ? 'kuaishou'
  : 'unknown'

function extractVideoLinks(text: string): string[] {
  const matches = text.match(URL_REGEX)
  if (!matches) return []
  const cleaned = matches
    .map(url => url.trim().replace(/[.,;；、]+$/g, ''))
    .filter(url =>
      /bilibili\.com|youtube\.com|youtu\.be|douyin\.com|kuaishou\.com|xiaohongshu\.com|xhslink\.(?:com|cn)|ixigua\.com/i.test(url),
    )
  return Array.from(new Set(cleaned))
}

// ====== 生成步骤 ======
const STEPS = [
  { label: '解析链接', key: 'PARSING' },
  { label: '下载音频', key: 'DOWNLOADING' },
  { label: '转写文字', key: 'TRANSCRIBING' },
  { label: '总结内容', key: 'SUMMARIZING' },
  { label: '保存完成', key: 'SUCCESS' },
]

// ====== 默认生成配置 ======
const AUTO_GEN_DEFAULTS = {
  style: 'adaptive',
  quality: 'medium',
  video_understand: false,
  format: ['link', 'summary'] as string[],
  extras: '请按内容洞察口径输出：视频主题、核心观点、关键信息与可复用的知识点。报告标题必须使用“# 内容洞察报告：视频标题”，如果原始标题缺失，请根据链接平台或视频内容生成一个简短标题。不要手写目录，前端会统一生成一个简洁目录。',
  grid_size: [3, 3],
}

type BatchTaskProgress = {
  taskId: string
  /** 提交时刻的会话快照：轮询靠它定位目标会话（见 startPolling 闭包修复） */
  conversationId?: string
  status: string
  title?: string
  url: string
  platform: string
  index: number
  /** 当前阶段百分比（0-100 或 null；仅进行中阶段有值） */
  progress?: number
  stage_progress?: number | null
  /** EVT Must Fix：任务已用时长（秒），后端 task_status 透出 */
  elapsed_s?: number
  /** EVT Must Fix：各阶段耗时（秒），完成后展示明细 */
  stage_times?: Record<string, number>
  /** 失败原因（后端分类后的中文说明，如 B 站风控/视频下架/链接不完整） */
  failMsg?: string
  /** 后端阶段提示（如「正在加载转写模型」），进度卡副行展示 */
  message?: string
}

function getFallbackTaskTitle(url: string, index: number): string {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '')
    return `第 ${index + 1} 条视频 · ${host}`
  } catch {
    return `第 ${index + 1} 条视频`
  }
}

// ====== 来源引用（轻量内联样式） ======
function InlineSources({ sources }: { sources: ChatSource[] }) {
  const [expanded, setExpanded] = useState(false)
  if (sources.length === 0) return null

  return (
    <div className="mt-4 border-t border-[var(--vi-hairline)] pt-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-xs text-[var(--vi-ink-3)] transition-colors hover:text-[var(--vi-ink-2)]"
      >
        <BookOpen className="h-3 w-3" />
        <span>参考 {sources.length} 条分析记录</span>
        <span className="text-[10px]">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="mt-2 space-y-1.5">
          {sources.map((src, i) => {
            const icon = { meta: '📋', markdown: '📝', transcript: '🎙' }[src.source_type] || '📄'
            const label = { meta: '信息', markdown: '分析', transcript: '原文' }[src.source_type] || ''
            const detail = src.section_title
              || (src.start_time !== undefined
                ? `${Math.floor((src.start_time || 0) / 60)}:${String(Math.floor((src.start_time || 0) % 60)).padStart(2, '0')}`
                : '')

            return (
              <div key={i} className="flex items-start gap-2 rounded-[1px] border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] px-3 py-2 text-xs">
                <span className="mt-0.5 shrink-0">{icon}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="tc font-medium text-[var(--vi-ink-2)]">[{i + 1}]</span>
                    <span className="truncate text-[var(--vi-ink)]">{src.video_title}</span>
                    {label && (
                      <span className="shrink-0 text-[10px] text-[var(--vi-ink-3)]">· {label}</span>
                    )}
                    {detail && (
                      <span className="tc shrink-0 text-[10px] text-[var(--vi-ink-3)]">· {detail}</span>
                    )}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-[var(--vi-ink-3)]">{src.text}</p>
                </div>
                {src.video_url && (
                  <a href={src.video_url} target="_blank" rel="noopener noreferrer"
                     className="shrink-0 p-1 text-[var(--vi-ink-3)] transition-colors hover:text-[var(--vi-ink)]">
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ====== Agent 思考过程（折叠） ======
function AgentThinking({ toolCalls, thinkingText, streaming, liveToolEvents }: {
  toolCalls: {
    tool: string
    arguments?: Record<string, unknown>
    args?: Record<string, unknown>
    round?: number
    restricted?: boolean
    elapsed_s?: number
    ok?: boolean
  }[]
  /** 本轮流式思考链（reasoning 增量拼接），回答完成后保留可回看 */
  thinkingText?: string
  /** 流式进行中：面板展开 + 呼吸指示 */
  streaming?: boolean
  /** 流式工具完成事件（与 toolCalls 二选一来源，流式优先展示） */
  liveToolEvents?: { tool: string; ok: boolean; restricted: boolean; elapsed_s: number }[]
}) {
  const hasContent = toolCalls.length > 0 || !!thinkingText || (liveToolEvents?.length ?? 0) > 0
  const [expanded, setExpanded] = useState(true)
  // 流式开始时自动展开；内容为空不渲染
  useEffect(() => { if (streaming) setExpanded(true) }, [streaming])
  if (!hasContent) return null

  const liveEvents = liveToolEvents ?? []
  const stepCount = liveEvents.length || toolCalls.length

  const labelMap: Record<string, string> = {
    // consume
    search_kb: '搜索洞察库',
    get_source: '获取来源',
    list_notes: '列出分析记录',
    clarify: '澄清问题',
    // produce
    validate_video_url: '校验链接',
    list_supported_platforms: '平台列表',
    generate_note: '生成笔记',
    ingest_board: '摄入收藏专辑',
  }

  // 轨道 Clip：工具调用 → 视频轨上的剪辑块 [TOOL_NAME 0.8s]
  // 结束=灰白 mono；失败仅用 Amber ✕ 标记（rec 红只保留给「运行中」录制指示）
  const renderClip = (key: string, tool: string, ok: boolean, elapsed?: number, extra?: string) => (
    <span
      key={key}
      className="tc inline-flex items-center gap-1.5 rounded-[1px] border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] px-2 py-[3px] text-[11px] leading-none text-[var(--vi-ink-2)]"
      title={labelMap[tool] || tool}
    >
      <span className="text-[var(--vi-ink-3)]">[</span>
      <span className="tracking-[0.04em]">{(tool || 'tool').toUpperCase()}</span>
      {typeof elapsed === 'number' && elapsed > 0 && (
        <span className="tabular-nums opacity-70">{elapsed.toFixed(1)}s</span>
      )}
      <span className="text-[var(--vi-ink-3)]">]</span>
      {extra && <span className="max-w-[180px] truncate opacity-60">{extra}</span>}
      {!ok && <span aria-hidden="true" className="text-amber-600">✕</span>}
    </span>
  )

  return (
    <div className="mb-3 ml-6 max-w-[85%]">
      {/* 监视器微面板：深色底 + 1px 发丝边 + mono 机器声部 */}
      <div className="overflow-hidden rounded-[2px] border border-[var(--vi-hairline)] bg-[var(--vi-surface)]">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-[var(--vi-hover)]"
        >
          <Brain className="h-3 w-3 shrink-0 text-[var(--vi-ink-3)]" />
          <span className="tc text-[11px] uppercase tracking-[0.14em] text-[var(--vi-ink-2)]">
            {streaming ? 'TRACE · LIVE' : 'TRACE'}
          </span>
          {streaming && (
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-rec"
              style={{ animationDuration: '600ms' }}
            />
          )}
          <span className="tc ml-auto shrink-0 text-[11px] tabular-nums text-[var(--vi-ink-3)]">
            {stepCount > 0 ? `${String(stepCount).padStart(2, '0')} STEP` : ''}
          </span>
          <span className="tc shrink-0 text-[10px] text-[var(--vi-ink-3)]">{expanded ? '▲' : '▼'}</span>
        </button>

        {expanded && (
          <div className="border-t border-[var(--vi-hairline)] px-3 py-2.5">
            {/* 轨道条：Clip 块按调用顺序排布 */}
            <div className="flex flex-wrap items-center gap-1.5">
              {liveEvents.map((tc, i) => {
                const args: any = {}
                const query = args?.query ?? args?.url ?? args?.board_url
                const queryStr = typeof query === 'string' ? query : ''
                return renderClip(`live-${i}`, tc.tool, tc.ok, tc.elapsed_s, queryStr.slice(0, 20))
              })}
              {!liveEvents.length && toolCalls.map((tc, i) => {
                // 兼容两种轨迹：旧 Agent 用 arguments，Harness 用 args
                const args: any = tc.arguments ?? tc.args ?? {}
                const query = args?.query ?? args?.url ?? args?.board_url
                const queryStr = typeof query === 'string' ? query : ''
                return renderClip(
                  `hist-${i}`,
                  tc.tool,
                  tc.ok !== false,
                  tc.elapsed_s,
                  queryStr ? queryStr.slice(0, 20) + (queryStr.length > 20 ? '…' : '') : undefined,
                )
              })}
              {/* 运行中的走带指示：仅流式进行时出现，不伪造耗时 */}
              {streaming && (
                <span className="tc inline-flex items-center gap-1.5 rounded-[1px] border border-rec/30 px-2 py-[3px] text-[11px] leading-none text-rec/80">
                  <span
                    aria-hidden="true"
                    className="h-1 w-1 animate-pulse rounded-full bg-rec"
                    style={{ animationDuration: '600ms' }}
                  />
                  RUN
                </span>
              )}
            </div>

            {/* 受限工具提示：产品红线在 UI 上可见 */}
            {(liveEvents.some(t => t.restricted) || toolCalls.some(t => t.restricted)) && (
              <div className="tc mt-2 w-full text-[10px] leading-relaxed text-amber-600">
                ⚠ 本轮含受限工具：仅处理你自己的内容，不自动登录、不爬取他人内容。
              </div>
            )}

            {/* 思考链正文：模型 reasoning 增量实时滚动，完成后保留回看 */}
            {thinkingText && (
              <div
                className="tc mt-2 w-full whitespace-pre-wrap rounded-[1px] border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] p-2 text-[11px] leading-[1.7] text-[var(--vi-ink-2)]"
                style={{ maxHeight: 180, overflowY: 'auto' }}
              >
                {thinkingText}
                {streaming && (
                  <span
                    className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-rec align-middle"
                    style={{ animationDuration: '600ms' }}
                  />
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ====== 模型选择器（ZCode 同款交互）======
function ModelPicker({
  modelList,
  value,
  onChange,
}: {
  modelList: { id: string; provider_id: string; model_name: string }[]
  value: { provider_id: string; model_name: string; label: string } | null
  onChange: (m: { provider_id: string; model_name: string; label: string } | null) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  // 按 provider 分组（保持后端返回顺序）
  const groups = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const m of modelList) {
      if (!map.has(m.provider_id)) map.set(m.provider_id, [])
      map.get(m.provider_id)!.push(m.model_name)
    }
    return Array.from(map.entries())
  }, [modelList])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={modelList.length === 0}
        className="max-w-[420px] truncate rounded-md bg-neutral-100 px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-200 disabled:opacity-50"
        title={modelList.length === 0 ? '未配置可用模型：请到「全局配置」添加供应商' : '切换供应商 / 模型'}
      >
        {value ? value.label : '未选择模型'} ▾
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-2 max-h-80 w-72 overflow-y-auto rounded-lg border border-neutral-200 bg-white p-1 shadow-lg">
          {groups.length === 0 && (
            <div className="px-3 py-2 text-xs text-neutral-400">暂无可用模型</div>
          )}
          {groups.map(([pid, models]) => (
            <div key={pid} className="mb-1">
              <div className="px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                {pid}
              </div>
              {models.map(mn => {
                const active = value?.provider_id === pid && value?.model_name === mn
                return (
                  <button
                    key={`${pid}/${mn}`}
                    type="button"
                    onClick={() => {
                      onChange({ provider_id: pid, model_name: mn, label: `${pid}/${mn}` })
                      setOpen(false)
                    }}
                    className={[
                      'flex w-full items-center justify-between rounded px-3 py-1.5 text-left text-xs',
                      active ? 'bg-blue-50 text-blue-600' : 'text-neutral-700 hover:bg-neutral-100',
                    ].join(' ')}
                  >
                    <span className="truncate">{mn}</span>
                    {active && <span>✓</span>}
                  </button>
                )
              })}
            </div>
          ))}
          {value && (
            <button
              type="button"
              onClick={() => { onChange(null); setOpen(false) }}
              className="mt-1 w-full border-t border-neutral-100 px-3 py-1.5 text-left text-[11px] text-neutral-400 hover:text-neutral-600"
            >
              恢复默认模型
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function getStageProgressLabel(status: string, pct?: number): string {
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return ''
  const rounded = Math.round(pct)
  switch (status) {
    case 'DOWNLOADING':
      return `下载 ${rounded}%`
    case 'TRANSCRIBING':
      return `转写 ${rounded}%`
    case 'SUMMARIZING':
      return `总结 ${rounded}%`
    default:
      return `${rounded}%`
  }
}

// 阶段名称标准映射（展示用，把英文阶段映射到中文）
const STAGE_NAME_MAP: Record<string, string> = {
  PARSING: '解析',
  DOWNLOADING: '下载',
  TRANSCRIBING: '转写',
  SUMMARIZING: 'LLM总结',
  FORMATTING: '格式化',
  SAVING: '存储',
}

// ====== 进度卡片 ======
function TaskProgressCard({
  taskStatus, taskTitle, taskId, onViewDetail, onStop, progress, failMsg,
  elapsed_s, stage_times, message,
}: {
  taskStatus: string; taskTitle?: string; taskId: string
  onViewDetail: () => void; onStop: (taskId: string) => void
  /** 当前阶段百分比（各阶段由后端进度回调透出，0-100） */
  progress?: number
  /** 失败原因（后端分类后的中文说明） */
  failMsg?: string
  /** 已用时长（秒） */
  elapsed_s?: number
  /** 各阶段耗时（秒） */
  stage_times?: Record<string, number>
  /** 后端阶段提示（如转写模型加载中） */
  message?: string
}) {
  if (taskStatus === 'CANCELLED') {
    return (
      <div className="my-3 rounded-xl border border-zinc-200 bg-zinc-50/80 p-4">
        <div className="flex items-center gap-2">
          <XCircle className="h-5 w-5 text-zinc-400" />
          <span className="font-medium text-zinc-600">任务已停止</span>
        </div>
        <p className="mt-1 text-sm text-zinc-500">该任务已按你的要求取消，可重新提交链接再次生成</p>
      </div>
    )
  }
  if (taskStatus === 'SUCCESS') {
    return (
      <div className="my-3 rounded-xl border border-teal-ok/25 bg-teal-ok/5 p-4">
        <div className="flex items-center gap-2">
          <CheckCircle2 className="h-5 w-5 text-teal-ok" />
          <span className="font-medium text-teal-ok">内容洞察生成完成</span>
          <span className="tc ml-auto text-[11px] text-ink-3">DONE</span>
        </div>
        {taskTitle && <p className="mt-1 truncate text-sm text-ink-2">{taskTitle}</p>}
        {/* EVT Must Fix：完成后的阶段耗时明细（用户「黑盒完成」不可信痛点，含 LLM 阶段耗时） */}
        {elapsed_s != null && elapsed_s > 0 && (
          <p className="tc mt-2 text-[11px] text-ink-3">
            全程 {elapsed_s >= 60 ? `${Math.floor(elapsed_s / 60)}m${Math.round(elapsed_s % 60)}s` : `${Math.round(elapsed_s)}s`}
            {stage_times && Object.keys(stage_times).length > 0 && (
              <span className="ml-1.5 opacity-75">
                （{Object.entries(stage_times)
                  .map(([k, v]) => `${STAGE_NAME_MAP[k] || k} ${Math.round(Number(v))}s`)
                  .join(' · ')}）
              </span>
            )}
          </p>
        )}
        <button onClick={onViewDetail}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-[var(--vi-ink)] px-3 py-1.5 text-sm font-medium text-white transition-colors hover:opacity-90"
        >
          <ExternalLink className="h-3.5 w-3.5" /> 查看详情
        </button>
      </div>
    )
  }

  if (taskStatus === 'FAILED') {
    return (
      <div className="my-3 rounded-xl border border-red-200 bg-red-50/70 p-4">
        <div className="flex items-center gap-2">
          <XCircle className="h-5 w-5 text-red-500" />
          <span className="font-medium text-red-800">内容洞察生成失败</span>
        </div>
        {/* 后端已把失败分类翻译成人话（风控限流/视频下架/链接不完整），
            有具体原因就展示，别让用户对着通用文案猜 */}
        <p className="mt-1 text-sm text-red-600">
          {failMsg ? failMsg : '请稍后重试或检查链接是否有效'}
        </p>
        <button
          onClick={onViewDetail}
          className="mt-2 inline-flex items-center gap-1.5 text-xs text-red-700 underline underline-offset-2 hover:text-red-800"
        >
          <ExternalLink className="h-3 w-3" /> 查看详情
        </button>
      </div>
    )
  }

  const currentIdx = STEPS.findIndex(s => s.key === taskStatus)
  const currentLabel = STEPS[currentIdx]?.label || '处理中'
  const overallPct = Math.round(
    ((currentIdx < 0 ? 0 : currentIdx) + (typeof progress === 'number' ? progress / 100 : 0.5)) / STEPS.length * 100,
  )

  return (
    <div className="my-3 overflow-hidden rounded-xl border border-hairline bg-white">
      {/* chrome 头：深底 + REC 点 + mono 百分比 */}
      <div className="flex items-center gap-2.5 bg-chrome px-4 py-2.5">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rec opacity-60" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-rec" />
        </span>
        <span className="text-sm font-medium text-white">正在生成 · {currentLabel}</span>
        {message && (
          <span className="tc ml-1 rounded bg-white/10 px-1.5 py-0.5 text-[11px] text-amber-200" title={message}>
            {message}
          </span>
        )}
        {typeof progress === 'number' && Number.isFinite(progress) && (
          <span className="tc ml-1 rounded bg-white/10 px-1.5 py-0.5 text-[11px] text-zinc-200">
            {getStageProgressLabel(taskStatus, progress)}
          </span>
        )}
        {/* EVT Must Fix：已用时长 + 基于当前速率的粗估剩余（标注为估算，非承诺） */}
        {typeof elapsed_s === 'number' && elapsed_s > 0 && (
          <span className="tc ml-1 rounded bg-white/10 px-1.5 py-0.5 text-[11px] text-zinc-200"
                title={typeof progress === 'number' && progress > 5
                  ? '按当前阶段进度速度估算，仅供参考' : undefined}>
            已用 {elapsed_s >= 60 ? `${Math.floor(elapsed_s / 60)}m${Math.round(elapsed_s % 60)}s` : `${Math.round(elapsed_s)}s`}
            {typeof progress === 'number' && progress > 5 && (() => {
              const rate = progress / elapsed_s
              const remain = Math.max(0, (100 - progress) / rate)
              return remain > 2 ? ` · 约还需 ${remain >= 60 ? `${Math.round(remain / 60)}m` : `${Math.round(remain)}s`}` : ''
            })()}
          </span>
        )}
        <span className="tc ml-auto text-[11px] text-zinc-500">OVERALL {overallPct}%</span>
        <button
          onClick={() => onStop(taskId)}
          className="rounded-lg border border-white/15 px-2.5 py-1 text-xs font-medium text-zinc-300 transition-colors hover:border-rec hover:text-rec"
        >
          停止
        </button>
      </div>
      <div className="p-4">
        {taskTitle && <p className="mb-3 truncate text-xs text-ink-2">{taskTitle}</p>}
        {/* 轨道化步骤条：teal✓ 已完成 / rec 播放头当前 / 灰空心未来，连线随进度填充 */}
        <div className="flex items-start">
          {STEPS.map((step, i) => {
            const done = i < currentIdx
            const active = i === currentIdx
            const last = i === STEPS.length - 1
            return (
              <div key={step.key} className="flex min-w-0 flex-1 flex-col items-center">
                <div className="flex w-full items-center">
                  <div className={`h-[3px] flex-1 rounded-l ${i === 0 ? 'opacity-0' : done || active ? 'bg-teal-ok/70' : 'bg-hairline'}`} />
                  {done && (
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-teal-ok text-white">
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    </span>
                  )}
                  {active && (
                    <span className="relative flex h-6 w-6 shrink-0 items-center justify-center">
                      <span className="absolute h-6 w-6 animate-ping rounded-full bg-rec opacity-30" />
                      <span className="h-3.5 w-3.5 rounded-full bg-rec ring-4 ring-rec/20" />
                    </span>
                  )}
                  {!done && !active && (
                    <span className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-hairline bg-white" />
                  )}
                  <div className={`h-[3px] flex-1 rounded-r ${last ? 'opacity-0' : i < currentIdx ? 'bg-teal-ok/70' : 'bg-hairline'}`} />
                </div>
                <span className={`mt-1.5 whitespace-nowrap text-[11px] ${active ? 'font-medium text-rec' : done ? 'text-teal-ok' : 'text-ink-3'}`}>
                  {step.label}
                  {active && typeof progress === 'number' && Number.isFinite(progress) && (
                    <span className="tc ml-1 font-mono text-ink-2 tabular-nums">
                      {Math.round(progress)}%
                    </span>
                  )}
                  {done && <span className="tc ml-1 text-teal-ok/70">✓</span>}
                </span>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ====== 洞察库状态栏 ======
function KbStatsBar({ stats }: { stats: KbStats | null }) {
  if (!stats || stats.total_notes === 0) return null
  return (
    <div className="tc mb-4 flex items-center gap-2 rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-4 py-2 text-[11px] text-[var(--vi-ink-2)]">
      <BookOpen className="h-3.5 w-3.5 text-[var(--vi-ink-3)]" />
      <span>洞察库 · {stats.total_notes} notes · {stats.total_chunks} chunks</span>
      <span className="ml-auto text-[var(--vi-ink-3)]">
        {stats.bm25_available ? 'BM25' : ''}
        {stats.bm25_available && stats.reranker_available ? ' · ' : ''}
        {stats.reranker_available ? 'BGE' : ''}
      </span>
    </div>
  )
}

// ====== 主组件 ======
export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const navigate = useNavigate()

  const {
    conversations,
    activeConversationId,
    createConversation,
    setActiveConversation,
    pruneEmptyConversations,
    addMessage,
    updateMessage,
    setConversationTasks,
    updateConversationTasks,
  } = useConversationStore()
  const addPendingTask = useTaskStore(state => state.addPendingTask)
  const modelList = useModelStore(state => state.modelList)
  const loadEnabledModels = useModelStore(state => state.loadEnabledModels)
  const providerList = useProviderStore(state => state.provider)
  const fetchProviderList = useProviderStore(state => state.fetchProviderList)

  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [detectedLinks, setDetectedLinks] = useState<string[]>([])
  const scrollRef = useRef<HTMLDivElement>(null)

  // 内容洞察生成进度：挂到会话记录上（跨会话切换/路由跳转持久，不再用组件 state）
  const pollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map())
  // handleGlobalStop 引用 pollRefs，声明顺序：pollRefs 必须在前
  const xhsAbortRef = useRef<{ aborted: boolean } | null>(null)
  const handleGlobalStop = useCallback(() => {
    // ① 中止合集预检阶段（等待浏览窗口等）
    if (xhsAbortRef.current) xhsAbortRef.current.aborted = true
    const tauri = (window as any).__TAURI_INTERNALS__
    if (tauri) {
      // ② 让 Rust 直接关闭 XHS 浏览窗口：被动模式见窗口关闭即返回 Ok，
      //    挂起的 invoke 立刻解除，handleSend 继续走「0 条可解析」收尾
      tauri.invoke('close_xhs_windows').catch(() => {})
    }
    // ③ 取消全部在途生成任务
    for (const [taskId, timer] of pollRefs.current) {
      clearInterval(timer)
      void cancel_task(taskId).catch(() => {})
      setTaskProgresses(prev => prev.map(item =>
        item.taskId === taskId ? { ...item, status: 'CANCELLED' } : item,
      ))
    }
    pollRefs.current.clear()
    setLoading(false)
    setRoutingIntent(false)
  }, [])
  // Agent 会话状态
  const [agentSources, setAgentSources] = useState<ChatSource[]>([])
  // 兼容两种轨迹：旧 Agent（arguments/round）与 Harness（args/restricted/elapsed_s/ok）
  const [agentToolCalls, setAgentToolCalls] = useState<
    {
      tool: string
      arguments?: Record<string, unknown>
      args?: Record<string, unknown>
      round?: number
      restricted?: boolean
      elapsed_s?: number
      ok?: boolean
    }[]
  >([])
  const [kbStats, setKbStats] = useState<KbStats | null>(null)
  // ====== 流式（SSE）状态：思考链实时可见 + 正文增量 ======
  const [streamThinking, setStreamThinking] = useState('')
  const [streamToolEvents, setStreamToolEvents] = useState<
    { tool: string; ok: boolean; restricted: boolean; elapsed_s: number }[]
  >([])
  const [streamActive, setStreamActive] = useState(false)
  const streamAbortRef = useRef<{ abort: () => void } | null>(null)
  // 用户手动选择的模型（供应商/模型选择器）；null = 跟随默认 targetModel
  const [manualModel, setManualModelState] = useState<{ provider_id: string; model_name: string; label: string } | null>(() => {
    // 手选模型持久化：跳页/切会话/刷新都不回落初始值（用户明确要求记住状态）
    try {
      const raw = localStorage.getItem('vidinsight_manual_model')
      return raw ? (JSON.parse(raw) as { provider_id: string; model_name: string; label: string }) : null
    } catch {
      return null
    }
  })
  const setManualModel = useCallback(
    (m: { provider_id: string; model_name: string; label: string } | null) => {
      setManualModelState(m)
      try {
        if (m) localStorage.setItem('vidinsight_manual_model', JSON.stringify(m))
        else localStorage.removeItem('vidinsight_manual_model')
      } catch { /* 存储不可用时静默降级为仅内存态 */ }
    },
    [],
  )
  // 链接+提问同发的自动问答登记：{这批任务 id, 用户的问题, 是否已回答}
  // 全部任务到终态后触发一次 handleAgentChat，用洞察库回答（未成功则如实告知）
  const [pendingAutoQA, setPendingAutoQA] = useState<
    { taskIds: string[]; question: string; answered: boolean }[]
  >(() => {
    try {
      const raw = localStorage.getItem('vidinsight_pending_auto_qa')
      return raw ? JSON.parse(raw) : []
    } catch {
      return []
    }
  })
  useEffect(() => {
    try { localStorage.setItem('vidinsight_pending_auto_qa', JSON.stringify(pendingAutoQA)) } catch { /* ignore */ }
  }, [pendingAutoQA])
  // 高阶意图路由（Premium / T7）：开启后输入先过一次 LLM 路由再分发；默认关闭走机械规则
  const [premiumRouter, setPremiumRouter] = useState<boolean>(
    () => localStorage.getItem('vidinsight_premium_router') === '1',
  )
  const [routingIntent, setRoutingIntent] = useState(false)
  const togglePremiumRouter = useCallback(() => {
    setPremiumRouter(prev => {
      const next = !prev
      localStorage.setItem('vidinsight_premium_router', next ? '1' : '0')
      toast.success(next ? '高阶意图路由已开启（每次发送约 1s LLM 判定）' : '已切回机械规则分发')
      return next
    })
  }, [])

  // 内容洞察生成进度：挂到会话记录上（跨会话切换/路由跳转持久，不再用组件 state）

  useEffect(() => { loadEnabledModels() }, [loadEnabledModels])
  useEffect(() => { fetchProviderList() }, [fetchProviderList])

  // 空对话复用：没有可用会话时，优先复用已有的空白「新对话」，
  // 避免每次进页面都凭空多出一个（对话历史会越堆越臃肿）
  useEffect(() => {
    pruneEmptyConversations()
    if (conversationId) return
    const isEmpty = (c: Conversation) =>
      (c.messages?.length ?? 0) === 0 && (c.taskProgresses?.length ?? 0) === 0
    const active = conversations.find(c => c.id === activeConversationId)
    const reusable =
      active && active.title === '新对话' && isEmpty(active)
        ? active
        : conversations.find(c => c.title === '新对话' && isEmpty(c))
    if (reusable) {
      setActiveConversation(reusable.id)
      navigate(`/chat/${reusable.id}`, { replace: true })
      return
    }
    const id = createConversation()
    navigate(`/chat/${id}`, { replace: true })
  }, [
    conversationId,
    conversations,
    activeConversationId,
    createConversation,
    setActiveConversation,
    pruneEmptyConversations,
    navigate,
  ])

  const currentConversation = useMemo(
    () => conversations.find(c => c.id === (conversationId || '')),
    [conversations, conversationId],
  )
  const messages = currentConversation?.messages ?? []

  // 进度卡片数据源：会话记录（随会话隔离、路由跳转不丢失）
  const taskProgresses: BatchTaskProgress[] = currentConversation?.taskProgresses ?? []
  const setTaskProgresses = (
    updater: BatchTaskProgress[] | ((prev: BatchTaskProgress[]) => BatchTaskProgress[]),
  ) => {
    if (!conversationId) return
    updateConversationTasks(conversationId, prev =>
      typeof updater === 'function' ? updater(prev) : updater,
    )
  }
  const commitTaskProgresses = (list: BatchTaskProgress[]) => {
    if (conversationId) setConversationTasks(conversationId, list)
  }

  // 僵尸卡片自检：应用重开/切路由回来时，对"处理中"的旧卡片查询一次后端真实状态；
  // 应用重启会丢任务队列，后端已不存在的任务自动标记为失败，避免卡片永远冻结在"处理中"
  const checkedStaleRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!conversationId || !currentConversation) return
    if (checkedStaleRef.current.has(conversationId)) return
    const stale = (currentConversation.taskProgresses ?? []).filter(
      t => t.status === 'PENDING' || t.status === 'RUNNING',
    )
    if (stale.length === 0) return
    checkedStaleRef.current.add(conversationId)
    stale.forEach(task => {
      get_task_status(task.taskId)
        .then((res: any) => {
          const st: string = res?.status || ''
          if (st === 'CANCELLED' || !st) {
            setTaskProgresses(prev => prev.map(item =>
              item.taskId === task.taskId
                ? { ...item, status: st === 'CANCELLED' ? 'CANCELLED' : 'FAILED' }
                : item,
            ))
          }
        })
        .catch((e: any) => {
          // 后端把 FAILED 作为业务错误 reject（code=500 + 中文原因），此前
          // 空 catch 把它吞掉，卡片就永远冻结在「处理中」——正是本段要修的问题。
          if (typeof e?.code === 'number' && e.code > 0) {
            setTaskProgresses(prev => prev.map(item =>
              item.taskId === task.taskId
                ? { ...item, status: 'FAILED', failMsg: e?.msg || '' }
                : item,
            ))
          }
          // 网络抖动：保持原状态，下次进入会话会重新自检
        })
    })
  }, [conversationId, currentConversation])

  useEffect(() => { setDetectedLinks(extractVideoLinks(input)) }, [input])

  // 链接检测提示文案：单条不叫「批量」；合集链接单独说明（发送后会自动展开成多条）
  const linkHint = useMemo(() => {
    const n = detectedLinks.length
    if (n === 0) return null
    const collectionCount = detectedLinks.filter(isCollectionLink).length
    const regular = n - collectionCount
    if (collectionCount > 0 && regular === 0) {
      return collectionCount === 1
        ? '检测到合集链接，发送后自动展开全部内容并逐条生成洞察'
        : `检测到 ${collectionCount} 个合集链接，发送后自动展开全部内容并逐条生成`
    }
    if (n === 1) return '检测到 1 条内容链接，发送后自动生成内容洞察'
    return `检测到 ${n} 条内容链接，发送后自动批量生成内容洞察`
  }, [detectedLinks])

  // 状态行右侧平台提示（纯展示；无链接时给出支持平台清单）
  const detectedPlatformStatus = useMemo(() => {
    const labels = Array.from(new Set(
      detectedLinks.map(url => PLATFORM_LABEL[detectPlatform(url)] ?? detectPlatform(url)),
    ))
    return labels.length > 0 ? labels.join(' / ') : 'BILIBILI / YOUTUBE / DOUYIN / XHS'
  }, [detectedLinks])

  // 自动滚动
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, loading, taskProgresses, agentSources, agentToolCalls])

  useEffect(() => {
    return () => {
      pollRefs.current.forEach(timer => clearInterval(timer))
      pollRefs.current.clear()
    }
  }, [])

  const targetModel = useMemo(() => {
    // 默认模型必须是「已配 API Key 的供应商」下的模型：
    // modelList 本身不携带 key 信息，按 providerList 里 apiKey 非空的供应商过滤，
    // 避免默认落到 zhipu 之类未配 key 的供应商上（问答会直接报「API Key 未配置」）
    const keyed = new Set(
      providerList.filter(p => p.enabled !== 0 && p.apiKey).map(p => String(p.id)),
    )
    const candidates = keyed.size
      ? modelList.filter(m => keyed.has(String(m.provider_id)))
      : modelList
    return candidates.find(m => m.model_name === 'deepseek-v4-pro')
      || candidates[0]
      // 兜底：过滤后为空但 modelList 非空时退回全量列表首个，保持旧行为
      || modelList[0]
      || null
  }, [modelList, providerList])

  // ====== 停止任务：协作式取消，后端在阶段边界响应 ======
  const handleStopTask = useCallback(async (taskId: string) => {
    try {
      await cancel_task(taskId)
    } catch { /* 请求失败也先在界面上标记，后端轮询会纠正 */ }
    const timer = pollRefs.current.get(taskId)
    if (timer) {
      clearInterval(timer)
      pollRefs.current.delete(taskId)
    }
    setTaskProgresses(prev => prev.map(item =>
      item.taskId === taskId ? { ...item, status: 'CANCELLED' } : item,
    ))
    toast.success('已停止该任务')
  }, [])

  // ====== 轮询任务状态 ======
  const startPolling = useCallback((task: BatchTaskProgress) => {
    const existingTimer = pollRefs.current.get(task.taskId)
    if (existingTimer) clearInterval(existingTimer)
    // 2026-10-20 闭包修复：轮询更新一律走 store 最新状态 + 任务自带的
    // conversationId。此前捕获组件渲染闭包的 setTaskProgresses（内含当时的
    // conversationId）——会话切换/重渲染后旧闭包把成功状态写进别的会话，
    // 当前会话卡片永远停在"处理中"（实测 toast 弹了卡片不动）。
    const convId = task.conversationId || conversationId || ''
    const updateTasks = (updater: (prev: ConversationTaskProgress[]) => ConversationTaskProgress[]) => {
      const store = useConversationStore.getState()
      const target = store.conversations.find(c => c.id === convId)
      if (!target) return
      store.updateConversationTasks(convId, () => updater(target.taskProgresses ?? []))
    }
    const stopPolling = () => {
      const t = pollRefs.current.get(task.taskId)
      if (t) clearInterval(t)
      pollRefs.current.delete(task.taskId)
    }

    const timer = setInterval(async () => {
      try {
        const res = await get_task_status(task.taskId)
        const status: string = res?.status || ''
        const progress = typeof res?.stage_progress === 'number'
          ? res.stage_progress
          : (typeof res?.progress === 'number' ? res.progress : undefined)
        const elapsed_s = typeof res?.elapsed_s === 'number' ? res.elapsed_s : undefined
        const msg: string | undefined = typeof res?.message === 'string' && res.message ? res.message : undefined
        updateTasks(prev => prev.map(item =>
          item.taskId === task.taskId ? { ...item, status, progress, stage_progress: res?.stage_progress, elapsed_s, message: msg } : item,
        ))
        if (status === 'SUCCESS') {
          stopPolling()
          const title = res?.result?.audio_meta?.title || getFallbackTaskTitle(task.url, task.index)
          updateTasks(prev => prev.map(item =>
            item.taskId === task.taskId ? { ...item, status: 'SUCCESS', title, elapsed_s: res?.elapsed_s, stage_times: res?.stage_times } : item,
          ))
          toast.success('内容洞察生成成功！')
        } else if (status === 'CANCELLED') {
          stopPolling()
          updateTasks(prev => prev.map(item =>
            item.taskId === task.taskId ? { ...item, status: 'CANCELLED' } : item,
          ))
        }
      } catch (e: any) {
        // FAILED 响应被拦截器 reject（code!=0）：在这里识别并落入失败卡，
        // 后端已把下载错误分类翻译成人话（风控限流/视频下架/链接不完整）
        if (e && typeof e.code === 'number' && e.code !== 0) {
          stopPolling()
          const failMsg: string = String(e.msg || '')
          updateTasks(prev => prev.map(item =>
            item.taskId === task.taskId ? { ...item, status: 'FAILED', failMsg } : item,
          ))
          toast.error(failMsg ? `生成失败：${failMsg}` : '内容洞察生成失败', { duration: 6000 })
        }
        // 网络抖动静默，下一轮轮询自愈
      }
    }, 3000)
    pollRefs.current.set(task.taskId, timer)
  }, [])

  // ====== Agent 对话 ======
  const handleAgentChat = useCallback(async (text: string) => {
    if (!conversationId || !targetModel) return '抱歉，AI 模型未配置。'

    setAgentToolCalls([])
    setAgentSources([])
    setStreamThinking('')
    setStreamToolEvents([])
    setStreamActive(true)

    try {
      // 使用 getState() 直接读取最新状态，避免 useMemo 闭包过期
      const latestMessages = useConversationStore.getState()
        .conversations.find(c => c.id === conversationId)?.messages ?? []
      const history = latestMessages.slice(-10).map(m => ({
        role: m.role, content: m.content,
      }))

      // 统一 Harness 流式优先：思考链/工具事件/正文增量实时可见（对齐 Codex/ZCode 体验）；
      // 流式失败（旧后端/网络层不支持 SSE）→ 非流式 harness → 旧 Agent，三级降级不中断。
      const provider_id = manualModel?.provider_id ?? targetModel.provider_id
      const model_name = manualModel?.model_name ?? targetModel.model_name

      const streamAnswer = await new Promise<string | null>(resolve => {
        let acc = ''
        let gotFinal = false
        const handle = askHarnessStream(
          { question: text, history, provider_id, model_name, allowed_tool_kinds: ['consume', 'produce'] },
          e => {
            switch (e.type) {
              case 'thinking':
                setStreamThinking(t => (t + e.delta).slice(-8000)) // 防超长思考撑爆内存
                break
              case 'tool':
                setStreamToolEvents(list => [...list,
                  { tool: e.tool, ok: e.ok, restricted: e.restricted, elapsed_s: e.elapsed_s }])
                break
              case 'delta':
                acc += e.text
                break
              case 'final':
                gotFinal = true
                setAgentSources(e.data.sources || [])
                setAgentToolCalls(e.data.tool_calls || [])
                if (e.data.kb_stats) setKbStats(e.data.kb_stats)
                resolve(e.data.answer || acc || null)
                break
              case 'error':
                // 上游报错（限流/额度/断流）原样透出，不打哑谜
                toast.error(`模型调用失败：${e.message}`, { duration: 6000 })
                resolve(gotFinal ? acc : null)
                break
            }
          },
        )
        streamAbortRef.current = handle
      })

      if (streamAnswer !== null) return streamAnswer
      console.warn('[AgentChat] 流式未拿到结果，降级非流式 harness')

      // 非流式 Harness：失败时自动降级回旧 Agent，保证问答链路不中断。
      try {
        const harnessResult = await askHarness({
          question: text,
          history,
          provider_id,
          model_name,
          allowed_tool_kinds: ['consume', 'produce'],
        })

        setAgentSources(harnessResult.sources || [])
        setAgentToolCalls(harnessResult.tool_calls || [])
        if (harnessResult.kb_stats) setKbStats(harnessResult.kb_stats)
        return harnessResult.answer
      } catch (harnessErr) {
        console.warn('[AgentChat] harness 失败，降级回旧 Agent:', harnessErr)
      }

      const result = await askAgent({
        question: text,
        history,
        provider_id,
        model_name,
      })

      setAgentSources(result.sources || [])
      setAgentToolCalls(result.tool_calls || [])
      if (result.kb_stats) setKbStats(result.kb_stats)
      return result.answer
    } catch (e: any) {
      // 打印完整错误信息到控制台，方便定位
      console.error('[AgentChat] 请求失败，完整错误:', e)
      console.error('[AgentChat] 错误码:', e?.code, '消息:', e?.msg, 'HTTP状态:', e?.response?.status)

      // 区分错误类型，给出明确诊断
      const status = e?.httpStatus || e?.response?.status
      const code = e?.code
      const msg = e?.msg || e?.message || ''
      const isNetworkFailure =
        code === 'ERR_NETWORK'
        || msg?.includes('Network Error')
        || (code === -1 && msg?.includes('网络连接'))

      if (isNetworkFailure) {
        return `⚠️ 无法连接到后端服务

**可能原因：**
- 后端未启动 → 在 \`backend/\` 目录执行 \`python main.py\`
- 后端端口冲突 → 检查 8483 端口是否被占用
- 后端启动失败 → 查看后端控制台的错误堆栈

**排查步骤：**
1. 打开 \`backend/\` 目录的终端
2. 执行 \`python main.py\`
3. 观察控制台输出是否出现 \`[startup 5/5] 启动完成\`
4. 如有报错，根据堆栈定位问题`
      }

      if (code === 'ECONNABORTED' || msg?.includes('timeout')) {
        return `⚠️ 请求超时，AI 模型响应过慢

**可能原因：**
- LLM API 响应慢（尤其是大模型首次推理）
- 网络到 LLM 供应商的连接延迟

**建议：**
- 稍后重试
- 在「全局配置」中切换更快的模型`
      }

      if (status === 500) {
        return `⚠️ 后端服务器内部错误（500）

**请查看后端控制台的完整堆栈信息**，通常能看到具体报错原因。

常见原因：
- LLM API Key 无效或余额不足
- 模型配置错误
- 数据库/向量库异常`
      }

      if (status && status >= 400) {
        return `⚠️ 请求失败 (HTTP ${status}): ${msg || '未知错误'}

请检查「全局配置」中的模型设置，或查看后端控制台日志。`
      }

      return `⚠️ ${msg || 'AI 服务暂时不可用'}

请检查后端是否正常运行：\`cd backend && python main.py\``
    }
  }, [conversationId, targetModel, manualModel])

  // ====== 自动问答闭环：链接+提问同发时，这批任务全部到终态后自动回答 ======
  const firedAutoQARef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (pendingAutoQA.length === 0 || !conversationId) return
    const statusMap = new Map(taskProgresses.map(t => [t.taskId, t.status] as const))
    const terminal = new Set(['SUCCESS', 'FAILED', 'CANCELLED'])
    for (const entry of pendingAutoQA) {
      if (entry.answered) continue
      const key = entry.taskIds.join(',')
      if (firedAutoQARef.current.has(key)) continue
      const sts = entry.taskIds.map(id => statusMap.get(id)).filter((s): s is string => !!s)
      if (sts.length < entry.taskIds.length) continue // 尚未全部登记
      if (sts.some(s => !terminal.has(s))) continue // 还有阶段在跑
      firedAutoQARef.current.add(key)
      setPendingAutoQA(list => list.map(e => (e.taskIds === entry.taskIds ? { ...e, answered: true } : e)))
      if (sts.some(s => s === 'SUCCESS')) {
        void handleAgentChat(entry.question)
          .then(answer => {
            addMessage(conversationId, { role: 'assistant', content: answer })
          })
          .catch(() => {
            addMessage(conversationId, {
              role: 'assistant',
              content: '自动回答你的问题时出现异常。洞察已经生成完毕，可以直接在对话框重新提问（会基于洞察库回答）。',
            })
          })
      } else {
        addMessage(conversationId, {
          role: 'assistant',
          content: '这批视频的洞察生成未能完成，暂时无法基于它们回答你的问题。可到「分析历史」查看失败原因后重试。',
        })
      }
      return // 一次 effect 只处理一个批次，其余交给下一次状态变更
    }
  }, [taskProgresses, pendingAutoQA, conversationId, handleAgentChat, addMessage])

  // ====== 发送消息 ======
  const handleSend = useCallback(async (value: string) => {
    const text = value.trim()
    if (!text || loading) return
    if (!conversationId) return

    // 用户消息先行入列：合集预检/收割的进度气泡要排在它后面
    addMessage(conversationId, { role: 'user', content: text })
    setInput('')
    setLoading(true)

    let videoLinks = extractVideoLinks(text)

    // ====== 高阶意图路由（Premium / T7）======
    // 机械正则只负责「链接存在性」这一事实；开启高阶开关时由 LLM 决定「拿这些链接做什么」。
    // 路由失败/超时/结论不可执行 → 自动回退机械规则（有链接解析，无链接进 Agent）。
    let routed: RouteIntentResponse | null = null
    if (premiumRouter) {
      setRoutingIntent(true)
      try {
        routed = await routeIntent({
          message: text,
          links: videoLinks,
          provider_id: manualModel?.provider_id ?? targetModel?.provider_id ?? '',
          model_name: manualModel?.model_name ?? targetModel?.model_name ?? '',
        })
      } catch {
        routed = null
      } finally {
        setRoutingIntent(false)
      }
      if (routed && (routed.intent === 'parse' || routed.intent === 'compare') && videoLinks.length === 0) {
        routed = { intent: 'ask', links: [], reason: '要求解析但消息中无链接' }
      }
      // LLM 从用户链接里挑了子集 → 只解析被点名的（例如「只总结第一条」）
      if (routed && (routed.intent === 'parse' || routed.intent === 'compare') && routed.links.length > 0) {
        videoLinks = routed.links
      }
    }
    const isVideoLink = (routed
      ? routed.intent === 'parse' || routed.intent === 'compare'
      : videoLinks.length > 0) && videoLinks.length > 0
    const isCompareIntent = routed?.intent === 'compare'

    // ====== 合集链接处理（所有平台统一入口）======
    // 小红书合集：每个合集开【一个】WebView 收割 xsec_token（页面卡片 href 自带），
    //   用户关闭窗口即中止全部收割；然后由后端展开合并 token。
    // 其余平台（YouTube/B站）直接展开；抖音/快手合集后端返回明确的不支持说明。
    const collectionMeta: string[] = []
    const preflightErrors: string[] = []
    // 被动捕获模式：被用户浏览过的笔记已进 XHSNoteCache，无 token 也能取详情。
    // 必须声明在合集块之外——第 ③ 步的无 token 过滤要用它做豁免，否则粘贴
    // 小红书单条链接时会抛 ReferenceError，整个发送流程中断且无任何提示。
    const cachedNoteIds = new Set<string>()
    try {
      const collectionUrls = videoLinks.filter(isCollectionLink)
      if (collectionUrls.length > 0) {
        // ① 小红书合集：开一个被动捕获窗口（用户手动浏览，工具零自动化，防账号风控）
        for (const colUrl of collectionUrls.filter(isXhsCollection)) {
          if (xhsAbortRef.current?.aborted) break  // 停止键已按下：不再开新窗口
          // 进度气泡：WebView 各阶段经 Rust emit 实时回传，原位更新这一条消息
          addMessage(conversationId, { role: 'assistant', content: '⏳ 已打开小红书浏览窗口：请像平常一样浏览并点开想分析的笔记（工具只在后台顺路记录，不会自动操作），浏览完直接关闭窗口。随时可按输入框旁的停止键中止' })
          const conv = useConversationStore.getState().conversations.find(c => c.id === conversationId)
          const progressIdx = (conv?.messages.length ?? 1) - 1
          const abortState = { aborted: false }
          xhsAbortRef.current = abortState
          try {
            await fetchXhsCollectionViaWebview(colUrl, e => {
              const content =
                e.event === 'fail'
                  ? `⚠️ 合集读取失败：${e.reason || '未知原因'}${e.diag ? `（页面实况 ${e.diag.slice(0, 120)}）` : ''}`
                  : e.event === 'captcha'
                  ? '🔒 浏览窗口出现安全验证（滑块）：请在窗口内完成拖动验证，完成后继续浏览想分析的笔记。不想等可直接按下方停止键'
                  : e.event === 'login'
                  ? '🔑 浏览窗口需要登录小红书：请在窗口内扫码登录，登录后继续浏览想分析的笔记。不想等可直接按下方停止键'
                  : e.msg ? `⏳ ${e.msg}`
                  : ''
              if (content) updateMessage(conversationId, progressIdx, { content })
            })
            updateMessage(conversationId, progressIdx, {
              content: abortState.aborted ? '⏹ 已按停止：浏览窗口已关闭，跳过本合集' : '✅ 浏览窗口已关闭，正在展开合集…',
            })
            if (abortState.aborted) {
              preflightErrors.push('已按停止：合集解析中止')
              break
            }
          } catch (e: any) {
            // Tauri invoke 的 Err 是裸字符串 rejection，不是 Error 对象
            const reason = typeof e === 'string' ? e : e?.message || e?.toString() || '未知原因'
            updateMessage(conversationId, progressIdx, {
              content: `⚠️ 小红书合集读取中止：${reason}`,
            })
            preflightErrors.push(reason)
            break  // 读取失败/被用户关闭 → 中止，避免连环弹窗
          } finally {
            if (xhsAbortRef.current === abortState) xhsAbortRef.current = null
          }
        }
        // ② 展开合集为逐条笔记/视频
        const expanded: string[] = []
        for (const colUrl of collectionUrls) {
          try {
            const col = await expandCollection(colUrl, detectCollectionPlatform(colUrl))
            const notes: any[] = col?.notes ?? []
            if (notes.length > 0) {
              for (const n of notes) {
                expanded.push(n.url)
                if (n.cached && n.note_id) cachedNoteIds.add(String(n.note_id))
              }
              collectionMeta.push(`《${col.name || '合集'}》展开 ${notes.length} 条内容`)
            } else {
              collectionMeta.push('合集展开为空，请确认链接有效')
            }
          } catch (e: any) {
            collectionMeta.push(`合集展开失败：${e?.response?.data?.message || e?.message || '未知错误'}`)
          }
        }
        // 合集 URL 本身不进解析管线（替换为展开后的逐条链接）
        videoLinks = [...videoLinks.filter(u => !isCollectionLink(u)), ...expanded]
      }

      // ③ 无 token 的小红书单条链接：XHS 网页版硬规则（无 token 详情页码墙，登录也一样），
      //    不再弹窗重试，直接诚实告知用户正确的链接来源。
      //    例外：被动捕获模式下被用户浏览过的笔记已进 XHSNoteCache，放行
      const tokenless = videoLinks.filter(u =>
        /xiaohongshu\.com/.test(u) && !u.includes('xsec_token=')
        && ![...cachedNoteIds].some(id => u.includes(id)))
      if (tokenless.length > 0) {
        videoLinks = videoLinks.filter(u => !tokenless.includes(u))
        preflightErrors.push(
          `${tokenless.length} 条小红书链接缺少访问凭证（xsec_token），无法解析。` +
          '请从 App 分享、或从合集/作者主页/搜索结果页复制链接（URL 中带 xsec_token= 参数）。')
      }
      if (preflightErrors.length > 0) {
        toast.error(preflightErrors[0].slice(0, 160), { duration: 9000 })
      }
    } catch (e: any) {
      // 预检阶段（合集展开/链接过滤）自身异常时不能静默：此前异常会直接中断
      // handleSend，用户只看到输入框转一下就没了，完全不知道发生了什么。
      const reason = typeof e === 'string' ? e : e?.msg || e?.message || '未知错误'
      toast.error(`链接预检失败：${reason}`, { duration: 8000 })
      addMessage(conversationId, {
        role: 'assistant',
        content: `⚠️ 链接预检失败：${reason}\n\n请检查链接后重试，或到「分析历史」查看后端日志。`,
      })
      setLoading(false)
      return
    }
    setAgentSources([])
    setAgentToolCalls([])

    try {
      if (isVideoLink) {
        if (!targetModel) {
          addMessage(conversationId, {
            role: 'assistant',
            content: '⚠️ 未找到可用的 AI 模型。\n\n请先在「全局配置 → AI 模型设置」中添加并启用一个模型。',
          })
          setLoading(false)
          return
        }

        // 合集处理全部失败/链接全被过滤（如被用户取消收割）→ 明确告知，不提交任何任务
        if (videoLinks.length === 0) {
          addMessage(conversationId, {
            role: 'assistant',
            content: '⚠️ 没有可解析的链接。\n\n' +
              (preflightErrors.length > 0 ? preflightErrors.join('\n\n') : '请检查链接后重试。'),
          })
          setLoading(false)
          return
        }

        const submissionResults = await Promise.all(videoLinks.map(async (videoUrl, index) => {
          const platform = detectPlatform(videoUrl)
          const payload: Parameters<typeof generateNote>[0] = {
            video_url: videoUrl,
            platform,
            quality: AUTO_GEN_DEFAULTS.quality,
            model_name: manualModel?.model_name ?? targetModel.model_name,
            provider_id: manualModel?.provider_id ?? targetModel.provider_id,
            style: AUTO_GEN_DEFAULTS.style,
            format: AUTO_GEN_DEFAULTS.format,
            extras: AUTO_GEN_DEFAULTS.extras,
            video_understand: AUTO_GEN_DEFAULTS.video_understand,
            grid_size: AUTO_GEN_DEFAULTS.grid_size,
          }

          try {
            const data: any = await generateNote(payload)
            const taskId: string = data?.task_id || ''
            if (!taskId) {
              return { ok: false as const, url: videoUrl, reason: '未获取到任务 ID' }
            }
            addPendingTask(taskId, platform, payload)
            return { ok: true as const, taskId, url: videoUrl, platform, index }
          } catch (e: any) {
            if (e?.data?.reason === 'transcriber_model_not_ready') {
              return {
                ok: false as const,
                url: videoUrl,
                reason: '转写模型尚未就绪，请先去「全局配置 → 音频转写配置」页面下载转写模型。',
              }
            }
            return { ok: false as const, url: videoUrl, reason: e?.message || '提交失败' }
          }
        }))

        const submitted = submissionResults.filter(item => item.ok)
        const failed = submissionResults.filter(item => !item.ok)
        if (submitted.length > 0) {
          const progressItems: BatchTaskProgress[] = submitted.map(item => ({
            taskId: item.taskId,
            conversationId,
            status: 'PENDING',
            url: item.url,
            platform: item.platform,
            index: item.index,
            title: getFallbackTaskTitle(item.url, item.index),
          }))
          commitTaskProgresses(progressItems)
          progressItems.forEach(startPolling)

          // 链接+提问同发：把问题挂到这批任务上，全部结束后自动基于洞察库回答
          const questionText = text.replace(/https?:\/\/\S+/g, ' ').replace(/\s+/g, ' ').trim()
          if (questionText.length >= 4) {
            setPendingAutoQA(prev => [
              ...prev.filter(e => !e.answered),
              { taskIds: progressItems.map(i => i.taskId), question: questionText, answered: false },
            ])
          }
        }

        const successLines = submitted.map((item, index) => {
          const platformLabel =
            item.platform === 'douyin' ? '抖音'
            : item.platform === 'bilibili' ? 'B站'
            : item.platform === 'youtube' ? 'YouTube'
            : item.platform === 'xiaohongshu' ? '小红书'
            : item.platform
          return String(index + 1) + '. ' + platformLabel + '：' + item.url
        })
        const failedLines = failed.map((item, index) =>
          String(index + 1) + '. ' + item.url + '（' + item.reason + '）',
        )

        addMessage(conversationId, {
          role: 'assistant',
          content: [
            (collectionMeta.length > 0 ? collectionMeta.join('；') + '，' : '')
            + (submitted.length > 1
              ? '已并发提交 ' + submitted.length + ' 条批量内容洞察任务。'
              : '正在生成内容洞察。'),
            successLines.length > 0 ? '\n已提交：\n' + successLines.join('\n') : '',
            failedLines.length > 0 ? '\n提交失败：\n' + failedLines.join('\n') : '',
            '\n你可以在「分析历史」中查看每条视频的生成结果。',
            isCompareIntent
              ? '\n🔎 已识别「对比分析」意图：以上内容解析完成后，直接追问即可与洞察库中的历史分析做对比（卖点 / 人群 / 话术 / 差异点等）。'
              : '',
          ].filter(Boolean).join('\n'),
        })
      } else {
        const answer = await handleAgentChat(text)
        addMessage(conversationId, { role: 'assistant', content: answer })
      }
    } finally {
      setLoading(false)
      setStreamActive(false)
      streamAbortRef.current = null
    }
  }, [loading, conversationId, addMessage, updateMessage, targetModel, manualModel, addPendingTask, startPolling, handleAgentChat, premiumRouter])

  // ====== 渲染 AI 消息 ======
  const renderAIContent = useCallback((content: string, msgIndex: number) => {
    const isLastAI = (() => {
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === 'assistant') return i === msgIndex
      }
      return false
    })()

    return (
      <div className="relative pl-5 sm:pl-6">
        {/* 左侧 1px 刻度线 + 时间码刻度点：机器声部的「胶片尺」 */}
        <div aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-0 w-[7px]">
          <div className="absolute inset-y-0 left-0 w-px bg-[var(--vi-hairline)]" />
          <div
            className="absolute inset-y-0 left-0 w-[5px] opacity-50"
            style={{
              backgroundImage:
                'repeating-linear-gradient(to bottom, var(--vi-ink-3) 0, var(--vi-ink-3) 1px, transparent 1px, transparent 32px)',
            }}
          />
          <div className="absolute left-0 top-[9px] h-[3px] w-[3px] -translate-x-px rounded-full bg-[var(--vi-ink-3)]" />
        </div>

        {/* Markdown 内容 — 出版级简报：首行结论提振、正文 15px/1.8、段间距 16px */}
        <div className="max-w-none
          text-[15px] leading-[1.8] text-[var(--vi-ink-2)]
          [&_p]:mt-0 [&_p]:mb-4 [&_p]:text-[15px] [&_p]:leading-[1.8] [&_p]:text-[var(--vi-ink-2)]
          [&>p:first-of-type]:mb-4 [&>p:first-of-type]:text-[16px] [&>p:first-of-type]:font-semibold [&>p:first-of-type]:leading-[1.65] [&>p:first-of-type]:text-[var(--vi-ink)]
          [&_h1]:mb-3 [&_h1]:mt-7 [&_h1]:text-[20px] [&_h1]:font-semibold [&_h1]:leading-snug [&_h1]:tracking-tight [&_h1]:text-[var(--vi-ink)]
          [&_h2]:mb-2.5 [&_h2]:mt-6 [&_h2]:text-[17px] [&_h2]:font-semibold [&_h2]:leading-snug [&_h2]:tracking-tight [&_h2]:text-[var(--vi-ink)]
          [&_h3]:mb-2 [&_h3]:mt-5 [&_h3]:text-[15px] [&_h3]:font-semibold [&_h3]:text-[var(--vi-ink)]
          [&_h4]:mb-2 [&_h4]:mt-4 [&_h4]:text-[14px] [&_h4]:font-semibold [&_h4]:text-[var(--vi-ink)]
          [&_ul]:my-3 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-3 [&_ol]:list-decimal [&_ol]:pl-5
          [&_li]:mb-1.5 [&_li]:text-[15px] [&_li]:leading-[1.8] [&_li]:text-[var(--vi-ink-2)]
          [&_li>p]:mb-0
          [&_strong]:font-semibold [&_strong]:text-[var(--vi-ink)]
          [&_em]:text-[var(--vi-ink-2)]
          [&_a]:text-[var(--vi-ink)] [&_a]:underline [&_a]:decoration-[var(--vi-ink-3)] [&_a]:underline-offset-2
          [&_code]:rounded-[2px] [&_code]:border [&_code]:border-[var(--vi-hairline)] [&_code]:bg-[var(--vi-code-bg)] [&_code]:px-1 [&_code]:py-px [&_code]:font-mono [&_code]:text-[13px] [&_code]:text-[var(--vi-ink)]
          [&_pre]:my-4 [&_pre]:overflow-x-auto [&_pre]:rounded-[2px] [&_pre]:border [&_pre]:border-[var(--vi-hairline)] [&_pre]:bg-[var(--vi-code-bg)] [&_pre]:p-3
          [&_pre_code]:border-0 [&_pre_code]:bg-transparent [&_pre_code]:p-0
          [&_blockquote]:my-4 [&_blockquote]:border-l-[3px] [&_blockquote]:border-rec [&_blockquote]:bg-[var(--vi-code-bg)] [&_blockquote]:py-2 [&_blockquote]:pl-4 [&_blockquote]:pr-3 [&_blockquote]:not-italic [&_blockquote]:text-[14px] [&_blockquote]:font-medium [&_blockquote]:leading-[1.7] [&_blockquote]:text-[var(--vi-ink-2)]
          [&_blockquote_p]:mb-0 [&_blockquote_p]:text-[14px] [&_blockquote_p]:font-medium [&_blockquote_p]:leading-[1.7]
          [&_hr]:my-6 [&_hr]:border-[var(--vi-hairline)]
          [&_table]:my-4 [&_table]:w-full [&_table]:border-collapse [&_table]:text-[13px]
          [&_th]:border [&_th]:border-[var(--vi-hairline)] [&_th]:bg-[var(--vi-hover)] [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:text-left [&_th]:font-semibold [&_th]:text-[var(--vi-ink)]
          [&_td]:border [&_td]:border-[var(--vi-hairline)] [&_td]:px-2.5 [&_td]:py-1.5 [&_td]:leading-relaxed [&_td]:text-[var(--vi-ink-2)]
          [&_img]:my-4 [&_img]:max-w-full [&_img]:rounded-[2px]
          [&_.katex-display]:my-4
        ">
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[rehypeKatex]}
          >
            {preprocessLatex(content)}
          </ReactMarkdown>
        </div>

        {/* 来源引用 */}
        {isLastAI && <InlineSources sources={agentSources} />}

        {/* AI 声明：有来源标数量；无来源只做中性声明（重开后内存态来源会丢，
            不能断言"未引用"误报带来源的历史回答） */}
        {isLastAI && agentSources.length > 0 && (
          <p className="tc mt-2 text-[11px] text-[var(--vi-ink-3)]">AI 生成 · 附 {agentSources.length} 条引用来源</p>
        )}
        {isLastAI && agentSources.length === 0 && content.length > 50 && (
          <p className="tc mt-2 text-[11px] text-[var(--vi-ink-3)]">回答由 AI 生成</p>
        )}
      </div>
    )
  }, [messages, agentSources])

  // ====== Bubble 配置（双声部：人声便签 / 机器简报）======
  const roles = useMemo(() => ({
    user: {
      placement: 'end' as const,
      avatar: false as const,
      variant: 'borderless' as const,
      // 右上对齐的冷灰哑光便签：发丝线 + 微 mono 剪辑标记，不再是黑底大色块
      className: 'max-w-[680px]',
      // 抵掉 Bubble.List 给 end 角色预留的 15% 左缩进，便签贴右缘
      style: { paddingInlineStart: 0 },
      styles: {
        content: {
          background: 'var(--vi-hover)',
          border: '1px solid var(--vi-hairline)',
          borderRadius: '2px',
          padding: '9px 12px',
          color: 'var(--vi-ink)',
          minHeight: 0,
        },
      },
      contentRender: (content: any) => (
        <div className="flex flex-col items-start gap-1">
          <span className="tc text-[10px] uppercase leading-none tracking-[0.18em] text-[var(--vi-ink-3)]">
            TC·IN
          </span>
          <div className="whitespace-pre-wrap text-[14px] leading-[1.6] text-[var(--vi-ink)]">
            {String(content ?? '')}
          </div>
        </div>
      ),
    },
    ai: {
      placement: 'start' as const,
      avatar: false as const,
      variant: 'borderless' as const,
      // AI 消息不套气泡：整幅宽度交给「出版级简报」容器（左刻度线由 renderAIContent 提供）
      className: 'w-full',
      // 抵掉 Bubble.List 给 start 角色预留的 15% 右缩进，让简报通栏排版
      style: { paddingInlineEnd: 0 },
      styles: { content: { padding: 0, minHeight: 0, background: 'transparent' } },
    },
  }), [])

  // ====== Bubble items ======
  const bubbleItems = useMemo(() => {
    const items = messages.map((msg, i) => ({
      key: `msg-${i}`,
      role: msg.role === 'user' ? ('user' as const) : ('ai' as const),
      content: msg.role === 'assistant' ? renderAIContent(msg.content, i) : msg.content,
    }))

    if (loading) {
      items.push({
        key: 'loading',
        role: 'ai' as const,
        content: (
          <div className="w-full max-w-sm rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-3">
            <div className="flex items-center gap-3">
              <div className="h-10 w-0.5 rounded-full bg-[#E5484D]" />
              <div className="flex-1 space-y-2 py-0.5">
                <div className="h-2.5 w-[40%] animate-pulse rounded bg-[var(--vi-hairline)]" />
                <div className="h-2.5 w-[75%] animate-pulse rounded bg-[var(--vi-hairline)]" />
                <div className="h-2.5 w-[60%] animate-pulse rounded bg-[var(--vi-hairline)]" />
              </div>
            </div>
          </div>
        ),
      } as any)
    }

    return items
  }, [messages, loading, renderAIContent])

  const placeholderText = kbStats && kbStats.total_notes > 0
    ? `向 ${kbStats.total_notes} 条内容分析记录提问，或粘贴视频链接生成新洞察…`
    : '粘贴视频链接生成结构化笔记，或输入任何问题…'

  return (
    <div className="flex h-full flex-col bg-[var(--vi-bg)]">
      {/* 消息区域（加 relative 以便羽化层定位） */}
      <div className="relative flex-1 min-h-0">
        <div ref={scrollRef} className="h-full overflow-y-auto px-4 py-4">
          <div className="mx-auto max-w-4xl">
            <KbStatsBar stats={kbStats} />

            {/* Agent 可观测性：把后台一直在写的 traces 摊开成可回放的轨迹 */}
            <div className="mb-3">
              <AgentTracePanel />
            </div>

          {messages.length === 0 && !loading && taskProgresses.length === 0 ? (
            /* ====== 空状态：非对称杂志刊头 ====== */
            <div className="py-10">
              <div className="grid grid-cols-1 gap-8 xl:grid-cols-[minmax(0,1fr)_248px] xl:gap-12">
                {/* 左：权威刊头 */}
                <div className="min-w-0 border-b border-[var(--vi-hairline)] pb-7">
                  <p className="tc mb-4 text-[11px] uppercase tracking-[0.3em] text-[var(--vi-ink-3)]">
                    VidInsight · Insight Studio
                  </p>
                  <h2
                    className="max-w-[15em] font-serif text-[36px] font-bold leading-[1.28] tracking-tight text-[var(--vi-ink)]"
                    style={{ fontFamily: '"Noto Serif SC", "Source Han Serif SC", serif' }}
                  >
                    把一条视频，<br className="hidden sm:block" />读成一份可追问的洞察。
                  </h2>
                  <p className="mt-4 max-w-xl text-sm leading-relaxed text-[var(--vi-ink-2)]">
                    粘贴视频链接生成结构化分析；或对已索引的内容直接提问——每个结论都带来源编号。
                  </p>
                  {/* Mono 读数器：静态占位，不新增 store 读取 */}
                  <p className="tc mt-6 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--vi-ink-3)]">
                    <span className="h-1.5 w-1.5 rounded-full bg-[var(--vi-ink-3)]" />
                    <span>今日已解析 0 分钟</span>
                    <span aria-hidden="true">·</span>
                    <span>累计 {kbStats?.total_notes ?? 0} 条洞察</span>
                    <span aria-hidden="true">·</span>
                    <span>来源可追溯</span>
                  </p>
                </div>

                {/* 右：副栏索引（纯装饰性版式锚点） */}
                <aside className="hidden xl:block">
                  <div className="border-t-2 border-[var(--vi-ink)] pt-3">
                    <p className="tc text-[10px] uppercase tracking-[0.3em] text-[var(--vi-ink-3)]">Issue 01</p>
                    <p className="tc mt-2 text-[11px] leading-relaxed text-[var(--vi-ink-2)]">
                      四张胶片索引卡<br />任选一条开始<br />也可直接粘贴链接
                    </p>
                    <div className="mt-5 h-px w-full bg-[var(--vi-hairline)]" />
                    <p className="tc mt-3 text-[10px] leading-relaxed text-[var(--vi-ink-3)]">
                      ENTER 发送<br />SHIFT + ENTER 换行
                    </p>
                  </div>
                </aside>
              </div>

              {/* 胶片索引卡：mono 编号 + hover 灰底平移 + 左缘 REC 指示条 */}
              <div className="mt-8 grid grid-cols-1 gap-3 sm:grid-cols-2">
                {[
                  { no: '01', text: '这批视频最常强调哪些要点？', hint: '跨视频对比 · 引用来源编号' },
                  { no: '02', text: '用户最关心的痛点是什么？', hint: '基于已索引内容检索' },
                  { no: '03', text: '帮我生成 5 个下周可拍摄的短视频选题。', hint: '从洞察到行动' },
                  { no: '04', text: '哪些话术适合参考但不能直接照搬？', hint: '话术复盘与改写' },
                ].map(item => (
                  <button key={item.no} onClick={() => handleSend(item.text)}
                    className="group relative flex items-start gap-4 overflow-hidden rounded-sm border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-4 py-4 text-left transition-all duration-200 hover:-translate-y-px hover:bg-[var(--vi-hover)] hover:shadow-[0_6px_18px_-8px_rgba(23,25,30,0.18)]"
                  >
                    <span
                      aria-hidden="true"
                      className="absolute left-0 top-0 h-full w-[2px] bg-[#E5484D] opacity-0 transition-opacity duration-200 group-hover:opacity-100"
                    />
                    <span className="tc mt-0.5 shrink-0 text-[11px] font-medium text-[var(--vi-ink-3)] transition-colors group-hover:text-[#E5484D]">
                      {item.no}//
                    </span>
                    <div className="min-w-0">
                      <p className="text-sm leading-snug text-[var(--vi-ink)]">{item.text}</p>
                      <p className="tc mt-1.5 text-[10px] uppercase tracking-[0.12em] text-[var(--vi-ink-3)]">{item.hint}</p>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              <Bubble.List items={bubbleItems} role={roles} style={{ minHeight: '100%' }} />

              <AgentThinking
                toolCalls={agentToolCalls}
                thinkingText={streamThinking}
                streaming={streamActive && loading}
                liveToolEvents={streamToolEvents}
              />

              {taskProgresses.length > 0 && (
                <div className="ml-12 max-w-[85%] space-y-2">
                  {taskProgresses.map(task => (
                    <TaskProgressCard
                      key={task.taskId}
                      taskStatus={task.status}
                      taskTitle={task.title}
                      taskId={task.taskId}
                      progress={task.progress}
                      elapsed_s={task.elapsed_s}
                      stage_times={task.stage_times}
                      failMsg={task.failMsg}
                      message={task.message}
                      onViewDetail={() => navigate(`/history/${task.taskId}`)}
                      onStop={handleStopTask}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      {/* 滚动区底部渐变羽化（位于输入框上方，pointer-events-none 避免遮挡交互） */}
      <div
        className="pointer-events-none absolute bottom-0 left-0 right-0 h-12 z-10"
        style={{ background: 'linear-gradient(to bottom, transparent, var(--vi-bg))' }}
      />
    </div>

      {/* 输入区域：悬浮 Command Deck（视觉悬浮，仍在文档流内，不遮挡消息） */}
      <div className="relative bg-[var(--vi-bg)] px-4 pb-5 pt-2">
        <div className="mx-auto w-full max-w-[880px]">
          <div className="rounded-2xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-3 pb-2 pt-2.5 shadow-[0_12px_32px_-4px_rgba(23,25,30,0.08)] transition-shadow duration-200 focus-within:border-[var(--vi-ink-3)] focus-within:shadow-[0_16px_40px_-6px_rgba(23,25,30,0.14)]">
            {/* 顶部 Mono 状态行（纯展示：运行态 / 意图 / 平台） */}
            <div className="tc mb-2 flex items-center gap-2 px-1 text-[10px] uppercase tracking-[0.18em] text-[var(--vi-ink-3)]">
              <span
                className={
                  'h-1.5 w-1.5 shrink-0 rounded-full ' +
                  (loading ? 'animate-pulse bg-[#E5484D]' : 'bg-[#0E7C66]')
                }
              />
              <span className="shrink-0">{loading ? 'RUNNING' : 'READY'}</span>
              <span aria-hidden="true" className="shrink-0 opacity-50">/</span>
              <span className="shrink-0">{detectedLinks.length > 0 ? '解析模式' : premiumRouter ? '智能路由' : '问答模式'}</span>
              <span aria-hidden="true" className="ml-auto shrink-0 opacity-50">/</span>
              <span className="hidden truncate sm:inline">{detectedPlatformStatus}</span>

              {/* 高阶意图路由开关（Premium / T7）— 行为不变，仅换色板 */}
              <button
                onClick={togglePremiumRouter}
                title="开启后每次发送先由轻量 LLM 判定意图（解析 / 对比 / 问答 / 闲聊），判定失败自动回退机械规则"
                className={
                  'ml-1 inline-flex shrink-0 items-center gap-1 rounded-sm border px-2 py-0.5 text-[10px] normal-case tracking-normal transition-colors ' +
                  (premiumRouter
                    ? 'border-[var(--vi-ink)] bg-[var(--vi-ink)] text-[var(--vi-bg)] hover:bg-[#2A2F36]'
                    : 'border-[var(--vi-hairline)] text-[var(--vi-ink-3)] hover:border-[var(--vi-ink-3)] hover:text-[var(--vi-ink-2)]')
                }
              >
                {routingIntent
                  ? <Loader2 className="h-3 w-3 animate-spin" />
                  : <Zap className="h-3 w-3" />}
                高阶意图路由{premiumRouter ? ' · 已开启' : ''}
              </button>
            </div>

            {/* 链接检测提示（纯展示） */}
            {linkHint && (
              <div className="tc mb-2 flex items-center gap-1.5 px-1 text-[10px] tracking-[0.04em] text-[var(--vi-ink-2)]">
                <span className="h-1 w-1 shrink-0 rounded-full bg-[#E5484D]" />
                {linkHint}
              </div>
            )}

            {/* Sender 输入框 — 逻辑不变，仅外层容器与样式类
                确保 textarea/contentEditable 充满宽度，防止文字过早换行；
                发送键重绘为黑主键实体感（禁用态保持灰） */}
            <div className="
              [&_.ant-sender-main]:!border-0 [&_.ant-sender-main]:!bg-transparent [&_.ant-sender-main]:!shadow-none
              [&_.ant-sender-content]:!w-full [&_.ant-sender-content]:!px-0.5 [&_.ant-sender-content]:!py-0.5
              [&_textarea]:!w-full [&_textarea]:!min-h-[44px] [&_textarea]:!leading-relaxed
              [&_textarea]:!text-sm [&_textarea]:!whitespace-pre-wrap [&_textarea]:!break-words
              [&_[contenteditable]]:!min-h-[44px] [&_[contenteditable]]:!leading-relaxed
              [&_[contenteditable]]:!text-sm [&_[contenteditable]]:!whitespace-pre-wrap
              [&_.ant-sender-actions-btn:enabled]:!h-9 [&_.ant-sender-actions-btn:enabled]:!w-9
              [&_.ant-sender-actions-btn:enabled]:!bg-[var(--vi-ink)]
              [&_.ant-sender-actions-btn:enabled]:!text-[var(--vi-bg)]
              [&_.ant-sender-actions-btn:enabled]:!shadow-[inset_0_1px_0_rgba(255,255,255,0.12),0_2px_0_rgba(0,0,0,0.16)]
              [&_.ant-sender-actions-btn:enabled]:hover:!bg-[#2A2F36]
              [&_.ant-sender-actions-btn:enabled]:hover:!text-white
              dark:[&_.ant-sender-actions-btn:enabled]:hover:!bg-[#F0F1F4]
              dark:[&_.ant-sender-actions-btn:enabled]:hover:!text-[#1F2328]
              [&_.ant-sender-actions-btn-disabled]:!bg-[var(--vi-hairline)] [&_.ant-sender-actions-btn-disabled]:!text-[var(--vi-ink-3)]
            ">
              <Sender
                value={input}
                onChange={setInput}
                onSubmit={handleSend}
                onCancel={handleGlobalStop}
                loading={loading}
                placeholder={placeholderText}
              />
            </div>

            {/* 底部：模型选择器 + 快捷键提示 */}
            <div className="mt-1 flex items-center gap-2 px-1 text-[11px] text-[var(--vi-ink-3)]">
              <span className="shrink-0">当前模型</span>
              <ModelPicker
                modelList={modelList}
                value={manualModel ?? (targetModel ? {
                  provider_id: targetModel.provider_id,
                  model_name: targetModel.model_name,
                  label: `${targetModel.provider_id}/${targetModel.model_name}`,
                } : null)}
                onChange={setManualModel}
              />
              <span className="tc ml-auto hidden shrink-0 items-center gap-1 text-[10px] tracking-[0.08em] text-[var(--vi-ink-3)] sm:flex">
                <kbd className="rounded-sm border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] px-1.5 py-0.5">ENTER</kbd>
                发送
                <span aria-hidden="true" className="px-0.5 opacity-50">·</span>
                <kbd className="rounded-sm border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] px-1.5 py-0.5">SHIFT+ENTER</kbd>
                换行
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
