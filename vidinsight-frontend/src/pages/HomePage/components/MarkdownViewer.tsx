import { useState, useEffect, useRef, useMemo, memo, FC } from 'react'
import type { ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import { Button } from '@/components/ui/button.tsx'
import { Copy, Scissors, ExternalLink } from 'lucide-react'
import { toast } from 'react-hot-toast'
import Error from '@/components/Lottie/error.tsx'
import Loading from '@/components/Lottie/Loading.tsx'
import Idle from '@/components/Lottie/Idle.tsx'
import StepBar from '@/pages/HomePage/components/StepBar.tsx'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { atomDark as codeStyle } from 'react-syntax-highlighter/dist/esm/styles/prism'
import Zoom from 'react-medium-image-zoom'
import 'react-medium-image-zoom/dist/styles.css'
import gfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeSlug from 'rehype-slug'
import 'katex/dist/katex.min.css'
import 'github-markdown-css/github-markdown-light.css'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { useTaskStore } from '@/store/taskStore'
import { noteStyles } from '@/constant/note.ts'
import { MarkdownHeader, ReportMasthead } from '@/pages/HomePage/components/MarkdownHeader.tsx'
import TranscriptViewer from '@/pages/HomePage/components/transcriptViewer.tsx'
import MarkmapEditor from '@/pages/HomePage/components/MarkmapComponent.tsx'
import ChatPanel from '@/pages/HomePage/components/ChatPanel.tsx'
import VideoBanner from '@/pages/HomePage/components/VideoBanner.tsx'
import { resolveAssetUrl } from '@/utils/image-url.ts'

interface VersionNote {
  ver_id: string
  content: string
  style: string
  model_name: string
  created_at?: string
}

interface MarkdownViewerProps {
  content: string | VersionNote[]
  status: 'idle' | 'loading' | 'success' | 'failed'
}

const steps = [
  { label: '解析链接', key: 'PARSING' },
  { label: '下载音频', key: 'DOWNLOADING' },
  { label: '转写文字', key: 'TRANSCRIBING' },
  { label: '总结内容', key: 'SUMMARIZING' },
  { label: '保存完成', key: 'SUCCESS' },
]

const remarkPlugins = [gfm, remarkMath]
const rehypePlugins = [rehypeKatex, rehypeSlug]

// ═══════════════════════════════════════════════════
// TOC 自动生成：统一由前端提取 ##/### 标题生成一个普通目录。
// 体验面③：同一份提取结果同时供给「文内目录」与「右侧胶卷目录」，
// 保证两处标题数据永远一致，不重复解析。
// ═══════════════════════════════════════════════════

interface TocEntry {
  /** 2 或 3 */
  level: number
  /** 文内目录展示原文（保持既有输出，不做改动） */
  text: string
  /** 侧栏展示用：去掉链接语法与末尾时间码，更适合 200px 窄栏 */
  label: string
  /** H2 章节序号（01/02…）；H3 为 '·' */
  displayNo: string
  /** 归一化 key：用于与渲染后的 DOM 标题精确对应 */
  key: string
  /** 在目录中的下标（同时写入 DOM 的 data-vi-toc） */
  index: number
}

function stripExistingToc(markdown: string): string {
  return markdown.replace(
    /^#{2,3}\s+(?:目录|Table\s*of\s*Contents|目次).*?(?=^#{2,3}\s)/ms,
    '',
  )
}

/** 标题文本归一化：抹平链接/加粗/编号/空白差异，供侧栏与 DOM 对齐 */
function normalizeHeadingKey(input: string): string {
  return input
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // markdown 链接 → 链接文字
    .replace(/\*?Content-(?:\[[\d:]+\]|[\d:]+)\*?/gi, '') // 原始时间码标记（含强调语法残留）
    .replace(/原片\s*@\s*\d{2}:\d{2}/g, '') // 渲染后的时间码链接文字
    .replace(/[*`_~]/g, '')
    .replace(/^\s*\d+(?:[.、]\d+)*[.、]?\s*/, '') // 去掉前导编号 "1. " "1.1 "
    .replace(/\s+/g, '')
    .toLowerCase()
}

/** 把标题原文整理成侧栏可读的短标签 */
function toTocLabel(text: string): string {
  const label = text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\*?Content-(?:\[[\d:]+\]|[\d:]+)\*?/gi, '') // 时间码标记不进侧栏
    .replace(/\s*原片\s*@\s*\d{2}:\d{2}\s*$/, '') // 章节标题末尾的时间码不占侧栏
    .replace(/\s+/g, ' ')
    .trim()
  return label || text
}

/** 从 Markdown 内容提取 ##/### 标题（唯一的解析入口） */
function extractTocEntries(markdown: string): TocEntry[] {
  const headingRe = /^(#{2,3})\s+(.+?)(?:\s*\*Content-\[[\d:]+\])?\s*$/gm
  const entries: TocEntry[] = []
  let h2Seq = 0
  let match: RegExpExecArray | null

  while ((match = headingRe.exec(markdown)) !== null) {
    const level = match[1].length // 2 or 3
    const rawText = match[2].trim()
    // 跳过目录标题本身
    if (/^目录|^Table\s*of\s*Contents|^目次|^AI\s*总结/i.test(rawText)) continue
    const cleanText = rawText
      .replace(/^[\d.]+\s*/, '') // 去掉前导编号 "1. " "1.1 "
      .replace(/\*{1,2}/g, '') // 去掉 markdown 加粗标记
      .trim()
    if (!cleanText || cleanText.length < 1) continue

    if (level === 2) h2Seq += 1
    entries.push({
      level,
      text: cleanText,
      label: toTocLabel(cleanText),
      displayNo: level === 2 ? String(h2Seq).padStart(2, '0') : '·',
      key: normalizeHeadingKey(cleanText),
      index: entries.length,
    })
  }

  return entries
}

/** 从 Markdown 内容提取 ##/### 标题，生成普通文本目录 */
function buildTocFromHeadings(markdown: string): string | null {
  const entries = extractTocEntries(markdown)

  if (entries.length < 2) return null // 太少的标题不生成目录

  const lines = ['## 目录', '']
  for (const e of entries) {
    const prefix = e.level === 3 ? '  -' : '-'
    // 2026-10-20 目录可点击：锚点用目录下标（vi-toc-N），与正文 h2/h3 渲染时
    // 写入的 id 一一对应（中文标题做 slug 有编码/重复问题，下标最稳）；
    // 标题里的 [ ] 转义防 markdown 链接语法被破坏
    const safe = e.text.replace(/([[\]])/g, m => (m === '[' ? '［' : '］'))
    lines.push(`${prefix} [${safe}](#vi-toc-${e.index})`)
  }
  lines.push('', '---', '')
  return lines.join('\n')
}

/** 注入或替换 TOC：如果已有 LLM 生成的目录段则替换，否则在第一个 ## 前插入 */
function injectToc(markdown: string): string {
  const withoutToc = stripExistingToc(markdown)
  const toc = buildTocFromHeadings(withoutToc)
  if (!toc) return withoutToc

  // 没有目录段 → 在第一个 ## 标题前插入
  const firstHeadingRe = /^#{2,3}\s/m
  const match = firstHeadingRe.exec(withoutToc)
  if (match) {
    const pos = match.index
    return withoutToc.slice(0, pos) + toc + withoutToc.slice(pos)
  }

  return withoutToc
}

// ═══════════════════════════════════════════════════
// 体验面③：报告封头标题 / 章节编号 / 时间码工具
// ═══════════════════════════════════════════════════

/** 从正文首个 H1 提取报告标题（后端固定输出「# 内容洞察报告：标题」） */
function extractReportTitle(markdown: string): string {
  const m = markdown.match(/^\s*#\s+([^\n]+)/m)
  if (!m) return ''
  return m[1]
    .replace(/^内容洞察报告[：:]\s*/, '')
    .replace(/[*`]/g, '')
    .trim()
}

/** 标题已提升为封头大标题，正文里不再重复渲染同一个 H1 */
function stripReportTitle(markdown: string): string {
  if (!extractReportTitle(markdown)) return markdown
  // 只剥掉第一个 H1 行（即报告标题），其余正文原样保留
  return markdown.replace(/^[ \t]*#[ \t]+[^\n]*\n+/m, '')
}

/**
 * audioMeta.duration 在各下载器里单位并不统一：
 * 抖音 / 快手返回毫秒，其余平台返回秒（见 backend/app/downloaders）。
 * 这里只做单位归一化，不改数据本身。
 */
const DURATION_MS_PLATFORMS = new Set(['douyin', 'kuaishou'])

function normalizeDurationSeconds(duration?: number, platform?: string): number {
  if (!duration || duration <= 0) return 0
  return DURATION_MS_PLATFORMS.has(platform || '') ? duration / 1000 : duration
}

/** 取出标题渲染内容里的纯文本（用于和目录条目对齐） */
function flattenText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(flattenText).join('')
  if (typeof node === 'object' && 'props' in (node as any)) {
    return flattenText((node as any).props?.children)
  }
  return ''
}

/** 去掉标题开头的 "1. " 编号，保留其余子节点（如原片时间码链接） */
function stripHeadingNumber(children: ReactNode): ReactNode {
  const numRe = /^\s*\d+(?:[.、]\d+)*[.、]?\s+/
  if (typeof children === 'string') return children.replace(numRe, '')
  if (Array.isArray(children) && typeof children[0] === 'string') {
    return [children[0].replace(numRe, ''), ...children.slice(1)]
  }
  return children
}

/**
 * 在 Radix ScrollArea 内滚动到目标元素。
 * Radix 使用内部 viewport 管理滚动，原生 scrollIntoView 不生效。
 */
function scrollTargetIntoView(target: Element) {
  const viewport = target.closest('[data-radix-scroll-area-viewport]') as HTMLElement | null
  if (viewport) {
    const targetTop = target.getBoundingClientRect().top
    const viewportTop = viewport.getBoundingClientRect().top
    const offset = viewport.scrollTop + (targetTop - viewportTop) - 80
    viewport.scrollTo({ top: offset, behavior: 'smooth' })
  } else {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

interface TocLookup {
  /** 归一化标题 → 目录条目（下标、序号都从这里取） */
  keyToEntry: Map<string, TocEntry>
  /** 标题里带时间码等附加内容时的前缀兜底匹配 */
  entries: TocEntry[]
}

/**
 * 构建 ReactMarkdown components 对象，baseURL 用于修正图片路径。
 * 使用函数 + useMemo 避免每次渲染都创建新的函数实例。
 * tocLookup 让标题带上 data-vi-toc，供右侧胶卷目录定位。
 */
function createMarkdownComponents(tocLookup: TocLookup) {
  const { keyToEntry, entries } = tocLookup

  const tocEntryOf = (children: ReactNode): TocEntry | undefined => {
    const key = normalizeHeadingKey(flattenText(children))
    const exact = keyToEntry.get(key)
    if (exact) return exact
    // 标题末尾挂了时间码/补充说明时，用前缀做兜底
    return entries.find(
      e => e.key.length > 1 && (key.startsWith(e.key) || e.key.startsWith(key)),
    )
  }

  return {
    h1: ({ children, node, ...props }: any) => (
      <h1
        className="my-6! scroll-m-20 font-serif text-[24px]! font-semibold! tracking-tight text-[var(--vi-ink)]"
        style={{ fontFamily: '"Noto Serif SC", "Source Han Serif SC", serif' }}
        {...props}
      >
        {children}
      </h1>
    ),
    h2: ({ children, node, ...props }: any) => {
      const entry = tocEntryOf(children)
      const text = flattenText(children)
      // 文内目录标题不占章节序号，压成一行 mono 索引标
      if (/^目录$/.test(text.trim())) {
        return (
          <h2 className="mt-6! mb-3! scroll-m-20 border-b-0! pb-0! first:mt-0!" {...props}>
            <span className="tc flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.28em] text-[var(--vi-ink-3)]">
              INDEX
              <span className="h-px flex-1 bg-[var(--vi-hairline)]" />
            </span>
          </h2>
        )
      }
      // H2 序号：优先用标题自带编号，其次用目录里的章节顺序
      const ownNumber = (text.match(/^\s*(\d{1,2})(?:[.、]\s|\s)/) || [])[1]
      const no = ownNumber?.padStart(2, '0') ?? entry?.displayNo ?? ''

      return (
        <h2
          className="mt-12! mb-5! scroll-m-20 border-b-0! pb-0! first:mt-0!"
          {...props}
          {...(entry ? { 'data-vi-toc': String(entry.index), id: `vi-toc-${entry.index}` } : {})}
        >
          <span className="flex items-baseline gap-3 border-b border-[var(--vi-hairline)] pb-2">
            {no && (
              <span className="tc shrink-0 text-[11px] font-medium tracking-[0.12em] text-[var(--vi-ink-3)]">
                {no} /
              </span>
            )}
            <span className="text-[22px]! font-semibold! leading-[1.35] tracking-[-0.01em] text-[var(--vi-ink)]">
              {stripHeadingNumber(children)}
            </span>
          </span>
        </h2>
      )
    },
    h3: ({ children, node, ...props }: any) => {
      const entry = tocEntryOf(children)
      return (
        <h3
          className="mt-7! mb-2! scroll-m-20 text-[15px]! font-semibold! leading-[1.5] tracking-[0.01em] text-[var(--vi-ink)]"
          {...props}
          {...(entry ? { 'data-vi-toc': String(entry.index), id: `vi-toc-${entry.index}` } : {})}
        >
          {/* H3 保留原有 1.1 / 1.2 层级编号，只做紧凑粗体处理 */}
          {children}
        </h3>
      )
    },
    h4: ({ children, ...props }: any) => (
      <h4
        className="text-[var(--vi-ink)] mt-6 mb-2 scroll-m-20 text-lg font-semibold tracking-tight"
        {...props}
      >
        {children}
      </h4>
    ),
    p: ({ children, ...props }: any) => (
      <p className="leading-7 [&:not(:first-child)]:mt-6" {...props}>
        {children}
      </p>
    ),
    a: ({ href, children, node, ...props }: any) => {
      const isOriginLink =
        typeof children[0] === 'string' &&
        (children[0] as string).startsWith('原片 @')

      if (isOriginLink) {
        const timeMatch = (children[0] as string).match(/原片 @ (\d{2}:\d{2})/)
        const timeText = timeMatch ? timeMatch[1] : '原片'

        // 体验面③：录音带样式的行内时间码微组件（极窄 mono + 剪切标）
        return (
          <span className="origin-link mx-0.5 inline-flex">
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              title="在原片对应时间点打开"
              className="tc inline-flex! items-center gap-1 rounded-[2px] border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)]! px-1.5 py-[1px] text-[10px] font-medium leading-[1.6] tracking-[0.04em] text-[var(--vi-ink-2)]! no-underline! transition-[translate,background-color,border-color,color] duration-150 ease-out hover:-translate-y-px hover:border-[var(--vi-ink-3)] hover:bg-[var(--vi-hover)]! hover:text-[var(--vi-ink)]! hover:no-underline"
              {...props}
            >
              <Scissors className="h-2.5 w-2.5 shrink-0 opacity-70" aria-hidden />
              <span className="text-[var(--vi-ink-3)]">TC</span>
              <span className="tabular-nums">{timeText}</span>
            </a>
          </span>
        )
      }

      // 处理笔记内部锚点链接（如目录跳转）
      if (href?.startsWith('#')) {
        const handleAnchorClick = (e: React.MouseEvent) => {
          e.preventDefault()
          const id = decodeURIComponent(href.slice(1))

          // 1. 优先精确匹配 id
          let target = document.getElementById(id)

          // 2. 精确失败时按 heading 文本模糊匹配
          if (!target) {
            const normalize = (s: string) =>
              s.replace(/[-：:\s*\[\]]/g, '').toLowerCase()
            const search = normalize(id)
            const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6')
            for (const h of headings) {
              const text = h.textContent || ''
              if (normalize(text).includes(search) || search.includes(normalize(text))) {
                target = h
                break
              }
            }
          }

          if (target) {
            // Radix ScrollArea 使用内部 viewport 管理滚动，
            // 原生的 scrollIntoView 不会生效。
            scrollTargetIntoView(target)
          } else {
            toast.error('未找到对应章节')
          }
        }

        // 目录项（vi-toc- 锚点）：整行可点、默认无下划线，悬停高亮——像真文档目录
        const isTocEntry = href.startsWith('#vi-toc-')
        return (
          <a
            href={href}
            onClick={handleAnchorClick}
            className={isTocEntry
              ? 'rounded-[3px] px-1 -mx-1 py-px font-medium text-[var(--vi-ink-2)]! no-underline! transition-colors duration-150 hover:bg-[var(--vi-hover)] hover:text-[var(--vi-ink)]!'
              : 'inline-flex! items-center gap-0.5 font-medium text-[var(--vi-ink)]! underline! decoration-[var(--vi-ink-3)] underline-offset-2 hover:decoration-[var(--vi-ink)]'}
            {...props}
          >
            {children}
          </a>
        )
      }

      return (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex! items-center gap-0.5 font-medium text-[var(--vi-ink)]! underline! decoration-[var(--vi-ink-3)] underline-offset-2 hover:decoration-[var(--vi-ink)]"
          {...props}
        >
          {children}
          {href?.startsWith('http') && (
            <ExternalLink className="ml-0.5 inline-block h-3 w-3" />
          )}
        </a>
      )
    },
    img: ({ node, ...props }: any) => {
      const src = resolveAssetUrl(props.src)

      return (
        <div className="my-8 flex justify-center">
          <Zoom>
            <img
              {...props}
              src={src}
              loading="lazy"
              decoding="async"
              onError={(event) => {
                event.currentTarget.style.display = 'none'
              }}
              className="max-w-full cursor-zoom-in rounded-lg object-cover shadow-md transition-all hover:shadow-lg"
              style={{ maxHeight: '500px' }}
            />
          </Zoom>
        </div>
      )
    },
    strong: ({ children, ...props }: any) => (
      <strong className="font-semibold text-[var(--vi-ink)]" {...props}>
        {children}
      </strong>
    ),
    li: ({ children, ...props }: any) => {
      const rawText = String(children)
      const isFakeHeading = /^(\*\*.+\*\*)$/.test(rawText.trim())

      if (isFakeHeading) {
        return (
          <div className="my-4 text-lg font-bold text-[var(--vi-ink)]">{children}</div>
        )
      }

      return (
        <li className="my-1" {...props}>
          {children}
        </li>
      )
    },
    ul: ({ children, ...props }: any) => (
      <ul className="my-6 ml-6 list-disc [&>li]:mt-2" {...props}>
        {children}
      </ul>
    ),
    ol: ({ children, ...props }: any) => (
      <ol className="my-6 ml-6 list-decimal [&>li]:mt-2" {...props}>
        {children}
      </ol>
    ),
    blockquote: ({ children, ...props }: any) => (
      // 体验面③：编辑部摘录 —— 3px 录制红竖线 + 冷灰底 + 斜体 500
      <blockquote
        className="my-7! rounded-r-[2px] border-l-[3px]! border-l-rec! bg-[var(--vi-code-bg)] py-3! pr-4! pl-4! text-[14.5px] font-medium italic leading-[1.75] text-[var(--vi-ink-2)]!"
        {...props}
      >
        {children}
      </blockquote>
    ),
    code: ({ inline, className, children, ...props }: any) => {
      const match = /language-(\w+)/.exec(className || '')
      const codeContent = String(children).replace(/\n$/, '')

      if (!inline && match) {
        return (
          <div className="group bg-muted relative my-6 overflow-hidden rounded-lg border shadow-sm">
            <div className="bg-muted text-muted-foreground flex items-center justify-between px-4 py-1.5 text-sm font-medium">
              <div>{match[1].toUpperCase()}</div>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(codeContent)
                  toast.success('代码已复制')
                }}
                className="bg-background/80 hover:bg-background flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors"
              >
                <Copy className="h-3.5 w-3.5" />
                复制
              </button>
            </div>
            <SyntaxHighlighter
              style={codeStyle}
              language={match[1]}
              PreTag="div"
              className="!bg-muted !m-0 !p-0"
              customStyle={{
                margin: 0,
                padding: '1rem',
                background: 'transparent',
                fontSize: '0.9rem',
              }}
              {...props}
            >
              {codeContent}
            </SyntaxHighlighter>
          </div>
        )
      }

      return (
        <code
          className="bg-muted relative rounded px-[0.3rem] py-[0.2rem] font-mono text-sm"
          {...props}
        >
          {children}
        </code>
      )
    },
    table: ({ children, ...props }: any) => (
      <div className="my-6 w-full overflow-y-auto">
        <table className="w-full border-collapse text-sm" {...props}>
          {children}
        </table>
      </div>
    ),
    th: ({ children, ...props }: any) => (
      <th
        className="border-muted-foreground/20 border px-4 py-2 text-left font-medium [&[align=center]]:text-center [&[align=right]]:text-right"
        {...props}
      >
        {children}
      </th>
    ),
    td: ({ children, ...props }: any) => (
      <td
        className="border-muted-foreground/20 border px-4 py-2 text-left [&[align=center]]:text-center [&[align=right]]:text-right"
        {...props}
      >
        {children}
      </td>
    ),
    hr: ({ ...props }: any) => (
      <hr
        className="my-8! h-px! border-0! border-t border-t-[var(--vi-hairline)]! bg-[var(--vi-hairline)]!"
        {...props}
      />
    ),
  }
}

// ═══════════════════════════════════════════════════
// 体验面③：右侧常驻胶卷目录（Filmstrip TOC）
// 只在 xl: 断点出现；标题数据来自同一份 extractTocEntries 结果。
// ═══════════════════════════════════════════════════

interface FilmstripTocProps {
  entries: TocEntry[]
  activeIndex: number
  onNavigate: (entry: TocEntry) => void
}

function FilmstripToc({ entries, activeIndex, onNavigate }: FilmstripTocProps) {
  if (entries.length < 2) return null

  return (
    <nav
      aria-label="章节胶卷目录"
      className="hidden w-[200px] shrink-0 flex-col border-l border-[var(--vi-hairline)] xl:flex"
    >
      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <span className="tc text-[9px] uppercase tracking-[0.28em] text-[var(--vi-ink-3)]">
          FILMSTRIP
        </span>
        <span className="tc text-[9px] tabular-nums text-[var(--vi-ink-3)]">
          {String(entries.length).padStart(2, '0')} SHOTS
        </span>
      </div>

      <div className="relative min-h-0 flex-1 overflow-y-auto pb-6">
        {/* 贯穿刻度主轴 */}
        <span
          aria-hidden
          className="pointer-events-none absolute bottom-6 left-[22px] top-0 w-px bg-[var(--vi-hairline)]"
        />
        <ul className="relative">
          {entries.map(entry => {
            const active = entry.index === activeIndex
            return (
              <li key={`${entry.index}-${entry.key}`}>
                <button
                  type="button"
                  onClick={() => onNavigate(entry)}
                  title={entry.label}
                  aria-current={active ? 'true' : undefined}
                  className={`group flex w-full items-start gap-2 py-1.5 pr-3 text-left transition-colors duration-150 ${
                    entry.level === 3 ? 'pl-9' : 'pl-4'
                  } ${active ? 'bg-[var(--vi-hover)]' : 'hover:bg-[var(--vi-hover)]/60'}`}
                >
                  {/* 刻度 + 当前章节 Teal 标记 */}
                  <span className="relative mt-[7px] flex h-2 w-[18px] shrink-0 items-center">
                    <span
                      aria-hidden
                      className={`absolute left-0 h-px w-[10px] transition-colors ${
                        active ? 'bg-teal-ok' : 'bg-[var(--vi-ink-3)]/60'
                      }`}
                    />
                    {active && (
                      <span aria-hidden className="absolute left-0 h-2.5 w-[2px] bg-teal-ok" />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="tc block text-[9px] leading-none text-[var(--vi-ink-3)]">
                      {entry.level === 2 ? entry.displayNo : '··'}
                    </span>
                    <span
                      className={`mt-1 block text-[11px] leading-[1.45] ${
                        active
                          ? 'font-medium text-[var(--vi-ink)]'
                          : 'text-[var(--vi-ink-2)] group-hover:text-[var(--vi-ink)]'
                      }`}
                    >
                      {entry.label}
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      </div>
    </nav>
  )
}

const MarkdownViewer: FC<MarkdownViewerProps> = memo(({ status }) => {
  const [copied, setCopied] = useState(false)
  const [currentVerId, setCurrentVerId] = useState<string>('')
  const [selectedContent, setSelectedContent] = useState<string>('')
  const [modelName, setModelName] = useState<string>('')
  const [style, setStyle] = useState<string>('')
  const [createTime, setCreateTime] = useState<string>('')
  const getCurrentTask = useTaskStore.getState().getCurrentTask
  const currentTask = useTaskStore(state => state.getCurrentTask())
  const taskStatus = currentTask?.status || 'PENDING'
  const retryTask = useTaskStore.getState().retryTask
  const isMultiVersion = Array.isArray(currentTask?.markdown)
  const [showTranscribe, setShowTranscribe] = useState(false)
  const [showChat, setShowChat] = useState<false | 'half' | 'full'>(false)
  const [viewMode, setViewMode] = useState<'map' | 'preview'>('preview')
  const svgRef = useRef<SVGSVGElement>(null)
  const [activeTocIndex, setActiveTocIndex] = useState(0)

  // 报告原文：去掉后端注入的「来源链接」行（其余内容原样保留）
  const reportSource = useMemo(() => {
    if (!selectedContent || selectedContent === 'loading' || selectedContent === 'empty') return ''
    return selectedContent.replace(/^>\s*来源链接：[^\n]*\n*/m, '')
  }, [selectedContent])

  // 封头大标题：正文首个 H1（「# 内容洞察报告：…」），缺失时退回音频标题
  const reportTitle = useMemo(
    () => extractReportTitle(reportSource) || currentTask?.audioMeta?.title || '',
    [reportSource, currentTask?.audioMeta?.title],
  )

  // 正文：H1 已提升为封头大标题，不再重复渲染
  const reportBody = useMemo(() => stripReportTitle(reportSource), [reportSource])

  // 同一份解析结果：供文内目录 + 右侧胶卷目录共用
  const tocEntries = useMemo(() => extractTocEntries(stripExistingToc(reportBody)), [reportBody])

  const tocLookup = useMemo<TocLookup>(() => {
    const keyToEntry = new Map<string, TocEntry>()
    for (const entry of tocEntries) keyToEntry.set(entry.key, entry)
    return { keyToEntry, entries: tocEntries }
  }, [tocEntries])

  const markdownComponents = useMemo(
    () => createMarkdownComponents(tocLookup),
    [tocLookup],
  )

  // 预处理 markdown：注入可点击目录（文内目录保留，锚点跳转照旧）
  const processedContent = useMemo(() => {
    if (!reportBody) return ''
    return injectToc(reportBody)
  }, [reportBody])

  // 多版本内容处理
  useEffect(() => {
    if (!currentTask) return

    if (!isMultiVersion) {
      setCurrentVerId('') // 清空旧版本 ID
      setModelName(currentTask.formData.model_name)
      setStyle(currentTask.formData.style)
      setCreateTime(currentTask.createdAt)
      setSelectedContent(currentTask?.markdown)
    } else {
      const latestVersion = [...currentTask.markdown].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      )[0]

      if (latestVersion) {
        setCurrentVerId(latestVersion.ver_id)
      }
    }
  }, [currentTask?.id, taskStatus])
  useEffect(() => {
    if (!currentTask || !isMultiVersion) return

    const currentVer = currentTask.markdown.find(v => v.ver_id === currentVerId)
    if (currentVer) {
      setModelName(currentVer.model_name)
      setStyle(currentVer.style)
      setCreateTime(currentVer.created_at || '')
      setSelectedContent(currentVer.content)
    }
  }, [currentVerId, currentTask?.id])

  // 滚动高亮：监听 Radix viewport 的滚动，定位当前阅读章节
  const readerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const root = readerRef.current
    if (!root || tocEntries.length < 2) return

    const viewport = root.querySelector(
      '[data-radix-scroll-area-viewport]',
    ) as HTMLElement | null
    if (!viewport) return

    let rafId = 0

    const updateActive = () => {
      rafId = 0
      const headings = Array.from(
        viewport.querySelectorAll<HTMLElement>('[data-vi-toc]'),
      )
      if (headings.length === 0) return

      const viewportTop = viewport.getBoundingClientRect().top
      // 阅读基线：视口上方 96px 内最后一个已越过的标题即当前章节
      const baseline = viewportTop + 96
      let current: number | null = null

      for (const heading of headings) {
        const index = Number(heading.getAttribute('data-vi-toc'))
        if (Number.isNaN(index)) continue
        if (heading.getBoundingClientRect().top <= baseline) {
          current = index
        } else {
          break
        }
      }

      // 触底时高亮最后一节，避免末章永远不亮
      const atBottom =
        viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 4
      if (atBottom) {
        const last = headings[headings.length - 1]
        const lastIndex = Number(last?.getAttribute('data-vi-toc'))
        if (!Number.isNaN(lastIndex)) current = lastIndex
      }

      if (current !== null) setActiveTocIndex(prev => (prev === current ? prev : current))
    }

    const onScroll = () => {
      if (rafId) return
      rafId = requestAnimationFrame(updateActive)
    }

    viewport.addEventListener('scroll', onScroll, { passive: true })
    updateActive()

    return () => {
      viewport.removeEventListener('scroll', onScroll)
      if (rafId) cancelAnimationFrame(rafId)
    }
  }, [tocEntries, processedContent, viewMode, showChat, showTranscribe])

  // 切换报告/版本时，高亮回到第一节
  useEffect(() => {
    setActiveTocIndex(0)
  }, [selectedContent])

  const handleTocNavigate = (entry: TocEntry) => {
    const root = readerRef.current
    if (!root) return
    const target = root.querySelector<HTMLElement>(`[data-vi-toc="${entry.index}"]`)
    if (target) {
      setActiveTocIndex(entry.index)
      scrollTargetIntoView(target)
    }
  }

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(selectedContent)
      setCopied(true)
      toast.success('已复制到剪贴板')
      setTimeout(() => setCopied(false), 2000)
    } catch (e) {
      toast.error('复制失败')
    }
  }
  const alertButton = {
    id: 'alert',
    title: '测试警告',
    content: '⚠️',
    onClick: () => alert('你点击了自定义按钮！'),
  }
  const exportButton = {
    id: 'export',
    title: '导出思维导图',
    content: '⤓',
    onClick: () => {
      const svgEl = svgRef.current
      if (!svgEl) return
      // 同上面的序列化逻辑
      const serializer = new XMLSerializer()
      const source = serializer.serializeToString(svgEl)
      const blob = new Blob(['<?xml version="1.0" encoding="UTF-8"?>', source], {
        type: 'image/svg+xml;charset=utf-8',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'mindmap.svg'
      a.click()
      URL.revokeObjectURL(url)
    },
  }
  const handleDownload = () => {
    const task = getCurrentTask()
    const name = task?.audioMeta.title || 'vidinsight-report'
    const blob = new Blob([selectedContent], { type: 'text/markdown;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `${name}.md`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }

  if (status === 'loading') {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center space-y-4 text-neutral-500">
        <StepBar steps={steps} currentStep={taskStatus} progress={currentTask?.stage_progress ?? undefined} />
        <Loading className="h-5 w-5" />
        <div className="text-center text-sm">
          <p className="text-lg font-bold">正在生成内容洞察，请稍候…</p>
          <p className="mt-2 text-xs text-neutral-500">正在提取卖点、痛点、脚本结构和运营建议</p>
        </div>
      </div>
    )
  }

  if (status === 'idle') {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center space-y-3 text-neutral-500">
        <Idle />
        <div className="text-center">
          <p className="text-lg font-bold">输入竞品视频链接并点击“生成内容洞察”</p>
          <p className="mt-2 text-xs text-neutral-500">适合消费品、护肤品、饮料、母婴、健身食品等短视频分析</p>
        </div>
      </div>
    )
  }

  if (status === 'failed' && !isMultiVersion) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-4 space-y-3">
        <Error />
        <div className="text-center">
          <p className="text-lg font-bold text-red-500">内容分析失败</p>
          <p className="mt-2 mb-2 text-xs text-red-400">请检查后台或稍后再试</p>

          <Button onClick={() => retryTask(currentTask.id)} size="lg">
            重试
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <MarkdownHeader
        currentTask={currentTask}
        isMultiVersion={isMultiVersion}
        currentVerId={currentVerId}
        setCurrentVerId={setCurrentVerId}
        modelName={modelName}
        style={style}
        noteStyles={noteStyles}
        onCopy={handleCopy}
        onDownload={handleDownload}
        createAt={createTime}
        showTranscribe={showTranscribe}
        setShowTranscribe={setShowTranscribe}
        showChat={showChat}
        setShowChat={setShowChat}
        viewMode={viewMode}
        setViewMode={setViewMode}
      />

      {viewMode === 'map' ? (
        <div className="flex w-full flex-1 overflow-hidden bg-[var(--vi-surface)]">
          <div className={'w-full'}>
            <MarkmapEditor
              value={selectedContent}
              onChange={() => {}}
              height="100%" // 根据需求可以设定百分比或固定高度
              title={currentTask?.audioMeta?.title || '思维导图'}
            />
          </div>
        </div>
      ) : (
        <div className="flex flex-1 overflow-hidden bg-[var(--vi-surface)]">
          {selectedContent && selectedContent !== 'loading' && selectedContent !== 'empty' ? (
            <>
              {showChat === 'full' && currentTask ? (
                <div className="h-full w-full">
                  <ChatPanel taskId={currentTask.id} mode="full" onModeChange={setShowChat} />
                </div>
              ) : (
              <>
              <div ref={readerRef} className="flex min-w-0 flex-1 overflow-hidden">
                <ScrollArea className="min-w-0 flex-1">
                  <div className="px-4 pt-6 xl:px-6">
                    <VideoBanner
                      audioMeta={currentTask?.audioMeta}
                      videoUrl={currentTask?.formData?.video_url}
                    />

                    {/* 体验面③：简报封头（衬线大标题 + mono 元数据阵列） */}
                    <ReportMasthead
                      title={reportTitle}
                      platform={
                        currentTask?.audioMeta?.platform ||
                        currentTask?.formData?.platform
                      }
                      videoId={currentTask?.audioMeta?.video_id}
                      durationSeconds={normalizeDurationSeconds(
                        currentTask?.audioMeta?.duration,
                        currentTask?.audioMeta?.platform || currentTask?.formData?.platform,
                      )}
                      modelName={modelName}
                      styleLabel={noteStyles.find(v => v.value === style)?.label || style}
                      createdAt={createTime}
                      versionLabel={isMultiVersion ? currentVerId.slice(-6) : ''}
                      taskId={currentTask?.id}
                    />

                    <div className={'markdown-body w-full px-0'}>
                      <ReactMarkdown
                        remarkPlugins={remarkPlugins}
                        rehypePlugins={rehypePlugins}
                        components={markdownComponents}
                      >
                        {processedContent}
                      </ReactMarkdown>
                    </div>
                  </div>
                </ScrollArea>

                {/* 体验面③：右侧常驻胶卷目录 */}
                <FilmstripToc
                  entries={tocEntries}
                  activeIndex={activeTocIndex}
                  onNavigate={handleTocNavigate}
                />
              </div>
              {showTranscribe && (
                <div className={'ml-2 w-2/4'}>
                  <TranscriptViewer />
                </div>
              )}
              {/* 侧边问答模式：markdown + ChatPanel 各占一半 */}
              {showChat === 'half' && currentTask && (
                <div className="ml-2 h-full w-1/2 shrink-0">
                  <ChatPanel taskId={currentTask.id} mode="half" onModeChange={setShowChat} />
                </div>
              )}
              </>
              )}
            </>
          ) : (
            <div className="flex h-full w-full items-center justify-center p-6">
              <div className="flex max-w-sm flex-col items-center rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-6 text-center shadow-sm">
                <div className="tc mb-2 rounded bg-[var(--vi-code-bg)] px-2 py-0.5 text-xs text-[var(--vi-ink-3)]">
                  READY · WAITING INPUT
                </div>
                <p className="mb-1 text-sm font-medium text-[var(--vi-ink)]">输入竞品视频链接并生成内容洞察报告</p>
                <p className="text-xs text-[var(--vi-ink-3)]">支持哔哩哔哩、YouTube、抖音等视频来源</p>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
})

MarkdownViewer.displayName = 'MarkdownViewer'

export default MarkdownViewer
