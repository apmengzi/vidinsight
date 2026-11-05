// UI 验收 DOM 探针（在 CDP eval 里执行，返回 JSON 摘要）
// 逐项检查：按钮形态/宽度/持久化/悬浮窗
const probes = {
  // P1-1/P1-2：AppLayout 侧栏按钮应为唯一圆形按钮，收起后无横条
  sidebar: () => {
    const btns = [...document.querySelectorAll('button')].filter(b =>
      /侧边栏/.test(b.title || b.getAttribute('aria-label') || ''))
    return btns.map(b => {
      const r = b.getBoundingClientRect()
      const cls = b.className
      return {
        title: b.title || b.getAttribute('aria-label'),
        round: /rounded-full/.test(cls),
        w: Math.round(r.width), h: Math.round(r.height),
        // 收起态：主内容区顶部是否残留横条（高<=64px 的通栏元素）
      }
    })
  },
  // 顶部横条检测：主内容区里 width>80% 且 height<=64 的可见条状 div
  topBars: () => {
    const main = document.querySelector('main') || document.body
    const bars = []
    main.querySelectorAll('div,header').forEach(el => {
      const r = el.getBoundingClientRect()
      const style = getComputedStyle(el)
      if (r.width > window.innerWidth * 0.6 && r.height > 0 && r.height <= 64
        && style.display !== 'none' && r.top < 80 && el.children.length <= 3) {
        bars.push({ tag: el.tagName, h: Math.round(r.height), top: Math.round(r.top),
          text: (el.textContent || '').trim().slice(0, 40) })
      }
    })
    return bars
  },
}

console.log(JSON.stringify(Object.fromEntries(Object.entries(probes).map(([k, fn]) => [k, fn()]))))
