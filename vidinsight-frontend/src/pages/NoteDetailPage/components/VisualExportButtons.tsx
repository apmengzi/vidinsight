/* VisualExportButtons.tsx ------------------------------------------------
 * 总结的可视化出口：一图流 + 配图提示词。
 *
 * A. 一图流（本地渲染）：把总结排版成 1080×1350 竖图，可下载/预览。
 *    纯本地 PIL 渲染，不联网——因为答辩要现场演示，不能赌外部服务活着
 *    （实测 WorkBuddy 反代 /v1/images/generations 只路由 chat、图像模型
 *    503 pool_empty；Miora 无公开 API 且需登录态）。
 *
 * B. 配图提示词：用 LLM 把总结转成 3 条文生图提示词，用户复制到 Miora
 *    等工具里自己生成。我们**不代登录、不自动点击**——只做提示词，符合
 *    「对第三方平台的交互必须回归真人行为」的产品红线。
 */
import { useState } from 'react'
import { ImageDown, Palette, Loader2, Copy, Check } from 'lucide-react'
import { Button } from '@/components/ui/button.tsx'
import { buildImagePrompts, buildOnePager } from '@/services/note.ts'
import toast from 'react-hot-toast'

export default function VisualExportButtons({
  markdown,
  sourceLabel,
  coverUrl,
}: {
  markdown: string
  sourceLabel?: string
  coverUrl?: string
}) {
  const [busyPoster, setBusyPoster] = useState(false)
  const [busyPrompt, setBusyPrompt] = useState(false)
  const [posterUrl, setPosterUrl] = useState<string | null>(null)
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
      })
      setPosterUrl(r.url)
      toast.success('一图流已生成')
    } catch (e: any) {
      toast.error(e?.msg || e?.message || '一图流生成失败')
    } finally {
      setBusyPoster(false)
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

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={makePoster}
          disabled={busyPoster}
          className="flex items-center gap-1.5 text-xs"
          title="把总结排版成一张竖版长图（本地渲染，不联网）"
        >
          {busyPoster ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ImageDown className="h-3.5 w-3.5" />}
          一图流
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={makePrompts}
          disabled={busyPrompt}
          className="flex items-center gap-1.5 text-xs"
          title="生成配图提示词，可粘贴到 Miora 等工具自己生图"
        >
          {busyPrompt ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Palette className="h-3.5 w-3.5" />}
          配图提示词
        </Button>
      </div>

      {posterUrl && (
        <div className="w-56 rounded-lg border border-neutral-200 bg-white p-2 shadow-sm">
          <img src={posterUrl} alt="一图流" className="w-full rounded" />
          <div className="mt-2 flex gap-2">
            <a
              href={posterUrl}
              download
              className="flex-1 rounded bg-neutral-900 px-2 py-1 text-center text-[11px] text-white hover:bg-neutral-700"
            >
              下载
            </a>
            <button
              onClick={() => window.open(posterUrl, '_blank')}
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
            复制到 Miora 等工具自己生成（我们不代操作）
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
