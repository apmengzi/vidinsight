import request from '@/utils/request.ts'

// opts.silent: 让本次请求失败时不弹全局红 toast（调用方自行 catch 处理，
// 比如 onboarding 撞名重试这种预期内失败）
interface CallOpts { silent?: boolean }
const cfg = (o?: CallOpts) => (o?.silent ? { suppressToast: true } : undefined)

export const getProviderList = async (opts?: CallOpts) => {
  return await request.get('/get_all_providers', cfg(opts))
}
export const getProviderById = async (id: string) => {
  return await request.get(`/get_provider_by_id/${id}`)
}
export const updateProviderById = async (data: any, opts?: CallOpts) => {
  return await request.post('/update_provider', data, cfg(opts))
}

export const addProvider = async (data: any, opts?: CallOpts) => {
  return await request.post('/add_provider', data, cfg(opts))
}

export const testConnection = async (data: any, opts?: CallOpts) => {
  return await request.post('/connect_test', data, cfg(opts))
}

export const fetchModels = async (providerId: string) => {
  return await request.get('/model_list/' + providerId)
}

export const fetchEnableModelById = async (id: string) => {
  return await request.get('/model_enable/' + id)
}

export async function addModel(
  data: { provider_id: string; model_name: string },
  opts?: CallOpts,
) {
  return request.post('/models', data, cfg(opts))
}

export const fetchEnableModels = async () => {
  return await request.get('/model_list')
}

export const deleteModelById = async (modelId: number) => {
  return await request.get(`/models/delete/${modelId}`)
}

// 模型能力标记：image=支持读图（关键帧抽取用），video=支持视频输入
export const updateModelCapabilities = async (
  modelId: number,
  capabilities: Record<string, unknown>,
) => {
  return await request.post('/model_capabilities', { model_id: modelId, capabilities })
}

// 供应商预设模板（结构参考 ZCode 的 templateRules）
export const fetchProviderTemplates = async () => {
  const res = await request.get('/provider_templates')
  return (res.data ?? res) as unknown[]
}

// API 协议格式枚举（名称 + 端点路径）
export const fetchApiFormats = async () => {
  const res = await request.get('/api_formats')
  return (res.data ?? res) as { value: string; label: string; path: string }[]
}
// 真实拉取 OpenAI 兼容供应商的可用模型清单（/v1/models）。
// 首启引导/供应商配置页用：填完 key 点「检测可用模型」→ 下拉选择。
export const fetchRemoteModels = async (api_key: string, base_url: string, opts?: CallOpts) => {
  const res = await request.post('/remote_models', { api_key, base_url }, cfg(opts))
  const d = (res.data ?? res) as { models?: string[]; count?: number }
  return d.models ?? []
}
