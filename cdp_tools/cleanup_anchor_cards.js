(async () => {
  const read = (key) => new Promise((res, rej) => {
    const req = indexedDB.open('keyval-store')
    req.onsuccess = () => {
      const g = req.result.transaction('keyval', 'readonly').objectStore('keyval').get(key)
      g.onsuccess = () => res(g.result)
      g.onerror = () => rej(g.error)
    }
  })
  const write = (key, val) => new Promise((res, rej) => {
    const req = indexedDB.open('keyval-store')
    req.onsuccess = () => {
      const p = req.result.transaction('keyval', 'readwrite').objectStore('keyval').put(val, key)
      p.onsuccess = () => res('ok')
      p.onerror = () => rej(p.error)
    }
  })
  const data = JSON.parse(await read('vidinsight-conversations'))
  let removed = 0
  for (const c of data.state.conversations) {
    if (c.taskProgresses) {
      const n = c.taskProgresses.length
      c.taskProgresses = c.taskProgresses.filter(t => !String(t.taskId).startsWith('vi-accept-'))
      removed += n - c.taskProgresses.length
    }
  }
  await write('vidinsight-conversations', JSON.stringify(data))
  return '清理完成，移除验收卡 ' + removed + ' 张'
})()
