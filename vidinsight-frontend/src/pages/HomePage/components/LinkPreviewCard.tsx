/* LinkPreviewCard.tsx ------------------------------------------------
 * 全平台「贴链接即预览」卡片：贴链接 → 秒级展示标题/封面/作者/清晰度，
 * 让用户在提交分钟级生成管线之前确认没贴错链接（hellotik 式体验）。
 *
 * 设计约束：预览是纯 UX 增强，三种状态都绝不阻塞主流程——
 *   loading  → 骨架卡
 *   失败     → 琥珀色提示（预览失败 ≠ 生成失败，可直接提交）
 *   成功     → 结果卡（封面走后端 /api/image_proxy 防盗链）
 */
import { useState } from 'react'
import { Loader2, Film, ImageIcon, X, Globe } from 'lucide-react'
import { videoPlatforms } from '@/constant/note.ts'
import type { LinkPreview } from '@/services/note.ts'

const fmtDuration = (s?: number | null) => {
  if (!s || s <= 0) return ''
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  return m > 0 ? `${m}:${String(sec).padStart(2, '0')}` : `0:${String(sec).padStart(2, '0')}`
}

const platformBadge = (value: string) => {
  const p = videoPlatforms.find(v => v.value === value)
  if (!p) return { label: value === 'other' ? '通用解析' : value, logo: null }
  return { label: p.label, logo: p.logo }
}

const Cover = ({ src, alt }: { src?: string; alt: string }) => {
  const [broken, setBroken] = useState(false)
  if (!src || broken) {
    return (
      <div className="flex h-20 w-32 shrink-0 items-center justify-center rounded bg-neutral-200 text-neutral-400">
        <Film className="h-6 w-6" />
      </div>
    )
  }
  return (
    <img
      src={`/api/image_proxy?url=${encodeURIComponent(src)}`}
      alt={alt}
      className="h-20 w-32 shrink-0 rounded object-cover"
      onError={() => setBroken(true)}
    />
  )
}

const Chip = ({ children }: { children: React.ReactNode }) => (
  <span className="rounded bg-neutral-200/80 px-1.5 py-0.5 text-[11px] text-neutral-600">
    {children}
  </span>
)

export default function LinkPreviewCard({
  loading,
  preview,
  failed,
  hint,
  url,
}: {
  loading: boolean
  preview: LinkPreview | null
  failed: boolean
  hint?: string
  url?: string
}) {
  if (loading) {
    return (
      <div className="mt-2 flex items-center gap-2 rounded-md border border-neutral-200 bg-neutral-50 p-3 text-sm text-neutral-500">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在识别链接…
      </div>
    )
  }

  if (failed) {
    return (
      <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
        未能预览该链接（平台限制或 Cookie 缺失）——<b>不影响直接生成</b>
        {hint && <div className="mt-0.5 text-xs text-amber-700">{hint}</div>}
        <WebViewFallbackButton url={url} />
        <HellotikFallbackButton url={url} />
      </div>
    )
  }

  if (!preview?.success) return null

  const { label, logo } = platformBadge(preview.platform)
  const dur = fmtDuration(preview.duration_s)
  const fmtCount = preview.formats?.filter(f => f.height).length || 0
  const imgCount = preview.images?.length || 0
  const cover = preview.cover || preview.images?.[0]?.url

  return (
    <div
      data-testid="link-preview-card"
      className="mt-2 rounded-md border border-neutral-200 bg-neutral-50/70 p-3"
    >
      <div className="flex gap-3">
        <Cover src={cover} alt={preview.title || '封面'} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <p className="line-clamp-1 text-sm font-medium text-neutral-800">
              {preview.title || '(未取得标题)'}
            </p>
            <span className="flex shrink-0 items-center gap-1 rounded bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-600">
              {logo && <span className="h-3 w-3 [&_svg]:h-3 [&_svg]:w-3">{logo()}</span>}
              {label}
            </span>
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-neutral-500">
            {dur && <span>{dur}</span>}
            {preview.uploader && <span>· {preview.uploader}</span>}
            {fmtCount > 0 && <span>· {fmtCount} 个清晰度</span>}
            {preview.note_type && (
              <Chip>{preview.note_type === 'video' ? '视频笔记' : '图文笔记'}</Chip>
            )}
            {imgCount > 0 && (
              <span className="flex items-center gap-0.5">
                <ImageIcon className="h-3 w-3" /> {imgCount} 张配图
              </span>
            )}
            {preview.likes != null && preview.likes !== '' && <Chip>👍 {preview.likes}</Chip>}
            {preview.collected != null && preview.collected !== '' && (
              <Chip>⭐ {preview.collected}</Chip>
            )}
            {preview.limited && <Chip>有限预览</Chip>}
          </div>

          {preview.description && (
            <p className="mt-1 line-clamp-1 text-xs text-neutral-400">{preview.description}</p>
          )}
        </div>
      </div>
      <p className="mt-1.5 text-[11px] text-neutral-400">
        已识别链接 · 确认无误后点击「生成内容洞察」
      </p>
    </div>
  )
}

// 供失败态外的场景复用（预留）
export const PreviewCardClose = ({ onClose }: { onClose: () => void }) => (
  <button type="button" onClick={onClose} className="text-neutral-400 hover:text-neutral-600">
    <X className="h-3.5 w-3.5" />
  </button>
)


/// WebView 兜底（桌面端专属）：yt-dlp 被风控拦截时，用真实浏览器窗口
/// 打开视频页读取信息（指纹/登录态 100% 真人，风控无法区分）。
function WebViewFallbackButton({ url }: { url?: string }) {
  const isDesktop = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const [busy, setBusy] = useState(false)
  if (!isDesktop || !url || !url.includes('bilibili.com')) return null

  const run = async () => {
    setBusy(true)
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const task = `wvm-${Date.now()}`
      // 窗口读取完成即自动关闭；返回后端暂存的完整元数据
      await invoke<string>('fetch_bilibili_meta', { url, task })
      window.dispatchEvent(new CustomEvent('vidinsight:webview-preview', { detail: { url, task } }))
    } catch (e) {
      console.warn('WebView 兜底失败:', e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      onClick={run}
      disabled={busy}
      className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-amber-300 bg-white/70 px-2.5 py-1 text-xs font-medium text-amber-800 transition-colors hover:bg-white disabled:opacity-60"
    >
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Globe className="h-3 w-3" />}
      {busy ? '正在浏览器窗口读取…' : '用浏览器窗口兜底读取'}
    </button>
  )
}


/// hellotik 兜底（桌面端专属）：照真人操作打开 hellotik 网页 →
/// 粘贴链接 → 点「解析视频」→ 把结果页的无水印直链取回来用。
/// 2026-10-04 实测（用真实分享链接）：B站 ✅ / 小红书 ✅；YouTube 三次均 ✗。
function HellotikFallbackButton({ url }: { url?: string }) {
  const isDesktop = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  if (!isDesktop || !url) return null
/// 2026-10-04 实测（真实分享链接）：B站 ✅ / 小红书 ✅；YouTube 三次均 ✗（明确拦掉）。
/// 抖音：用户反馈实测可用（我无真实链接自证，故通道放开待确认）。
const supported =
    url.includes('bilibili.com') ||
    url.includes('b23.tv') ||
    url.includes('xiaohongshu.com') ||
    url.includes('xhslink') ||
    url.includes('douyin.com')
if (!supported) return null

  const run = async () => {
    setBusy(true)
    setMsg(null)
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const task = `ht-${Date.now()}`
      await invoke<string>('fetch_hellotik_direct', { url, task })
      window.dispatchEvent(
        new CustomEvent('vidinsight:hellotik-preview', { detail: { url, task } })
      )
    } catch (e: any) {
      setMsg(typeof e === 'string' ? e : String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-1.5">
      <button
        onClick={run}
        disabled={busy}
        className="inline-flex items-center gap-1.5 rounded-md border border-neutral-300 bg-white/70 px-2.5 py-1 text-xs font-medium text-neutral-700 transition-colors hover:bg-white disabled:opacity-60"
      >
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Globe className="h-3 w-3" />}
        {busy ? '正在用网页解析…' : '用第三方网页兜底解析'}
      </button>
      {msg && <div className="mt-1 text-xs text-amber-700">{msg}</div>}
    </div>
  )
}
