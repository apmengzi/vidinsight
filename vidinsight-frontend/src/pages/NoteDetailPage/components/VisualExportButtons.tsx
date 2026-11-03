/* VisualExportButtons.tsx ------------------------------------------------
 * 总结的可视化出口：一图流 + 配图提示词。
 *
 * 一图流有两条路（2026-11-04 改造）：
 *   A. **本地排版（默认）**：LLM 通读整篇笔记 → 提炼 {标题/副标题/要点/结论}
 *      → PIL 渲染 1080×1350。文字 100% 准确、秒级、无额度限制。
 *      LLM 不可用时自动降级为「本地抽取」（抓标题和列表项），功能不失效。
 *   B. **AI 排版（可选）**：同一份提炼结果 → 拼成信息图提示词 → 文生图。
 *      更好看，但可能写错字、约 20-30s、每日限次，所以不作为默认。
 *
 * 配图提示词：把总结转成 3 条文生图提示词，用户复制到别处自己生成。
 * 我们**不代登录、不自动点击**——符合「对第三方平台的交互回归真人行为」红线。
 */
import { useState } from 'react'
import { ImageDown, Palette, Loader2, Copy, Check, Sparkles, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'
import { buildImagePrompts, buildOnePager, buildOnePagerImage } from '@/services/note.ts'
import { resolveAssetUrl } from '@/utils/image-url.ts'
import toast from 'react-hot-toast'

export default function VisualExportButtons({
  markdown,
  sourceLabel,
  coverUrl,
  taskId,
}: {
  markdown: string
  sourceLabel?: string
  coverUrl?: string
  /** 笔记任务 ID：用于后端缓存提炼结果，同一条笔记重复生成不再烧 token */
  taskId?: string
}) {
  const [busyPoster, setBusyPoster] = useState(false)
  const [busyAi, setBusyAi] = useState(false)
  const [busyPrompt, setBusyPrompt] = useState(false)
  const [poster, setPoster] = useState<{ url: string; mode?: string; points?: number } | null>(null)
  const [prompts, setPrompts] = useState<string[]>([])
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null)

  const makePoster = async () => {
    if (!markdown || markdown.length < 20) {
      toast.error('内容太短，无法生成一图流')
      return
    }
    setBusyPoster(true)
    try {
      const r = await buildOnePager({
        markdown,
        source_label: sourceLabel || '',
        cover_url: coverUrl || '',
        task_id: taskId,
      })
      setPoster({ url: r.url, mode: r.mode, points: r.points })
      if (r.mode === 'local') {
        toast.success('一图流已生成（模型不可用，已用本地抽取）')
      } else {
        toast.success('一图流已生成')
      }
    } catch (e: any) {
      toast.error(e?.msg || e?.message || '一图流生成失败')
    } finally {
      setBusyPoster(false)
    }
  }

  const makeAiPoster = async () => {
    if (!markdown || markdown.length < 40) {
      toast.error('内容太短，无法生成一图流')
      return
    }
    setBusyAi(true)
    try {
      const r = await buildOnePagerImage({ markdown, task_id: taskId })
      setPoster({ url: r.url, mode: 'ai', points: r.points })
      toast.success(
        r.remaining_today != null
          ? `AI 一图流已生成（今日剩余 ${r.remaining_today} 张）`
          : 'AI 一图流已生成',
      )
    } catch (e: any) {
      toast.error(e?.msg || e?.message || 'AI 一图流生成失败', { duration: 6000 })
    } finally {
      setBusyAi(false)
    }
  }

  const makePrompts = async () => {
    if (!markdown || markdown.length < 20) {
      toast.error('内容太短，无法生成提示词')
      return
    }
    setBusyPrompt(true)
    try {
      const r = await buildImagePrompts({ markdown })
      setPrompts(r.prompts || [])
      if (!r.prompts?.length) toast.error('未生成有效提示词')
    } catch (e: any) {
      toast.error(e?.msg || e?.message || '提示词生成失败')
    } finally {
      setBusyPrompt(false)
    }
  }

  const copyText = async (text: string, idx: number) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedIdx(idx)
      setTimeout(() => setCopiedIdx(null), 1600)
    } catch {
      toast.error('复制失败，请手动选中复制')
    }
  }

  const modeLabel =
    poster?.mode === 'ai' ? 'AI 排版' : poster?.mode === 'local' ? '本地抽取' : 'LLM 提炼'

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={makePoster}
          disabled={busyPoster || busyAi}
          className="flex items-center gap-1.5 text-xs"
          title="AI 通读整篇笔记提炼要点，排成一张竖版长图（文字准确、秒级）"
        >
          {busyPoster ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ImageDown className="h-3.5 w-3.5" />}
          一图流
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={makeAiPoster}
          disabled={busyPoster || busyAi}
          className="flex items-center gap-1.5 text-xs"
          title="用文生图做更漂亮的排版（约 20-30 秒，每日有额度限制）"
        >
          {busyAi ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
          AI 排版
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={makePrompts}
          disabled={busyPrompt}
          className="flex items-center gap-1.5 text-xs"
          title="生成配图提示词，可粘贴到其他工具自己生图"
        >
          {busyPrompt ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Palette className="h-3.5 w-3.5" />}
          配图提示词
        </Button>
      </div>

      {poster && (
        <div className="w-56 rounded-lg border border-neutral-200 bg-white p-2 shadow-sm">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="tc flex items-center gap-1 text-[10px] text-neutral-400">
              {poster.mode === 'ai' && <Sparkles className="h-2.5 w-2.5" />}
              {modeLabel}
              {poster.points ? ` · ${poster.points} 要点` : ''}
            </span>
          </div>
          <img src={resolveAssetUrl(poster.url)} alt="一图流" className="w-full rounded" />
          <div className="mt-2 flex gap-2">
            <a
              href={resolveAssetUrl(poster.url)}
              download
              className="flex-1 rounded bg-neutral-900 px-2 py-1 text-center text-[11px] text-white hover:bg-neutral-700"
            >
              下载
            </a>
            <button
              onClick={() => window.open(resolveAssetUrl(poster.url), '_blank')}
              className="flex-1 rounded border border-neutral-300 px-2 py-1 text-[11px] text-neutral-700 hover:bg-neutral-50"
            >
              新窗口
            </button>
          </div>
        </div>
      )}

      {prompts.length > 0 && (
        <div className="w-72 rounded-lg border border-neutral-200 bg-white p-2 shadow-sm">
          <p className="tc mb-1.5 text-[10px] text-neutral-400">
            复制到其他工具自己生成（我们不代操作）
          </p>
          <div className="space-y-1.5">
            {prompts.map((p, i) => (
              <div key={i} className="flex items-start gap-1.5 rounded bg-neutral-50 p-1.5">
                <span className="tc shrink-0 text-[10px] text-neutral-400">{i + 1}</span>
                <span className="flex-1 text-[11px] leading-snug text-neutral-700">{p}</span>
                <button
                  onClick={() => copyText(p, i)}
                  className="shrink-0 text-neutral-400 hover:text-neutral-700"
                >
                  {copiedIdx === i ? (
                    <Check className="h-3 w-3 text-emerald-600" />
                  ) : (
                    <Copy className="h-3 w-3" />
                  )}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
