import { spawn } from 'node:child_process'
import fs from 'node:fs'
export async function open(port = 9335, profile = '/tmp/cdp-demo-profile') {
  const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--hide-scrollbars', '--window-size=1200,860', 'about:blank'], { stdio: 'ignore' })
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  let wsu; for (let i = 0; i < 50 && !wsu; i++) { try { wsu = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page')?.webSocketDebuggerUrl } catch {} ; if (!wsu) await sleep(200) }
  const ws = new WebSocket(wsu); await new Promise((r) => ws.addEventListener('open', r))
  let id = 0; const pend = new Map()
  ws.addEventListener('message', (m) => { const d = JSON.parse(m.data); if (pend.has(d.id)) { const p = pend.get(d.id); pend.delete(d.id); d.error ? p.rej(new Error(JSON.stringify(d.error))) : p.res(d.result) } })
  const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })) })
  await send('Page.enable'); await send('Runtime.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 1200, height: 860, deviceScaleFactor: 1, mobile: false })
  const api = {
    send, sleep,
    async go(url, wait = 2500) { await send('Page.navigate', { url }); await sleep(wait) },
    async eval(expr) { const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 300)); return r.result.value },
    async move(x, y) { await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }) },
    async click(x, y, wait = 700) {
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }); await sleep(120)
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 }); await sleep(wait)
    },
    async key(key, code, vk, wait = 500) {
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode: vk, ...(key.length === 1 ? { text: key } : {}) })
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: vk }); await sleep(wait)
    },
    async type(text, wait = 400) { for (const ch of text) { await send('Input.insertText', { text: ch }); await sleep(70) } await sleep(wait) },
    async shot(file) { const { data } = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(file, Buffer.from(data, 'base64')) },
    async center(sel) { return api.eval(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null; const r = e.getBoundingClientRect(); return [Math.round(r.x + r.width/2), Math.round(r.y + r.height/2)] })()`) },
    close() { ws.close(); chrome.kill() },
  }
  return api
}
