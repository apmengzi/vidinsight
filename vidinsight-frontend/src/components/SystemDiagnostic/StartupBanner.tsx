import { useEffect, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'

// 桌面端启动诊断横幅。监听 Tauri 侧 emit 的 backend-warning / backend-error / backend-terminated。
// 只在 Tauri 环境生效；纯 web 环境（无 window.__TAURI_INTERNALS__）下静默不挂载。

type Severity = 'info' | 'warning' | 'error'

interface DiagnosticPayload {
  exe_path?: string
  path_has_non_ascii?: boolean
  path_has_space?: boolean
  parent_writable?: boolean
  platform?: string
}

interface BannerState {
  severity: Severity
  title: string
  detail: string
  payload?: DiagnosticPayload
}

const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

function describeWarning(payload: DiagnosticPayload): { title: string; detail: string } {
  const parts: string[] = []
  if (payload.path_has_non_ascii) {
    parts.push('安装路径包含非 ASCII 字符（中文 / 日文等）')
  }
  if (payload.path_has_space) {
    parts.push('安装路径包含空格')
  }
  if (payload.parent_writable === false) {
    parts.push('安装目录不可写（缺少权限或只读）')
  }
  return {
    title: '检测到可能导致后端启动失败的安装路径',
    detail:
      `${parts.join('；')}。\n` +
      '建议把 VidInsight 重新安装到一个纯英文、无空格、可写的路径下（如 C:\\VidInsight\\ 或 /Applications/）。\n' +
      `当前路径：${payload.exe_path || '未知'}`,
  }
}

const StartupBanner = () => {
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [restarting, setRestarting] = useState(false)

  useEffect(() => {
    if (!isTauri) return

    let unlisteners: Array<() => void> = []

    ;(async () => {
      const { listen } = await import('@tauri-apps/api/event')

      const offWarning = await listen<DiagnosticPayload>('backend-warning', event => {
        const { title, detail } = describeWarning(event.payload || {})
        setRestarting(false)
        setBanner({
          severity: 'warning',
          title,
          detail,
          payload: event.payload,
        })
      })

      // 后端退出：不再写死「不可关闭」。误报场景真实存在——
      // 遗留孤儿进程占着端口时，新 sidecar 起不来（banner 说它退了），
      // 但孤儿还在响应 HTTP，用户实际还能用。横幅必须可关 + 提供一键重启。
      const offTerminated = await listen<number | null>('backend-terminated', event => {
        if (restarting) return // 主动重启杀旧进程的副作用，不是异常
        setBanner({
          severity: 'error',
          title: '后端进程已退出',
          detail: `退出码：${event.payload ?? '未知'}。点击「一键重启」恢复，或关闭本提示继续使用。`,
        })
      })

      const offRestarting = await listen('backend-restarting', () => {
        setRestarting(true)
      })

      // 后端被「一键重启」拉起来 / ready-probe 检测到 sidecar 就绪后，清横幅
      const offRestarted = await listen('backend-restarted', () => {
        setRestarting(false)
        setBanner(b => (b?.severity === 'error' ? null : b))
      })
      const offReady = await listen('backend-ready', () => {
        setRestarting(false)
        setBanner(b => (b?.severity === 'error' ? null : b))
      })

      // backend-error 是 sidecar stderr，量大噪音多，这里不直接展示，留给 P2 的日志面板。
      unlisteners = [offWarning, offTerminated, offRestarting, offRestarted, offReady]
    })()

    return () => {
      unlisteners.forEach(fn => fn())
    }
  }, [restarting])

  if (!banner) return null

  const handleRestart = async () => {
    setRestarting(true)
    setBanner({
      severity: 'info',
      title: '正在一键重启后端…',
      detail: '已清理可能占用端口的遗留进程，正在拉起新的后端（最长约 45 秒）。',
    })
    try {
      await invoke('restart_backend_sidecar')
      // 成功路径：backend-restarted / backend-ready 事件会把横幅清掉
    } catch (e) {
      setRestarting(false)
      setBanner({
        severity: 'error',
        title: '后端重启失败',
        detail: String(e) + '\n可以再试一次一键重启，或直接重启 VidInsight 应用。',
      })
    }
  }

  const colorByLevel: Record<Severity, string> = {
    info: 'bg-blue-50 border-blue-300 text-blue-900',
    warning: 'bg-amber-50 border-amber-300 text-amber-900',
    error: 'bg-red-50 border-red-300 text-red-900',
  }

  const iconByLevel: Record<Severity, string> = {
    info: 'ℹ️',
    warning: '⚠️',
    error: '✕',
  }

  return (
    <div
      className={`fixed left-0 right-0 top-0 z-[9999] flex items-start gap-3 border-b px-4 py-2 text-sm shadow-sm ${colorByLevel[banner.severity]}`}
    >
      <span className="text-lg">{iconByLevel[banner.severity]}</span>
      <div className="flex-1 min-w-0">
        <div className="font-medium">{banner.title}</div>
        <pre className="mt-0.5 whitespace-pre-wrap break-words font-sans text-xs opacity-90">
          {banner.detail}
        </pre>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {banner.severity === 'error' && !restarting && banner.title !== '后端重启失败' && (
          <button
            className="rounded border border-current/30 bg-white/60 px-3 py-1 text-xs font-medium hover:bg-white"
            onClick={handleRestart}
          >
            🔄 一键重启
          </button>
        )}
        {/* 所有横幅都可关闭：诊断信息不该变成挡视线的死横幅 */}
        <button
          className="rounded px-2 py-0.5 text-xs hover:bg-black/10"
          onClick={() => {
            setBanner(null)
            setRestarting(false)
          }}
          aria-label="关闭通知"
        >
          ✕
        </button>
      </div>
    </div>
  )
}

export default StartupBanner
