/*
 * The live window's page, tested without a browser.
 *
 * live.html is loaded in jsdom with a fake EventSource, and test_page.py feeds it the events the server really
 * sends (the captured turn's snapshot, and a live run's tool + view events built by code-live.py itself).
 * Each "dump" step prints what the page is showing as compact JSON: the stage's file tabs, the Log's tool
 * entries, which code rows are marked as read, and what the collapsed / opened entry shows.
 *
 * What it catches: code that throws, a view that never lands on a tab, marks on the wrong lines, a Log entry
 * that repeats the file text the view already shows, a file named two ways opening two tabs.
 * What it cannot see: layout, position, colours (jsdom has no rendering). Those are checked on the real page.
 *
 *   node tests/page.cjs <actions.json>            # the actions test_page.py wrote
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// jsdom has no layout. {"layout": true} gives the stage's views one: 300 px tall while on screen, 18 px a line of
// content, scrollTop clamped to what fits and a scroll event after each change (as a browser does), and a view
// that leaves the screen loses its scroll position (display: none). Enough to test what follows the end.
function fakeLayout(window) {
  const LINE = 18, H = 300, P = window.HTMLElement.prototype;
  const isView = (e) => e.classList && e.classList.contains("view");
  const shown = (e) => e.classList.contains("on");
  const lines = (e) => (e.textContent.match(/\n/g) || []).length +
    e.querySelectorAll(".cmd, .res, .ln, .fh, .url, .gap, .say").length;
  const height = (e) => Math.max(H, lines(e) * LINE);
  Object.defineProperty(P, "clientHeight", { configurable: true, get() { return isView(this) && shown(this) ? H : 0; } });
  Object.defineProperty(P, "scrollHeight", { configurable: true, get() { return isView(this) && shown(this) ? height(this) : 0; } });
  Object.defineProperty(P, "scrollTop", {
    configurable: true,
    get() { return isView(this) && shown(this) ? Math.min(this._st || 0, height(this) - H) : 0; },
    set(v) {
      if (!isView(this) || !shown(this)) return;
      const n = Math.max(0, Math.min(Math.round(+v || 0), height(this) - H));
      if (n === (this._st || 0)) return;
      this._st = n;
      window.setTimeout(() => this.dispatchEvent(new window.Event("scroll")), 0);
    },
  });
  new window.MutationObserver((ms) => ms.forEach((m) => {
    if (isView(m.target) && !shown(m.target)) m.target._st = 0;
  })).observe(window.document, { subtree: true, attributes: true, attributeFilter: ["class"] });
}

function open(embedded, layout, hash) {
  const html = fs.readFileSync(path.join(ROOT, "live.html"), "utf8");
  const streams = [];      // every EventSource the page opens
  const posted = [];       // what the page told its parent (embedded: in a chat, as Open WebUI shows it)
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push("page error: " + e.message));
  vc.on("error", (m) => errors.push("console.error: " + m));
  vc.on("warn", (m) => errors.push("console.warn: " + m));
  const dom = new JSDOM(html, {
    url: "http://127.0.0.1:4117/w/chat-1/sig/?s=ses_page_test&m=msg_page_test&full=1" + (hash ? "#" + hash : ""),
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.EventSource = class {            // the page's stream: we are the stream
        constructor(url) { this.url = url; this.readyState = 0; streams.push(this); }
        close() { this.readyState = 2; }
      };
      window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      if (layout) fakeLayout(window);
      if (embedded) Object.defineProperty(window, "parent", { configurable: true, value: { postMessage: (d) => posted.push(d) } });
    },
  });
  if (!streams.length) errors.push("the page never opened its event stream");
  return { dom, streams, errors, posted };
}

function feed(app, ev) {
  const es = app.streams[app.streams.length - 1];
  if (!es || !es.onmessage) throw new Error("no event stream to feed " + ev.k);
  es.onmessage({ data: JSON.stringify(ev) });
}

// the marked line numbers, joined into ranges: how a view says "these lines were read"
function markedRuns(rows) {
  const out = [];
  let cur = null;
  rows.forEach((r) => {
    if (!r.rd) return;
    const n = +r.n;
    if (cur && n === cur[1] + 1) cur[1] = n;
    else { cur = [n, n]; out.push(cur); }
  });
  return out;
}

function rowsOf(box) {
  return Array.from(box.querySelectorAll(".ln")).map((r) => ({
    n: r.querySelector(".no").textContent,
    rd: r.classList.contains("rd"),
  }));
}

function dump(app) {
  const d = app.dom.window.document;
  const tabs = Array.from(d.querySelectorAll("#tabs .tab")).map((x) => ({
    text: x.textContent, title: x.title, on: x.classList.contains("on"), busy: x.classList.contains("busy"),
  }));
  const stage = Array.from(d.querySelectorAll("#views .vfile")).map((v) => {
    const rows = rowsOf(v.querySelector(".code"));
    return {
      fh: v.querySelector(".fh").textContent.trim(), on: v.classList.contains("on"),
      lines: rows.length, first: rows.length ? rows[0].n : "", last: rows.length ? rows[rows.length - 1].n : "",
      marked: markedRuns(rows), gaps: Array.from(v.querySelectorAll(".gap")).map((g) => g.textContent),
    };
  });
  const log = Array.from(d.querySelectorAll("#log .tool")).map((t) => {
    const rows = rowsOf(t.querySelector(".code"));
    return {
      name: t.querySelector(".tn").textContent, summary: t.querySelector(".ts").textContent,
      open: t.classList.contains("open"),
      hk: Array.from(t.querySelectorAll(".code .hk")).map((x) => x.textContent),
      lines: rows.length, marked: markedRuns(rows),
      gaps: Array.from(t.querySelectorAll(".code .gap")).map((g) => g.textContent),
      out: t.querySelector(".out").textContent, outHidden: t.querySelector(".out").classList.contains("more"),
    };
  });
  return {
    mode: app.dom.window.document.getElementById("win").className,
    now: app.dom.window.document.getElementById("now").textContent,
    model: d.getElementById("model").textContent, modelClass: d.getElementById("model").className,
    caption: { label: d.querySelector("#caption .cl").textContent, text: d.querySelector("#caption .ct").textContent,
               cls: d.getElementById("caption").className },
    tabs, stage, log,
    views: Array.from(d.querySelectorAll("#views .view")).map((v) => ({   // in tab order
      cls: v.className, on: v.classList.contains("on"), top: v.scrollTop, end: Math.max(0, v.scrollHeight - v.clientHeight),
      follow: v.follow, text: v.textContent.slice(-160),
    })),
    latest: d.getElementById("vjump") ? d.getElementById("vjump").classList.contains("on") : null,
    posted: app.posted.slice(),
  };
}

async function main() {
  let actions = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  // {"embedded": true} first: the page sits in a chat's frame; {"layout": true}: views get a size (fakeLayout)
  // {"hash": "fill=1"}: the window as the Code page's Watch stage
  const first = actions[0] && (actions[0].embedded || actions[0].layout || actions[0].hash) ? actions[0] : null;
  if (first) actions = actions.slice(1);
  const app = open(!!(first && first.embedded), !!(first && first.layout), first && first.hash);
  const out = {};
  await sleep(60);                                  // the page's own first frames
  for (const a of actions) {
    if (a.ev) feed(app, a.ev);
    else if (a.wait) await sleep(a.wait);            // the page's 1-second idle ticker runs meanwhile
    else if (a.dump) out[a.dump] = dump(app);
    else if (a.clickLog != null) {
      const t = app.dom.window.document.querySelectorAll("#log .tool")[a.clickLog];
      if (!t) throw new Error("no Log entry " + a.clickLog);
      t.querySelector(".tl").dispatchEvent(new app.dom.window.Event("click", { bubbles: true }));
    } else if (a.scrollView != null) {                // the person scrolls view n (tab order) to a spot or its end
      const v = app.dom.window.document.querySelectorAll("#views .view")[a.scrollView];
      if (!v) throw new Error("no view " + a.scrollView);
      v.scrollTop = a.to === "end" ? v.scrollHeight : a.to;
    } else if (a.click) {
      const b = app.dom.window.document.querySelector(a.click);
      if (!b) throw new Error("nothing matches " + a.click);
      b.dispatchEvent(new app.dom.window.MouseEvent("click", { bubbles: true }));
    } else throw new Error("unknown action " + JSON.stringify(a));
    await sleep(80);                                // let mark()/flush() run
  }
  out.errors = app.errors;
  process.stdout.write(JSON.stringify(out, null, 1) + "\n");
  app.dom.window.close();
}

main().catch((e) => { console.error("page test failed: " + (e && e.stack || e)); process.exit(1); });
