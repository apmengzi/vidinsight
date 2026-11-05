(async () => {
  // 原生 IDB 读 zustand persist 存储（idb-keyval 默认库 keyval-store / store keyval）
  const read = (key) => new Promise((res, rej) => {
    const req = indexedDB.open('keyval-store')
    req.onsuccess = () => {
      const db = req.result
      const tx = db.transaction('keyval', 'readonly')
      const g = tx.objectStore('keyval').get(key)
      g.onsuccess = () => res(g.result)
      g.onerror = () => rej(g.error)
    }
    req.onerror = () => rej(req.error)
  })
  const write = (key, val) => new Promise((res, rej) => {
    const req = indexedDB.open('keyval-store')
    req.onsuccess = () => {
      const db = req.result
      const tx = db.transaction('keyval', 'readwrite')
      const p = tx.objectStore('keyval').put(val, key)
      p.onsuccess = () => res('ok')
      p.onerror = () => rej(p.error)
    }
    req.onerror = () => rej(req.error)
  })

  const raw = await read('vidinsight-conversations')
  if (!raw) return 'FAIL: conversations 存储不存在'
  const data = JSON.parse(raw)
  const conv = data.state.conversations[0]
  if (!conv) return 'FAIL: 无会话'

  const userCount = conv.messages.filter(m => m.role === 'user').length
  // 找最后一条 user 消息的下标（真实问答留下的）
  let lastUserIdx = -1
  conv.messages.forEach((m, i) => { if (m.role === 'user') lastUserIdx = i })
  if (lastUserIdx < 0) return 'FAIL: 会话无 user 消息'

  // 注入两个锚点不同的成功卡：一个锚 lastUserIdx，一个锚 0（第一条 user 消息）
  conv.taskProgresses = (conv.taskProgresses || []).filter(t => !String(t.taskId).startsWith('vi-accept-'))
  conv.taskProgresses.push({
    taskId: 'vi-accept-anchor-last', status: 'SUCCESS', conversationId: conv.id,
    anchorMessageIndex: lastUserIdx, title: '验收卡A（锚最后一条user）', url: 'https://example.com/a',
    platform: 'bilibili', index: 0, elapsed_s: 42, stage_times: {},
  })
  if (lastUserIdx !== 0) {
    conv.taskProgresses.push({
      taskId: 'vi-accept-anchor-first', status: 'SUCCESS', conversationId: conv.id,
      anchorMessageIndex: 0, title: '验收卡B（锚第一条user）', url: 'https://example.com/b',
      platform: 'youtube', index: 1, elapsed_s: 33, stage_times: {},
    })
  }
  await write('vidinsight-conversations', JSON.stringify(data))
  return JSON.stringify({ 注入: 'ok', 会话: conv.title, 消息数: conv.messages.length, lastUserIdx, userCount })
})()
