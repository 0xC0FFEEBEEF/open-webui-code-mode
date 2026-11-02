// node liveshot.mjs <urls-file> <out-prefix> <plan>
// Renders each URL (one per line) in an Open-WebUI-like sandboxed iframe on a page served as the Open WebUI origin
// (intercepted, so frame-ancestors applies as in the real thing). URLs are read from a file and never printed.
// plan: comma list of "wait:SEC", "shot:NAME", "click:X,Y" (page coordinates), e.g. "wait:4,shot:a,click:400,20,wait:3,shot:b"
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const [file, prefix, plan = 'wait:5,shot:a'] = process.argv.slice(2);
const urls = fs.readFileSync(file, 'utf8').split('\n').map(s => s.trim()).filter(Boolean);
const W = +(process.env.W || 900), H = +(process.env.H || 1100);
const port = 9400 + Math.floor(Math.random() * 400);
const dir = `/tmp/claude-1000/liveshot-${port}`;
const chrome = spawn('google-chrome-stable', ['--headless=new', `--remote-debugging-port=${port}`, '--no-first-run', '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessForNavigations,PrivateNetworkAccessForWorkers,PrivateNetworkAccessRespectPreflightResults',
  `--user-data-dir=${dir}`, `--window-size=${W},${H}`, 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws;
for (let i = 0; i < 40 && !ws; i++) {
  await sleep(250);
  try { const t = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); ws = t.find(x => x.type === 'page').webSocketDebuggerUrl; } catch {}
}
const sock = new WebSocket(ws); await new Promise(r => sock.onopen = r);
let id = 0; const pending = {}; const logs = [];
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending[i] = r; sock.send(JSON.stringify({ id: i, method, params, sessionId })); });
const wrapper = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#171717;color:#ececec;font:15px system-ui,sans-serif}
.msg{max-width:780px;margin:24px auto;padding:0 16px}
.st{color:#9b9b9b;font-size:14px;margin:6px 0}.chip{display:inline-block;font-size:12px;padding:2px 10px;border-radius:99px;background:#2a2a2a;margin:8px 0}
iframe{width:100%;border:0;border-radius:16px;display:block}
</style></head><body>
${urls.map((u, i) => `<div class="msg"><b>slerm-turing.qwen3.6-35b-a3b-ud-q4kxl</b><div class="st">Thinking it through…</div>
<div class="my-2"><iframe id="f${i}" sandbox="allow-scripts allow-forms allow-popups allow-downloads" src="${u.replace(/"/g, '&quot;')}"></iframe></div>
<div class="chip">● Preview</div></div>`).join('')}
<script>addEventListener('message',e=>{document.querySelectorAll('iframe').forEach(f=>{if(e.source===f.contentWindow&&e.data&&e.data.type==='iframe:height')f.style.height=e.data.height+'px'})})</script>
</body></html>`;
sock.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending[m.id]) { pending[m.id](m.result ?? m); delete pending[m.id]; }
  if (m.method === 'Fetch.requestPaused') {
    send('Fetch.fulfillRequest', { requestId: m.params.requestId, responseCode: 200,
      responseHeaders: [{ name: 'Content-Type', value: 'text/html' }], body: Buffer.from(wrapper).toString('base64') });
  }
  if (m.method === 'Runtime.consoleAPICalled' && /warn|error/.test(m.params.type))
    logs.push(m.params.type + ': ' + m.params.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 300));
  if (m.method === 'Runtime.exceptionThrown') logs.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 400));
  if (m.method === 'Log.entryAdded') logs.push('log: ' + m.params.entry.level + ' ' + m.params.entry.text.slice(0, 300).replace(/\/w\/[^/]+\/[0-9a-f]{32}/g, '/w/<chat>/***'));
};
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
await send('Fetch.enable', { patterns: [{ urlPattern: 'http://cryoai:8080/__livetest*' }] });
await send('Page.navigate', { url: process.env.SOLO ? urls[0] : 'http://cryoai:8080/__livetest' });
for (const step of plan.split(';')) {
  const [k, v] = step.split(':');
  if (k === 'wait') await sleep(+v * 1000);
  if (k === 'shot') {
    const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(`${prefix}-${v}.png`, Buffer.from(r.data, 'base64'));
    console.log('shot', `${prefix}-${v}.png`);
  }
  if (k === 'click') {
    const [x, y] = v.split(',').map(Number);
    for (const type of ['mousePressed', 'mouseReleased'])
      await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
  }
  if (k === 'wheel') {
    const [x, y, dy] = v.split(',').map(Number);
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX: 0, deltaY: dy });
  }
}
console.log(logs.join('\n') || '(no console warnings/errors)');
chrome.kill(); await sleep(500); try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
process.exit(0);
