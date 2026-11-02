/* The task strip above the running live window (10-24, the user: "a little summary as to what it is currently doing
 * for each task ... when it moves to the next task it would update that", "somewhere above or below the opencode
 * window"). It shows the agent's own "## Now" lines (code-features: d.current), or the first open box without them,
 * and only while the window says it shows the running turn. Text from the checklist is never markup. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "00086b8e-0000-4000-8000-0000000000aa";
const DEPLOY = "http://owui.test:8095";
const items = [
  { id: "A.1", label: "owl builds", detail: "", sha: "", done: true, section: "Acceptance" },
  { id: "B4", label: "ChatGPT auth chain", detail: "", sha: "", done: false, section: "Plan" },
  { id: "B5", label: "ChatGPT-only crates", detail: "", sha: "", done: false, section: "Plan" },
];
let data = {
  exists: true, kind: "project", feature: null, title: "Owl", updated: 1000, server_time: 1010,
  done: 1, total: 3, now: 1, open: [], items,
  current: { task: "B4 ChatGPT auth chain", doing: "tracing who still reads `chatgpt_base_url`" },
};
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

function liveFrame(page) {
  const f = page.document.createElement("iframe");
  f.src = "http://ai-sandbox:4099/w/" + CHAT + "/" + "0".repeat(32) + "/?s=ses_abc&m=msg_now";
  const box = page.document.createElement("div");
  box.appendChild(f);
  page.document.getElementById("messages").appendChild(box);
  f.getClientRects = () => [{}];
  return f;
}
function report(page, frame, live) {
  page.window.dispatchEvent(new page.window.MessageEvent("message", {
    data: { type: "iframe:height", height: 420, live }, source: frame.contentWindow,
  }));
}

(async () => {
  let page, frame;
  const strip = () => page.$(".code-task-strip");

  await run("while a turn runs, a strip above its live window says which task the agent is on and what it is doing", async () => {
    page = await load({
      url: "http://owui.test/c/" + CHAT,
      fetch: (call) => {
        if (call.origin === DEPLOY && call.path === "/api/checklist/" + CHAT) return data;
        if (call.path === "/api/tasks/chat/" + CHAT) return { task_ids: ["t1"] };
        return undefined;
      },
    });
    await page.waitFor(() => page.visible(page.$("#code-ck-rail")), "the checklist to load");
    assert(!strip(), "no strip before a live window says it is running");
    frame = liveFrame(page);
    report(page, frame, true);
    await page.waitFor(() => strip(), "the strip");
    assert(strip().nextElementSibling === frame, "the strip sits right above the running window");
    assert(page.text(page.$(".cts-l", strip())) === "Task" && page.text(page.$(".cts-n", strip())) === "1/3",
      "label and count: " + page.text(strip()));
    assert(page.text(page.$(".cts-t", strip())) === "B4 ChatGPT auth chain", "the task: " + page.text(strip()));
    const doing = page.$(".cts-d", strip());
    assert(doing && page.text(doing) === "tracing who still reads chatgpt_base_url" && page.$("code", doing),
      "what it is doing, with its code span: " + (doing && doing.innerHTML));
    noErrors(page);
  });

  await run("when the agent moves to the next task the strip follows (and flashes once)", async () => {
    data = Object.assign({}, data, { done: 2, now: 2, items: [items[0], Object.assign({}, items[1], { done: true }), items[2]],
      current: { task: "B5 ChatGPT-only crates", doing: "deleting the chatgpt/ crate and owl apply" } });
    await page.waitFor(() => page.text(page.$(".cts-t", strip())) === "B5 ChatGPT-only crates", "the next task", 8000);
    assert(page.text(page.$(".cts-n", strip())) === "2/3", "the count moved on: " + page.text(strip()));
    assert(strip().classList.contains("cts-new"), "a new task flashes");
    noErrors(page);
  });

  await run("without Now lines it shows the first open box, labelled as that", async () => {
    data = Object.assign({}, data, { current: null });
    await page.waitFor(() => page.text(page.$(".cts-l", strip())) === "First open item", "the fallback", 8000);
    assert(page.text(page.$(".cts-t", strip())) === "B5 ChatGPT-only crates", "the first open box: " + page.text(strip()));
    assert(!page.$(".cts-d", strip()), "no Doing line it does not have");
    noErrors(page);
  });

  await run("checklist text stays text", async () => {
    data = Object.assign({}, data, { current: { task: "<img src=x onerror=alert(1)>", doing: "<b>bold</b> **really**" } });
    await page.waitFor(() => page.text(page.$(".cts-t", strip())) === "<img src=x onerror=alert(1)>", "the odd task", 8000);
    assert(!page.$("img", strip()) && !page.$(".cts-d b", strip()).textContent.includes("<"), "markup was drawn: " + strip().innerHTML);
    assert(page.text(page.$(".cts-d b", strip())) === "really", "**bold** is kept the checklist's way");
    noErrors(page);
  });

  await run("clicking it opens the checklist; when the turn ends the strip goes", async () => {
    page.click(strip());
    await page.waitFor(() => page.$("#code-preview-panel") && page.$("#code-preview-panel").classList.contains("open"), "the panel");
    const now = page.$("#code-preview-panel .ck-now");
    assert(now && page.text(now).includes("<img src=x onerror=alert(1)>"), "the panel's Doing now is the agent's own task: " + (now && page.text(now)));
    report(page, frame, false);
    await page.waitFor(() => !strip(), "the strip to go when the window stops being live");
    noErrors(page);
  });
  finish();
})();
