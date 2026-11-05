/**
 * 供应商预设选择页（结构参考 ZCode 开源版 zai-org/ZCode 的
 * ProviderTemplatePicker）。
 *
 * 为什么要预设：用户手填 baseUrl 极易出错（智谱有 coding-plan 和 standard
 * 两个入口、Anthropic 协议和 OpenAI 协议端点不同）。预设把「名称 + baseUrl +
 * apiFormat + 模型列表」打包好，用户只需填 API Key。
 *
 * 分组顺序硬编码（智谱优先），与 ZCode 一致——不用 rank 字段。
 */
import { useEffect, useState } from 'react'
import { ChevronRight, Plus, ArrowLeft } from 'lucide-react'
import { fetchProviderTemplates } from '@/services/model'
import AILogo from '@/components/Form/modelForm/Icons'

export type ProviderTemplate = {
  id: string
  name: string
  group: string
  apiFormat: string
  baseUrl: string
  models: string[]
  apiKeyUrl: string
  /** @lobehub/icons 的导出名，如 Zhipu / DeepSeek / Moonshot */
  logo?: string
}

interface Props {
  onPick: (tpl: ProviderTemplate) => void
  onCustom: () => void
}

const GROUP_ORDER = ['智谱', '其他']

export function ProviderTemplatePicker({ onPick, onCustom }: Props) {
  const [templates, setTemplates] = useState<ProviderTemplate[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchProviderTemplates()
      .then(list => setTemplates(list as ProviderTemplate[]))
      .catch(() => setTemplates([]))
      .finally(() => setLoading(false))
  }, [])

  const groups = GROUP_ORDER.map(g => ({
    name: g,
    items: templates.filter(t => t.group === g),
  })).filter(g => g.items.length > 0)

  if (loading) {
    return <div className="p-6 text-sm text-[var(--vi-ink-3)]">加载预设…</div>
  }

  return (
    <div className="flex flex-col gap-5 p-5">
      <div className="flex items-center gap-2">
        <ArrowLeft className="h-4 w-4 text-[var(--vi-ink-3)]" />
        <h3 className="text-base font-semibold text-[var(--vi-ink)]">添加供应商</h3>
      </div>

      {groups.map(group => (
        <div key={group.name}>
          <p className="mb-2 text-xs font-medium text-[var(--vi-ink-3)]">{group.name}</p>
          <div className="grid grid-cols-2 gap-2">
            {group.name === '其他' && (
              <button
                onClick={onCustom}
                className="flex items-center gap-2.5 rounded-lg border border-dashed border-[var(--vi-hairline)] px-3 py-3 text-left text-sm text-[var(--vi-ink-2)] transition-colors hover:border-[var(--vi-ink-3)] hover:bg-[var(--vi-hover)]"
              >
                <span className="flex h-7 w-7 shrink-0 items-center justify-center">
                  <Plus className="h-4 w-4" />
                </span>
                创建自定义供应商
              </button>
            )}
            {group.items.map(t => (
              <button
                key={t.id}
                onClick={() => onPick(t)}
                className="group flex items-center justify-between gap-2 rounded-lg border border-[var(--vi-hairline)] px-3 py-3 text-left transition-colors hover:border-[var(--vi-ink-3)] hover:bg-[var(--vi-hover)]"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  {/* 品牌图标（@lobehub/icons），无匹配时 AILogo 自动回退占位 */}
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center">
                    <AILogo name={t.logo || 'custom'} size={22} />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-[var(--vi-ink)]">{t.name}</p>
                    <p className="truncate font-mono text-[10px] text-[var(--vi-ink-3)]">
                      {t.apiFormat} · {t.models.length} 个模型
                    </p>
                  </div>
                </div>
                <ChevronRight className="h-4 w-4 shrink-0 text-[var(--vi-ink-3)]" />
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
