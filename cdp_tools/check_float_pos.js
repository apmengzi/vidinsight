(() => {
  const dlg = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (!dlg) return 'FAIL: 悬浮窗不在'
  const r = dlg.getBoundingClientRect()
  return JSON.stringify({ x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) })
})()
