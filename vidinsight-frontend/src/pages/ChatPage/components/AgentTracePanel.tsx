/* AgentTracePanel.tsx ------------------------------------------------
 * Agent 轨迹回放面板（可观测性闭环的 UI 端）。
 *
 * 为什么需要它：后端 AgentTraceRecorder 一直在写 traces/*.jsonl，但此前没有
 * 消费端——记录了却看不见。这个面板把「感知 → 决策 → 行动 → 反馈」每一步
 * 摊开：意图分类、每轮 LLM 耗时/token、工具名与入参、最终状态与用量。
 *
 * 答辩价值：能当场证明 VidInsight 是真 Agent（多轮工具编排），
 * 而不是"把问题丢给 LLM 一次出答案"。
 */
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, RotateCcw, Activity } from 'lucide-react'
import toast from 'react-hot-toast'
import request from '@/utils/request'

interface TraceItem {
  // 后端 /api/agent/traces 返回 {name, size_bytes, mtime}（见 app/routers/trace.py）
  name: string
  size_bytes: number
  mtime: number
}

interface TraceListResponse {
  traces: TraceItem[]
  dir?: string
}

interface TraceDetailResponse {
  name: string
  events: TraceEvent[]
}

interface TraceEvent {
  event: string
  ts?: string
  round?: number
  ms?: number
  tool?: string
  args?: Record<string, unknown>
  result?: string
  status?: string
  answer_len?: number
  sources?: number
  rounds?: number
  total_ms?: number
  prompt_tokens?: number
  completion_tokens?: number
  question?: string
  intent?: string
  note?: string
}

const EVENT_LABEL: Record<string, string> = {
  start: '感知',
  llm: '决策',
  tool: '行动',
  final: '反馈',
}

// 事件声部（去紫）：感知/决策/行动/反馈统一为冷灰 + mono，靠边框与灰度区分，
// 不用彩色徽章；轨迹里的工具事件都是「已结束」，按设计用灰白 mono，仅 final 用 teal 完成态。
const EVENT_TONE: Record<string, string> = {
  start: 'border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] text-[var(--vi-ink-2)]',
  llm: 'border-[var(--vi-hairline)] bg-[var(--vi-hover)] text-[var(--vi-ink)]',
  tool: 'border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] text-[var(--vi-ink)]',
  final: 'border-teal-ok/35 bg-teal-ok/5 text-teal-ok',
}

function fmtTime(mtime: number): string {
  const d = new Date(mtime * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export default function AgentTracePanel() {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<TraceItem[]>([])
  const [loading, setLoading] = useState(false)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [events, setEvents] = useState<TraceEvent[]>([])

  const loadList = async () => {
    setLoading(true)
    try {
      // 通过统一 request 实例访问（兼容本地开发与 Tauri 打包环境），suppressToast 允许业务层精细提示
      const res = (await request.get('/agent/traces?limit=20', {
        suppressToast: true,
      })) as TraceListResponse
      const list: TraceItem[] = res?.traces ?? []
      setItems(list)
      if (list.length && !activeId) setActiveId(list[0].name)
    } catch (err: any) {
      const msg = err?.msg || err?.message || '网络连接异常'
      toast.error(`轨迹列表加载失败：${msg}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) loadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!activeId) return
    let alive = true
    // 后端要求完整文件名（含 .jsonl），正则白名单校验后才读盘
    request
      .get(`/agent/trace/${encodeURIComponent(activeId)}`, { suppressToast: true })
      .then((res: any) => {
        if (alive) setEvents(res?.events ?? [])
      })
      .catch((err: any) => {
        if (alive) {
          setEvents([])
          const msg = err?.msg || err?.message
          if (msg) toast.error(`轨迹详情加载失败：${msg}`)
        }
      })
    return () => {
      alive = false
    }
  }, [activeId])

  return (
    <div className="overflow-hidden rounded-[2px] border border-[var(--vi-hairline)] bg-[var(--vi-surface)]">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-[var(--vi-hover)]"
      >
        {open
          ? <ChevronDown className="h-3.5 w-3.5 text-[var(--vi-ink-3)]" />
          : <ChevronRight className="h-3.5 w-3.5 text-[var(--vi-ink-3)]" />}
        <Activity className="h-3.5 w-3.5 text-[var(--vi-ink-3)]" />
        <span className="tc text-[11px] uppercase tracking-[0.14em] text-[var(--vi-ink-2)]">
          Agent Trace
        </span>
        <span className="tc ml-auto text-[10px] tabular-nums text-[var(--vi-ink-3)]">
          {items.length ? `${String(items.length).padStart(2, '0')} REC` : ''}
        </span>
      </button>

      {open && (
        <div className="border-t border-[var(--vi-hairline)] p-3">
          <div className="mb-2 flex items-center gap-2">
            <button
              type="button"
              onClick={loadList}
              disabled={loading}
              className="tc inline-flex items-center gap-1 rounded-[1px] border border-[var(--vi-hairline)] px-2 py-1 text-[11px] text-[var(--vi-ink-2)] transition-colors hover:bg-[var(--vi-hover)] disabled:opacity-60"
            >
              {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
              刷新
            </button>
            {items.length > 0 && (
              <select
                value={activeId ?? ''}
                onChange={e => setActiveId(e.target.value)}
                className="tc min-w-0 flex-1 rounded-[1px] border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-2 py-1 text-[11px] text-[var(--vi-ink-2)] outline-none focus:border-[var(--vi-ink-3)]"
              >
                {items.map(it => (
                  <option key={it.name} value={it.name}>
                    {fmtTime(it.mtime)} · {it.name.slice(9, 15)}
                  </option>
                ))}
              </select>
            )}
          </div>

          {items.length === 0 && !loading && (
            <p className="tc py-4 text-center text-[11px] text-[var(--vi-ink-3)]">
              暂无轨迹——发起一次对话后生成
            </p>
          )}

          <div className="max-h-72 space-y-1 overflow-y-auto">
            {events.map((e, i) => (
              <div
                key={i}
                className="flex items-start gap-2 rounded-[1px] border border-[var(--vi-hairline)] bg-[var(--vi-code-bg)] px-2 py-1.5"
              >
                <span
                  className={`tc shrink-0 rounded-[1px] border px-1.5 py-0.5 text-[10px] leading-none ${
                    EVENT_TONE[e.event] ?? 'border-[var(--vi-hairline)] bg-[var(--vi-hover)] text-[var(--vi-ink-3)]'
                  }`}
                >
                  {EVENT_LABEL[e.event] ?? e.event}
                </span>
                <div className="tc min-w-0 flex-1 text-[11px] leading-relaxed text-[var(--vi-ink-2)]">
                  {e.event === 'start' && (
                    <>
                      <span>{(e.question || '').slice(0, 80)}</span>
                      {e.intent && (
                        <span className="ml-1 text-[var(--vi-ink-3)]">intent={e.intent}</span>
                      )}
                    </>
                  )}
                  {e.event === 'llm' && (
                    <span>
                      轮次 {e.round} · {e.ms ?? 0}ms · {e.prompt_tokens ?? 0}↑/
                      {e.completion_tokens ?? 0}↓{e.note ? ` · ${e.note}` : ''}
                    </span>
                  )}
                  {e.event === 'tool' && (
                    <>
                      <span className="font-medium text-[var(--vi-ink)]">{e.tool}</span>
                      <span className="ml-1 tabular-nums text-[var(--vi-ink-3)]">
                        {e.ms ?? 0}ms{typeof e.round === 'number' ? ` · 轮次 ${e.round}` : ''}
                      </span>
                      {e.result && (
                        <div className="truncate text-[var(--vi-ink-3)]">{String(e.result).slice(0, 90)}</div>
                      )}
                    </>
                  )}
                  {e.event === 'final' && (
                    <span>
                      {e.status} · {e.rounds ?? 0} 轮 · {e.total_ms ?? 0}ms · 答案{' '}
                      {e.answer_len ?? 0} 字 · 来源 {e.sources ?? 0}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
