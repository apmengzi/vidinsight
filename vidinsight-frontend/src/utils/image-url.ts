const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')
const ASSET_BASE = API_BASE.replace(/\/api$/, '')

function isSpecialUrl(url: string) {
  return /^(data:|blob:|file:)/i.test(url)
}

function isBackendAssetUrl(url: string) {
  if (url.startsWith('/static/') || url.startsWith('/uploads/')) return true

  try {
    const parsed = new URL(url)
    const apiOrigin = API_BASE.startsWith('http') ? new URL(API_BASE).origin : window.location.origin
    return (
      parsed.origin === apiOrigin &&
      (parsed.pathname.startsWith('/static/') || parsed.pathname.startsWith('/uploads/'))
    )
  } catch {
    return false
  }
}

export function resolveAssetUrl(src?: string | null) {
  if (!src) return ''
  if (isSpecialUrl(src) || /^https?:\/\//i.test(src)) return src
  if (src.startsWith('/')) return `${ASSET_BASE}${src}`
  return src
}

export function proxiedImageUrl(src?: string | null) {
  if (!src) return ''
  const resolved = resolveAssetUrl(src)
  if (!resolved || isSpecialUrl(resolved) || isBackendAssetUrl(resolved)) return resolved
  if (!/^https?:\/\//i.test(resolved)) return resolved
  return `${API_BASE}/image_proxy?url=${encodeURIComponent(resolved)}`
}
