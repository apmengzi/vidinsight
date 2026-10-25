import request from '@/utils/request'
import toast from 'react-hot-toast'

export const generateNote = async (data: {
  video_url: string
  platform: string
  quality: string
  model_name: string
  provider_id: string
  task_id?: string
  format: Array<string>
  style: string
  extras?: string
  video_understand?: boolean
  video_interval?: number
  grid_size: Array<number>
}) => {
  try {
    console.log('generateNote', data)
    // 2026-10-20：后端首次任务有懒加载冷启动（is_model_ready + NoteGenerator
    // 构造链首次执行 8-10s），吃 axios 全局 10s 必超时——任务实际已受理并成功，
    // 前端却报"提交失败"（06:58 实测：任务 25s 生成完毕，UI 却显示网络错误）。
    // 提交确认类请求给 30s 覆盖冷启动；任务本体后台跑+轮询，不受此影响。
    const response = await request.post('/generate_note', data, { timeout: 30000 })

    if (!response) {
      if (response?.data?.msg) {
        toast.error(response.data.msg)
      }
      return null
    }
    toast.success('内容洞察任务已提交')

    console.log('res', response)
    return response
  } catch (e: any) {
    console.error('请求出错', e)
    throw e
  }
}

export const delete_task = async ({ video_id, platform }) => {
  try {
    const data = {
      video_id,
      platform,
    }
    const res = await request.post('/delete_task', data)

    toast.success('分析记录已删除')
    return res
  } catch (e) {
    toast.error('请求异常，删除分析记录失败')
    console.error('删除分析记录失败:', e)
    throw e
  }
}

export const expandXhsCollection = async (video_url: string) => {
  try {
    const res = await request.post('/xhs/expand_collection', { video_url })
    return res.data ?? res
  } catch (e: any) {
    console.error('合集解析失败', e)
    throw e
  }
}

// ====== 全平台「贴链接即预览」（/api/parse 统一解析层）======

export interface LinkPreview {
  success: boolean
  platform: string
  engine?: string
  url?: string
  title?: string
  duration_s?: number | null
  uploader?: string
  description?: string
  cover?: string
  tags?: string[]
  is_video?: boolean
  formats?: { format_id?: string; ext?: string; height?: number | null; url: string }[]
  // 小红书富字段
  note_type?: string
  images?: { url: string }[]
  likes?: string
  collected?: string
  comments?: string
  ip_location?: string
  // 失败契约
  reason?: string
  hint?: string
  limited?: boolean
}

// 预览是纯 UX 增强：任何失败都返回结构化结果或 null，绝不抛错阻塞主流程
// （生成管线有自己的平台专用通道，预览失败 ≠ 生成失败）
export const parseVideoLink = async (url: string): Promise<LinkPreview | null> => {
  try {
    const res = await request.post('/parse', { video_url: url })
    return (res.data ?? res) as LinkPreview
  } catch (e) {
    console.warn('链接预览失败（不影响生成）:', e)
    return null
  }
}

export const cancel_task = async (task_id: string) => {
  try {
    return await request.post(`/tasks/${task_id}/cancel`)
  } catch (e) {
    console.error('取消任务失败', e)
    throw e
  }
}

export interface TaskStatusResponse {
  status: string
  message?: string
  progress?: number | null
  stage_progress?: number | null
  elapsed_s?: number
  stage_times?: Record<string, number>
  task_id: string
  result?: {
    markdown?: string
    transcript?: any
    audio_meta?: any
    [key: string]: any
  }
}

export const get_task_status = async (task_id: string): Promise<TaskStatusResponse> => {
  try {
    // suppressToast：FAILED 响应由轮询方分类展示（后端已翻译成中文原因），
    // 拦截器不再重复弹通用错误
    return await request.get('/task_status/' + task_id, { suppressToast: true })
  } catch (e) {
    console.error('请求出错', e)
    throw e
  }
}

// ====== 历史页后端兜底 ======

export interface ServerTaskItem {
  task_id: string
  status: string
  created_at: string
  title: string
  platform: string
  video_id: string
  cover_url: string
  duration: number
  video_url: string
  markdown: string
}

export interface ServerTaskList {
  total: number
  items: ServerTaskItem[]
}

// note_results 是唯一持久层；浏览器 IndexedDB 只存本浏览器提交过的任务。
// 历史页挂载时拉后端全量列表合并，换浏览器/清缓存后历史不再凭空消失。
export const fetchTaskList = async (limit = 200): Promise<ServerTaskList> => {
  try {
    return await request.get('/tasks/list', { params: { limit }, suppressToast: true })
  } catch (e) {
    console.warn('拉取后端任务列表失败（历史页退化为仅本地记录）', e)
    return { total: 0, items: [] }
  }
}

// ====== 小红书合集 token 收割（WebView 通道）======

// 桌面端开一个 WebView 加载合集页。两种模式：
// - 收割模式（已废弃）：自动导航/搜索/滚动，触发过小红书账号警告
// - 被动模式（当前唯一）：窗口只当记录仪，用户像平常一样浏览，
//   XHR/fetch 钩子顺路记录 XHS 自己返回的笔记数据，零自动行为
// 窗口被用户关闭时 invoke 会 reject（"已取消"），调用方据此中止后续弹窗。
export interface HarvestPhaseEvent {
  event: string // phase | login | captcha | done | partial | none | fail
  msg: string // 阶段进度文案（fail 时为空，用 reason）
  reason: string
  diag?: string // fail 时的页面实况诊断（path/ready/bodyLen/ua）
}

export const fetchXhsCollectionViaWebview = async (
  url: string,
  onPhase?: (e: HarvestPhaseEvent) => void,
): Promise<void> => {
  const tauri = (window as any).__TAURI_INTERNALS__
  if (!tauri) return  // 浏览器 dev 模式无 Tauri 环境：跳过收割，走后端裸展开
  let unlisten: (() => void) | null = null
  if (onPhase) {
    try {
      // Rust 侧收到 WebView JS 的信号时全局 emit；这里只挂本次调用的监听
      const { listen } = await import('@tauri-apps/api/event')
      unlisten = await listen<HarvestPhaseEvent>('xhs-harvest-phase', e => onPhase(e.payload))
    } catch {
      unlisten = null // 事件通道不可用只影响进度展示，不影响收割本身
    }
  }
  try {
    const result: string = await tauri.invoke('fetch_xhs_collection_detail', { url, passive: true })
    // 页面 CSP 拦截了 WebView 内的 fetch 时，数据经标题通道带回，由这里转投
    if (result) {
      await request.post('/xhs/ingest_tokens', JSON.parse(result), { timeout: 5000 } as any)
    }
  } finally {
    unlisten?.()
  }
}

// ====== 通用合集展开 ======

export interface ExpandedCollection {
  name: string
  total: number
  logged_in?: boolean
  notes: { note_id: string; title: string; type: string; url: string; has_token?: boolean; cached?: boolean }[]
}

// 支持平台：小红书合集、YouTube 播放列表、B站合集（yt-dlp 平铺）。
// 抖音/快手合集依赖平台签名接口，后端会返回明确的错误说明。
export const expandCollection = async (video_url: string, platform: string) => {
  try {
    const res = await request.post('/collections/expand', { video_url, platform })
    return (res.data ?? res) as ExpandedCollection
  } catch (e: any) {
    console.error('合集解析失败', e)
    throw e
  }
}

// ====== 收藏专辑（board）======

export interface BoardNote {
  note_id: string
  title: string
  type: string
  /** 封面 URL（捕获时从卡片对象顺路提取，xhscdn 需经 image_proxy 展示） */
  cover?: string
  /** 本地封面路径（/static/covers/...，本地离线优先） */
  cover_local?: string
  xsec_token?: string
  xsec_source?: string
  detail_cached?: boolean
}

export interface CookieStatusItem {
  valid: boolean
  hint?: string
}

export interface CookieStatusResponse {
  xhs?: CookieStatusItem
  bilibili?: CookieStatusItem
  [platform: string]: CookieStatusItem | undefined
}

// 平台 Cookie 状态预检（返回 { xhs: { valid, hint }, bilibili: { valid, hint } }）
export const getCookieStatus = async (): Promise<CookieStatusResponse> => {
  const res = await request.get('/xhs/cookie_status', { suppressToast: true })
  return (res.data ?? res) as CookieStatusResponse
}

export interface BoardInfo {
  board_id: string
  name: string
  total: number
  captured: number
  notes: BoardNote[]
}

// 专辑壳解析（匿名 SSR）+ 已捕获笔记清单合并
export const expandBoard = async (video_url: string): Promise<BoardInfo> => {
  // 同 generate_note：后端展开要打小红书 API（网络往返 + 冷启动），10s 全局超时不够
  const res = await request.post('/collections/expand', { video_url, platform: 'xiaohongshu' }, { timeout: 30000 })
  return (res.data ?? res) as BoardInfo
}

// 打开被动捕获窗口（用户自己翻收藏，XHR 顺路记录）；返回窗口 label
export const openBoardCaptureWindow = async (url: string): Promise<string> => {
  const tauri = (window as any).__TAURI_INTERNALS__
  if (!tauri) throw new Error('桌面端专属功能：请在 VidInsight 桌面应用内使用')
  return (await tauri.invoke('open_xhs_passive_window', { url })) as string
}

// 关闭全部 XHS 捕获窗口
export const closeXhsWindows = async (): Promise<void> => {
  const tauri = (window as any).__TAURI_INTERNALS__
  if (!tauri) return
  await tauri.invoke('close_xhs_windows').catch(() => {})
}

// 拉取专辑勾选工作台数据（被动捕获的笔记清单 + 详情缓存标记）
export const getBoardNotes = async (boardId: string): Promise<BoardInfo> => {
  const res = await request.get(`/xhs/board/${boardId}/notes`)
  return (res.data ?? res) as BoardInfo
}

// ── 一图流 / 配图 ────────────────────────────────
// 两条路：A 本地渲染（零依赖保底）；B 生成提示词供用户在 Miora 等工具里
// 手动生图（不代登录、不自动点击，符合真人行为红线）。

export interface OnePagerResult {
  url: string
  path: string
}

export const buildOnePager = async (payload: {
  markdown: string
  source_label?: string
  cover_url?: string
}): Promise<OnePagerResult> => {
  const res = await request.post('/note/one_pager', payload)
  return (res.data ?? res) as OnePagerResult
}

export const buildImagePrompts = async (payload: {
  markdown: string
  style?: string
}): Promise<{ prompts: string[]; raw: string }> => {
  const res = await request.post('/note/image_prompt', payload)
  return (res.data ?? res) as { prompts: string[]; raw: string }
}
