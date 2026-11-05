(async () => {
  // 找「洞察问答」工具栏按钮（MarkdownHeader）
  const btn = [...document.querySelectorAll('button')].find(b => (b.textContent || '').includes('洞察问答'))
  if (!btn) return 'FAIL: 无洞察问答按钮（当前可能无笔记详情）'
  const mainW = () => {
    const ms = [...document.querySelectorAll('main')]
    return Math.round(ms[ms.length - 1].getBoundingClientRect().width)
  }
  const before = mainW()
  btn.click()
  await new Promise(r => setTimeout(r, 800))

  const dlg = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (!dlg) return 'FAIL: 悬浮窗未出现'
  const r = dlg.getBoundingClientRect()
  const cs = getComputedStyle(dlg)
  const after = mainW()
  return JSON.stringify({
    悬浮窗出现: 'PASS',
    定位方式: cs.position,
    几何: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    视口: { w: window.innerWidth, h: window.innerHeight },
    宽度380: Math.round(r.width) === 380 ? 'PASS' : '宽=' + Math.round(r.width),
    高度60vh: Math.abs(r.height - window.innerHeight * 0.6) < 4 ? 'PASS' : '高=' + Math.round(r.height),
    默认右下角: r.x + r.width > window.innerWidth - 60 && r.y + r.height > window.innerHeight - 60 ? 'PASS' : 'CHECK',
    正文不被挤压: before === after ? 'PASS (' + after + 'px)' : 'FAIL: ' + before + ' -> ' + after,
  }, null, 1)
})()
