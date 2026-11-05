import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import {
  Form,
  FormField,
  FormItem,
  FormLabel,
  FormControl,
  FormMessage,
  FormDescription,
} from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useParams, useNavigate } from 'react-router-dom'
import { useProviderStore } from '@/store/providerStore'
import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { testConnection, fetchModels, deleteModelById, updateModelCapabilities, fetchProviderTemplates, addModel } from '@/services/model.ts'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select.tsx' // ⚡新增 fetchModels
import { ModelSelector } from '@/components/Form/modelForm/ModelSelector.tsx'
import {
  ModelCapabilityDialog,
  type ModelCapabilities,
} from '@/components/Form/modelForm/ModelCapabilityDialog.tsx'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert.tsx'
import { Tags } from 'lucide-react'
import { X } from 'lucide-react'
import { useModelStore } from '@/store/modelStore'

// ✅ Provider表单schema
const ProviderSchema = z.object({
  name: z.string().min(2, '名称不能少于 2 个字符'),
  apiKey: z.string().optional(),
  baseUrl: z.string().url('必须是合法 URL'),
  type: z.string(),
  // API 协议格式（参考 ZCode provider.api.type），默认 OpenAI 兼容
  apiFormat: z.string().default('openai-chat-completions').optional(),
})

type ProviderFormValues = z.infer<typeof ProviderSchema>

// ✅ Model表单schema
const ModelSchema = z.object({
  modelName: z.string().min(1, '请选择或填写模型名称'),
})

type ModelFormValues = z.infer<typeof ModelSchema>
interface IModel {
  id: string
  created: number
  object: string
  owned_by: string
  permission: string
  root: string
}
const ProviderForm = ({ isCreate = false }: { isCreate?: boolean }) => {
  let { id } = useParams()
  const navigate = useNavigate()
  const isEditMode = !isCreate

  const getProviderById = useProviderStore(state => state.getProviderById)
  const loadProviderById = useProviderStore(state => state.loadProviderById)
  const updateProvider = useProviderStore(state => state.updateProvider)
  const addNewProvider = useProviderStore(state => state.addNewProvider)
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState(false)
  const [isBuiltIn, setIsBuiltIn] = useState(false)
  const loadModelsById= useModelStore(state => state.loadModelsById)
  const [modelOptions, setModelOptions] = useState<IModel[]>([]) // ⚡新增，保存模型列表
  const [models, setModels]= useState([])
  // 正在配置能力的模型（null = 弹窗关闭）
  const [editingModel, setEditingModel] = useState<any>(null)
  // 预设带来的模型清单（用户填完 Key 后可一键批量启用）
  const [presetModels, setPresetModels] = useState<string[]>([])
  const [presetHint, setPresetHint] = useState<any>(null)
  const [bulkAdding, setBulkAdding] = useState(false)
  const [modelLoading, setModelLoading] = useState(false)
  const randomColor = ()=>{
    return '#' + Math.floor(Math.random() * 16777215).toString(16)
  }

  const [search, setSearch] = useState('')
  const providerForm = useForm<ProviderFormValues>({
    resolver: zodResolver(ProviderSchema),
    defaultValues: {
      name: '',
      apiKey: '',
      baseUrl: '',
      type: 'custom',
    },
  })
  const filteredModelOptions = modelOptions.filter(model => {
    const keywords = search.trim().toLowerCase().split(/\s+/) // 支持多个关键词
    const target = model.id.toLowerCase()
    return keywords.every(kw => target.includes(kw))
  })

  const modelForm = useForm<ModelFormValues>({
    resolver: zodResolver(ModelSchema),
    defaultValues: {
      modelName: '',
    },
  })

  useEffect(() => {

    const load = async () => {
      if (isEditMode) {

        const data = await loadProviderById(id!)
        // 后端返回 snake_case，表单用 camelCase
        providerForm.reset({
          name: data.name,
          apiKey: data.api_key,
          baseUrl: data.base_url,
          type: data.type,
          apiFormat: data.api_format || 'openai-chat-completions',
        })
        setIsBuiltIn(data.type === 'built-in')
      } else {
        // 新建：若 URL 带 ?preset=xxx，从预设拉 baseUrl / apiFormat / 名称预填
        const presetId = new URLSearchParams(window.location.search).get('preset')
        if (presetId) {
          try {
            const tpls = (await fetchProviderTemplates()) as any[]
            const tpl = tpls.find((t: any) => t.id === presetId)
            if (tpl) {
              providerForm.reset({
                name: tpl.name,
                apiKey: '',
                baseUrl: tpl.baseUrl,
                type: 'custom',
                apiFormat: tpl.apiFormat,
              })
              setPresetModels(tpl.models || [])
              setPresetHint(tpl)
              setIsBuiltIn(false)
              setLoading(false)
              return
            }
          } catch {
            // 模板拉取失败就退化成空表单，不阻塞
          }
        }
        providerForm.reset({
          name: '',
          apiKey: '',
          baseUrl: '',
          type: 'custom',
          apiFormat: 'openai-chat-completions',
        })
        setIsBuiltIn(false)
      }
      const models = await loadModelsById(id!)
      if(models){
        console.log('🔧 模型列表:', models)
        setModels(models)

      }
      setLoading(false)
    }
    load()
  }, [id])
  const handelDelete=async (modelId)=>{
    if (!window.confirm('确定要删除这个模型吗？')) return

    try {
      const res = await deleteModelById(modelId)
      console.log('🔧 删除结果:', res)

      toast.success('删除成功')

    } catch (e) {
      toast.error('删除异常')
    }
  }

  // 保存模型能力（弹窗里改完点保存）。乐观更新本地列表，失败提示但不回滚
  // ——能力只是元数据，回滚会让用户以为改动丢了，反而更困惑。
  const handleCapability = async (modelId: number, caps: ModelCapabilities) => {
    setModels(prev => prev.map(m => (m.id === modelId ? { ...m, capabilities: caps } : m)))
    try {
      await updateModelCapabilities(modelId, caps as Record<string, unknown>)
      toast.success('模型配置已保存')
    } catch (e) {
      toast.error('模型配置保存失败')
    }
  }
  // 测试连通性
  const handleTest = async () => {
    const values = providerForm.getValues()
    if (!values.apiKey || !values.baseUrl) {
      toast.error('请填写 API Key 和 Base URL')
      return
    }
    try {
      if (!id){
        toast.error('请先保存供应商信息')
        return
      }
      setTesting(true)
     await testConnection({
             id
          })

        toast.success('测试连通性成功 🎉')

    } catch (error) {

      toast.error(`连接失败: ${data.data.msg || '未知错误'}`)
      // toast.error('测试连通性异常')
    } finally {
      setTesting(false)
    }
  }

  // 加载模型列表
  const handleModelLoad = async () => {
    const values = providerForm.getValues()
    if (!values.apiKey || !values.baseUrl) {
      toast.error('请先填写 API Key 和 Base URL')
      return
    }
    try {
      setModelLoading(true) // ✅ 开始 loading
      const res = await fetchModels(id!, { noCache: true }) // 这里稍后解释
      if (res.data.code === 0 && res.data.data.models.data.length > 0) {
        setModelOptions(res.data.data.models.data)
        console.log('🔧 模型列表:', res.data.data)
        toast.success('模型列表加载成功 🎉')
      } else {
        toast.error('未获取到模型列表')
      }
    } catch (error) {
      toast.error('加载模型列表失败')
    } finally {
      setModelLoading(false) // ✅ 结束 loading
    }
  }

  // 保存Provider信息
  const onProviderSubmit = async (values: ProviderFormValues) => {
    // 前端 camelCase → 后端 snake_case
    const body = {
      name: values.name,
      api_key: values.apiKey,
      base_url: values.baseUrl,
      type: values.type,
      api_format: values.apiFormat || 'openai-chat-completions',
    }
    if (isEditMode) {
      await updateProvider({ ...body, id: id! })
      toast.success('更新供应商成功')
    } else {
       id = await addNewProvider(body)

      toast.success('新增供应商成功')
    }
    // 刷新页面

  }

  // 保存Model信息
  const onModelSubmit = async (values: ModelFormValues) => {
    toast.success(`保存模型: ${values.modelName}`)
    await loadModelsById(id!)
  }

  // 一键启用预设里的全部模型（需要先保存过供应商，否则没有 provider id）
  const handleBulkAddPreset = async () => {
    if (!id) {
      toast.error('请先保存供应商信息，再批量启用模型')
      return
    }
    const existing = new Set((models || []).map((m: any) => m.model_name))
    const todo = presetModels.filter(n => !existing.has(n))
    if (!todo.length) {
      toast.success('预设模型已全部启用')
      return
    }
    setBulkAdding(true)
    let ok = 0
    for (const name of todo) {
      try {
        await addModel({ provider_id: id!, model_name: name })
        ok++
      } catch {
        // 单个失败不阻塞
      }
    }
    setBulkAdding(false)
    await loadModelsById(id!)
    toast.success(`已启用 ${ok}/${todo.length} 个模型`)
  }

  if (loading) return <div className="p-4">加载中...</div>

  return (
    <div className="flex flex-col gap-8 p-4">
      {/* Provider信息表单 */}
      <Form {...providerForm}>
        <form
          onSubmit={providerForm.handleSubmit(onProviderSubmit)}
          className="flex max-w-xl flex-col gap-4"
        >
          <div className="text-lg font-bold">
            {isEditMode ? '编辑模型供应商' : '新增模型供应商'}
          </div>
          {/* 预设提示：告诉用户哪些字段是自动填好的，只需补 Key */}
          {presetHint && !isEditMode && (
            <div className="rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-hover)] px-3 py-2.5 text-sm">
              <p className="font-medium text-[var(--vi-ink)]">
                已套用预设「{presetHint.name}」
              </p>
              <p className="mt-1 text-xs text-[var(--vi-ink-2)]">
                API 格式与地址已自动填好，只需补 API Key。
                {presetHint.apiKeyUrl && (
                  <>
                    {' '}
                    <a
                      href={presetHint.apiKeyUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="underline underline-offset-2 hover:text-[var(--vi-ink)]"
                    >
                      获取 Key
                    </a>
                  </>
                )}
              </p>
              {presetModels.length > 0 && (
                <p className="mt-1.5 text-xs text-[var(--vi-ink-3)]">
                  预设包含 {presetModels.length} 个模型：
                  {presetModels.slice(0, 4).join('、')}
                  {presetModels.length > 4 ? ' 等' : ''}
                </p>
              )}
            </div>
          )}
          {!isBuiltIn && !presetHint && (
            <div className="text-sm text-red-500 italic">
              自定义模型供应商需要确保兼容 OpenAI SDK
            </div>
          )}
          <FormField
            control={providerForm.control}
            name="name"
            render={({ field }) => (
              <FormItem className="flex items-center gap-4">
                <FormLabel className="w-24 text-right">名称</FormLabel>
                <FormControl>
                  <Input {...field} disabled={isBuiltIn} className="flex-1" />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={providerForm.control}
            name="apiKey"
            render={({ field }) => (
              <FormItem className="flex items-center gap-4">
                <FormLabel className="w-24 text-right">API Key</FormLabel>
                <FormControl>
                  <Input {...field} className="flex-1" />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          {/* API 格式：照搬 ZCode 的「名称 + 端点路径」写法。
              同一供应商可能同时提供 Anthropic 和 OpenAI 两套端点
              （智谱有 /api/anthropic 和 /api/paas/v4），选错就 404，
              把路径写出来能显著降低配错率。 */}
          <FormField
            control={providerForm.control}
            name="apiFormat"
            render={({ field }) => (
              <FormItem className="flex items-center gap-4">
                <FormLabel className="w-24 text-right">API 格式</FormLabel>
                <FormControl>
                  <select
                    {...field}
                    value={field.value || 'openai-chat-completions'}
                    className="flex-1 rounded-md border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-3 py-2 text-sm text-[var(--vi-ink)]"
                  >
                    <option value="openai-chat-completions">
                      Chat Completions (/chat/completions)
                    </option>
                    <option value="anthropic-messages">
                      Anthropic Messages (/v1/messages)
                    </option>
                    <option value="openai-responses">Responses (/responses)</option>
                  </select>
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={providerForm.control}
            name="baseUrl"
            render={({ field }) => (
              <FormItem className="flex items-center gap-4">
                <FormLabel className="w-24 text-right">API地址</FormLabel>
                <FormControl>
                  <Input {...field} className="flex-1" />
                </FormControl>
                <Button type="button" onClick={handleTest} variant="ghost" disabled={testing}>
                  {testing ? '测试中...' : '测试连通性'}
                </Button>
                <FormMessage />
              </FormItem>
            )}
          />
          <FormField
            control={providerForm.control}
            name="type"
            render={({ field }) => (
              <FormItem className="flex items-center gap-4">
                <FormLabel className="w-24 text-right">类型</FormLabel>
                <FormControl>
                  <Input {...field} disabled className="flex-1" />
                </FormControl>
                <FormMessage />
              </FormItem>
            )}
          />
          <div className="pt-2">
            <Button type="submit" disabled={!providerForm.formState.isDirty}>
              {isEditMode ? '保存修改' : '保存创建'}
            </Button>
          </div>
        </form>
      </Form>

      {/* 模型信息表单 */}
      <div className="flex max-w-xl flex-col gap-4">
        <div className="flex flex-col gap-2">
          <span className="font-bold">模型列表</span>
          <div className={'flex flex-col gap-2 rounded bg-[#FEF0F0] p-2.5'}>
            <h2 className={'font-bold'}>注意!</h2>
            <span>请确保已经保存供应商信息,以及通过测试连通性.</span>
          </div>
          <ModelSelector providerId={id!} />

          {/*<datalist id="model-options">*/}
          {/*  {modelOptions.map(model => (*/}
          {/*    <option key={model.id + '1'} value={model.id} />*/}
          {/*  ))}*/}
          {/*</datalist>*/}
        </div>
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="font-bold">已启用模型</span>
            {/* 预设带来的模型一键启用：省掉逐个下拉添加 */}
            {presetModels.length > 0 && (
              <Button
                type="button"
                variant="ghost"
                className="h-7 text-xs"
                disabled={bulkAdding}
                onClick={handleBulkAddPreset}
              >
                {bulkAdding ? '添加中…' : `一键启用预设 ${presetModels.length} 个模型`}
              </Button>
            )}
          </div>
          <div className={'flex flex-wrap gap-2 rounded  p-2.5'}>
            {
              models && models.map(model => {
                return (
                  <span key={model.id} className="inline-flex items-center gap-1 rounded-md bg-neutral-100 px-2 py-0.5 text-sm text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300">
                    {model.model_name}
                    <button type="button" onClick={() => handelDelete(model.id)} className="hover:text-neutral-900 dark:hover:text-white">
                      <X className="h-3 w-3" />
                    </button>
                  </span>

                )
              })
            }

          </div>

          {/* 模型列表：每个模型一行，点「配置」打开能力弹窗
              （结构参考 ZCode 的 ProviderModelMetadataDialog） */}
          <span className="pt-2 font-bold">模型列表</span>
          <p className="text-xs text-neutral-500">
            点「配置」可设置输入类型（图片/视频/PDF）、上下文窗口、推理档位等。
            勾选「图片」后生成笔记时会抽取视频关键画面读图取信息。
          </p>
          <div className="flex flex-col gap-1.5">
            {models && models.map(model => {
              const caps = (model.capabilities || {}) as ModelCapabilities
              const canReadImage = !!caps.inputFormat?.supportsImage
              return (
                <div
                  key={model.id}
                  className="flex items-center gap-2 rounded-md border border-[var(--vi-hairline)] px-2.5 py-2"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-[var(--vi-ink)]">
                    {model.model_name}
                  </span>
                  {canReadImage && (
                    <span className="shrink-0 rounded bg-[var(--vi-hover)] px-1.5 py-0.5 text-[10px] text-[var(--vi-ink-2)]">
                      读图
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => setEditingModel(model)}
                    className="shrink-0 text-xs text-[var(--vi-ink-2)] underline-offset-2 hover:text-[var(--vi-ink)] hover:underline"
                  >
                    配置
                  </button>
                  <button
                    type="button"
                    onClick={() => handelDelete(model.id)}
                    className="shrink-0 text-[var(--vi-ink-3)] hover:text-red-500"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              )
            })}
            {(!models || models.length === 0) && (
              <span className="text-sm text-neutral-400">还没有启用的模型</span>
            )}
          </div>
          {/*<ModelSelector providerId={id!} />*/}

          {/*<datalist id="model-options">*/}
          {/*  {modelOptions.map(model => (*/}
          {/*    <option key={model.id + '1'} value={model.id} />*/}
          {/*  ))}*/}
          {/*</datalist>*/}
        </div>
      </div>

      {/* 模型能力配置弹窗 */}
      <ModelCapabilityDialog
        open={!!editingModel}
        modelName={editingModel?.model_name || ''}
        capabilities={(editingModel?.capabilities || {}) as ModelCapabilities}
        onClose={() => setEditingModel(null)}
        onSave={async caps => {
          if (editingModel) await handleCapability(editingModel.id, caps)
          setEditingModel(null)
        }}
      />
    </div>
  )
}

export default ProviderForm
