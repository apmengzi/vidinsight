import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { addProvider, addModel, testConnection, getProviderList, updateProviderById, fetchRemoteModels } from '@/services/model'
import { getTranscriberConfig, updateTranscriberConfig } from '@/services/transcriber'
import logo from '@/assets/vidinsight-logo.png'

// 常见 OpenAI 兼容供应商预设：下拉选择后 API 地址同步填入（key 不动）
const PROVIDER_PRESETS: { name: string; base_url: string; hint?: string }[] = [
  { name: 'OpenAI', base_url: 'https://api.openai.com/v1' },
  { name: 'DeepSeek', base_url: 'https://api.deepseek.com', hint: 'deepseek-chat / deepseek-reasoner' },
  { name: 'Qwen（通义）', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', hint: 'qwen-turbo / qwen-plus / qwen-max' },
  { name: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', hint: 'glm-4-flash（免费）/ glm-4-plus' },
  { name: 'Groq', base_url: 'https://api.groq.com/openai/v1', hint: 'llama-3.3-70b 等' },
  { name: 'Gemini', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai/', hint: 'gemini-2.0-flash' },
  { name: 'Ollama（本机）', base_url: 'http://127.0.0.1:11434/v1', hint: '本机已启动 Ollama 时可用' },
  { name: '自定义', base_url: '', hint: '自托管 / 中转网关，手填地址' },
]

// 后端 R.error / ProviderError 的形状是 { code, msg, data }，没有 .message。
// 直接 ${e} 会渲染成 [object Object]，这里统一抽取可读文案。
function errText(e: any): string {
  if (!e) return '未知错误'
  if (typeof e === 'string') return e
  return e.msg || e.message || JSON.stringify(e)
}

const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

// 后端连通性自检不走共享 axios（会弹 toast），用裸 fetch 避免启动期 toast 叠堆
function getBackendBase(): string {
  const fromEnv = (import.meta as any).env?.VITE_API_BASE_URL as string | undefined
  return ((fromEnv && fromEnv.length > 0) ? fromEnv : '/api').replace(/\/$/, '')
}
async function pingBackend(): Promise<boolean> {
  try {
    const res = await fetch(`${getBackendBase()}/sys_check`)
    if (!res.ok) return false
    const json = await res.json().catch(() => null)
    return json?.code === 0
  }
  catch {
    return false
  }
}

// 桌面端首启 4 步引导。完成后写 localStorage('vidinsight-onboarded') = '1'，路由守卫不再拦。
//
// 1. 后端连通性自检
// 2. LLM 供应商 + 模型（最简：只引导填一个 OpenAI-兼容供应商 + 一个 model 名）
// 3. 转写引擎选择（推荐 Groq 在线，避开本地模型下载坑）
// 4. （可选）Cookie 同步说明（仅当用户关注 B 站等需要登录态的平台时）

const ONBOARD_KEY = 'vidinsight-onboarded'

export function isOnboarded(): boolean {
  return localStorage.getItem(ONBOARD_KEY) === '1'
}

function markOnboarded() {
  localStorage.setItem(ONBOARD_KEY, '1')
}

const Onboarding = () => {
  const navigate = useNavigate()
  const [step, setStep] = useState(1)
  const [error, setError] = useState('')

  // step 1
  const [pinging, setPinging] = useState(false)
  const [backendOk, setBackendOk] = useState<boolean | null>(null)

  // step 2
  const [providerName, setProviderName] = useState('OpenAI')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('https://api.openai.com/v1')
  const [modelName, setModelName] = useState('gpt-4o-mini')
  const [providerId, setProviderId] = useState<string | null>(null)
  const [savingProvider, setSavingProvider] = useState(false)
  // 真实检测可用模型：填完 key 点「检测」→ /remote_models → 下拉选择
  const [remoteModels, setRemoteModels] = useState<string[]>([])
  const [probingModels, setProbingModels] = useState(false)
  const [probeError, setProbeError] = useState('')

  // step 3
  const [transcriberType, setTranscriberType] = useState<string>('groq')
  const [savingTranscriber, setSavingTranscriber] = useState(false)

  // step 4：B 站一键登录（保底措施：主要为字幕通道，可跳过）
  const [biliCapturing, setBiliCapturing] = useState(false)
  const [biliDone, setBiliDone] = useState(false)

  function next() {
    setError('')
    setStep(s => s + 1)
  }
  function prev() {
    setError('')
    setStep(s => Math.max(1, s - 1))
  }

  // step 1: ping 后端
  // 关键点：旧实现 useEffect 只在 step===1 时 ping 一次。失败后 backendOk=false 永远卡死，
  // 即便后端随后就绪了也不会刷新。现在改成：
  //   - 手动重试按钮调用 doPing
  //   - Tauri backend-ready / backend-restarted 事件触发 doPing
  //   - 初次失败后 2s 自动再 ping 一次（覆盖 sidecar 慢热场景）
  const doPing = useCallback(async () => {
    setPinging(true)
    const ok = await pingBackend()
    setBackendOk(ok)
    setPinging(false)
    return ok
  }, [])

  useEffect(() => {
    if (step !== 1) return
    let cancelled = false
    let timerId: ReturnType<typeof setTimeout> | null = null
    let offReady: (() => void) | null = null
    let offRestarted: (() => void) | null = null

    ;(async () => {
      const ok = await doPing()
      if (cancelled) return
      if (!ok) {
        // 后端可能正在解压/启动，2s 后再试一次
        timerId = setTimeout(() => { if (!cancelled) doPing() }, 2000)
      }

      // 桌面端订阅 Tauri 事件：后端真正就绪 / 重启完成时立刻再检查一次
      if (isTauri) {
        try {
          const { listen } = await import('@tauri-apps/api/event')
          offReady = await listen('backend-ready', () => { if (!cancelled) doPing() })
          offRestarted = await listen('backend-restarted', () => { if (!cancelled) doPing() })
        }
        catch { /* 拿不到事件 API 不致命 */ }
      }
    })()

    return () => {
      cancelled = true
      if (timerId) clearTimeout(timerId)
      offReady?.()
      offRestarted?.()
    }
  }, [step, doPing])

  async function saveProvider() {
    setError('')
    if (!apiKey.trim()) { setError('请填 API Key'); return }
    if (!baseUrl.trim()) { setError('请填 API 地址'); return }
    if (!providerName.trim()) { setError('请填供应商名'); return }
    if (!modelName.trim()) { setError('请填模型名'); return }    setSavingProvider(true)
    try {
      const name = providerName.trim()
      let pid: string | undefined

      // 后端 seed_default_providers() 会预置 OpenAI / DeepSeek / Qwen 等同名供应商，
      // 直接 add_provider 撞名会报「供应商名称已存在」。所以：撞名时改为
      // 「找到那个已存在的同名供应商 → 更新它的 key / base_url」而不是新建。
      // 这些调用都带 silent:true —— 撞名是预期内的，不弹全局红 toast。
      try {
        const res: any = await addProvider({
          name,
          api_key: apiKey.trim(),
          base_url: baseUrl.trim(),
          type: 'custom',
          logo: 'custom',
        }, { silent: true })
        pid = (res?.data ?? res) as string | undefined
        if (!pid) throw new Error('后端未返回 provider id')
      }
      catch (addErr: any) {
        const msg = errText(addErr)
        if (!msg.includes('已存在')) throw addErr
        // 撞名：复用已存在的同名供应商
        const list: any[] = (await getProviderList({ silent: true })) || []
        const existing = list.find(p => p?.name === name)
        if (!existing?.id) throw new Error(`供应商「${name}」已存在但无法定位，请换个名字`)
        pid = existing.id
        await updateProviderById({
          id: pid,
          api_key: apiKey.trim(),
          base_url: baseUrl.trim(),
          enabled: 1,
        }, { silent: true })
      }

      setProviderId(pid!)

      // 加一个默认 model（同名 model 已存在时后端会报错，这里也容错）
      try {
        await addModel({ provider_id: pid!, model_name: modelName.trim() }, { silent: true })
      }
      catch (modelErr: any) {
        const msg = errText(modelErr)
        if (!msg.includes('已存在')) throw modelErr
        // 模型已存在，直接继续
      }

      // 测试连通（失败不阻断流程，让用户自己决定继续）
      try { await testConnection({ id: pid!, model: modelName.trim() }, { silent: true }) }
      catch (e: any) {
        console.warn('测试连接失败：', errText(e))
      }
      next()
    }
    catch (e: any) {
      setError(`保存失败：${errText(e)}`)
    }
    finally {
      setSavingProvider(false)
    }
  }

  async function saveTranscriber() {
    setError('')
    setSavingTranscriber(true)
    try {
      // fast-whisper / mlx-whisper 需指定 model size；在线 (groq/bcut/kuaishou) 不用
      const needsSize = transcriberType === 'fast-whisper' || transcriberType === 'mlx-whisper'
      await updateTranscriberConfig({
        transcriber_type: transcriberType,
        ...(needsSize ? { whisper_model_size: 'tiny' } : {}),
      } as any)
      next()
    }
    catch (e: any) {
      setError(`保存失败：${errText(e)}`)
    }
    finally {
      setSavingTranscriber(false)
    }
  }

  function finish() {
    markOnboarded()
    navigate('/', { replace: true })
  }

  // 真实拉取供应商可用模型（OpenAI 兼容 /v1/models）
  const probeModels = useCallback(async () => {
    setProbeError('')
    if (!apiKey.trim()) { setProbeError('请先填 API Key'); return }
    if (!baseUrl.trim()) { setProbeError('请先填 API 地址'); return }
    setProbingModels(true)
    setRemoteModels([])
    try {
      const list = await fetchRemoteModels(apiKey.trim(), baseUrl.trim(), { silent: true })
      if (list.length === 0) {
        setProbeError('该供应商未返回模型列表（部分网关不实现 /v1/models），直接手填模型名即可')
      } else {
        setRemoteModels(list)
        // 自动预选一个常见默认（含 ollama 场景 llm 前缀 / deepseek / gpt-4o-mini）
        const pref = list.find(m => /deepseek-chat/.test(m))
          || list.find(m => /gpt-4o-mini/.test(m))
          || list.find(m => /glm-4-flash/.test(m))
          || list.find(m => /qwen-(turbo|plus|flash)/.test(m))
          || list.find(m => /^(llm|qwen|glm|gpt|claude|deepseek)/i.test(m))
          || list[0]
        if (pref) setModelName(pref)
      }
    } catch (e: any) {
      setProbeError(`检测失败：${errText(e)}——可检查 Key/地址后重试，或直接手填模型名`)
    } finally {
      setProbingModels(false)
    }
  }, [apiKey, baseUrl])

  return (
    /* 2026-10-18 去玩具感（审计报告第5项）：蓝粉梦幻渐变 → 剪辑室冷灰；
       卡片微边框，与「剪辑室」工作区质感一致 */
    <div className="min-h-screen flex items-center justify-center bg-[#F5F6F8] p-6">
      <div className="w-full max-w-xl rounded-xl border border-[#E3E6EA] bg-white p-6 shadow-sm">
        <div className="flex items-center gap-3 mb-4">
          <img src={logo} alt="logo" className="h-10 w-10" />
          <div>
            <h1 className="text-xl font-bold text-[#1F2328]">欢迎使用 VidInsight / 灵悉</h1>
            <p className="text-xs text-gray-500">几步配置后就可以开始分析竞品短视频。</p>
          </div>
        </div>

        {/* Stepper */}
        <div className="mb-5 flex items-center gap-2 text-xs text-gray-500">
          {[1, 2, 3, 4].map(s => (
            <div key={s} className="flex items-center gap-2">
              <div
                className={`flex h-6 w-6 items-center justify-center rounded-full border ${step >= s ? 'border-[#1F2328] bg-[#1F2328] text-white' : 'border-gray-300 bg-white text-gray-400'}`}
              >{s}</div>
              {s < 4 && <div className={`h-px w-8 ${step > s ? 'bg-[#1F2328]' : 'bg-gray-300'}`} />}
            </div>
          ))}
        </div>

        {step === 1 && (
          <section className="flex flex-col gap-3">
            <h2 className="font-semibold">第 1 步 · 后端连通性</h2>
            <p className="text-sm text-gray-600">桌面端会自动启动 Python 后端进程。检查连通中…</p>
            {pinging && <div className="text-sm text-gray-500">检测中…</div>}
            {backendOk === true && <div className="rounded bg-green-50 p-2 text-sm text-green-700">✓ 后端已就绪</div>}
            {backendOk === false && (
              <div className="rounded bg-red-50 p-2 text-sm text-red-700">
                ✗ 暂时连不上后端。可能正在初始化（首次启动会下载依赖），等 1-2 分钟再试。
                右下角的「后端」状态点会持续监控。
              </div>
            )}
            <div className="flex gap-2 justify-end">
              {backendOk !== true && (
                <button
                  className="px-3 py-1.5 text-sm rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-50"
                  disabled={pinging}
                  onClick={doPing}
                >
                  {pinging ? '检测中…' : '重新检测'}
                </button>
              )}
              <button className="px-4 py-1.5 text-sm rounded bg-[#1F2328] text-white hover:bg-[#2A2F36] disabled:opacity-50" disabled={!backendOk} onClick={next}>
                下一步
              </button>
            </div>
          </section>
        )}

        {step === 2 && (
          <section className="flex flex-col gap-3">
            <h2 className="font-semibold">第 2 步 · 模型供应商</h2>
            <p className="text-sm text-gray-600">选一个 OpenAI 兼容供应商并填入 API Key；也可以跳过，之后在「全局配置 → AI 模型」里再配。</p>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-gray-600">供应商</span>
              <select
                className="input border rounded px-2 py-1 bg-white"
                value={PROVIDER_PRESETS.some(p => p.name === providerName) ? providerName : '自定义'}
                onChange={e => {
                  const preset = PROVIDER_PRESETS.find(p => p.name === e.target.value)
                  if (!preset) return
                  setProviderName(preset.name)
                  if (preset.base_url) setBaseUrl(preset.base_url)  // API 地址同步更换
                  setRemoteModels([])
                  setProbeError('')
                }}
              >
                {PROVIDER_PRESETS.map(p => (
                  <option key={p.name} value={p.name}>{p.name}{p.hint ? `（${p.hint}）` : ''}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-gray-600">API 地址</span>
              <input className="input border rounded px-2 py-1" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-gray-600">API Key</span>
              <input type="password" className="input border rounded px-2 py-1" value={apiKey}
                onChange={e => { setApiKey(e.target.value); setRemoteModels([]); setProbeError('') }} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-gray-600">模型名</span>
              <div className="flex gap-2">
                {remoteModels.length > 0 ? (
                  <select
                    className="input border rounded px-2 py-1 bg-white flex-1"
                    value={remoteModels.includes(modelName) ? modelName : ''}
                    onChange={e => setModelName(e.target.value)}
                  >
                    {!remoteModels.includes(modelName) && <option value="">请选择（当前手填：{modelName}）</option>}
                    {remoteModels.map(m => <option key={m} value={m}>{m}</option>)}
                  </select>
                ) : (
                  <input className="input border rounded px-2 py-1 flex-1" value={modelName}
                    onChange={e => setModelName(e.target.value)}
                    placeholder="如 gpt-4o-mini / deepseek-chat / qwen-turbo" />
                )}
                <button
                  type="button"
                  className="shrink-0 px-3 py-1 text-sm rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-50"
                  disabled={probingModels || !apiKey.trim()}
                  onClick={probeModels}
                >
                  {probingModels ? '检测中…' : '检测可用模型'}
                </button>
              </div>
              {remoteModels.length > 0 && (
                <span className="text-xs text-green-600">✓ 已从供应商拉取到 {remoteModels.length} 个可用模型，可直接下拉选择</span>
              )}
              {probeError && <span className="text-xs text-amber-600">{probeError}</span>}
            </label>
            {error && <div className="text-xs text-red-600">{error}</div>}
            <div className="flex gap-2 justify-between items-center">
              <button className="text-sm text-gray-500 hover:text-gray-800" onClick={prev}>上一步</button>
              <div className="flex gap-2 items-center">
                <button className="text-sm text-gray-400 hover:text-gray-700" onClick={next}>跳过</button>
                <button className="px-4 py-1.5 text-sm rounded bg-[#1F2328] text-white hover:bg-[#2A2F36] disabled:opacity-50" disabled={savingProvider} onClick={saveProvider}>
                  {savingProvider ? '保存中…' : '保存并下一步'}
                </button>
              </div>
            </div>
          </section>
        )}

        {step === 3 && (
          <section className="flex flex-col gap-3">
            <h2 className="font-semibold">第 3 步 · 音频转写引擎</h2>
            <p className="text-sm text-gray-600">把视频音频转成文字。<strong>推荐在线引擎</strong>，避免本地下载 ~600MB 的模型。</p>
            <div className="grid gap-2">
              {[
                { value: 'groq', title: 'Groq（在线，推荐）', desc: '注册 https://groq.com/ 拿免费 key；速度快、英文语料佳。无需本地模型。' },
                { value: 'bcut', title: '必剪（在线，免登）', desc: '免登，中文表现好；偶尔限流。' },
                { value: 'kuaishou', title: '快手（在线，免登）', desc: '与必剪类似，备选。' },
                { value: 'fast-whisper', title: 'Faster Whisper（本地）', desc: '完全离线但首次需下载 ~75MB（tiny）至 ~3GB（large-v3）的模型。CPU 慢。' },
              ].map(opt => (
                <label key={opt.value} className={`flex gap-3 p-3 rounded border cursor-pointer ${transcriberType === opt.value ? 'border-[#1F2328] bg-[#F0F1F3]' : 'border-gray-200 hover:border-gray-300'}`}>
                  <input type="radio" name="transcriber" value={opt.value} checked={transcriberType === opt.value} onChange={e => setTranscriberType(e.target.value)} />
                  <div>
                    <div className="text-sm font-medium">{opt.title}</div>
                    <div className="text-xs text-gray-500 mt-0.5">{opt.desc}</div>
                  </div>
                </label>
              ))}
            </div>
            {error && <div className="text-xs text-red-600">{error}</div>}
            <div className="flex gap-2 justify-between">
              <button className="text-sm text-gray-500 hover:text-gray-800" onClick={prev}>上一步</button>
              <button className="px-4 py-1.5 text-sm rounded bg-[#1F2328] text-white hover:bg-[#2A2F36] disabled:opacity-50" disabled={savingTranscriber} onClick={saveTranscriber}>
                {savingTranscriber ? '保存中…' : '保存并下一步'}
              </button>
            </div>
          </section>
        )}

        {step === 4 && (
          <section className="flex flex-col gap-3">
            <h2 className="font-semibold">第 4 步 · 平台登录态（可选，可跳过）</h2>
            <p className="text-sm text-gray-600">
              B 站视频解析已有兜底通道（无需配置），这里配置登录态是<strong>保底措施</strong>——
              主要用于读取 B 站<strong>字幕</strong>（含 UP 主不上传的 AI 字幕，需要有效登录态）。
            </p>
            {/* B 站一键登录（2026-10-18 用户要求加入首启引导）：复用 capture_site_cookie
                基建——弹 WebView 窗口，用户在窗口里登录，捕获 SESSDATA 后自动收窗。
                与设置页「一键登录读取」同款，定位为可跳过的保底步骤。 */}
            <button
              disabled={biliCapturing}
              onClick={async () => {
                const tauri = (window as any).__TAURI_INTERNALS__
                if (!tauri) {
                  setError('一键登录需要桌面端环境')
                  return
                }
                setBiliCapturing(true)
                setError('')
                try {
                  const { invoke } = await import('@tauri-apps/api/core')
                  const cookie = await invoke<string>('capture_site_cookie', {
                    site: 'https://www.bilibili.com/',
                    waitCookie: 'SESSDATA',
                  })
                  if (cookie) {
                    // 与设置页同款后端存储（downloader.json），字幕通道直接可用
                    const { updateDownloaderCookie } = await import('@/services/downloader.ts')
                    await updateDownloaderCookie({ cookie, platform: 'bilibili' })
                    setBiliDone(true)
                  }
                } catch (e: any) {
                  // 用户关闭窗口也会走到这里，不阻塞引导
                  setError(typeof e === 'string' ? e : e?.message || '未捕获到登录态（可跳过，稍后在「设置 → 下载配置」重试）')
                } finally {
                  setBiliCapturing(false)
                }
              }}
              className="inline-flex items-center justify-center gap-2 rounded-lg bg-[#1F2328] px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-[#2A2F36] disabled:opacity-50"
            >
              {biliCapturing ? '等待登录中…（在弹出的窗口里登录 B 站）' : biliDone ? '✓ B 站登录态已捕获' : '一键登录 B 站（推荐）'}
            </button>
            {biliDone && (
              <div className="rounded-lg border border-teal-600/30 bg-teal-50 px-3 py-2 text-xs text-teal-700">
                已保存。B 站字幕通道将在你总结 B 站视频时自动使用。
              </div>
            )}
            <p className="text-xs text-gray-400">
              抖音 / 快手等平台的 cookie 可在「设置 → 下载配置」里同样一键收集；YouTube 一般不需要。
            </p>
            <div className="flex gap-2 justify-between">
              <button className="text-sm text-gray-500 hover:text-gray-800" onClick={prev}>上一步</button>
              <button className="px-4 py-1.5 text-sm rounded bg-[#1F2328] text-white hover:bg-[#2A2F36]" onClick={finish}>
                {biliDone ? '完成，进入 VidInsight' : '跳过，直接进入'}
              </button>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

export default Onboarding
