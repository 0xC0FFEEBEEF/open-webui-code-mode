/* Watch never goes blank (10-26, the user: follow, keep the last result, move with me, reload on its own) and the
 * Fixer view's buttons and Claude's hand-off reach the page. jsdom has no layout or animation: this checks what is
 * shown and which requests go out. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "873c87b1-9f9d-4127-9fed-0795922ddf3a";
const OTHER = "00086b8e-ead1-4a11-bda0-01bf85c2a543";
const SIG = "0".repeat(32);
const liveUrl = (chat, m) => "http://ai-sandbox:4099/w/" + chat + "/" + SIG + "/?s=ses_" + m + "&m=msg_" + m;
const FIXER = "http://ai-sandbox:4098";
const VIEW = FIXER + "/f/20261026-0449-e5a3/" + "f".repeat(32) + "/";
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let active = { ok: true, active: "" };
  const ops = [];
  const page = await load({
    url: "http://owui.test/c/" + CHAT,
    fetch: (call) => {
      if (call.path === "/api/chat/completions" && call.body && call.body.code_fixer) {
        ops.push(call.body.code_fixer);
        const o = call.body.code_fixer.op;
        if (o === "active") return active;
        if (o === "cancel" || o === "escalate") return { ok: true, status: o === "cancel" ? "cancelled" : "escalated" };
        return { ok: false, error: "x" };
      }
      if (call.path === "/api/chat/completions" && call.body && call.body.code_ask) return { ok: true, asks: [] };
      return undefined;
    },
  });
  const wt = () => page.$("#code-watch");
  const stage = () => page.$(".wt-live");
  const post = (source, data, origin) =>
    page.window.dispatchEvent(new page.window.MessageEvent("message", { data, source, origin: origin || "" }));
  function liveFrame(chat, m, live) {
    const f = page.document.createElement("iframe");
    f.src = liveUrl(chat, m);
    page.document.getElementById("messages").appendChild(f);
    f.getClientRects = () => [{}];
    post(f.contentWindow, { type: "iframe:height", height: 420, live: !!live });
    return f;
  }

  const first = liveFrame(CHAT, "a", true);

  await run("Watch opens on the running window", async () => {
    post(first.contentWindow, { type: "code:watch" });
    await page.waitFor(() => wt() && stage().getAttribute("src") === liveUrl(CHAT, "a") + "#fill=1", "the stage");
  });

  await run("the stage that stops talking is reloaded by itself", async () => {
    // a stage that speaks stays as it is
    post(stage().contentWindow, { type: "code:watch-state", live: true, since: Date.now() - 5000 });
    await page.sleep(300);
    const seen = [];
    const obs = new page.window.MutationObserver(() => seen.push(stage().getAttribute("src")));
    obs.observe(stage(), { attributes: true, attributeFilter: ["src"] });
    await page.waitFor(() => seen.includes("about:blank"), "the reload", 26000);
    await page.waitFor(() => stage().getAttribute("src") === liveUrl(CHAT, "a") + "#fill=1", "the same window back", 3000);
    obs.disconnect();
  });

  await run("another chat keeps Watch open, waiting for that chat's work, and follows it when it starts", async () => {
    page.window.history.pushState({}, "", "/c/" + OTHER);
    first.remove();
    await page.waitFor(() => /Nothing is running in this chat yet/.test(page.text(".wt-empty")), "the waiting note");
    assert(wt(), "Watch is still open");
    assert(stage().getAttribute("src") === "about:blank", "the old chat's window is gone from the stage");
    const next = liveFrame(OTHER, "b", true);
    await page.waitFor(() => stage().getAttribute("src") === liveUrl(OTHER, "b") + "#fill=1", "the new chat's window");
    assert(!page.$(".wt-empty"), "the note goes");
    assert(JSON.parse(page.window.sessionStorage.getItem("code-mode.watch-reopen")).chat === OTHER, "a reload brings Watch back here");
    next.setAttribute("data-keep", "1");
    noErrors(page);
  });

  await run("the Fixer view's Cancel reaches the pipe for that report only, from that frame only", async () => {
    active = { ok: true, active: VIEW, title: "The preview pane shows the Code mode window", since: Math.floor(Date.now() / 1000) - 30, phase: "fixer" };
    await page.waitFor(() => page.$("#code-wf .wf-frame"), "the Fixer on the stage", 12000);
    const frame = page.$("#code-wf .wf-frame");
    post(page.window, { type: "fixer:cancel", id: "20261026-0449-e5a3" }, FIXER);              // not a fixer frame
    post(frame.contentWindow, { type: "fixer:cancel", id: "20261026-9999-zzzz" }, FIXER);     // not its report
    post(frame.contentWindow, { type: "fixer:cancel", id: "20261026-0449-e5a3" }, "http://evil.test");
    await page.sleep(200);
    assert(!ops.some((o) => o.op === "cancel"), "none of those may cancel: " + JSON.stringify(ops));
    post(frame.contentWindow, { type: "fixer:cancel", id: "20261026-0449-e5a3" }, FIXER);
    await page.waitFor(() => ops.some((o) => o.op === "cancel"), "the cancel");
    const c = ops.find((o) => o.op === "cancel");
    assert(c.id === "20261026-0449-e5a3" && c.chat_id === OTHER, JSON.stringify(c));
  });

  await run("Cancel on the stage takes two clicks", async () => {
    const before = ops.filter((o) => o.op === "cancel").length;
    page.click("#code-wf .wf-cancel");
    assert(page.text("#code-wf .wf-cancel") === "Cancel it?", "armed first");
    await page.sleep(100);
    assert(ops.filter((o) => o.op === "cancel").length === before, "nothing sent on the first click");
    page.click("#code-wf .wf-cancel");
    await page.waitFor(() => ops.filter((o) => o.op === "cancel").length === before + 1, "sent on the second");
  });

  await run("when the fixer hands it to Claude the stage says so: CALLING CLAUDE, then CLAUDE · ON IT", async () => {
    const frame = page.$("#code-wf .wf-frame");
    post(frame.contentWindow, { type: "fixer:status", alive: true, done: false, phase: "claude", id: "20261026-0449-e5a3" }, FIXER);
    await page.waitFor(() => page.$("#code-wf").getAttribute("data-phase") === "claude", "the Claude phase");
    assert(/CALLING CLAUDE/.test(page.text(".wf-flash")), "the hand-off flash");
    assert(page.text(".wf-chip") === "CLAUDE · ON IT", page.text(".wf-chip"));
  });

  await run("its verdict stays on the stage while nothing else runs, and the stage follows the work when it picks up", async () => {
    const live = page.$$("iframe").find((f) => f.getAttribute("data-keep") === "1");
    post(live.contentWindow, { type: "iframe:height", height: 420, live: false });   // the builder is paused
    const frame = page.$("#code-wf .wf-frame");
    post(frame.contentWindow, { type: "fixer:status", done: true, verdict: "fixed", phase: "claude", id: "20261026-0449-e5a3" }, FIXER);
    await page.waitFor(() => page.$("#code-wf").getAttribute("data-state") === "done", "the verdict");
    assert(page.text(".wf-chip") === "CLAUDE · FIXED", page.text(".wf-chip"));
    active = { ok: true, active: "" };
    await page.waitFor(() => page.$("#code-wf") && page.$("#code-wf").getAttribute("data-rest") === "1", "the stamp moving aside", 9000);
    assert(page.$("#code-wf"), "still on the stage: nothing else is running");
    const resumed = liveFrame(OTHER, "c", true);
    await page.waitFor(() => !page.$("#code-wf"), "the fold once the builder runs again", 6000);
    await page.waitFor(() => stage().getAttribute("src") === liveUrl(OTHER, "c") + "#fill=1", "the stage on the new round");
    resumed.remove();
    noErrors(page);
  });

  finish();
})();
