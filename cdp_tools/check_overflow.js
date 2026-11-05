(() => {
  const de = document.documentElement
  const issues = []
  // 1. 横向溢出
  if (de.scrollWidth > de.clientWidth + 2) {
    issues.push('横向溢出: scrollWidth=' + de.scrollWidth + ' > clientWidth=' + de.clientWidth)
  }
  // 2. 关键元素超出视口右缘
  const tags = ['button', 'aside', 'main', 'header']
  tags.forEach(tag => {
    document.querySelectorAll(tag).forEach(el => {
      const r = el.getBoundingClientRect()
      if (r.width > 0 && r.right > de.clientWidth + 3) {
        issues.push(tag + ' 超右缘: ' + (el.title || el.getAttribute('aria-label') || el.className.toString().slice(0, 40)) + ' right=' + Math.round(r.right))
      }
    })
  })
  // 3. 悬浮窗在视口内
  const dlg = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (dlg) {
    const r = dlg.getBoundingClientRect()
    if (r.x < 0 || r.y < 0 || r.right > de.clientWidth + 1 || r.bottom > de.clientHeight + 1) {
      issues.push('悬浮窗越界: ' + JSON.stringify({ x: Math.round(r.x), y: Math.round(r.y), right: Math.round(r.right), bottom: Math.round(r.bottom), vw: de.clientWidth, vh: de.clientHeight }))
    }
  }
  // 4. 收起按钮与悬浮窗/其他元素重叠检测（圆形按钮层 z-20，悬浮窗 z-50 本就盖上面——只查同层可见重叠）
  const roundBtns = [...document.querySelectorAll('button')].filter(b => /rounded-full/.test(b.className) && b.getBoundingClientRect().height > 0)
  const overlap = []
  for (let i = 0; i < roundBtns.length; i++) {
    for (let j = i + 1; j < roundBtns.length; j++) {
      const a = roundBtns[i].getBoundingClientRect(), c = roundBtns[j].getBoundingClientRect()
      const x = Math.max(0, Math.min(a.right, c.right) - Math.max(a.left, c.left))
      const y = Math.max(0, Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top))
      if (x > 4 && y > 4) overlap.push((roundBtns[i].title || i) + ' ↔ ' + (roundBtns[j].title || j))
    }
  }
  if (overlap.length) issues.push('圆形按钮互相重叠: ' + overlap.join('; '))

  return JSON.stringify({
    视口: de.clientWidth + 'x' + de.clientHeight,
    圆形按钮数: roundBtns.length,
    结果: issues.length === 0 ? 'PASS: 无溢出无重叠' : issues,
  }, null, 1)
})()
