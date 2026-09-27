import fs from 'node:fs'
import path from 'node:path'
import { open } from './drive.mjs'
const DIR = process.argv[2]
const b = await open(Number(process.env.CDP_PORT || 9336), process.env.CDP_PROFILE || '/tmp/cdp-demo-profile')
fs.rmSync(path.join(DIR, 'frames'), { recursive: true, force: true }); fs.mkdirSync(path.join(DIR, 'frames'))
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const read = (f) => fs.readFileSync(path.join(DIR, f), 'utf8').replace(/\s+$/, '')
let n = 0; const frames = []
async function shot(hold) { const f = `f${String(n++).padStart(4, '0')}.png`; await b.shot(path.join(DIR, 'frames', f)); frames.push({ f, hold }) }
async function term(html, hold, opts = {}) {
  await b.eval(`(() => { const t = document.getElementById('term'); t.style.whiteSpace = ${JSON.stringify(opts.wrap ? 'pre-wrap' : 'pre')}; t.innerHTML = ${JSON.stringify(html)}; t.scrollTop = ${opts.top ? 0 : 't.scrollHeight'}; return 1 })()`)
  await shot(hold)
}
async function type(prefix, text, cps = 3) {
  for (let i = 0; i <= text.length; i += cps) await term(prefix + `<span class="u">&gt; ${esc(text.slice(0, i))}</span><span class="cur"> </span>`, 0.045)
  return prefix + `<span class="u">&gt; ${esc(text)}</span>\n`
}
const tool = (s) => `<span class="ok">●</span> <span class="b">${esc(s)}</span>\n`
const colorTable = (t) => t.split('\n').map((l) => { const e = esc(l); if (/^(TEMPO|LOKI) ·/.test(l)) return `<span class="h">${e}</span>`; if (/^Not covered:|^Next:|^\s+reading:|^\s+not in this map:/.test(l)) return `<span class="warn">${e}</span>`; if (/^\s+(message|traces|signature):/.test(l)) return `<span class="dim">${e}</span>`; if (/^(Error sweep|map: generated)/.test(l)) return `<span class="b">${e}</span>`; if (/^\s+\d+\. /.test(l)) return `<span class="ok">${e}</span>`; return e }).join('\n')
const colorReport = (t) => t.split('\n').map((l) => { const e = esc(l); return /^#/.test(l) ? `<span class="h">${e}</span>` : /^\*\*/.test(l) ? `<span class="b">${e}</span>` : e }).join('\n')
// caption pill injected into archify pages
async function caption(text) {
  await b.eval(`(() => { let c = document.getElementById('demo-cap'); if (!c) { c = document.createElement('div'); c.id = 'demo-cap'; c.style.cssText = 'position:fixed;left:18px;bottom:18px;z-index:2147483647;background:rgba(9,14,22,.92);color:#e6edf3;border:1px solid #58a6ff;border-radius:8px;padding:9px 14px;font:600 14px/1.3 system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.45);max-width:520px'; document.body.appendChild(c) } c.textContent = ${JSON.stringify(text)}; return 1 })()`)
}
async function clickText(text) {
  const p = await b.eval(`(() => { const els = [...document.querySelectorAll('button,[role=button],[role=menuitemradio],[role=menuitem],[role=option],li,div,span')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (e.innerText || '').trim().split('\\n')[0].trim() === ${JSON.stringify(text)} }); els.sort((a, b) => a.getBoundingClientRect().width * a.getBoundingClientRect().height - b.getBoundingClientRect().width * b.getBoundingClientRect().height); const e = els[0]; if (!e) return null; const r = e.getBoundingClientRect(); return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)] })()`)
  if (!p) throw new Error('não achei: ' + text)
  await b.click(p[0], p[1], 900)
}
const K = { esc: () => b.key('Escape', 'Escape', 27, 400) }

// ── 1: sweep ──
await b.go(`file://${DIR}/stage.html`, 700)
let s = await type('', '/sherlock-holmes:error-sweep  any errors? use the bundled cascade fixture')
s += tool('Skill(sherlock-holmes:error-sweep)'); await term(s, 0.4)
s += tool('Bash(python3 …/sweep-errors.py --format table --fixture …/cascade)'); await term(s, 0.5)
const table = colorTable(read('sweep.txt')).split('\n')
for (let i = 10; i < table.length; i += 10) await term(s + '\n' + table.slice(0, i).join('\n'), 0.06)
s += '\n' + table.join('\n') + '\n'
await term(s, 3.2)

// ── 2: map ──
s = await type('', 'map')
s += tool('Bash(python3 …/window-map.py --start 2026-09-17T13:00:00.000Z --end 2026-09-17T14:10:00.000Z --fixture …/cascade)'); await term(s, 0.6)
s += '\n' + colorTable(read('map.txt').replace(/\/[^ ]*\/map\.html/, '/tmp/trace-debug/sweep-20260917T130000000Z-20260917T141000000Z.map.html')) + '\n'
await term(s, 4.0, { wrap: true, top: true })

// ── 3: exploring the map ──
await b.go(`file://${DIR}/map.html`, 2500)
await caption('Window map · one arrow per kind of call, counted in the error traces'); await shot(2.6)
await caption('Find a node (/)'); await b.click(894, 796, 500)
for (const ch of 'cust') { await b.type(ch, 60); await shot(0.12) }
await shot(1.0)
await b.key('Enter', 'Enter', 13, 1200); await caption('Its passport: upstream, downstream, the recorded calls'); await shot(2.6)
await K.esc(); await K.esc()
await caption('Click an arrow: the kind of call it stands for'); await b.click(611, 411, 1200); await shot(2.4)
await K.esc(); await K.esc()
await caption('PATH: the recorded route between two services'); await b.click(784, 796, 600); await b.click(287, 292, 600); await b.click(936, 292, 1200); await shot(2.6)
await K.esc(); await K.esc()
await b.go(`file://${DIR}/map.html`, 2200)
await caption('MAP: the semantic radar of the whole diagram'); await b.click(819, 796, 1100); await shot(1.8); await b.click(819, 796, 400)
await caption('LENS: compare roles - backends vs databases'); await b.click(855, 796, 800); await clickText('Database'); await shot(2.2)
await b.click(855, 796, 400); await K.esc()
await caption('Guided chapter: only the calls with error status'); await b.click(922, 147, 1400); await shot(2.2)
await K.esc(); await K.esc()
await b.go(`file://${DIR}/map.html`, 2200)
await caption('Visual style: Classic → Blueprint → Editorial'); await b.click(842, 38, 800); await shot(1.2)
await clickText('Blueprint'); await b.move(600, 600); await shot(1.8)
await b.click(842, 38, 800); await clickText('Editorial'); await b.move(600, 600); await shot(1.8)
await caption('Light theme'); await b.click(738, 38, 1000); await shot(1.8)
await caption('Presentation stage'); await b.click(1037, 38, 1500); await shot(2.2)

// ── 4: diagram 1 ──
await b.go(`file://${DIR}/stage.html`, 700)
s = await type('', 'diagram 1')
s += tool('Bash(python3 …/collect-trace.py --trace-id 2aa803b2e40c97a2490d754a465fe9de --source tempo --fixture …/01-downstream-503)')
s += tool('Bash(python3 …/trace-diagram.py --input /tmp/trace-debug/2aa803b2e40c97a2490d754a465fe9de.json)')
s += '\n<span class="ok">diagram: generated /tmp/trace-debug/2aa803b2e40c97a2490d754a465fe9de.html</span>\n<span class="dim">(6 messages; quality showcase; evidence only: spans as recorded, no causal claim)</span>\n'
await term(s, 1.8)
await b.go(`file://${DIR}/diagram.html`, 2500)
await caption('One trace: every call and its return, with recorded times and status'); await shot(2.8)
await caption('Play story: the guided chapters, step by step'); await b.key('p', 'KeyP', 80, 1300); await shot(1.3); await b.sleep(1200); await shot(1.3); await b.sleep(1200); await shot(1.3)

// ── 5: investigate 1 ──
await b.go(`file://${DIR}/stage.html`, 700)
s = await type('', 'investigate 1')
s += tool('Skill(sherlock-holmes:trace-debug)  2aa803b2e40c97a2490d754a465fe9de')
s += tool('Agent(sherlock-holmes)  timeline → first anomaly → hypotheses → elimination → causal chain'); await term(s, 0.8)
const rep = colorReport(read('report-excerpt.md')).split('\n')
for (let i = 8; i < rep.length; i += 8) await term(s + '\n' + rep.slice(0, i).join('\n'), 0.08, { wrap: true, top: true })
await term(s + '\n' + rep.join('\n') + '\n<span class="dim">…  (evidence, gaps and at most three next checks follow)</span>', 5.0, { wrap: true, top: true })

fs.writeFileSync(path.join(DIR, 'frames', 'list.txt'), frames.map((x) => `file '${x.f}'\nduration ${x.hold}`).join('\n') + `\nfile '${frames[frames.length - 1].f}'\n`)
console.log('frames:', frames.length, 'duração:', frames.reduce((a, x) => a + x.hold, 0).toFixed(1) + 's')
b.close()
