import request from '@/utils/request'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ChatSource {
  citation: string
  text: string
  video_title: string
  video_url: string
  source_type: 'meta' | 'markdown' | 'transcript'
  task_id: string
  section_title?: string
  start_time?: number
  end_time?: number
}

export interface ToolCallTrace {
  tool: string
  arguments: Record<string, unknown>
  round: number
}

/** Harness 工具轨迹：比旧 trace 多了受限标记、耗时、成功与否 */
export interface HarnessToolTrace {
  tool: string
  args: Record<string, unknown>
  /** 受限工具（会触达第三方平台），UI 需显式提示 */
  restricted: boolean
  elapsed_s: number
  ok: boolean
}

export type ToolKind = 'consume' | 'produce'

/** Harness 问答响应：在 AskResponse 之上携带生产/检索统一轨迹 */
export interface HarnessAskResponse {
  answer: string
  sources: ChatSource[]
  intent: 'search' | 'task' | 'chitchat' | 'produce'
  tool_calls: HarnessToolTrace[]
  kb_stats: KbStats
  /** 推理轮数 */
  rounds?: number
  /** 降级作答（超轮次或模型异常兜底） */
  degraded?: boolean
  elapsed_s?: number
}

export interface KbStats {
  total_chunks: number
  total_notes: number
  bm25_docs: number
  bm25_available: boolean
  reranker_available: boolean
}

// ====== 流式 Harness（SSE）：思考链 / 工具事件 / 正文增量 ======

/** SSE 事件类型，与后端 harness.chat_stream 的 yield 一一对应 */
export type HarnessStreamEvent =
  | { type: 'status'; stage: string; intent?: string; round?: number }
  | { type: 'thinking'; delta: string }
  | { type: 'tool'; tool: string; ok: boolean; restricted: boolean; elapsed_s: number; error?: string | null }
  | { type: 'delta'; text: string }
  | { type: 'final'; data: HarnessAskResponse & { thinking?: string } }
  | { type: 'error'; message: string; reason?: string }

/**
 * 流式 Harness 问答：fetch 读 SSE（axios 不支持真正的逐块流），逐事件回调。
 *
 * 返回 abort 函数（用户点停止 / 组件卸载时切断连接）。
 * 网络层任何失败都会以 onEvent({type:'error'}) 送达——调用方无需再 catch。
 */
export const askHarnessStream = (
  data: {
    question: string
    history: ChatMessage[]
    provider_id: string
    model_name: string
    task_id?: string
    allowed_tool_kinds?: ToolKind[]
  },
  onEvent: (e: HarnessStreamEvent) => void,
): { abort: () => void } => {
  const ctrl = new AbortController()
  ;(async () => {
    try {
      const base = (import.meta as any).env?.VITE_API_BASE_URL || '/api'
      const res = await fetch(`${base}/chat/ask/harness/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...data, allowed_tool_kinds: data.allowed_tool_kinds ?? ['consume', 'produce'] }),
        signal: ctrl.signal,
      })
      if (!res.ok || !res.body) {
        onEvent({ type: 'error', message: `后端连接失败（HTTP ${res.status}）`, reason: 'http_error' })
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // SSE 帧以空行分隔；逐帧解析，残帧留到下一轮
        const frames = buf.split('\n\n')
        buf = frames.pop() || ''
        for (const frame of frames) {
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue
            try {
              onEvent(JSON.parse(line.slice(6)) as HarnessStreamEvent)
            } catch { /* 半截 JSON 忽略，等下一帧 */ }
          }
        }
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') return // 用户主动停止，不算错误
      onEvent({ type: 'error', message: `连接中断: ${String(e?.message || e).slice(0, 150)}`, reason: 'network' })
    }
  })()
  return { abort: () => ctrl.abort() }
}

export interface AskResponse {
  answer: string
  sources: ChatSource[]
  intent: 'search' | 'task' | 'chitchat'
  tool_calls: ToolCallTrace[]
  kb_stats: KbStats
}

export interface LegacyAskResponse {
  answer: string
  sources: {
    text: string
    source_type: string
    section_title?: string
    start_time?: number
    end_time?: number
  }[]
}

export type IndexStatus = 'idle' | 'indexing' | 'indexed' | 'failed'

export interface ChatStatusResponse {
  indexed: boolean
  status: IndexStatus
}

// ====== 知识库管理 ======

export const indexTask = async (taskId: string): Promise<void> => {
  return await request.post('/chat/index', { task_id: taskId })
}

export const getChatStatus = async (taskId: string): Promise<ChatStatusResponse> => {
  return await request.get(`/chat/status?task_id=${taskId}`)
}

export const getKbStats = async (): Promise<KbStats> => {
  return await request.get('/chat/kb/stats')
}

export const rebuildKb = async (): Promise<{ rebuilt: number }> => {
  return await request.post('/chat/kb/rebuild')
}

// ====== Agent 问答（新版，主要使用）======

export const askAgent = async (data: {
  question: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
  task_id?: string
}): Promise<AskResponse> => {
  // suppressToast: 由 ChatPage 统一处理错误展示，避免全局 toast 重复弹
  return await request.post('/chat/ask', data, {
    timeout: 300000, // Agent 多轮推理，每轮 LLM API 可能 40-60s
    suppressToast: true,
  } as any)
}

// ====== 统一 Harness 问答（produce + consume 同环）======

/**
 * 走统一 harness 内核：既能检索问答，也能生产笔记/摄入收藏专辑。
 *
 * @param allowed_tool_kinds 场景隔离白名单——
 *   ['consume'] 只开检索（只读面板，绝不触发生产）
 *   ['produce'] 只开生产（导入面板）
 *   默认全开（主对话）
 */
export const askHarness = async (data: {
  question: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
  task_id?: string
  allowed_tool_kinds?: ToolKind[]
}): Promise<HarnessAskResponse> => {
  return await request.post(
    '/chat/ask/harness',
    { ...data, allowed_tool_kinds: data.allowed_tool_kinds ?? ['consume', 'produce'] },
    {
      // 生产工具（下载+转写+LLM）是分钟级，比纯检索更长
      timeout: 600000,
      suppressToast: true,
    } as any,
  )
}

// ====== 旧版问答（向后兼容）======

export const askLegacy = async (data: {
  task_id: string
  question: string
  history: ChatMessage[]
  provider_id: string
  model_name: string
}): Promise<LegacyAskResponse> => {
  return await request.post('/chat/ask/legacy', data, { timeout: 60000 })
}

// Alias for backward compatibility
export const askQuestion = askLegacy

// ====== 高阶意图路由（Premium / T7）======

export interface RouteIntentResponse {
  intent: 'parse' | 'compare' | 'ask' | 'chitchat' | 'auto_fallback'
  links: string[]
  reason: string
  cached?: boolean
}

export const routeIntent = async (data: {
  message: string
  links: string[]
  provider_id: string
  model_name: string
}): Promise<RouteIntentResponse> => {
  return await request.post('/chat/route_intent', data, { timeout: 15000 } as any)
}
