/*
 * Page-script tests without Open WebUI.
 *
 * load() puts owui/code-toggle.js into a fake Open WebUI page (jsdom: a model picker in a <nav>, a message bar with
 * #input-menu-button, a chat box) and hands back helpers to click, type and look. With {bridge: true} the page's
 * GitHub calls (POST /api/chat/completions with code_github) go to the REAL pipe (pipe_bridge.py), which talks to a
 * fake GitHub (tests/fake_github.py): one test covers page script + pipe together.
 *
 * What it catches: code that throws, controls that never appear, wrong requests, page and pipe disagreeing.
 * What it cannot see: layout, position, colours (jsdom has no rendering). Those are checked on the real page.
 *
 * Anything in page.errors fails a test: uncaught errors, console.warn/error (the script's warnOnce), alert() or
 * confirm(), the GitHub token reaching the page, a GitHub call that carries chat_id / id / session_id.
 */
const fs = require("fs");
const path = require("path");
const cp = require("child_process");
const readline = require("readline");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..", "..");
const PAGE = `<!doctype html><html class="dark"><head></head><body>
<nav><button id="model-selector-0-button">qwen3.8-27b</button></nav>
<div id="chat-container">
  <div id="messages"></div>
  <div id="message-input-container">
    <div id="chat-input" contenteditable="true"></div>
    <div class="flex justify-between">
      <div class="left-group"><button id="input-menu-button" type="button">+</button></div>
      <div class="right-group"><button id="send-message-button" type="button">send</button></div>
    </div>
  </div>
</div>
</body></html>`;

const open = { pages: [], bridges: [] };
let failed = 0;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** Start pipe_bridge.py (the real pipe + a fake GitHub). */
async function startBridge() {
  const py = process.env.CODE_MODE_PY || "python3";
  const child = cp.spawn(py, [path.join(__dirname, "pipe_bridge.py")], { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (d) => { stderr += d; });
  const first = await new Promise((resolve, reject) => {
    const rl = readline.createInterface({ input: child.stdout });
    rl.once("line", resolve);
    child.once("exit", (code) => reject(new Error("pipe_bridge.py exited (" + code + "): " + stderr.slice(-800))));
  });
  const info = JSON.parse(first);
  const base = "http://127.0.0.1:" + info.port;
  const bridge = {
    base,
    token: info.token,
    stderr: () => stderr,
    /** Set fake-GitHub attributes and/or the caller's role: control({github: {pending: 2}, role: "user"}). */
    control: async (obj) => (await fetch(base + "/_test", { method: "POST", body: JSON.stringify(obj) })).json(),
    /** {repos, github_requests, state_file, state_mode} */
    info: async () => (await fetch(base + "/_test")).json(),
    stop: () => child.kill(),
  };
  open.bridges.push(bridge);
  return bridge;
}

/**
 * Load the page script into a fake Open WebUI page.
 *   opts.url     page address (default http://owui.test/, a new chat; use /c/<id> for an existing chat)
 *   opts.bridge  true = start a bridge, or pass one from an earlier load() to share its state
 *   opts.fetch   async (call) => answer | undefined : answer other requests yourself ({status, json} or any value)
 *   opts.script  path of the page script (default owui/code-toggle.js)
 */
async function load(opts = {}) {
  const errors = [];
  const calls = [];
  const vc = new VirtualConsole();
  const show = (x) => (x && (x.stack || x.message)) || String(x);
  vc.on("jsdomError", (e) => errors.push("uncaught: " + show(e.detail || e)));
  vc.on("error", (...a) => errors.push("console.error: " + a.map(show).join(" ")));
  vc.on("warn", (...a) => errors.push("console.warn: " + a.map(show).join(" ")));
  const dom = new JSDOM(PAGE, {
    url: opts.url || "http://owui.test/", runScripts: "outside-only", pretendToBeVisual: true, virtualConsole: vc,
  });
  const w = dom.window;
  const bridge = opts.bridge === true ? await startBridge() : opts.bridge || null;
  const clipboard = [];
  const opened = [];
  w.localStorage.setItem("token", "test-token");
  w.alert = (m) => { errors.push("alert() used: " + m); };
  w.confirm = (m) => { errors.push("confirm() used: " + m); return false; };
  w.open = (u) => { opened.push(String(u)); return null; };
  Object.defineProperty(w.navigator, "clipboard", {
    configurable: true, value: { writeText: async (t) => { clipboard.push(String(t)); } },
  });
  w.scrollTo = () => {};

  const answer = (status, data) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => data, text: async () => (typeof data === "string" ? data : JSON.stringify(data)),
    headers: { get: () => null },
  });
  w.fetch = async (url, init = {}) => {
    const u = new URL(String(url), w.location.href);
    let body = init.body;
    try { body = init.body ? JSON.parse(init.body) : null; } catch (e) { /* not JSON */ }
    const headers = {};
    for (const [k, v] of Object.entries(init.headers || {})) headers[k.toLowerCase()] = v;
    const call = { path: u.pathname, origin: u.origin, search: u.search, method: (init.method || "GET").toUpperCase(), body, headers };
    calls.push(call);
    if (opts.fetch) {
      const mine = await opts.fetch(call);
      if (mine !== undefined) return mine && mine.json ? mine : answer((mine && mine.status) || 200, mine);
    }
    if (call.path === "/api/chat/completions" && body && body.code_github && bridge) {
      for (const k of ["chat_id", "id", "session_id"]) {
        if (k in body) errors.push("a GitHub call carried " + k + " (Open WebUI would run it as a chat turn)");
      }
      if (headers.authorization !== "Bearer test-token") errors.push("a GitHub call went without the Open WebUI token");
      const r = await fetch(bridge.base + "/api/chat/completions", { method: "POST", body: JSON.stringify(body) });
      const text = await r.text();
      if (text.includes(bridge.token)) errors.push("the GitHub token reached the page (op " + JSON.stringify(body.code_github) + ")");
      call.answer = JSON.parse(text);
      return answer(200, call.answer);
    }
    if (call.path === "/api/models") return answer(200, { data: [] });
    if (call.path.startsWith("/api/compose")) return new Promise(() => {}); // a long poll with nothing queued
    if (call.path === "/api/chats") return answer(200, { chats: [] });
    return answer(404, {});
  };

  w.eval(fs.readFileSync(opts.script || path.join(ROOT, "owui", "code-toggle.js"), "utf8"));

  const doc = w.document;
  const el = (x) => (typeof x === "string" ? doc.querySelector(x) : x);
  const page = {
    window: w, document: doc, calls, errors, clipboard, opened, bridge,
    $: (sel, root) => (root || doc).querySelector(sel),
    $$: (sel, root) => Array.from((root || doc).querySelectorAll(sel)),
    /** Text of an element with whitespace collapsed ("" when it is missing). */
    text: (x) => ((el(x) && el(x).textContent) || "").replace(/\s+/g, " ").trim(),
    /** In the page and neither it nor a parent is display:none / visibility:hidden / hidden. */
    visible: (x) => {
      let n = el(x);
      if (!n || !n.isConnected) return false;
      for (; n && n.nodeType === 1; n = n.parentElement) {
        const cs = w.getComputedStyle(n);
        if (n.hidden || cs.display === "none" || cs.visibility === "hidden") return false;
      }
      return true;
    },
    click: (x) => {
      const n = el(x);
      if (!n) throw new Error("click: nothing matches " + x);
      n.dispatchEvent(new w.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      n.dispatchEvent(new w.MouseEvent("mouseup", { bubbles: true, cancelable: true }));
      n.click();
    },
    /** Put text in an input the way typing does (focus, value, input + change events). */
    type: (x, value) => {
      const n = el(x);
      if (!n) throw new Error("type: nothing matches " + x);
      n.focus();
      n.value = value;
      n.dispatchEvent(new w.Event("input", { bubbles: true }));
      n.dispatchEvent(new w.Event("change", { bubbles: true }));
    },
    press: (x, key) => {
      const n = el(x) || doc.body;
      n.dispatchEvent(new w.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
      n.dispatchEvent(new w.KeyboardEvent("keyup", { key, bubbles: true, cancelable: true }));
    },
    sleep,
    /** Wait until fn() is truthy and return it; throws "timed out waiting for <what>" after ms. */
    waitFor: async (fn, what, ms = 4000) => {
      const end = Date.now() + ms;
      for (;;) {
        let v;
        try { v = await fn(); } catch (e) { v = null; }
        if (v) return v;
        if (Date.now() > end) {
          throw new Error("timed out waiting for " + (what || fn) + (errors.length ? "\n  page errors: " + errors.join("\n  ") : ""));
        }
        await sleep(15);
      }
    },
    /** The GitHub ops the page sent, in order: [{op, ...}, ...] */
    githubOps: () => calls.filter((c) => c.body && c.body.code_github).map((c) => c.body.code_github),
    close: () => { try { w.close(); } catch (e) { /* already closed */ } },
  };
  open.pages.push(page);
  return page;
}

/** Run one named check; a failure is printed and makes the file exit 1. The checks of a file build on each other
 *  (one page, clicked through in order), so after a failure the rest are skipped: fix the first FAIL first. */
async function run(name, fn) {
  if (failed) {
    console.log("SKIP " + name + " (an earlier check failed)");
    return;
  }
  try {
    await fn();
    console.log("PASS " + name);
  } catch (e) {
    failed += 1;
    const lines = String((e && e.stack) || e).split("\n");
    const where = lines.filter((l) => /test_[^/]*\.cjs/.test(l)).slice(0, 1);
    console.log("FAIL " + name + "\n  " + lines.filter((l) => !/^\s+at /.test(l)).concat(where).join("\n  "));
  }
}

function assert(cond, message) {
  if (!cond) throw new Error(message || "assertion failed");
}

/** Call last: closes pages and bridges, exits non-zero when a check failed. */
function finish() {
  open.pages.forEach((p) => p.close());
  open.bridges.forEach((b) => b.stop());
  process.exit(failed ? 1 : 0);
}

module.exports = { load, startBridge, run, assert, finish, sleep };
