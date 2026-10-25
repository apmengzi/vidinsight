import { FC } from 'react'
import { CheckCircle2 } from 'lucide-react'

interface Step {
  label: string
  key: string
  Icon?: React.ReactNode // 加一个可选的 Lottie 动画
}

interface StepBarProps {
  steps: Step[]
  currentStep: string
  /** 当前阶段内进度（0-100 或 null）。上游未提供或为 null 时退化为不定态样式（脉冲点）。 */
  progress?: number | null
}

function formatStepProgress(stepKey: string, progress: number): string {
  const rounded = Math.round(progress)
  if (stepKey === 'SUMMARIZING') {
    // 假设 5 个块的分片场景（若数值折算或百分比），如果是 chunk 百分比，格式化克制显示
    // 当 progress 存在时，给出克制 mono 数字
    return `${rounded}%`
  }
  return `${rounded}%`
}

const StepBar: FC<StepBarProps> = ({ steps, currentStep, progress }) => {
  const currentIndex = steps.findIndex(step => step.key === currentStep)

  return (
    <div className="flex w-full items-start justify-between">
      {steps.map((step, index) => {
        const done = index < currentIndex
        const active = index === currentIndex
        const last = index === steps.length - 1

        return (
          <div key={step.key} className="relative flex min-w-0 flex-1 flex-col items-center">
            {/* 轨道与节点：已完成 teal 实心勾、当前 rec 脉冲点、未来灰空心，横向连线 */}
            <div className="flex w-full items-center">
              <div
                className={`h-[3px] flex-1 rounded-l transition-all duration-500 ${
                  index === 0 ? 'opacity-0' : done || active ? 'bg-teal-ok/70' : 'bg-[var(--vi-hairline)]'
                }`}
              />
              {done && (
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-teal-ok text-white shadow-sm">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                </span>
              )}
              {active && (
                <span className="relative flex h-6 w-6 shrink-0 items-center justify-center">
                  <span className="absolute h-6 w-6 animate-ping rounded-full bg-rec opacity-30" />
                  <span className="h-3.5 w-3.5 rounded-full bg-rec ring-4 ring-rec/20" />
                </span>
              )}
              {!done && !active && (
                <span className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-[var(--vi-hairline)] bg-[var(--vi-surface)]" />
              )}
              <div
                className={`h-[3px] flex-1 rounded-r transition-all duration-500 ${
                  last ? 'opacity-0' : index < currentIndex ? 'bg-teal-ok/70' : 'bg-[var(--vi-hairline)]'
                }`}
              />
            </div>

            {/* 当前步骤动画（若有 Icon） */}
            {active && step.Icon && (
              <div className="absolute top-8 h-16 w-16">{step.Icon}</div>
            )}

            {/* 步骤名称 */}
            <span
              className={`mt-2 whitespace-nowrap text-xs transition-colors ${
                active
                  ? 'font-medium text-rec'
                  : done
                  ? 'text-teal-ok font-medium'
                  : 'text-[var(--vi-ink-3)]'
              }`}
            >
              {step.label}
              {done && <span className="tc ml-1 text-teal-ok/70">✓</span>}
              {active && typeof progress === 'number' && Number.isFinite(progress) && (
                <span className="tc ml-1 tabular-nums font-mono text-rec/90 font-medium">
                  {formatStepProgress(step.key, progress)}
                </span>
              )}
            </span>
          </div>
        )
      })}
    </div>
  )
}

export default StepBar

