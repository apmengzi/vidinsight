import { useCallback, useEffect, useState } from 'react'

type Theme = 'light' | 'dark'
const KEY = 'vidinsight_theme'

function apply(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

/** VS Code 式主题切换：浅色 studio / 深色剪辑室，localStorage 持久化 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const saved = localStorage.getItem(KEY)
      if (saved === 'light' || saved === 'dark') return saved
    } catch { /* ignore */ }
    return 'light'
  })

  useEffect(() => {
    apply(theme)
    try { localStorage.setItem(KEY, theme) } catch { /* ignore */ }
  }, [theme])

  const toggle = useCallback(() => setTheme(t => (t === 'dark' ? 'light' : 'dark')), [])
  return { theme, toggle }
}
