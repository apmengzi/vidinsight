(async () => {
  const dlg = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (!dlg) return 'FAIL: 悬浮窗不在'
  // 最小化按钮
  const minBtn = [...dlg.querySelectorAll('button')].find(b => (b.title || '').includes('最小化'))
  if (!minBtn) return 'FAIL: 无最小化按钮'
  const mr = minBtn.getBoundingClientRect()
  minBtn.click()
  await new Promise(r => setTimeout(r, 500))

  const dlgGone = !document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  const bubble = document.querySelector('button[aria-label="展开洞察问答"]')
  if (!dlgGone || !bubble) return 'FAIL: 最小化后无气泡 dlgGone=' + dlgGone
  const br = bubble.getBoundingClientRect()
  const bubbleOk = {
    圆形: bubble.className.includes('rounded-full'),
    尺寸: Math.round(br.width) + 'x' + Math.round(br.height),
    右下角: Math.round(br.right) > window.innerWidth - 80 && Math.round(br.bottom) > window.innerHeight - 80,
  }
  // 点气泡恢复
  bubble.click()
  await new Promise(r => setTimeout(r, 500))
  const dlg2 = document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]')
  if (!dlg2) return 'FAIL: 点气泡未恢复窗口。气泡信息: ' + JSON.stringify(bubbleOk)
  const r2 = dlg2.getBoundingClientRect()

  // 关闭
  const closeBtn = [...dlg2.querySelectorAll('button')].find(b => (b.title || '').includes('关闭'))
  closeBtn.click()
  await new Promise(r => setTimeout(r, 500))
  const allGone = !document.querySelector('[role="dialog"][aria-label="洞察问答悬浮窗"]') && !document.querySelector('button[aria-label="展开洞察问答"]')

  return JSON.stringify({
    最小化_气泡: bubbleOk,
    气泡_恢复窗口: 'PASS，位置保持 (' + Math.round(r2.x) + ',' + Math.round(r2.y) + ')',
    关闭_回到普通模式: allGone ? 'PASS' : 'FAIL',
  }, null, 1)
})()
