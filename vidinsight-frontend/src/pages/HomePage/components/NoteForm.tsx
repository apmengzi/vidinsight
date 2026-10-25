/* NoteForm.tsx ---------------------------------------------------- */
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form.tsx'
import { useEffect, useRef, useState } from 'react'
import { useForm, useWatch, type FieldErrors } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Info, Loader2, Plus } from 'lucide-react'
import toast from 'react-hot-toast'
import { Alert, AlertDescription } from '@/components/ui/alert.tsx'
import { expandCollection, fetchXhsCollectionViaWebview, generateNote, parseVideoLink, type LinkPreview } from '@/services/note.ts'
import LinkPreviewCard from '@/pages/HomePage/components/LinkPreviewCard.tsx'
import { uploadFile } from '@/services/upload.ts'
import { useTaskStore } from '@/store/taskStore'
import { useModelStore } from '@/store/modelStore'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip.tsx'
import { Checkbox } from '@/components/ui/checkbox.tsx'
import { ScrollArea } from '@/components/ui/scroll-area.tsx'
import { Button } from '@/components/ui/button.tsx'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx'
import { Input } from '@/components/ui/input.tsx'
import { Textarea } from '@/components/ui/textarea.tsx'
import { noteStyles, noteFormats, videoPlatforms } from '@/constant/note.ts'
import { fetchModels } from '@/services/model.ts'
import { useNavigate } from 'react-router-dom'

/* -------------------- 校验 Schema -------------------- */
const formSchema = z
  .object({
    video_url: z.string().optional(),
    platform: z.string().nonempty('请选择平台'),
    quality: z.enum(['fast', 'medium', 'slow']),
    screenshot: z.boolean().optional(),
    link: z.boolean().optional(),
    model_name: z.string().nonempty('请选择模型'),
    format: z.array(z.string()).default([]),
    style: z.string().nonempty('请选择分析模板'),
    extras: z.string().optional(),
    video_understanding: z.boolean().optional(),
    video_interval: z.coerce.number().min(1).max(30).default(6).optional(),
    grid_size: z
      .tuple([z.coerce.number().min(1).max(10), z.coerce.number().min(1).max(10)])
      .default([2, 2])
      .optional(),
  })
  .superRefine(({ video_url, platform }, ctx) => {
    if (platform === 'local') {
      if (!video_url) {
        ctx.addIssue({ code: 'custom', message: '本地视频路径不能为空', path: ['video_url'] })
      }
    }
    else {
      if (!video_url) {
        ctx.addIssue({ code: 'custom', message: '视频链接不能为空', path: ['video_url'] })
      }
      else {
        try {
          const url = new URL(video_url)
          if (!['http:', 'https:'].includes(url.protocol))
            throw new Error()
        }
        catch {
          ctx.addIssue({ code: 'custom', message: '请输入正确的视频链接', path: ['video_url'] })
        }
      }
    }
  })

export type NoteFormValues = z.infer<typeof formSchema>

/* -------------------- 可复用子组件 -------------------- */
const SectionHeader = ({ title, tip }: { title: string; tip?: string }) => (
  <div className="my-3 flex items-center justify-between">
    <h2 className="block">{title}</h2>
    {tip && (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Info className="hover:text-primary h-4 w-4 cursor-pointer text-neutral-400" />
          </TooltipTrigger>
          <TooltipContent className="text-xs">{tip}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    )}
  </div>
)

const CheckboxGroup = ({
  value = [],
  onChange,
  disabledMap,
}: {
  value?: string[]
  onChange: (v: string[]) => void
  disabledMap: Record<string, boolean>
}) => (
  <div className="flex flex-wrap space-x-1.5">
    {noteFormats.map(({ label, value: v }) => (
      <label key={v} className="flex items-center space-x-2">
        <Checkbox
          checked={value.includes(v)}
          disabled={disabledMap[v]}
          onCheckedChange={checked =>
            onChange(checked ? [...value, v] : value.filter(x => x !== v))
          }
        />
        <span>{label}</span>
      </label>
    ))}
  </div>
)

/* -------------------- 主组件 -------------------- */
const NoteForm = () => {
  const navigate = useNavigate();
  const [isUploading, setIsUploading] = useState(false)
  const [uploadSuccess, setUploadSuccess] = useState(false)
  // 提交中标记：合集逐条提交是串行的（每条一次网络往返），没有它用户会
  // 以为按钮没反应而反复点击，重复提交同一批任务。
  const [submitting, setSubmitting] = useState(false)
  /* ---- 全局状态 ---- */
  const { addPendingTask, currentTaskId, setCurrentTask, getCurrentTask, retryTask } =
    useTaskStore()
  const { loadEnabledModels, modelList, showFeatureHint, setShowFeatureHint } = useModelStore()

  /* ---- 表单 ---- */
  const form = useForm<NoteFormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      platform: 'bilibili',
      quality: 'medium',
      model_name: modelList[0]?.model_name || '',
      style: 'adaptive',
      video_interval: 6,
      grid_size: [2, 2],
      format: [],
    },
  })
  const currentTask = getCurrentTask()

  /* ---- 派生状态（只 watch 一次，提高性能） ---- */
  const platform = useWatch({ control: form.control, name: 'platform' }) as string
  const videoUrl = useWatch({ control: form.control, name: 'video_url' }) as string | undefined
  const videoUnderstandingEnabled = useWatch({ control: form.control, name: 'video_understanding' })
  const editing = currentTask && currentTask.id

  /* ---- 链接预览（纯 UX 增强，绝不阻塞生成主流程）----
   * 贴链接 → 700ms 防抖 → /api/parse 秒级识别 → 卡片展示标题/封面/作者；
   * 顺带做平台自动纠正（解析出的平台可靠，免去手动切换下拉框）。
   * 失败一律降级为琥珀提示——生成管线有自己的平台专用通道。 */
  const [preview, setPreview] = useState<LinkPreview | null>(null)

  // WebView 兜底结果回填：LinkPreviewCard 的「浏览器窗口兜底读取」完成后，
  // 从后端暂存取回元数据并按成功态渲染（与正常预览同一张卡）
  useEffect(() => {
    const handler = async (e: Event) => {
      const detail = (e as CustomEvent<{ url: string; task: string }>).detail
      if (!detail?.task) return
      try {
        const res = await fetch(`/api/parse/webview_meta/${detail.task}`).then(r => r.json())
        const d = res?.data
        if (!d?.ready || !d.data) return
        setPreviewFailed(false)
        setPreviewLoading(false)
        setPreview({
          success: true,
          platform: 'bilibili',
          title: d.data.title || '',
          duration_s: d.data.duration || 0,
          cover: d.data.cover || '',
          uploader: d.data.uploader || '',
          formats: [],
          note: '浏览器窗口读取（风控兜底通道）',
        } as LinkPreview)
      } catch { /* 兜底失败保持失败态 */ }
    }
    window.addEventListener('vidinsight:webview-preview', handler as EventListener)
    return () => window.removeEventListener('vidinsight:webview-preview', handler as EventListener)
  }, [])

  // hellotik 兜底结果回填：网页解析拿到直链后按成功态渲染，并把直链挂到
  // 表单上供后续下载使用（主通道 yt-dlp 被风控时，这是能拿到源片的路子）。
  useEffect(() => {
    const handler = async (e: Event) => {
      const detail = (e as CustomEvent<{ url: string; task: string }>).detail
      if (!detail?.task) return
      try {
        const res = await fetch(`/api/parse/hellotik/${detail.task}`).then(r => r.json())
        const d = res?.data
        if (!d?.ready || !d.data) return
        const parsed = d.data
        if (!parsed?.success) {
          setPreviewHint(parsed?.hint || '第三方网页解析未拿到直链')
          return
        }
        setPreviewFailed(false)
        setPreviewLoading(false)
        setPreview({
          success: true,
          // 平台由后端按直链域名判定（B站→bilibili / 小红书→xiaohongshu）
          platform: parsed.platform || 'bilibili',
          title: parsed.title || '',
          duration_s: 0,
          cover: parsed.cover || '',
          uploader: '',
          formats: [],
          note: '第三方网页兜底解析（已取得无水印直链）',
        } as LinkPreview)
        // 直链留存：提交时后端优先用它，跳过 yt-dlp 风控环节
        setDirectUrlMap(prev => ({ ...prev, [detail.url]: parsed.direct_url }))
      } catch { /* 兜底失败保持原状态 */ }
    }
    window.addEventListener('vidinsight:hellotik-preview', handler as EventListener)
    return () => window.removeEventListener('vidinsight:hellotik-preview', handler as EventListener)
  }, [])

  // 源链接 → hellotik 直链（仅 B 站兜底场景有值）
  const [directUrlMap, setDirectUrlMap] = useState<Record<string, string>>({})
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewFailed, setPreviewFailed] = useState(false)
  const [previewHint, setPreviewHint] = useState('')
  const lastParsedUrlRef = useRef('')
  const previewable = platform !== 'local' && !!videoUrl && /^https?:\/\//i.test(videoUrl.trim())

  useEffect(() => {
    const url = (videoUrl || '').trim()
    if (!previewable || editing) {
      if (!previewable) {
        setPreview(null)
        setPreviewFailed(false)
        setPreviewHint('')
        lastParsedUrlRef.current = ''
      }
      return
    }
    if (url === lastParsedUrlRef.current) return
    const timer = setTimeout(async () => {
      setPreviewLoading(true)
      setPreviewFailed(false)
      setPreviewHint('')
      const result = await parseVideoLink(url)
      setPreviewLoading(false)
      if (url !== (videoUrl || '').trim()) return // 输入已变化，丢弃过期结果
      lastParsedUrlRef.current = url
      if (result?.success) {
        setPreview(result)
        // 平台自动纠正：解析出的平台比手动下拉更可靠（仅限已知平台、非 other）
        if (
          result.platform &&
          result.platform !== 'other' &&
          result.platform !== platform &&
          videoPlatforms.some(p => p.value === result.platform)
        ) {
          form.setValue('platform', result.platform)
        }
      } else {
        setPreview(null)
        setPreviewFailed(true)
        setPreviewHint(result?.hint || result?.reason || '')
      }
    }, 700)
    return () => clearTimeout(timer)
  }, [videoUrl, platform, previewable, editing])

  const goModelAdd = () => {
    navigate("/settings/model");
  };
  /* ---- 副作用 ---- */
  useEffect(() => {
    loadEnabledModels()

    return
  }, [])
  useEffect(() => {
    if (!currentTask) return
    const { formData } = currentTask

    console.log('currentTask.formData.platform:', formData.platform)

    form.reset({
      platform: formData.platform || 'bilibili',
      video_url: formData.video_url || '',
      model_name: formData.model_name || modelList[0]?.model_name || '',
      style: formData.style || 'adaptive',
      quality: formData.quality || 'medium',
      extras: formData.extras || '',
      screenshot: formData.screenshot ?? false,
      link: formData.link ?? false,
      video_understanding: formData.video_understanding ?? false,
      video_interval: formData.video_interval ?? 6,
      grid_size: formData.grid_size ?? [2, 2],
      format: formData.format ?? [],
    })
  }, [
    // 当下面任意一个变了，就重新 reset
    currentTaskId,
    // modelList 用来兜底 model_name
    modelList.length,
    // 还要加上 formData 的各字段，或者直接 currentTask
    currentTask?.formData,
  ])

  /* ---- 帮助函数 ---- */
  const isGenerating = () => !['SUCCESS', 'FAILED', undefined].includes(getCurrentTask()?.status)
  const generating = isGenerating()
  const handleFileUpload = async (file: File, cb: (url: string) => void) => {
    const formData = new FormData()
    formData.append('file', file)
    setIsUploading(true)
    setUploadSuccess(false)

    try {
  
      const  data  = await uploadFile(formData)
        cb(data.url)
        setUploadSuccess(true)
    } catch (err: any) {
      // 上传失败此前只打 console，用户看到的是"上传中"直接变回空态，完全不知道失败了
      const reason = err?.msg || err?.message || '未知错误'
      toast.error(`上传失败：${reason}`, { duration: 6000 })
      console.error('上传失败:', err)
    } finally {
      setIsUploading(false)
    }
  }

  const onSubmit = async (values: NoteFormValues) => {
    if (submitting) return
    setSubmitting(true)
    try {
      await submitTask(values)
    } finally {
      setSubmitting(false)
    }
  }

  const submitTask = async (values: NoteFormValues) => {
    const payload: NoteFormValues = {
      ...values,
      provider_id: modelList.find(m => m.model_name === values.model_name)!.provider_id,
      task_id: currentTaskId || '',
    }
    // hellotik 兜底拿到过直链 → 一并提交，后端可直接下载源片，绕开 B 站风控
    const direct = directUrlMap[(values.video_url || '').trim()]
    if (direct) (payload as any).direct_url = direct
    if (currentTaskId) {
      await retryTask(currentTaskId, payload)
      return
    }

    // 小红书合集链接：先开一个 WebView 收割 xsec_token，再展开为逐条笔记批量提交
    if (values.platform === 'xiaohongshu' && /collection\/item\//.test(values.video_url || '')) {
      try {
        // 收割 token（一个合集一个窗口；被用户关闭会 reject → 直接中止并提示）
        await fetchXhsCollectionViaWebview(values.video_url || '')
        const col = await expandCollection(values.video_url || '', 'xiaohongshu')
        const notes: any[] = col?.notes ?? []
        if (notes.length === 0) {
          toast.error('合集中未解析到笔记，请确认已配置小红书 Cookie')
          return
        }
        let ok = 0
        const failures: string[] = []
        for (const note of notes) {
          try {
            const batchPayload = { ...payload, video_url: note.url, task_id: '' }
            const data = await generateNote(batchPayload)
            addPendingTask(data.task_id, values.platform, batchPayload)
            ok++
          } catch (e: any) {
            // 单条失败不阻塞其余，但失败原因要汇总给用户看，
            // 否则「提交 3/10 条」这种结果无法解释
            failures.push(e?.msg || e?.message || '未知原因')
          }
        }
        if (ok > 0) {
          toast.success(`已从《${col.name || '合集'}》提交 ${ok}/${notes.length} 条笔记任务`)
          form.setValue('video_url', '')
          if (failures.length > 0) {
            toast.error(`${failures.length} 条提交失败：${failures[0]}`, { duration: 8000 })
          }
        } else {
          toast.error(
            `合集内笔记全部提交失败：${failures[0] || '请检查网络或 Cookie 配置'}`,
            { duration: 8000 },
          )
        }
      } catch (e: any) {
        toast.error(e?.response?.data?.message || e?.msg || e?.message || '合集解析失败')
      }
      return
    }

    // 无 token 的小红书单条链接：XHS 网页版硬规则（详情页只认 xsec_token，登录也一样），
    // 提交必然码墙失败，提前诚实告知
    if (values.platform === 'xiaohongshu' && /xiaohongshu\.com/.test(values.video_url || '')
        && !(values.video_url || '').includes('xsec_token=')) {
      toast.error('该链接缺少访问凭证（xsec_token），无法解析。\n请从 App 分享，或从合集/作者主页/搜索结果页复制链接（URL 带 xsec_token= 参数）。', { duration: 9000 })
      return
    }

    try {
      const data = await generateNote(payload)
      addPendingTask(data.task_id, values.platform, payload)
    } catch (e: any) {
      // 就绪门禁：本地转写模型还没下载好。后端返回 reason='transcriber_model_not_ready'，
      // 引导用户去「设置 → 音频转写配置」下载，而不是留一个静默失败的任务。
      if (e?.data?.reason === 'transcriber_model_not_ready') {
        const downloading = e?.data?.downloading
        toast.error(
          downloading
            ? '转写模型正在下载中，请稍候再提交'
            : '转写模型尚未下载，请先去「音频转写配置」页下载',
        )
        if (!downloading) navigate('/settings/transcriber')
        return
      }
      // axios 拦截器对带响应体的错误已弹过 toast；这里只兜底纯网络错误
      // （拦截器同样会弹），并保证 promise 不变成未处理 rejection。
      console.error('提交任务失败：', e)
    }
  }
  const onInvalid = (errors: FieldErrors<NoteFormValues>) => {
    // 校验失败此前只写 console：用户点了「生成」页面毫无反应，以为按钮坏了。
    const first = Object.values(errors).find(Boolean) as { message?: string } | undefined
    toast.error(first?.message || '请完善表单必填项后再提交')
    console.warn('表单校验失败：', errors)
  }
  const handleCreateNew = () => {
    // 🔁 这里清空当前任务状态
    // 比如调用 resetCurrentTask() 或者 navigate 到一个新页面
    setCurrentTask(null)
  }
  const FormButton = () => {
    const label = generating
      ? '正在分析…'
      : submitting
      ? '提交中…'
      : editing
      ? '重新分析'
      : '生成内容洞察'

    return (
      <div className="flex gap-2">
        <Button
          type="submit"
          className={!editing ? 'w-full' : 'w-2/3' + ' bg-primary'}
          disabled={generating || submitting}
        >
          {(generating || submitting) && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {label}
        </Button>

        {editing && (
          <Button type="button" variant="outline" className="w-1/3" onClick={handleCreateNew}>
            <Plus className="mr-2 h-4 w-4" />
            新建分析
          </Button>
        )}
      </div>
    )
  }

  /* -------------------- 渲染 -------------------- */
  return (
    <div className="h-full w-full">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit, onInvalid)} className="space-y-4">
          {/* 顶部按钮 */}
          <FormButton></FormButton>

          {/* 视频链接 & 平台 */}
          <SectionHeader title="竞品视频链接" tip="支持 B 站、YouTube、抖音等平台，也可导入本地视频" />
          <div className="flex gap-2">
            {/* 平台选择 */}

            <FormField
              control={form.control}
              name="platform"
              render={({ field }) => (
                <FormItem>
                  <Select
                    disabled={!!editing}
                    value={field.value}
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger className="w-32">
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {videoPlatforms?.map(p => (
                        <SelectItem key={p.value} value={p.value}>
                          <div className="flex items-center justify-center gap-2">
                            <div className="h-4 w-4">{p.logo()}</div>
                            <span>{p.label}</span>
                          </div>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage style={{ display: 'none' }} />
                </FormItem>
              )}
            />
            {/* 链接输入 / 上传框 */}
            <FormField
              control={form.control}
              name="video_url"
              render={({ field }) => (
                <FormItem className="flex-1">
                  {platform === 'local' ? (
                    <>
                      <Input disabled={!!editing} placeholder="请输入本地竞品视频路径" {...field} />
                    </>
                  ) : (
                    <Input disabled={!!editing} placeholder="请输入竞品短视频链接" {...field} />
                  )}
                  <FormMessage style={{ display: 'none' }} />
                </FormItem>
              )}
            />
          </div>

          {platform !== 'local' && (
            <LinkPreviewCard
              loading={previewLoading}
              preview={preview}
              failed={previewFailed}
              hint={previewHint}
              url={(videoUrl || '').trim()}
            />
          )}

          <FormField
            control={form.control}
            name="video_url"
            render={({ field }) => (
              <FormItem className="flex-1">
                {platform === 'local' && (
                  <>
                    <div
                      className="hover:border-primary mt-2 flex h-40 cursor-pointer items-center justify-center rounded-md border-2 border-dashed border-gray-300 transition-colors"
                      onDragOver={e => {
                        e.preventDefault()
                        e.stopPropagation()
                      }}
                      onDrop={e => {
                        e.preventDefault()
                        const file = e.dataTransfer.files?.[0]
                        if (file) handleFileUpload(file, field.onChange)
                      }}
                      onClick={() => {
                        const input = document.createElement('input')
                        input.type = 'file'
                        input.accept = 'video/*'
                        input.onchange = e => {
                          const file = (e.target as HTMLInputElement).files?.[0]
                          if (file) handleFileUpload(file, field.onChange)
                        }
                        input.click()
                      }}
                    >
                      {isUploading ? (
                        <p className="text-center text-sm text-blue-500">上传中，请稍候…</p>
                      ) : uploadSuccess ? (
                        <p className="text-center text-sm text-green-500">上传成功！</p>
                      ) : (
                        <p className="text-center text-sm text-gray-500">
                          拖拽文件到这里上传 <br />
                          <span className="text-xs text-gray-400">或点击选择文件</span>
                        </p>
                      )}
                    </div>
                  </>
                )}
                <FormMessage />
              </FormItem>
            )}
          />
          <div className="grid grid-cols-2 gap-2">
            {/* 模型选择 */}
            {

             modelList.length>0?(     <FormField
               className="w-full"
               control={form.control}
               name="model_name"
               render={({ field }) => (
                 <FormItem>
                   <SectionHeader title="模型选择" tip="不同模型效果不同，建议自行测试" />
                   <Select
                     onOpenChange={()=>{
                       loadEnabledModels()
                     }}
                     value={field.value}
                     onValueChange={field.onChange}
                     defaultValue={field.value}
                   >
                     <FormControl>
                       <SelectTrigger className="w-full min-w-0 truncate">
                         <SelectValue />
                       </SelectTrigger>
                     </FormControl>
                     <SelectContent>
                       {modelList.map(m => (
                         <SelectItem key={m.id} value={m.model_name}>
                           {m.model_name}
                         </SelectItem>
                       ))}
                     </SelectContent>
                   </Select>
                   <FormMessage />
                 </FormItem>
               )}
             />): (
               <FormItem>
                 <SectionHeader title="模型选择" tip="不同模型效果不同，建议自行测试" />
                  <Button type={'button'} variant={
                    'outline'
                  } onClick={()=>{goModelAdd()}}>请先添加模型</Button>
                 <FormMessage />
               </FormItem>
             )
            }

            {/* 分析模板 */}
            <FormField
              className="w-full"
              control={form.control}
              name="style"
              render={({ field }) => (
                <FormItem>
                  <SectionHeader title="分析模板" tip="选择内容洞察报告的呈现方式" />
                  <Select
                    value={field.value}
                    onValueChange={field.onChange}
                    defaultValue={field.value}
                  >
                    <FormControl>
                      <SelectTrigger className="w-full min-w-0 truncate">
                        <SelectValue />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {noteStyles.map(({ label, value }) => (
                        <SelectItem key={value} value={value}>
                          <span className="flex items-center gap-1.5">
                            {label}
                            {value === 'adaptive' && (
                              <span className="tc rounded bg-[var(--vi-ink)] px-1 py-0.2 text-[9px] font-semibold text-[var(--vi-surface)]">
                                AI
                              </span>
                            )}
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {field.value === 'adaptive' && (
                    <p className="text-[11px] text-[var(--vi-ink-3)]">
                      AI 将根据视频内容自动设计最合适的章节结构
                    </p>
                  )}
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>
          {/* 视频理解 */}
          <SectionHeader title="视频理解" tip="将视频截图发给多模态模型辅助分析" />
          <div className="flex flex-col gap-2">
            <FormField
              control={form.control}
              name="video_understanding"
              render={({ field }) => (
                <FormItem>
                  <div className="flex items-center gap-2">
                    <FormLabel>启用</FormLabel>
                    <Checkbox
                      checked={videoUnderstandingEnabled}
                      onCheckedChange={v => form.setValue('video_understanding', v)}
                    />
                  </div>
                  <FormMessage />
                </FormItem>
              )}
            />

            <div className="grid grid-cols-2 gap-4">
              {/* 采样间隔 */}
              <FormField
                control={form.control}
                name="video_interval"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>采样间隔（秒）</FormLabel>
                    <Input disabled={!videoUnderstandingEnabled} type="number" {...field} />
                    <FormMessage />
                  </FormItem>
                )}
              />
              {/* 拼图大小 */}
              <FormField
                control={form.control}
                name="grid_size"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>拼图尺寸（列 × 行）</FormLabel>
                    <div className="flex items-center space-x-2">
                      <Input
                        disabled={!videoUnderstandingEnabled}
                        type="number"
                        value={field.value?.[0] || 3}
                        onChange={e => field.onChange([+e.target.value, field.value?.[1] || 3])}
                        className="w-16"
                      />
                      <span>x</span>
                      <Input
                        disabled={!videoUnderstandingEnabled}
                        type="number"
                        value={field.value?.[1] || 3}
                        onChange={e => field.onChange([field.value?.[0] || 3, +e.target.value])}
                        className="w-16"
                      />
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <Alert variant="warning" className="text-sm">
              <AlertDescription>
                <strong>提示：</strong>视频理解功能必须使用多模态模型。
              </AlertDescription>
            </Alert>
          </div>

          {/* 报告元素 */}
          <FormField
            control={form.control}
            name="format"
            render={({ field }) => (
              <FormItem>
                <SectionHeader title="报告元素" tip="选择要包含的分析报告元素" />
                <CheckboxGroup
                  value={field.value}
                  onChange={field.onChange}
                  disabledMap={{
                    link: platform === 'local',
                    screenshot: !videoUnderstandingEnabled,
                  }}
                />
                <FormMessage />
              </FormItem>
            )}
          />

          {/* 备注 */}
          <FormField
            control={form.control}
            name="extras"
            render={({ field }) => (
              <FormItem>
                <SectionHeader title="补充要求" tip="可在 Prompt 结尾附加自定义分析要求" />
                <Textarea placeholder="例如：重点分析目标人群、用户痛点和下周可拍摄选题…" {...field} />
                <FormMessage />
              </FormItem>
            )}
          />
        </form>
      </Form>
    </div>
  )
}

export default NoteForm
