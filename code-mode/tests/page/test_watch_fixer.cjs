/* Watch: the Fixer takes the stage (pipe 0.28.0; the user: make it catch the eye, "holy crap something happened and
 * it's investigating itself"). While a report runs for the chat, the stage shows the fixer's viewer behind hazard tape
 * with an alarm the first time; Esc / Hide / Keep minimized fold it into a pill on the stage; the verdict is stamped
 * on it and the stage goes back to the builder. jsdom has no layout or animation: this checks what is there and when. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "873c87b1-9f9d-4127-9fed-0795922ddf3a";
const SIG = "0".repeat(32);
const LIVE = "http://ai-sandbox:4099/w/" + CHAT + "/" + SIG + "/?s=ses_abc&m=msg_now";
const VIEW = "http://ai-sandbox:4098/f/20261026-0332-a998/" + "e".repeat(32) + "/";
const FIXER = "http://ai-sandbox:4098";
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let active = { ok: true, active: "" };
  const ops = [];
  const page = await load({
    url: "http://owui.test/c/" + CHAT,
    fetch: (call) => {
      if (call.path === "/api/chat/completions" && call.body && call.body.code_fixer) {
        ops.push(call.body.code_fixer);
        return call.body.code_fixer.op === "active" ? active : { ok: false, error: "not in this test" };
      }
      if (call.path === "/api/chat/completions" && call.body && call.body.code_ask) return { ok: true, asks: [] };
      return undefined;
    },
  });
  const wt = () => page.$("#code-watch");
  const wf = () => page.$("#code-wf");
  const f = page.document.createElement("iframe");
  f.src = LIVE;
  page.document.getElementById("messages").appendChild(f);
  f.getClientRects = () => [{}];
  const post = (source, data, origin) =>
    page.window.dispatchEvent(new page.window.MessageEvent("message", { data, source, origin: origin || "" }));

  await run("Watch asks the pipe whether the Fixer is on this chat; nothing shows while it is not", async () => {
    post(f.contentWindow, { type: "iframe:height", height: 420, live: true });
    post(f.contentWindow, { type: "code:watch" });
    await page.waitFor(() => wt(), "Watch");
    await page.waitFor(() => ops.some((o) => o.op === "active" && o.chat_id === CHAT), "the active op");
    assert(!wf(), "no Fixer stage");
    assert(wt().getAttribute("data-mode") !== "fixer");
  });

  await run("a report starts: the alarm, then the fixer's viewer on the stage behind hazard tape", async () => {
    active = { ok: true, active: VIEW, title: "The preview pane is showing the code mode window", since: Math.floor(Date.now() / 1000) - 75 };
    await page.waitFor(() => wf(), "the Fixer stage", 12000);
    assert(page.$(".wt-stage").contains(wf()), "it sits on the stage");
    assert(page.$(".wf-frame", wf()).getAttribute("src") === VIEW, "the viewer");
    assert(/SOMETHING BROKE/.test(page.text(".wf-intro")) && /investigating itself/.test(page.text(".wf-intro")), "the alarm");
    assert(page.text(".wf-title") === "The preview pane is showing the code mode window");
    assert(page.text(".wf-chip") === "FIXER · INVESTIGATING" && /CHAT PAUSED/.test(page.text(".wf-bar")));
    assert(/^1:[1-5]\d$/.test(page.text(".wf-clock")), "how long it has run: " + page.text(".wf-clock"));
    await page.waitFor(() => wt().getAttribute("data-mode") === "fixer", "the header in Fixer mode");
    assert(page.text(".wt-chip") === "FIXER");
    await page.waitFor(() => !page.$(".wf-intro"), "the alarm to give way to the viewer", 5000);
    noErrors(page);
  });

  await run("Esc folds it into a blinking pill on the stage (Watch stays); Show brings it back, without a second alarm", async () => {
    page.press(page.document.body, "Escape");
    assert(wt(), "Watch is still open");
    assert(!page.visible(wf()), "the Fixer stage hides");
    await page.waitFor(() => page.$(".wt-stage #code-wf-pill"), "the pill");
    assert(/Fixer investigating · The preview pane/.test(page.text("#code-wf-pill")), page.text("#code-wf-pill"));
    page.click("#code-wf-pill .cmp-pill-b");
    await page.waitFor(() => page.visible(wf()), "the stage again");
    assert(!page.$(".wf-intro"), "the alarm sounds once per report");
  });

  await run("Keep minimized holds (in storage) and shows the pill instead", async () => {
    page.click(".wf-keep");
    assert(page.window.localStorage.getItem("code-mode.fixer-minimized") === "1");
    await page.waitFor(() => !page.visible(wf()) && page.$("#code-wf-pill"), "the pill");
    page.click("#code-wf-pill .cmp-pill-b");   // Show = this report only
    await page.waitFor(() => page.visible(wf()), "shown");
    page.click(".wf-keep");
    assert(page.window.localStorage.getItem("code-mode.fixer-minimized") === null, "switched back");
  });

  await run("the verdict is stamped on it, then the stage goes back to the builder with a note", async () => {
    const frame = page.$(".wf-frame", wf());
    post(page.window, { type: "fixer:status", done: true, verdict: "fixed" }, FIXER);   // not the stage's viewer: ignored
    await page.sleep(50);
    assert(wf().getAttribute("data-state") === "run");
    post(frame.contentWindow, { type: "fixer:status", done: true, verdict: "fixed" }, FIXER);
    await page.waitFor(() => wf().getAttribute("data-state") === "done", "the done state");
    assert(page.text(".wf-stamp b") === "FIXED" && page.text(".wf-chip") === "FIXER · FIXED");
    await page.waitFor(() => !wf(), "the fold", 9000);
    assert(/Fixer: fixed · the agent carries on/.test(page.text("#code-wf-pill")), page.text(".wt-stage"));
    await page.waitFor(() => wt().getAttribute("data-mode") !== "fixer", "the header back");
    noErrors(page);
  });

  await run("a report shown to its verdict does not come back, even while the pipe still lists it for a moment", async () => {
    const before = ops.filter((o) => o.op === "active").length;
    await page.waitFor(() => ops.filter((o) => o.op === "active").length > before, "the next look", 10000);
    await page.sleep(300);
    assert(!wf(), "no second takeover for the same report");
    active = { ok: true, active: "" };
    noErrors(page);
  });

  finish();
})();
