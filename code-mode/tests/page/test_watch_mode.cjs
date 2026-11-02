/* Watch mode (10-25, the user: a "show" button - no message box, the agent's work centred, the preview or the checklist
 * beside it, and the slerm panel's AI tab folded in; the preview "doesn't really change", so it shows only a few
 * minutes at a time and the turn finishes on it). jsdom has no layout: these check what is shown and when. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "00086b8e-0000-4000-8000-0000000000aa";
const DEPLOY = "http://owui.test:8095";
const SIG = "0".repeat(32);
const LIVE = "http://ai-sandbox:4099/w/" + CHAT + "/" + SIG + "/?s=ses_abc&m=msg_now";
const PREVIEW = "http://ai-sandbox:4097/p/" + CHAT + "/" + "ab".repeat(16) + "/";
const KEY = "code-mode.watch-preview-min";
const items = [
  { id: "B5", label: "ChatGPT-only crates", detail: "", sha: "", done: true, section: "Plan" },
  { id: "B6", label: "docs sweep", detail: "", sha: "", done: false, section: "Plan" },
  { id: "B4", label: "ChatGPT auth chain", detail: "", sha: "", done: false, section: "Plan" },
];
const CK = { exists: true, kind: "project", feature: null, title: "Codex CLI fork", updated: 1000, server_time: 1010,
  done: 1, total: 3, now: 1, open: [], items, current: { task: "B6 docs/CI sweep", doing: "cluster job 4712 re-running the `core` tests" } };
const ENGINE = { model: "STRATA-qwen3.8-flash-next-iq3xxs", node: "slerm-node04", engine: "strata", state: "generating", phase: "thinking",
  tok_s: 23.4, tok_s_mean: 25.5, generated: 670, prompt_tokens: 210269, prompt_read: null, prompt_total: null, pps: 50.7, max_context: 262144,
  gpus: [{ name: "Tesla P100-PCIE-16GB", util: 81, mem_used: 15308, mem_total: 16384, temp: 60, power: 106.35, pcie_rx: 783.7 },
         { name: "Tesla P100-PCIE-16GB", util: 64, mem_used: 15654, mem_total: 16384, temp: 54, power: 49.7, pcie_rx: 16 }],
  host: { mem_used: 38328, mem_total: 44913, cpu: 37.6, cpus: 36, disk_read: 2.14 },
  requests: [31.9, 34.5, 34.9, 30.7, 30.3, 38.0, 48.2, 32.9, 34.4, 30.7, 32.7, 30.1].map((v, i) => ({
    prompt: 209984 - i * 500, reused: 209955 - i * 500, read: i === 2 ? 4821 : 29, out: 257, tok_s: v, secs: 8.7, t: 1000 - i })) };
const TOOLS = { tools: [["shell", 3425], ["edit", 857], ["read", 319], ["write", 93], ["grep", 13], ["skill", 2], ["subagent", 2], ["execute", 2], ["websearch", 1]],
  calls: 4714, steps: 4418, sendbacks: 18,
  mcp: [{ name: "context7", status: "connected", calls: 0 }, { name: "deepwiki", status: "connected", calls: 0 },
        { name: "exa", status: "connected", calls: 0 }, { name: "grep", status: "connected", calls: 0 }],
  kinds: [["not built", 15], ["left behind", 8], ["removed", 5], ["not true", 3], ["given up", 2], ["invented", 1]] };
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

function liveFrame(page) {
  const f = page.document.createElement("iframe");
  f.src = LIVE;
  const box = page.document.createElement("div");
  box.appendChild(f);
  page.document.getElementById("messages").appendChild(box);
  f.getClientRects = () => [{}];
  return f;
}
function post(page, source, data) {
  page.window.dispatchEvent(new page.window.MessageEvent("message", { data, source }));
}
function report(page, frame, live) { post(page, frame.contentWindow, { type: "iframe:height", height: 420, live }); }

(async () => {
  let page, frame, version = "v1", engineCalls = 0, toolsCalls = 0;
  const wt = () => page.$("#code-watch");
  const tab = () => wt() && wt().getAttribute("data-tab");

  await run("the live window's eye button opens Watch: the turn as the stage, its task above, the checklist beside it", async () => {
    page = await load({
      url: "http://owui.test/c/" + CHAT,
      fetch: (call) => {
        if (call.origin === DEPLOY && call.path === "/api/checklist/" + CHAT) return CK;
        if (call.path === "/api/tasks/chat/" + CHAT) return { task_ids: ["t1"] };
        if (call.path.endsWith("/__version")) return { version, files: 3, entry: "index.html" };
        if (call.path === "/w/" + CHAT + "/" + SIG + "/engine") { engineCalls++; return { engine: ENGINE, now: Date.now() }; }
        if (call.path === "/w/" + CHAT + "/" + SIG + "/tools") { toolsCalls++; return TOOLS; }
        return undefined;
      },
    });
    page.window.localStorage.setItem(KEY, "0.03");   // 1.8 s: the preview's few minutes, shortened for the test
    frame = liveFrame(page);
    report(page, frame, true);
    await page.waitFor(() => page.visible(page.$("#code-ck-rail")), "the checklist to load");
    post(page, page.window, { type: "code:watch" });
    await page.sleep(50);
    assert(!wt(), "only a live window of the page may open it");
    post(page, frame.contentWindow, { type: "code:watch" });
    await page.waitFor(() => wt(), "Watch");
    const stage = page.$(".wt-live", wt());
    assert(stage.getAttribute("src") === LIVE + "#fill=1", "the stage is the turn's live window, full height: " + stage.getAttribute("src"));
    assert(page.$$('iframe[src*=":4099/w/"]:not(.wt-live)').length === 1, "the stage's copy is not one of the chat's windows");
    await page.waitFor(() => tab() === "checklist", "the checklist beside it");
    assert(page.text(page.$(".wt-tt")) === "B6 docs/CI sweep" && page.text(page.$(".wt-td")) === "cluster job 4712 re-running the core tests",
      "the task line: " + page.text(page.$(".wt-task")));
    assert(page.text(page.$(".wt-ckc")) === "1/3", "the count");
    assert(page.$(".wt-ck .ck-row") || page.$(".wt-ck .ck-now"), "the checklist drawn in the panel");
    assert(page.text(page.$(".wt-chip")) === "WORKING", "working: " + page.text(page.$(".wt-chip")));
    await page.waitFor(() => page.text(page.$(".wt-sstate")) === "GENERATING", "the engine strip", 5000);
    assert(page.text(page.$(".wt-etext")) === "thinking · 23.4 tok/s", "strip: " + page.text(page.$(".wt-etext")));
    assert(page.$$(".wt-mgpu").length === 2, "both GPUs in the strip");
    noErrors(page);
  });

  await run("Engine: the slerm panel's AI tab, folded in", async () => {
    page.click('.wt-seg button[data-tab="engine"]');
    await page.waitFor(() => tab() === "engine", "the engine tab");
    assert(page.text(page.$(".wt-etok")) === "23.4" && /^tok\/s · thinking · mean 25\.5$/.test(page.text(page.$(".wt-ephase"))), "speed: " + page.text(page.$(".wt-big")));
    assert(page.text(page.$(".wt-ectx")) === "210,269 / 262,144", "context: " + page.text(page.$(".wt-ectx")));
    assert(page.$$(".wt-dec i").length === 12 && page.$$(".wt-reads i.big").length === 1, "decode bars and one big read");
    assert(page.$$(".wt-gpus .wt-gpu").length === 2 && page.text(page.$(".wt-gpus .wt-gm")) === "14.9/16 GB", "GPUs: " + page.text(page.$(".wt-gpus")));
    assert(/^Node: RAM 37\.4 \/ 43\.9 GB · CPU 38% of 36 · disk 2\.1 MB\/s$/.test(page.text(page.$(".wt-node"))), "node: " + page.text(page.$(".wt-node")));
    assert(page.$$(".wt-reqs span").length === 30, "five requests under a header");
    assert(page.$(".wt-plink").getAttribute("href") === "http://slerm-head:8080/", "a link to the panel");
    noErrors(page);
  });

  await run("Tools: the most used tools this turn and over the chat, the MCP servers and the review send-backs", async () => {
    post(page, page.$(".wt-live").contentWindow, { type: "code:watch-state", live: true, steps: 3, tools: { shell: 5, edit: 2, read: 1 } });
    page.click('.wt-seg button[data-tab="tools"]');
    await page.waitFor(() => tab() === "tools" && page.$$(".wt-allbars .wt-tbar").length, "the tools tab with the chat's counts");
    assert(toolsCalls === 1, "code-live asked once: " + toolsCalls);
    assert(page.text(page.$(".wt-tturn")) === "8 calls · 3 steps", "this turn: " + page.text(page.$(".wt-tturn")));
    const words = (r) => Array.from(r.children).map((c) => page.text(c)).filter(Boolean).join(" ");
    const turn = page.$$(".wt-turnbars .wt-tbar").map(words);
    assert(turn[0] === "shell 5 63%" && turn.length === 3, "turn bars: " + turn.join(" | "));
    assert(page.text(page.$(".wt-tall")) === "4,714 calls · 4,418 steps", "whole chat: " + page.text(page.$(".wt-tall")));
    const all = page.$$(".wt-allbars .wt-tbar").map(words);
    assert(all.length === 8 && all[0] === "shell 3,425 73%", "chat bars: " + all.join(" | "));
    assert(/^1 more tools · 1 calls$/.test(page.text(page.$(".wt-allbars .wt-small"))), "the rest: " + page.text(page.$(".wt-allbars .wt-small")));
    const mcps = page.$$(".wt-mcp").map(words);
    assert(mcps.length === 4 && mcps[0] === "context7 connected never used", "MCP rows: " + mcps.join(" | "));
    assert(page.text(page.$(".wt-tsb")) === "18 times", "send-backs: " + page.text(page.$(".wt-tsb")));
    const kinds = page.$$(".wt-kind").map((k) => k.firstChild.textContent + " " + page.text(k.lastChild));
    assert(kinds[0] === "not built 15" && kinds.length === 6, "kinds: " + kinds.join(" | "));
    noErrors(page);
  });

  await run("the preview holds the panel only its few minutes after it changes, then the checklist takes it back", async () => {
    page.click('.wt-seg button[data-tab="checklist"]');
    const a = page.document.createElement("a");
    a.href = PREVIEW;
    a.textContent = "Preview";
    page.$("#messages").appendChild(a);
    await page.sleep(3000);   // the first version poll
    version = "v2";           // the agent saved a page
    await page.waitFor(() => tab() === "preview", "the preview, after the change", 6000);
    assert(page.$(".wt-pframe").getAttribute("src") === PREVIEW, "the project's preview loaded");
    assert(!page.$("#code-preview-panel") || !page.$("#code-preview-panel").classList.contains("open"), "the side panel stays shut under Watch");
    assert(/checklist comes back in/.test(page.text(page.$(".wt-pnote"))), "it says when it goes: " + page.text(page.$(".wt-pnote")));
    await page.waitFor(() => tab() === "checklist", "the checklist back after its minutes", 5000);
    noErrors(page);
  });

  await run("a picked tab stays; when the turn is over the panel finishes on the preview", async () => {
    page.click('.wt-seg button[data-tab="engine"]');
    await page.sleep(1500);
    assert(tab() === "engine", "picked by hand: it stays");
    report(page, frame, false);   // the turn ends
    await page.waitFor(() => tab() === "preview", "the preview at the end", 12000);
    assert(page.text(page.$(".wt-chip")) === "DONE", "done: " + page.text(page.$(".wt-chip")));
    assert(page.text(page.$(".wt-pnote")) === "The turn is over: this is what it built.", "note: " + page.text(page.$(".wt-pnote")));
    await page.sleep(2500);
    assert(tab() === "preview", "and it stays there");
    noErrors(page);
  });

  await run("an overlay docks over the stage; Esc closes the overlay first, then Watch; the Watch button sits in the pill", async () => {
    report(page, frame, true);
    post(page, frame.contentWindow, { type: "code:compaction", state: { ph: "read", since: Date.now(), text: "",
      prog: { state: "reading", read: 1000, total: 9000 } } });
    await page.waitFor(() => page.$("#code-cmp"), "the compaction overlay");
    assert(page.$("#code-cmp").parentElement === page.$(".wt-stage"), "over the stage only");
    page.press(null, "Escape");
    await page.waitFor(() => page.$("#code-cmp").classList.contains("is-hidden"), "Esc hides the overlay");
    assert(wt(), "Watch is still open");
    assert(page.$("#code-cmp-pill") && page.$("#code-cmp-pill").parentElement === page.$(".wt-stage"), "its pill on the stage");
    page.press(null, "Escape");
    await page.waitFor(() => !wt(), "Esc again leaves Watch");
    assert(page.$("#code-cmp").parentElement === page.document.body, "the overlay back on the page");
    assert(page.$("#code-cmp-pill") && page.$("#code-cmp-pill").parentElement.id === "message-input-container", "its pill above the message box again");
    page.click(".ct-code");
    await page.waitFor(() => page.$("#code-toggle-pill") && page.$("#code-toggle-pill").getAttribute("data-watch") === "1", "the Watch segment in Code mode");
    page.click(".ct-watch");
    await page.waitFor(() => wt(), "Watch from the pill");
    page.click(".wt-exit");
    await page.waitFor(() => !wt(), "Exit");
    assert(engineCalls > 0, "the engine was asked through code-live");
    noErrors(page);
  });
  finish();
})();
