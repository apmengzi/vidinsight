// CDP 9333 验收工具（Tauri WebView2）
// 用法:
//   node cdp.mjs eval '<js 表达式>'            — Runtime.evaluate（awaitPromise, returnByValue）
//   node cdp.mjs shot out.png [w] [h]          — 截图（可选先设置视口大小）
//   node cdp.mjs nav 'http://tauri.localhost/#/history'  — 导航
//   node cdp.mjs resize 1280 800               — Emulation 设置视口（虚拟分辨率）
//   node cdp.mjs console                       — 抓最近 console 消息（通过注入监听）
import { writeFileSync, readFileSync } from 'node:fs'

const BASE = 'http://127.0.0.1:9333'

async function getPageWs() {
  const list = await (await fetch(`${BASE}/json`)).json()
  const page = list.find(p => p.type === 'page')
  if (!page) throw new Error('no page target')
  return { ws: page.webSocketDebuggerUrl, id: page.id }
}

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl)
    this.seq = 0
    this.pending = new Map()
    this.events = []
    this.ws.onmessage = ev => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(JSON.stringify(msg.error)))
        else resolve(msg.result)
      } else if (msg.method) {
        this.events.push(msg)
      }
    }
    this.ready = new Promise((res, rej) => {
      this.ws.onopen = res
      this.ws.onerror = rej
    })
  }
  send(method, params = {}) {
    const id = ++this.seq
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }))
  }
  async eval(expr, { awaitPromise = true } = {}) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr,
      awaitPromise,
      returnByValue: true,
    })
    if (r.exceptionDetails) {
      throw new Error('page exception: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text))
    }
    return r.result?.value
  }
  close() { this.ws.close() }
}

const [, , cmd, ...args] = process.argv

const { ws } = await getPageWs()
const cdp = new CDP(ws)
await cdp.ready

try {
  if (cmd === 'eval') {
    const v = await cdp.eval(args[0])
    console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1))
  } else if (cmd === 'evalfile') {
    // 从文件读表达式，规避 shell 引号/转义干扰
    const v = await cdp.eval(readFileSync(args[0], 'utf8'))
    console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1))
  } else if (cmd === 'shot') {
    const [out, w, h] = args
    if (w && h) {
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width: Number(w), height: Number(h), deviceScaleFactor: 1, mobile: false,
      })
      await new Promise(r => setTimeout(r, 600)) // 等布局/动画稳定
    }
    const r = await cdp.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(out, Buffer.from(r.data, 'base64'))
    console.log('saved', out)
  } else if (cmd === 'resize') {
    const [w, h] = args
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: Number(w), height: Number(h), deviceScaleFactor: 1, mobile: false,
    })
    await new Promise(r => setTimeout(r, 500))
    console.log('resized', w, h)
  } else if (cmd === 'nav') {
    await cdp.send('Page.navigate', { url: args[0] })
    await new Promise(r => setTimeout(r, 1200))
    console.log('navigated', args[0])
  } else if (cmd === 'console') {
    // 注入捕获器抓 console.error / React 警告（页面加载后的增量）
    const v = await cdp.eval(`(() => {
      window.__vi_logs = window.__vi_logs || []
      if (!window.__vi_hooked) {
        window.__vi_hooked = true
        const origError = console.error.bind(console)
        console.error = (...a) => { window.__vi_logs.push(['error', a.map(String).join(' ')]); origError(...a) }
        const origWarn = console.warn.bind(console)
        console.warn = (...a) => { window.__vi_logs.push(['warn', a.map(String).join(' ')]); origWarn(...a) }
      }
      return window.__vi_logs.slice()
    })()`)
    console.log(JSON.stringify(v, null, 1))
  } else if (cmd === 'logs') {
    const v = await cdp.eval('window.__vi_logs ? window.__vi_logs.slice() : "no hook (run console first)"')
    console.log(JSON.stringify(v, null, 1))
  } else if (cmd === 'drag') {
    // CDP 真实输入管线拖动：mousePressed → mouseMoved(带按钮态) → mouseReleased
    const [x1, y1, x2, y2] = args.map(Number)
    const base = { button: 'left', buttons: 1, clickCount: 1 }
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: x1, y: y1, ...base })
    // 分多步 move，路径平滑且事件序列贴近真实拖动
    const steps = 8
    for (let i = 1; i <= steps; i++) {
      await cdp.send('Input.dispatchMouseEvent', {
        type: 'mouseMoved',
        x: x1 + ((x2 - x1) * i) / steps,
        y: y1 + ((y2 - y1) * i) / steps,
        ...base,
      })
      await new Promise(r => setTimeout(r, 30))
    }
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x2, y: y2, ...base })
    await new Promise(r => setTimeout(r, 300))
    console.log('dragged', x1, y1, '->', x2, y2)
  } else if (cmd === 'click') {
    const [x, y] = args.map(Number)
    const base = { button: 'left', buttons: 1, clickCount: 1 }
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, ...base })
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, ...base })
    await new Promise(r => setTimeout(r, 300))
    console.log('clicked', x, y)
  } else if (cmd === 'setwin') {
    // Browser.setWindowBounds：真实改窗口尺寸（CDP 标准，WebView2 支持）
    const [w, h] = args.map(Number)
    const { windowId } = await cdp.send('Browser.getWindowForTarget')
    await cdp.send('Browser.setWindowBounds', {
      windowId,
      bounds: { left: 60, top: 60, width: w, height: h, windowState: 'normal' },
    })
    await new Promise(r => setTimeout(r, 900))
    const v = await cdp.eval('document.documentElement.clientWidth + "x" + document.documentElement.clientHeight')
    console.log('window ->', w, h, '| client:', v)
  } else {
    console.log('unknown cmd', cmd)
  }
} finally {
  cdp.close()
}
