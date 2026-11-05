(async () => {
  // 切深色主题（AppLayout 底部的「深色剪辑室/浅色工作面」按钮）
  const themeBtn = [...document.querySelectorAll('button')].find(b =>
    (b.textContent || '').includes('深色剪辑室') || (b.textContent || '').includes('浅色工作面'))
  if (!themeBtn) return 'FAIL: 无主题切换按钮'
  const isDark = document.documentElement.classList.contains('dark')
  if (!isDark) { themeBtn.click(); await new Promise(r => setTimeout(r, 800)) }

  // 打开悬浮窗
  const chatBtn = [...document.querySelectorAll('button')].find(b => (b.textContent || '').includes('洞察问答'))
  if (chatBtn && !document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')) {
    chatBtn.click(); await new Promise(r => setTimeout(r, 700))
  }
  const dlg = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (!dlg) return 'FAIL: 悬浮窗未打开'

  const lum = (rgbStr) => {
    const m = rgbStr.match(/\d+(\.\d+)?/g)
    if (!m) return -1
    const [r, g, b] = m.map(Number)
    const f = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const contrast = (a, b) => {
    const l1 = lum(a), l2 = lum(b)
    if (l1 < 0 || l2 < 0) return -1
    const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1]
    return (hi + 0.05) / (lo + 0.05)
  }
  const cs = getComputedStyle(dlg)
  const bg = cs.backgroundColor
  const ink = getComputedStyle(dlg.querySelector('.text-sm.font-medium')).color
  const ink3 = getComputedStyle(dlg.querySelector('.tc') || dlg).color
  return JSON.stringify({
    theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light',
    悬浮窗底色: bg, 标题字色: ink,
    对比度_标题: contrast(ink, bg).toFixed(2) + ':1（WCAG AA 需 ≥4.5）',
    对比度判定: contrast(ink, bg) >= 4.5 ? 'PASS' : 'FAIL',
  }, null, 1)
})()
