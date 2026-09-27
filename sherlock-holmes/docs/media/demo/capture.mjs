import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
const DIR = process.argv[2]
const W = 1200, H = 860
const chrome = spawn('google-chrome', ['--headless=new', '--remote-debugging-port=9333', `--user-data-dir=${DIR}/chrome-prof`, '--no-first-run', '--hide-scrollbars', `--window-size=${W},${H}`, 'about:blank'], { stdio: 'ignore' })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let ws, id = 0; const pending = new Map()
async function connect() {
  for (let i = 0; i < 50; i++) {
    try { const l = await (await fetch('http://127.0.0.1:9333/json/list')).json(); const pg = l.find((t) => t.type === 'page'); if (pg) return pg.webSocketDebuggerUrl } catch {}
    await sleep(200)
  }
  throw new Error('chrome não subiu')
}
const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })) })
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const read = (f) => fs.readFileSync(path.join(DIR, f), 'utf8').replace(/\s+$/, '')
let n = 0; const frames = []
async function shot(hold) {
  const { data } = await send('Page.captureScreenshot', { format: 'png' })
  const f = `f${String(n++).padStart(4, '0')}.png`; fs.writeFileSync(path.join(DIR, 'frames', f), Buffer.from(data, 'base64')); frames.push({ f, hold })
}
async function term(html, hold, opts = {}) {
  const r = await send('Runtime.evaluate', { expression: `(() => { const t = document.getElementById('term'); t.style.whiteSpace = ${JSON.stringify(opts.wrap ? 'pre-wrap' : 'pre')}; t.innerHTML = ${JSON.stringify(html)}; t.scrollTop = ${opts.top ? 0 : 't.scrollHeight'}; return t.textContent.length })()`, returnByValue: true })
  if (r.exceptionDetails) throw new Error('evaluate: ' + JSON.stringify(r.exceptionDetails).slice(0, 300))
  await shot(hold)
}
async function type(prefix, text, cps = 3) {
  for (let i = 0; i <= text.length; i += cps) await term(prefix + `<span class="u">&gt; ${esc(text.slice(0, i))}</span><span class="cur"> </span>`, 0.045)
  return prefix + `<span class="u">&gt; ${esc(text)}</span>\n`
}
const tool = (s) => `<span class="ok">●</span> <span class="b">${esc(s)}</span>\n`
function colorTable(t) {
  return t.split('\n').map((l) => {
    const e = esc(l)
    if (/^(TEMPO|LOKI) ·/.test(l)) return `<span class="h">${e}</span>`
    if (/^Not covered:|^Next:/.test(l)) return `<span class="warn">${e}</span>`
    if (/^\s+(message|traces|signature):/.test(l)) return `<span class="dim">${e}</span>`
    if (/^Error sweep/.test(l)) return `<span class="b">${e}</span>`
    return e
  }).join('\n')
}
function colorReport(t) {
  return t.split('\n').map((l) => { const e = esc(l); return /^#/.test(l) ? `<span class="h">${e}</span>` : /^\*\*/.test(l) ? `<span class="b">${e}</span>` : e }).join('\n')
}
ws = new WebSocket(await connect())
await new Promise((r) => ws.addEventListener('open', r))
ws.addEventListener('message', (m) => { const d = JSON.parse(m.data); if (d.id && pending.has(d.id)) { const p = pending.get(d.id); pending.delete(d.id); d.error ? p.rej(new Error(d.error.message)) : p.res(d.result) } })
await send('Page.enable'); await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false })
fs.mkdirSync(path.join(DIR, 'frames'), { recursive: true })
await send('Page.navigate', { url: 'file://' + path.join(DIR, 'stage.html') }); await sleep(800)

// ── cena 1: varredura ──
let s = await type('', '/sherlock-holmes:error-sweep  any errors? use the bundled cascade fixture')
await term(s, 0.5)
s += tool('Skill(sherlock-holmes:error-sweep)'); await term(s, 0.5)
s += tool('Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/trace-debug/scripts/sweep-errors.py --format table --fixture …/cascade)'); await term(s, 0.7)
const table = colorTable(read('sweep.txt')).split('\n')
for (let i = 8; i < table.length; i += 8) await term(s + '\n' + table.slice(0, i).join('\n'), 0.07)
s += '\n' + table.join('\n') + '\n\n'
await term(s, 3.5)
s += 'Reply <span class="u">"diagram N"</span> to draw that row\'s first example trace, or <span class="u">"investigate N"</span> for a full trace-debug investigation.\n\n'
await term(s, 3.5)

// ── cena 2: diagram 1 ──
s = await type('', 'diagram 1')
await term(s, 0.4)
s += tool('Bash(python3 …/collect-trace.py --trace-id 2aa803b2e40c97a2490d754a465fe9de --source tempo --format prompt --fixture …/01-downstream-503)'); await term(s, 0.6)
s += tool('Bash(python3 …/trace-diagram.py --input /tmp/trace-debug/2aa803b2e40c97a2490d754a465fe9de.json)'); await term(s, 0.6)
s += '\n<span class="ok">diagram: generated /tmp/trace-debug/2aa803b2e40c97a2490d754a465fe9de.html</span>\n<span class="dim">(6 messages; quality showcase; evidence only: spans as recorded, no causal claim)</span>\n'
await term(s, 2.2)

// ── cena 3: o diagrama no navegador ──
await send('Page.navigate', { url: 'file://' + path.join(DIR, 'diagram.html') }); await sleep(2500)
await shot(3.0)
for (const key of ['p']) {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key, text: key, code: 'KeyP', windowsVirtualKeyCode: 80 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: 'KeyP', windowsVirtualKeyCode: 80 })
  for (let i = 0; i < 4; i++) { await sleep(1300); await shot(1.3) }
}

// ── cena 4: investigate 1 ──
await send('Page.navigate', { url: 'file://' + path.join(DIR, 'stage.html') }); await sleep(800)
s = await type('', 'investigate 1')
await term(s, 0.4)
s += tool('Skill(sherlock-holmes:trace-debug)  2aa803b2e40c97a2490d754a465fe9de'); await term(s, 0.4)
s += tool('Agent(sherlock-holmes)  timeline → first anomaly → hypotheses → elimination → causal chain'); await term(s, 0.9)
const rep = colorReport(read('report-excerpt.md')).split('\n')
for (let i = 6; i < rep.length; i += 6) await term(s + '\n' + rep.slice(0, i).join('\n'), 0.09, { wrap: true, top: true })
await term(s + '\n' + rep.join('\n') + '\n<span class="dim">…  (evidence, gaps and at most three next checks follow)</span>', 6.0, { wrap: true, top: true })

fs.writeFileSync(path.join(DIR, 'frames', 'list.txt'), frames.map((x) => `file '${x.f}'\nduration ${x.hold}`).join('\n') + `\nfile '${frames[frames.length - 1].f}'\n`)
console.log('frames:', frames.length, 'duração total:', frames.reduce((a, x) => a + x.hold, 0).toFixed(1) + 's')
ws.close(); chrome.kill()
