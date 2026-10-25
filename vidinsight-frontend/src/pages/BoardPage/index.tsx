import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertCircle, Bookmark, Check, ExternalLink, FolderPlus, Loader2, RefreshCw, Square, X } from 'lucide-react'
import { toast } from 'sonner'
import {
  BoardNote,
  closeXhsWindows,
  expandBoard,
  generateNote,
  getBoardNotes,
  getCookieStatus,
  openBoardCaptureWindow,
} from '@/services/note'
import { useModelStore } from '@/store/modelStore'
import { useTaskStore } from '@/store/taskStore'
import { proxiedImageUrl, resolveAssetUrl } from '@/utils/image-url'

// 收藏专辑勾选工作台（2026-09-21 产品转向：别人的合集 → 用户自己的收藏专辑）
// 流程：贴 board 链接 → 壳解析（专辑名/总数）+ 已捕获清单 →
//   「打开捕获窗口」翻自己的收藏（被动记录，零自动化）→ 列表出现 →
//   勾选 → 批量提交生成（沿用全局任务轮询）。

const BOARD_URL_RE = /xiaohongshu\.com\/board\/([0-9a-f]{24})/i

// 捕获链路目前只存轻量元数据（无时长）。后端一旦补上 duration，右上时长徽标自动出现。
type BoardNoteWithDuration = BoardNote & { duration?: number }

/** 秒 → 时间码（TC 03:45 / TC 1:02:03） */
function formatTimecode(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  const ss = String(r).padStart(2, '0')
  return h > 0 ? `TC ${h}:${mm}:${ss}` : `TC ${mm}:${ss}`
}

// 就绪面板底纹：16px 极淡点阵（只用既有 hairline token，不引入新色相）
const DOT_GRID = {
  backgroundImage: 'radial-gradient(var(--vi-hairline) 1px, transparent 1px)',
  backgroundSize: '16px 16px',
} as const

type Phase = 'idle' | 'loading' | 'capturing' | 'ready'

export default function BoardPage() {
  const navigate = useNavigate()
  const modelList = useModelStore(state => state.modelList)
  const loadEnabledModels = useModelStore(state => state.loadEnabledModels)
  const addPendingTask = useTaskStore(state => state.addPendingTask)

  const [url, setUrl] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [inlineError, setInlineError] = useState('')
  const [board, setBoard] = useState<{ boardId: string; name: string; total: number } | null>(null)
  const [notes, setNotes] = useState<BoardNote[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)

  // ── 多专辑支持（2026-10-17 用户需求）──
  // 专辑列表持久化在 localStorage，切 tab 即加载对应笔记清单。
  type SavedBoard = { boardId: string; name: string; total: number; captured?: number }
  const [boards, setBoards] = useState<SavedBoard[]>(() => {
    try {
      return JSON.parse(localStorage.getItem('vidinsight_boards') || '[]')
    } catch { return [] }
  })
  const persistBoards = (list: SavedBoard[]) => {
    setBoards(list)
    localStorage.setItem('vidinsight_boards', JSON.stringify(list))
  }

  // 「添加专辑」弹层/面板折叠开关
  const [showAddModal, setShowAddModal] = useState(false)

  // 登录态预检与告警条状态
  const [cookieWarning, setCookieWarning] = useState<string | null>(null)

  const verifyLoginStatus = useCallback(async (opts?: { silent?: boolean }) => {
    try {
      const res = await getCookieStatus()
      const xhs = res?.xhs
      if (xhs && !xhs.valid) {
        const hint = xhs.hint || '小红书登录态已失效，请重新登录'
        setCookieWarning(hint)
        toast.error(`小红书登录态异常：${hint}`, {
          duration: 8000,
          action: {
            label: '重新登录',
            onClick: () => {
              if (board) {
                openBoardCaptureWindow(`https://www.xiaohongshu.com/board/${board.boardId}`)
                  .then(() => setPhase('capturing'))
                  .catch((e: any) => toast.error(`打开窗口失败：${e?.message || e}`))
              } else {
                openBoardCaptureWindow('https://www.xiaohongshu.com')
                  .then(() => setPhase('capturing'))
                  .catch((e: any) => toast.error(`打开窗口失败：${e?.message || e}`))
              }
            },
          },
        })
        return false
      }
      setCookieWarning(null)
      return true
    } catch {
      // 预检接口若不可达（后端未就绪），保持静默不阻断工作流
      return true
    }
  }, [board])

  // 页面初次进入时执行登录预检；若有已保存专辑且尚未选定，默认激活第一个
  useEffect(() => {
    verifyLoginStatus({ silent: true })
  }, [verifyLoginStatus])

  const switchBoard = useCallback(async (b: SavedBoard) => {
    setBoard(b)
    setSelected(new Set())
    verifyLoginStatus({ silent: true })
    try {
      const info = await getBoardNotes(b.boardId)
      const list = info.notes ?? []
      setNotes(list)
      // 同步更新持久化项中的已捕获条数
      setBoards(prev => {
        const next = prev.map(item => item.boardId === b.boardId ? { ...item, captured: list.length } : item)
        localStorage.setItem('vidinsight_boards', JSON.stringify(next))
        return next
      })
    } catch (e: any) {
      // 切专辑失败此前静默清空清单：用户看到「已捕获 0 条」，会误以为
      // 自己抓的数据丢了。明确告知是拉取失败，并保留原有列表。
      toast.error(`切换专辑失败：${e?.msg || e?.message || '未知错误'}`)
    }
  }, [verifyLoginStatus])

  // 默认加载第一个专辑
  useEffect(() => {
    if (!board && boards.length > 0) {
      switchBoard(boards[0])
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 链接类型即时判定：贴错立刻内联提示（不用点按钮才发现）
  const urlKind = useMemo((): 'board' | 'collection' | 'invalid' => {
    const t = url.trim()
    if (/xiaohongshu\.com\/board\/[0-9a-f]{24}/i.test(t)) return 'board'
    if (/xiaohongshu\.com\/collection\/item\//i.test(t)) return 'collection'
    return 'invalid'
  }, [url])

  useEffect(() => { loadEnabledModels() }, [loadEnabledModels])

  // 2026-10-20：默认模型曾找 'deepseek-v4-pro'（不存在）→ 兜底列表第一位，
  // 撞上无 key 供应商直接「模型供应商错误」。改为 workbuddy/deepseek 优先，
  // 且允许在批量生成栏手动换模型（localStorage 记忆，跨会话保留）。
  const [boardModelKey, setBoardModelKey] = useState<string>(
    () => localStorage.getItem('vidinsight_board_model') || '',
  )
  const pickBoardModel = (key: string) => {
    setBoardModelKey(key)
    localStorage.setItem('vidinsight_board_model', key)
  }
  const targetModel = useMemo(() => {
    if (boardModelKey) {
      const [pid, mname] = boardModelKey.split('::')
      const manual = modelList.find(m => m.provider_id === pid && m.model_name === mname)
      if (manual) return manual
    }
    return modelList.find(m => m.provider_id === 'workbuddy' && m.model_name === 'deepseek-v4.1-flash')
      || modelList.find(m => m.provider_id === 'workbuddy' && (m.model_name || '').includes('deepseek'))
      || modelList.find(m => m.provider_id === 'workbuddy')
      || modelList[0] || null
  }, [modelList, boardModelKey])

  const refreshNotes = useCallback(async (boardId: string): Promise<boolean> => {
    // 2026-10-20 防零捕获假象：关窗瞬间后端在冲刷捕获数据（ingest 突发 +
    // 封面落盘），紧随其后的清单拉取偶发超时。此前一次失败就弹「网络错误」
    // 且列表停在旧值，用户误以为「一条都没捕获到」（实测数据其实全在库里）。
    // 改为小重试（3 次 × 2s 退避），全部失败才报错且明确告知数据已保存。
    // 成功前绝不动 notes——失败不清空、不显示过期计数当最终态。
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const info = await getBoardNotes(boardId)
        setNotes(info.notes ?? [])
        return true
      } catch (e: any) {
        if (attempt < 2) {
          await new Promise(r => setTimeout(r, 2000))
          continue
        }
        toast.error(`拉取专辑清单失败（捕获数据已保存，不会丢失），请稍后点「刷新」重试：${e?.msg || e?.message || '未知错误'}`)
        return false
      }
    }
    return false
  }, [])

  const handleLoad = useCallback(async () => {
    setInlineError('')
    const t = url.trim()
    if (urlKind === 'collection') {
      setInlineError('这是「合集」链接（别人的合集走内容分析页）。本页面是你的收藏专辑：请粘贴 xiaohongshu.com/board/… 形式的链接——手机 App 里打开专辑 → 右上角分享 → 复制链接')
      return
    }
    if (urlKind !== 'board') {
      setInlineError('请粘贴小红书收藏专辑链接（形如 xiaohongshu.com/board/…）。手机 App 里打开专辑 → 分享 → 复制链接')
      return
    }
    setPhase('loading')
    try {
      const info = await expandBoard(t)
      const nb = { boardId: info.board_id, name: info.name, total: info.total }
      setBoard(nb)
      setNotes(info.notes ?? [])
      setSelected(new Set())
      setPhase('ready')
      // 多专辑：加入持久化列表（已存在则更新名字/总数）
      setBoards(prev => {
        const next = prev.filter(b => b.boardId !== nb.boardId)
        next.unshift(nb)
        localStorage.setItem('vidinsight_boards', JSON.stringify(next))
        return next
      })
      if ((info.notes?.length ?? 0) === 0) {
        toast.info(`专辑《${info.name}》共 ${info.total} 条，还没有捕获记录。点「打开捕获窗口」翻一遍你的收藏即可`, { duration: 8000 })
      }
    } catch (e: any) {
      setPhase('idle')
      const msg = e?.response?.data?.message || e?.message || '未知错误'
      setInlineError(`专辑解析失败：${msg}`)
    }
  }, [url, urlKind])

  const handleCapture = useCallback(async () => {
    if (!board) return
    const ok = await verifyLoginStatus({ silent: false })
    if (!ok) return
    try {
      await openBoardCaptureWindow(`https://www.xiaohongshu.com/board/${board.boardId}`)
      setPhase('capturing')
      toast.info('捕获窗口已打开：点右上角「⇩」自动滚到底（或自己翻），滚完会自动关窗并刷新清单', { duration: 10000 })
    } catch (e: any) {
      toast.error(`打开捕获窗口失败：${typeof e === 'string' ? e : e?.message || '未知错误'}`)
    }
  }, [board, verifyLoginStatus])

  // 捕获中自动检测窗口关闭 → 自动刷新清单（用户手动关窗 / 自动滚完关窗 都生效）
  useEffect(() => {
    if (phase !== 'capturing' || !board) return
    const tauri = (window as any).__TAURI_INTERNALS__
    if (!tauri) return
    let stopped = false
    const timer = setInterval(async () => {
      if (stopped) return
      try {
        const open = await tauri.invoke('xhs_capture_window_open')
        if (!open) {
          stopped = true
          clearInterval(timer)
          setPhase('ready')
          const ok = await refreshNotes(board.boardId)
          if (ok) toast.success('捕获窗口已关闭，清单已刷新')
        }
      } catch {
        /* 忽略：窗口可能刚关闭 */
      }
    }, 2000)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [phase, board, refreshNotes])

  const handleStopCapture = useCallback(async () => {
    await closeXhsWindows()
    setPhase('ready')
    if (board) await refreshNotes(board.boardId)
  }, [board, refreshNotes])

  // ── 「刷新清单」= 后台自动捕获（2026-10-17 用户需求）──
  // 点击后自动打开捕获窗口 → 自动滚动遍历 → 自动关窗 → 自动刷新，
  // 全程后台完成，用户不用盯着窗口（窗口会自动跑完全流程）。
  const handleRefreshCapture = useCallback(async () => {
    if (!board) return
    const ok = await verifyLoginStatus({ silent: false })
    if (!ok) return
    try {
      await openBoardCaptureWindow(`https://www.xiaohongshu.com/board/${board.boardId}`)
      setPhase('capturing')
      toast.info('正在后台更新清单：窗口会自动滚动遍历全部笔记，完成后自动关闭并刷新', { duration: 8000 })
    } catch (e: any) {
      toast.error(`后台捕获失败：${typeof e === 'string' ? e : e?.message || '未知错误'}`)
    }
  }, [board, verifyLoginStatus])

  const toggle = (nid: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(nid)) next.delete(nid)
      else next.add(nid)
      return next
    })
  }

  const resolveCoverUrl = useCallback((n: BoardNote) => {
    // 封面显示：img src 优先 cover_local（Tauri 相对路径自动拼后端绝对地址），fallback 原 cover（走 proxiedImageUrl / image_proxy）
    if (n.cover_local) {
      return resolveAssetUrl(n.cover_local)
    }
    if (n.cover) {
      return proxiedImageUrl(n.cover)
    }
    return ''
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!board || selected.size === 0 || !targetModel) return
    setSubmitting(true)
    try {
      const picked = notes.filter(n => selected.has(n.note_id))
      let ok = 0
      for (const n of picked) {
        const noteUrl = n.xsec_token
          ? `https://www.xiaohongshu.com/explore/${n.note_id}?xsec_token=${encodeURIComponent(n.xsec_token)}&xsec_source=${n.xsec_source || 'pc_user'}`
          : `https://www.xiaohongshu.com/explore/${n.note_id}`
        try {
          const data: any = await generateNote({
            video_url: noteUrl,
            platform: 'xiaohongshu',
            quality: 'medium',
            model_name: targetModel.model_name,
            provider_id: targetModel.provider_id,
            style: 'adaptive',
            format: ['link', 'summary'],
            extras: '',
            video_understand: false,
            grid_size: [3, 3],
          })
          const taskId: string = data?.task_id || ''
          if (taskId) {
            addPendingTask(taskId, 'xiaohongshu', { video_url: noteUrl })
            ok++
          }
        } catch (e: any) {
          toast.error(`「${(n.title || '').slice(0, 24)}」提交失败：${e?.response?.data?.message || e?.message || '未知原因'}`)
        }
      }
      if (ok > 0) {
        toast.success(`已提交 ${ok} 条生成任务，可在「分析历史」查看进度`)
        navigate('/history')
      }
    } finally {
      setSubmitting(false)
    }
  }, [board, selected, notes, targetModel, addPendingTask, navigate])

  const cachedCount = notes.filter(n => n.detail_cached).length

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-1 flex items-center gap-2">
        <Bookmark className="h-5 w-5 text-[var(--vi-ink)]" />
        <h1 className="text-lg font-semibold text-neutral-800">我的收藏 · 知识库入库</h1>
      </div>
      <p className="mb-5 text-sm text-neutral-500">
        粘贴你的小红书收藏专辑链接 → 打开捕获窗口翻一遍收藏（工具只顺路记录，不会自动操作）→ 勾选想分析的笔记批量生成洞察
      </p>

      {/* ── 登录态失效警示条 ── */}
      {cookieWarning && (
        <div className="mb-4 flex items-center justify-between rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>小红书登录态异常：{cookieWarning}</span>
          </div>
          <button
            onClick={() => {
              if (board) {
                openBoardCaptureWindow(`https://www.xiaohongshu.com/board/${board.boardId}`)
                  .then(() => setPhase('capturing'))
                  .catch((e: any) => toast.error(`打开窗口失败：${e?.message || e}`))
              } else {
                openBoardCaptureWindow('https://www.xiaohongshu.com')
                  .then(() => setPhase('capturing'))
                  .catch((e: any) => toast.error(`打开窗口失败：${e?.message || e}`))
              }
            }}
            className="ml-3 shrink-0 rounded border border-amber-300 bg-white px-2.5 py-1 text-xs font-medium text-amber-800 shadow-xs hover:bg-amber-100/60 dark:border-amber-800 dark:bg-neutral-900 dark:text-amber-200"
          >
            打开捕获窗口重新登录
          </button>
        </div>
      )}

      {/* ── 多专辑切换器与添加专辑入口 ── */}
      <div className="mb-5 flex flex-wrap items-center gap-2 border-b border-neutral-200 pb-4 dark:border-neutral-800">
        <span className="mr-1 shrink-0 text-sm font-medium text-neutral-500">我的专辑</span>
        {boards.map(b => {
          const active = board?.boardId === b.boardId
          const count = b.captured !== undefined ? b.captured : b.total
          return (
            <button
              key={b.boardId}
              onClick={() => switchBoard(b)}
              className={`group flex items-center gap-2 rounded-lg border px-3.5 py-2 text-sm font-medium transition-colors ${
                active
                  ? 'border-[var(--vi-ink-3)] bg-neutral-100 text-[var(--vi-ink)] shadow-xs dark:bg-neutral-800'
                  : 'border-neutral-300 bg-white text-neutral-700 hover:border-neutral-400 hover:bg-neutral-50 dark:bg-neutral-900 dark:border-neutral-700 dark:text-neutral-300'
              }`}
            >
              <Bookmark className={`h-4 w-4 ${active ? 'text-[var(--vi-ink)]' : 'text-neutral-400'}`} />
              <span>{b.name}</span>
              <span className={`rounded-full px-1.5 py-0.5 text-[11px] ${
                active ? 'bg-neutral-200 text-neutral-800 dark:bg-neutral-700 dark:text-neutral-200' : 'bg-neutral-100 text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400'
              }`} title={b.captured !== undefined ? `已捕获 ${b.captured} 条` : `未捕获，平台账面 ${b.total} 条`}>
                {count}
              </span>
              <span
                onClick={e => {
                  e.stopPropagation()
                  const next = boards.filter(x => x.boardId !== b.boardId)
                  persistBoards(next)
                  if (active) {
                    if (next.length > 0) {
                      switchBoard(next[0])
                    } else {
                      setBoard(null)
                      setNotes([])
                    }
                  }
                }}
                className="ml-0.5 cursor-pointer rounded p-0.5 text-neutral-300 opacity-0 transition-opacity hover:bg-neutral-200 hover:text-neutral-600 group-hover:opacity-100 dark:hover:bg-neutral-700"
                title="从列表移除（不影响已捕获数据）"
              >
                <X className="h-3 w-3" />
              </span>
            </button>
          )
        })}

        <button
          onClick={() => {
            setShowAddModal(prev => !prev)
            setInlineError('')
          }}
          className={`flex items-center gap-1.5 rounded-lg border border-dashed px-3 py-2 text-sm font-medium transition-colors ${
            showAddModal
              ? 'border-[var(--vi-ink)] bg-neutral-100 text-[var(--vi-ink)]'
              : 'border-neutral-300 text-neutral-600 hover:border-[var(--vi-ink-3)] hover:text-neutral-800 dark:border-neutral-700 dark:text-neutral-400'
          }`}
        >
          <FolderPlus className="h-4 w-4 text-[var(--vi-ink-3)]" />
          <span>添加专辑</span>
        </button>
      </div>

      {/* ── 添加专辑录入卡片（折叠展开或在无专辑时默认显示） ── */}
      {(showAddModal || boards.length === 0) && (
        <div className="mb-6 rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-4 shadow-xs">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-[var(--vi-ink-2)]">
              添加新收藏专辑
            </span>
            {boards.length > 0 && (
              <button
                onClick={() => setShowAddModal(false)}
                className="rounded p-1 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600 dark:hover:bg-neutral-800"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <p className="mb-3 text-xs text-neutral-500">
            支持小红书 App 分享的收藏专辑链接（xiaohongshu.com/board/…）。粘贴解析后将自动添加到上方专辑库并载入。
          </p>
          <div className="flex gap-2">
            <input
              value={url}
              onChange={e => { setUrl(e.target.value); setInlineError('') }}
              onKeyDown={e => e.key === 'Enter' && handleLoad()}
              placeholder="https://www.xiaohongshu.com/board/…"
              className={`flex-1 rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--vi-ink-3)] ${
                urlKind === 'collection' ? 'border-amber-300 bg-amber-50' : 'border-neutral-200'
              }`}
            />
            <button
              onClick={async () => {
                try {
                  const text = await navigator.clipboard.readText()
                  const m = String(text || '').match(/https?:\/\/[^\s]*xiaohongshu\.com\/board\/[0-9a-f]{24}[^\s]*/i)
                  if (m) {
                    setUrl(m[0]); setInlineError('')
                  } else {
                    setInlineError('剪贴板里没有找到小红书专辑链接（应以 xiaohongshu.com/board/ 开头）')
                  }
                } catch {
                  setInlineError('无法读取剪贴板，请手动粘贴（浏览器可能未授权剪贴板权限）')
                }
              }}
              className="shrink-0 rounded-lg bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-700 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300"
            >
              粘贴
            </button>
            <button
              onClick={async () => {
                await handleLoad()
                setShowAddModal(false)
              }}
              disabled={phase === 'loading'}
              className="rounded-lg bg-[var(--vi-ink)] px-4 py-2 text-sm font-medium text-white hover:bg-[#2A2F36] disabled:opacity-50"
            >
              {phase === 'loading' ? <Loader2 className="h-4 w-4 animate-spin" /> : '解析并入库'}
            </button>
          </div>

          {urlKind === 'collection' && !inlineError && (
            <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              这是「合集」链接（别人的合集，走「内容分析」页）。本页面要的是你的<b>收藏专辑</b>链接：手机 App 打开专辑 → 右上角分享 → 复制链接（xiaohongshu.com/board/…）
            </div>
          )}
          {inlineError && (
            <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {inlineError}
            </div>
          )}
        </div>
      )}

      {/* 内联提示：无展开弹层时若在主页贴错直接看 */}
      {!showAddModal && boards.length > 0 && inlineError && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {inlineError}
        </div>
      )}
      <div className="mb-4" />

      {board && (
        <>
          <div className="mb-4 rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-4 shadow-sm">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium text-neutral-800">《{board.name}》</p>
                {/* 2026-10-20 用户定案：捕获数与专辑总数不对等是客观情况（平台
                    懒加载/不渲染部分条目），不必用琥珀色预警条一直刻意提示，
                    只保留中性的计数行；缺很多时用户自己会点「刷新清单」续捕 */}
                <p className="text-xs text-neutral-500">
                  {notes.length > 0
                    ? <>共 {notes.length} 条{cachedCount > 0 && `（${cachedCount} 条已有正文缓存，勾选即可生成）`}</>
                    : <>平台账面 {board.total} 条，捕获后按实际可见数统计</>}
                </p>
              </div>
              <div className="flex gap-2">
                {phase === 'capturing' ? (
                  <button
                    onClick={handleStopCapture}
                    className="flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50"
                  >
                    <Square className="h-3.5 w-3.5" /> 完成捕获并刷新
                  </button>
                ) : (
                  <button
                    onClick={handleCapture}
                    className="flex items-center gap-1.5 rounded-lg bg-[var(--vi-ink)] px-3 py-1.5 text-sm font-medium text-white hover:bg-[#2A2F36]"
                  >
                    <ExternalLink className="h-3.5 w-3.5" /> {notes.length === 0 ? '打开捕获窗口' : '继续捕获更多'}
                  </button>
                )}
                <button
                  onClick={handleRefreshCapture}
                  disabled={phase === 'capturing'}
                  className="flex items-center gap-1.5 rounded-lg border border-neutral-200 px-3 py-1.5 text-sm text-neutral-600 hover:bg-neutral-50 disabled:opacity-50"
                  title="后台自动打开捕获窗口、滚动遍历全部笔记、自动关闭并更新清单"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${phase === 'capturing' ? 'animate-spin' : ''}`} />
                  {phase === 'capturing' ? '后台捕获中…' : '刷新清单'}
                </button>
              </div>
            </div>
          </div>

          {/* 勾选列表 */}
          {notes.length > 0 ? (
            <div className="rounded-xl border border-neutral-200">
              <div className="flex items-center justify-between border-b border-neutral-100 px-4 py-2.5 text-xs text-neutral-500">
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={selected.size === notes.length && notes.length > 0}
                      onChange={e => setSelected(e.target.checked ? new Set(notes.map(n => n.note_id)) : new Set())}
                    />
                    全选（{selected.size}/{notes.length}）
                  </label>
                  <select
                    value={boardModelKey || (targetModel ? `${targetModel.provider_id}::${targetModel.model_name}` : '')}
                    onChange={e => pickBoardModel(e.target.value)}
                    className="rounded-lg border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-2 py-1 text-[11px] text-[var(--vi-ink)] outline-none"
                    title="批量生成使用的模型（默认 workbuddy/DeepSeek，选择会记住）"
                  >
                    {modelList.length === 0 && <option value="">（无可用模型）</option>}
                    {modelList.map(m => (
                      <option key={`${m.provider_id}::${m.model_name}`} value={`${m.provider_id}::${m.model_name}`}>
                        {m.provider_id} / {m.model_name}
                      </option>
                    ))}
                  </select>
                  <span className="text-[11px] text-[var(--vi-ink-3)]">
                    自适应结构（AI 按视频内容自动设计章节）
                  </span>
                </div>
                <button
                  onClick={handleSubmit}
                  disabled={selected.size === 0 || submitting || !targetModel}
                  className="rounded-lg bg-[var(--vi-ink)] px-4 py-1.5 text-sm font-medium text-white hover:bg-[#2A2F36] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : `生成所选 ${selected.size > 0 ? `(${selected.size})` : ''}`}
                </button>
              </div>
              {/* 封面卡片流：小红书刷页体验——单击卡片即勾选，右上角打勾 */}
              <div className="relative">
                {/* 上缘 32px 渐进羽化 */}
                <div
                  className="pointer-events-none absolute left-0 right-0 top-0 z-10 h-8 rounded-t-xl"
                  style={{ background: 'linear-gradient(to bottom, var(--vi-bg), transparent)' }}
                />
                <div className="max-h-[520px] overflow-y-auto p-3">
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
                    {notes.map(n => {
                      const isSel = selected.has(n.note_id)
                      const duration = (n as BoardNoteWithDuration).duration
                      return (
                        <div
                          key={n.note_id}
                          onClick={() => toggle(n.note_id)}
                          className={`group relative cursor-pointer overflow-hidden rounded-xl border bg-[var(--vi-surface)] transition-all duration-200 ${
                            isSel
                              ? 'border-[var(--vi-ink)] shadow-[0_0_0_1px_var(--vi-ink)]'
                              : 'border-[var(--vi-hairline)] hover:border-[var(--vi-ink-3)]'
                          }`}
                        >
                          {/* 3:4 竖图封面（小红书封面原生比例，禁止改 16:9） */}
                          <div className="relative aspect-[3/4] w-full overflow-hidden bg-[var(--vi-hover)]">
                            {resolveCoverUrl(n) ? (
                              <img
                                src={resolveCoverUrl(n)}
                                alt={n.title}
                                loading="lazy"
                                className="h-full w-full object-cover transition-transform duration-200 ease-out group-hover:scale-[1.02]"
                              />
                            ) : (
                              <div
                                className="flex h-full w-full items-center justify-center text-[var(--vi-ink-3)]"
                                style={DOT_GRID}
                              >
                                <Bookmark className="h-7 w-7 opacity-60" />
                              </div>
                            )}
                            {/* 右上角勾选圆环：未选幽灵磨砂 / 选中实心黑键白勾 */}
                            <span
                              className={`absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full border transition-all duration-200 ${
                                isSel
                                  ? 'border-[var(--vi-ink)] bg-[var(--vi-ink)] text-[var(--vi-surface)] opacity-100'
                                  : 'border-white/80 bg-black/20 text-transparent opacity-0 backdrop-blur-sm group-hover:opacity-100'
                              }`}
                            >
                              <Check className="h-3.5 w-3.5" strokeWidth={3} />
                            </span>
                            {/* 右上时长徽标位：有 duration 数据才显示（无数据不留空洞） */}
                            {typeof duration === 'number' && duration > 0 && (
                              <span className="tc absolute right-2 top-10 rounded bg-black/55 px-1.5 py-0.5 text-[10px] tabular-nums text-white">
                                {formatTimecode(duration)}
                              </span>
                            )}
                            {/* 类型角标（左下） */}
                            <span className="tc absolute bottom-2 left-2 rounded bg-black/55 px-1.5 py-0.5 text-[10px] text-white">
                              {n.type === 'video' ? '视频' : '图文'}
                            </span>
                          </div>
                          {/* 标题 */}
                          <div className="px-2.5 py-2">
                            <p className="line-clamp-2 text-xs leading-snug text-[var(--vi-ink)]">{n.title || '未命名'}</p>
                            {n.detail_cached && (
                              <p className="tc mt-1 text-[10px] text-teal-ok">已缓存正文</p>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
                {/* 滚动区底部渐变羽化 */}
                <div
                  className="pointer-events-none absolute bottom-0 left-0 right-0 h-8 rounded-b-xl z-10"
                  style={{ background: 'linear-gradient(to bottom, transparent, var(--vi-bg))' }}
                />
              </div>
            </div>
          ) : (
            phase === 'ready' && (
              <div
                className="rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-8 text-center"
                style={DOT_GRID}
              >
                <p className="tc text-[11px] tracking-[0.18em] text-[var(--vi-ink-3)]">READY · 素材箱空</p>
                <p className="mt-2 text-sm text-[var(--vi-ink-2)]">
                  点上方「打开捕获窗口」，在窗口里像平常一样翻这个专辑（滚动加载），条目会自动出现在这里
                </p>
              </div>
            )
          )}
        </>
      )}

      {!board && phase === 'idle' && (
        <div
          className="rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] p-10 text-center"
          style={DOT_GRID}
        >
          <Bookmark className="mx-auto mb-3 h-8 w-8 text-[var(--vi-ink-3)] opacity-60" />
          <p className="tc text-[11px] tracking-[0.18em] text-[var(--vi-ink-3)]">READY · 素材箱待命</p>
          <p className="mt-2 text-sm text-[var(--vi-ink-2)]">
            在手机小红书里「分享 → 复制链接」你的收藏专辑，粘贴到上面开始
          </p>
        </div>
      )}
    </div>
  )
}
