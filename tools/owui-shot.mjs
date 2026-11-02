// echo TOKEN | node owui-shot.mjs <url> <out.png> [js-to-eval-after-load] [waitSeconds]
// Headless Chrome on an Open WebUI page, signed in with the token read from stdin (kept in memory only: set into
// the page's localStorage before Open WebUI boots). Env W/H = viewport (default 1440x900), DARK=1 dark theme,
// STEPS=file.js: a list of [js, waitSeconds] evaluated in order after load (each result printed).
// LOADER=file serves that file as /static/loader.js (try a page script without deploying it); PRE=js runs before
// the page's own scripts; MOCK=file.json: [{"prefix", "status", "type", "body", "headers"}] answers matching requests
// ({"prefix", "fail": true} refuses the connection instead: a server that is down).
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const [url, out, js = '', wait = '8'] = process.argv.slice(2);
const token = fs.readFileSync(0, 'utf8').trim();
const W = +(process.env.W || 1440), H = +(process.env.H || 900);
const port = 9333 + Math.floor(Math.random() * 500);
const dir = `/tmp/claude-1000/owui-shot-${port}`;
const chrome = spawn('google-chrome-stable', ['--headless=new', `--remote-debugging-port=${port}`, '--no-first-run',
  `--user-data-dir=${dir}`, `--window-size=${W},${H}`, '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
  'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws;
for (let i = 0; i < 40 && !ws; i++) {
  await sleep(250);
  try { const t = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        ws = t.find(x => x.type === 'page').webSocketDebuggerUrl; } catch {}
}
const sock = new WebSocket(ws); await new Promise(r => sock.onopen = r);
let id = 0; const pending = {}; const logs = []; let fetchHandler = null;
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending[i] = r; sock.send(JSON.stringify({ id: i, method, params })); });
sock.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending[m.id]) { pending[m.id](m.result ?? m); delete pending[m.id]; }
  if (m.method === 'Fetch.requestPaused' && fetchHandler) fetchHandler(m);
  if (m.method === 'Runtime.consoleAPICalled' && /warn|error/.test(m.params.type))
    logs.push(m.params.type + ': ' + m.params.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 300));
  if (m.method === 'Runtime.exceptionThrown') logs.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 400));
};
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send('Page.enable'); await send('Runtime.enable');
const mocks = process.env.MOCK ? JSON.parse(fs.readFileSync(process.env.MOCK, 'utf8')) : [];
if (process.env.LOADER) mocks.push({ prefix: '/static/loader.js', status: 200, type: 'application/javascript',
                                     body: fs.readFileSync(process.env.LOADER, 'utf8') });
if (mocks.length) await send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
fetchHandler = m => {
  const u = new URL(m.params.request.url);
  const hit = mocks.find(x => (u.pathname + u.search + u.hash).startsWith(x.prefix) || m.params.request.url.startsWith(x.prefix));
  if (!hit) return send('Fetch.continueRequest', { requestId: m.params.requestId });
  if (hit.fail) return send('Fetch.failRequest', { requestId: m.params.requestId, errorReason: 'ConnectionRefused' });
  send('Fetch.fulfillRequest', { requestId: m.params.requestId, responseCode: hit.status || 200,
    responseHeaders: [{ name: 'Content-Type', value: hit.type || 'application/json' }, { name: 'Cache-Control', value: 'no-store' },
      ...Object.entries(hit.headers || {}).map(([name, value]) => ({ name, value }))],   // e.g. CORS for another port
    body: Buffer.from(typeof hit.body === 'string' ? hit.body : JSON.stringify(hit.body)).toString('base64') });
};
if (process.env.PRE) await send('Page.addScriptToEvaluateOnNewDocument', { source: process.env.PRE });
const theme = process.env.DARK ? 'dark' : 'light';
await send('Page.addScriptToEvaluateOnNewDocument', { source:
  `try{localStorage.setItem('token',${JSON.stringify(token)});localStorage.setItem('theme','${theme}');}catch(e){}` });
await send('Page.navigate', { url });
await sleep(+wait * 1000);
const evalJs = async e => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true });
  return r.result?.value ?? r.exceptionDetails?.exception?.description ?? r; };
if (js) console.log(JSON.stringify(await evalJs(js), null, 1));
// A step [js, wait, "url part"] runs inside the (possibly cross-origin) frame whose URL contains that part.
async function frameEval(part, e) {
  const tree = (await send('Page.getFrameTree')).frameTree;
  const all = []; (function walk(n) { all.push(n.frame); (n.childFrames || []).forEach(walk); })(tree);
  const fr = all.find(f => f.url.includes(part));
  if (!fr) return 'no frame matching ' + part;
  const w = await send('Page.createIsolatedWorld', { frameId: fr.id, worldName: 'shot' });
  const r = await send('Runtime.evaluate', { expression: e, contextId: w.executionContextId, awaitPromise: true, returnByValue: true });
  return r.result?.value ?? r.exceptionDetails?.exception?.description ?? r;
}
if (process.env.STEPS) for (const [s, w, part] of JSON.parse(fs.readFileSync(process.env.STEPS, 'utf8'))) {
  const v = part ? await frameEval(part, s) : await evalJs(s);
  if (v !== undefined) console.log(JSON.stringify(v, null, 1)); await sleep(w * 1000); }
const shot = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
for (const l of logs.slice(0, 30)) console.error(l);
chrome.kill(); await sleep(300); fs.rmSync(dir, { recursive: true, force: true }); process.exit(0);
