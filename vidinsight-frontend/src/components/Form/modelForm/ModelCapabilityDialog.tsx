/**
 * 模型能力配置弹窗（结构参考 ZCode 开源版 zai-org/ZCode 的
 * ProviderModelMetadataDialog）。
 *
 * 为什么单独做一个弹窗而不是塞进现有表单：
 *   现有表单只有「模型名」一个字段，而真实模型有 12 个能力维度
 *   （上下文窗口、输出上限、5 种输入模态、4 种能力位、推理档位）。
 *   平铺进主表单会把供应商配置挤爆，弹窗按需展开更清晰。
 */
import { useState, useEffect } from 'react'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

export type ModelCapabilities = {
  contextWindow?: number | null
  maxOutputTokens?: number | null
  inputFormat?: {
    supportsText?: boolean
    supportsImage?: boolean
    supportsVideo?: boolean
    supportsAudio?: boolean
    supportsPdf?: boolean
  }
  supportsToolCall?: boolean
  supportsJsonSchemaOutput?: boolean
  supportsNativeWebSearch?: boolean
  supportsMidConversationSystem?: boolean
  reasoningLevels?: string[]
  reasoningMap?: string | null
}

interface Props {
  open: boolean
  modelName: string
  capabilities: ModelCapabilities
  onClose: () => void
  onSave: (caps: ModelCapabilities) => void
}

// 输入类型（ZCode: ProviderModelModalityOptions）
const INPUT_FORMATS = [
  { key: 'supportsText', label: '文本', locked: true },
  { key: 'supportsImage', label: '图片' },
  { key: 'supportsVideo', label: '视频' },
  { key: 'supportsAudio', label: '音频' },
  { key: 'supportsPdf', label: 'PDF' },
] as const

// 模型能力（ZCode: 三个布尔位）
const CAPABILITIES = [
  { key: 'supportsJsonSchemaOutput', label: '结构化输出' },
  { key: 'supportsNativeWebSearch', label: '原生联网搜索' },
  { key: 'supportsMidConversationSystem', label: '对话中系统消息' },
  { key: 'supportsToolCall', label: '工具调用' },
] as const

// 推理档位映射形态（ZCode 用 CEL DSL，这里收敛成 4 种实际用到的）
const REASONING_MAPS = [
  { value: '', label: '不设置' },
  { value: 'reasoning_effort', label: 'reasoning_effort = 档位' },
  { value: 'thinking_toggle', label: 'thinking.type = enabled/disabled' },
  { value: 'output_config_effort', label: 'output_config.effort = 档位' },
  { value: 'enable_thinking', label: 'enable_thinking = 布尔' },
]

export function ModelCapabilityDialog({
  open,
  modelName,
  capabilities,
  onClose,
  onSave,
}: Props) {
  const [draft, setDraft] = useState<ModelCapabilities>(capabilities)
  const [levelInput, setLevelInput] = useState('')

  useEffect(() => {
    if (open) setDraft(capabilities)
  }, [open, capabilities])

  if (!open) return null

  const input = draft.inputFormat || {}
  const levels = draft.reasoningLevels || []

  const toggleInput = (key: string) => {
    setDraft(d => ({
      ...d,
      inputFormat: { ...d.inputFormat, [key]: !(d.inputFormat as any)?.[key] },
    }))
  }

  const toggleCap = (key: string) => {
    setDraft(d => ({ ...d, [key]: !(d as any)[key] }))
  }

  const addLevel = () => {
    const v = levelInput.trim()
    if (!v || levels.includes(v)) return
    setDraft(d => ({ ...d, reasoningLevels: [...(d.reasoningLevels || []), v] }))
    setLevelInput('')
  }

  const removeLevel = (v: string) => {
    setDraft(d => ({ ...d, reasoningLevels: (d.reasoningLevels || []).filter(x => x !== v) }))
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[85vh] w-full max-w-[560px] overflow-y-auto rounded-xl border border-[var(--vi-hairline)] bg-[var(--vi-surface)] shadow-xl">
        {/* 头部 */}
        <div className="flex items-center justify-between border-b border-[var(--vi-hairline)] px-5 py-4">
          <div>
            <h3 className="text-base font-semibold text-[var(--vi-ink)]">配置模型</h3>
            <p className="mt-0.5 font-mono text-xs text-[var(--vi-ink-3)]">{modelName}</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-[var(--vi-ink-3)] hover:bg-[var(--vi-hover)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex flex-col gap-5 px-5 py-4">
          {/* 上下文窗口 */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium text-[var(--vi-ink)]">上下文窗口</label>
            <Input
              type="number"
              placeholder="如 128000"
              value={draft.contextWindow ?? ''}
              onChange={e =>
                setDraft(d => ({
                  ...d,
                  contextWindow: e.target.value ? Number(e.target.value) : null,
                }))
              }
            />
          </div>

          {/* 最大输出 Token */}
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium text-[var(--vi-ink)]">最大输出 Token</label>
            <Input
              type="number"
              placeholder="如 8192"
              value={draft.maxOutputTokens ?? ''}
              onChange={e =>
                setDraft(d => ({
                  ...d,
                  maxOutputTokens: e.target.value ? Number(e.target.value) : null,
                }))
              }
            />
          </div>

          {/* 高级配置 */}
          <div className="border-t border-[var(--vi-hairline)] pt-4">
            <p className="mb-3 text-xs font-medium uppercase tracking-wide text-[var(--vi-ink-3)]">
              高级配置
            </p>

            {/* 输入类型 */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-[var(--vi-ink)]">
                输入类型
              </label>
              <div className="flex flex-wrap gap-2">
                {INPUT_FORMATS.map(f => {
                  const checked = f.locked || !!(input as any)[f.key]
                  return (
                    <button
                      key={f.key}
                      type="button"
                      disabled={f.locked}
                      onClick={() => !f.locked && toggleInput(f.key)}
                      className={`rounded-md border px-3 py-1.5 text-sm transition-colors ${
                        checked
                          ? 'border-[var(--vi-ink)] bg-[var(--vi-ink)] text-[var(--vi-surface)]'
                          : 'border-[var(--vi-hairline)] text-[var(--vi-ink-2)] hover:border-[var(--vi-ink-3)]'
                      } ${f.locked ? 'cursor-default opacity-70' : 'cursor-pointer'}`}
                    >
                      {f.label}
                    </button>
                  )
                })}
              </div>
              <p className="mt-1.5 text-xs text-[var(--vi-ink-3)]">
                勾选「图片」后，生成笔记时会抽取视频关键画面读图取信息
              </p>
            </div>

            {/* 模型能力 */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-[var(--vi-ink)]">
                模型能力
              </label>
              <div className="flex flex-wrap gap-2">
                {CAPABILITIES.map(c => {
                  const checked = !!(draft as any)[c.key]
                  return (
                    <button
                      key={c.key}
                      type="button"
                      onClick={() => toggleCap(c.key)}
                      className={`rounded-md border px-3 py-1.5 text-sm transition-colors ${
                        checked
                          ? 'border-[var(--vi-ink)] bg-[var(--vi-ink)] text-[var(--vi-surface)]'
                          : 'border-[var(--vi-hairline)] text-[var(--vi-ink-2)] hover:border-[var(--vi-ink-3)]'
                      }`}
                    >
                      {c.label}
                    </button>
                  )
                })}
              </div>
            </div>

            {/* 推理等级 */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-[var(--vi-ink)]">
                推理等级（从低到高）
              </label>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {levels.map(v => (
                  <span
                    key={v}
                    className="inline-flex items-center gap-1 rounded-md bg-[var(--vi-hover)] px-2 py-1 text-xs text-[var(--vi-ink)]"
                  >
                    {v}
                    <button onClick={() => removeLevel(v)} className="hover:text-red-500">
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                {levels.length === 0 && (
                  <span className="text-xs text-[var(--vi-ink-3)]">未设置（模型不支持推理档位）</span>
                )}
              </div>
              <div className="flex gap-2">
                <Input
                  placeholder="如 low / high / max，回车添加"
                  value={levelInput}
                  onChange={e => setLevelInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      addLevel()
                    }
                  }}
                  className="h-8"
                />
                <Button type="button" variant="ghost" onClick={addLevel} className="h-8">
                  添加
                </Button>
              </div>
            </div>

            {/* 推理参数映射 */}
            <div>
              <label className="mb-2 block text-sm font-medium text-[var(--vi-ink)]">
                推理参数映射
              </label>
              <select
                value={draft.reasoningMap || ''}
                onChange={e => setDraft(d => ({ ...d, reasoningMap: e.target.value || null }))}
                className="w-full rounded-md border border-[var(--vi-hairline)] bg-[var(--vi-surface)] px-3 py-2 text-sm text-[var(--vi-ink)]"
              >
                {REASONING_MAPS.map(m => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-between border-t border-[var(--vi-hairline)] px-5 py-3">
          <button
            onClick={() => setDraft({})}
            className="text-xs text-[var(--vi-ink-3)] hover:text-[var(--vi-ink)]"
          >
            重置表单
          </button>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>
              取消
            </Button>
            <Button onClick={() => onSave(draft)}>保存</Button>
          </div>
        </div>
      </div>
    </div>
  )
}
