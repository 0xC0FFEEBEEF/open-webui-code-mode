/* The compaction overlay (10-25, the user: "a nice animated overlay (like the question dialog) that would pop up and
 * show what the model is reading during compaction then clearing out and showing what it is writing during
 * compaction, then showing it reading it in and disappearing after compaction is over").
 * The running live window posts "code:compaction" states (live.html cPost); the page draws them. jsdom has no layout:
 * these check what is shown and when, the look is checked on the real page. */
const { load, run, assert, finish } = require("./harness.cjs");

const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
const since = Date.now() - 65000;
const TAPE = [
  { k: "sum", s: "The summary from the last compaction", w: 20000 },
  { k: "you", s: "continue", w: 8 },
  { k: "says", s: "Building fresh binaries <img src=x onerror=alert(1)>", w: 900 },
  { k: "sh", s: "cargo test -p codex-core --lib", w: 30000 },
  { k: "edit", s: "core/src/auth.rs", w: 4000 },
  { k: "says", s: "Both git records committed.", w: 300 },
  { k: "sh", s: "slerm-job run --later 'read the summary' -- bash check.sh", w: 2000 },
];

function liveFrame(page) {
  const f = page.document.createElement("iframe");
  f.src = "http://ai-sandbox:4099/w/chat-1/" + "0".repeat(32) + "/?s=ses_abc&m=msg_now";
  page.document.getElementById("messages").appendChild(f);
  return f;
}
function post(page, source, state, tape) {
  const data = { type: "code:compaction", state };
  if (tape) data.tape = tape;
  page.window.dispatchEvent(new page.window.MessageEvent("message", { data, source }));
}
const read = (extra) => Object.assign({ ph: "read", since, text: "", model: "STRATA-qwen3.8-flash-next-iq3xxs", ssd: 117392,
  prog: { state: "reading", read: 150000, total: 199335, pps: 358 } }, extra || {});

(async () => {
  let page, frame;
  const ov = () => page.$("#code-cmp");
  const pill = () => page.$("#code-cmp-pill");

  await run("a compaction in the running window opens the overlay: reading the conversation, its history running past", async () => {
    page = await load();
    post(page, page.window, read(), TAPE);   // not a live window: ignored
    await page.sleep(50);
    assert(!ov(), "only a live window of the page may open it");
    frame = liveFrame(page);
    post(page, frame.contentWindow, read(), TAPE);
    await page.waitFor(() => ov() && page.visible(ov()), "the overlay");
    assert(ov().getAttribute("data-ph") === "read", "phase read");
    assert(page.visible(page.$(".cmp-s-read")) && !page.visible(page.$(".cmp-s-write")), "only the reading part shows");
    assert(page.text(page.$(".cmp-s-read .cmp-num")) === "150,000 / 199,335 tokens", "tokens: " + page.text(page.$(".cmp-s-read .cmp-num")));
    assert(/^Reading at 358 tok\/s · about 2:17 left$/.test(page.text(page.$(".cmp-s-read .cmp-note"))), "note: " + page.text(page.$(".cmp-s-read .cmp-note")));
    assert(page.text(page.$(".cmp-ssdnote")) === "117,392 already cached", "cached: " + page.text(page.$(".cmp-ssdnote")));
    const rows = page.$$(".cmp-s-read .cmp-ln");
    assert(rows.length === TAPE.length + 2, "the history between the instructions and the summarize request: " + rows.length);
    assert(page.$$(".cmp-s-read .cmp-ln.is-head").length === 1, "one read head");
    assert(page.$$(".cmp-s-read .cmp-ln.is-ssd").length >= 1, "the part the engine already had is marked");
    assert(/^1:0\d$/.test(page.text(page.$(".cmp-clock"))), "the clock counts from the compaction's start: " + page.text(page.$(".cmp-clock")));
    assert(!page.$(".cmp-s-read img") && page.text(rows[3]).includes("<img src=x"), "history text stays text");
    assert(/STRATA-qwen3\.8/.test(page.text(page.$(".cmp-sub"))), "names the model");
    noErrors(page);
  });

  await run("then the history folds away and the summary types itself out", async () => {
    post(page, frame.contentWindow, read({ ph: "write", text: "## Objective\n- Fork the **Codex** CLI into `owl`\n- <b>no</b> trace\n## Requirements\n- No ChatGPT",
      prog: { state: "generating", gen: 412, rate: 38.2 } }));
    await page.waitFor(() => ov().getAttribute("data-ph") === "write", "phase write");
    assert(page.visible(page.$(".cmp-s-write")) && !page.visible(page.$(".cmp-s-read")), "only the writing part shows");
    assert(page.text(page.$(".cmp-fold")) === "199,335 tokens read · 7 messages and tool calls", "fold: " + page.text(page.$(".cmp-fold")));
    assert(page.text(page.$(".cmp-s-write .cmp-num")) === "412 tokens · 38 tok/s", "written: " + page.text(page.$(".cmp-s-write .cmp-num")));
    const lines = page.$$(".cmp-s-write .cmp-doc > div").map((d) => page.text(d));
    assert(lines.join("|") === "Objective|• Fork the Codex CLI into owl|• <b>no</b> trace|Requirements|• No ChatGPT", "lines: " + lines.join("|"));
    assert(page.$$(".cmp-s-write .cmp-d-h").length === 2, "headings");
    assert(!page.$(".cmp-doc b") && page.$(".cmp-s-write .cmp-doc > div:last-child .cmp-caret"), "text, with the caret at the end");
    const steps = page.$$(".cmp-step").map((s) => s.className);
    assert(steps[0].includes("is-done") && steps[1].includes("is-on") && !/is-/.test(steps[2]), "steps: " + steps);
    noErrors(page);
  });

  await run("Hide (or Esc) folds it into a pill above the message box; Show brings it back", async () => {
    page.click(".cmp-hide");
    await page.waitFor(() => !page.visible(ov()), "the overlay to hide");
    assert(pill() && page.visible(pill()) && pill().parentElement.id === "message-input-container", "the pill above the message box");
    assert(page.text(page.$(".cmp-pill-t")) === "Compacting · writing the summary · 412 tokens", "pill: " + page.text(page.$(".cmp-pill-t")));
    post(page, frame.contentWindow, read({ ph: "write", text: "## Objective", prog: { state: "generating", gen: 500, rate: 38 } }));
    await page.waitFor(() => page.text(page.$(".cmp-pill-t")).endsWith("500 tokens"), "the pill to follow");
    assert(!page.visible(ov()), "a new state does not reopen a hidden overlay");
    page.click(".cmp-pill-b");
    await page.waitFor(() => page.visible(ov()), "the overlay back");
    assert(!pill(), "no pill while it shows");
    page.press(null, "Escape");
    await page.waitFor(() => !page.visible(ov()) && pill(), "Esc to hide it");
    page.click(".cmp-pill-b");
    await page.waitFor(() => page.visible(ov()), "shown again");
    noErrors(page);
  });

  await run("reading it back in: instructions, then the summary with a cursor going through it, then the recent messages", async () => {
    const st = { ph: "readin", since, model: "m", ssd: 117392, tokens: { output: 5159 }, recent: 108000,
      text: "## Objective\n- Fork the CLI\n## Decisions\n- Delete, not convert" };
    post(page, frame.contentWindow, Object.assign({ prog: { state: "reading", read: 5000, total: 53501, pps: 373 } }, st));
    await page.waitFor(() => ov().getAttribute("data-ph") === "readin", "phase readin");
    const lbl = page.$$(".cmp-seglbl span").map((s) => page.text(s));
    assert(lbl[0] === "Instructions ≈ 18.3k" && lbl[1] === "Summary 5.2k" && lbl[2] === "Recent messages, kept as they were ≈ 30k", "labels: " + lbl);
    assert(/standing instructions/.test(page.text(page.$(".cmp-s-readin .cmp-note"))), "instructions first");
    assert(page.visible(page.$(".cmp-tape-in")) && !page.visible(page.$(".cmp-s-readin .cmp-doc")), "the instruction list");
    post(page, frame.contentWindow, Object.assign({ prog: { state: "reading", read: 21000, total: 53501, pps: 373 } }, st));
    await page.waitFor(() => page.visible(page.$(".cmp-s-readin .cmp-doc")), "the summary being read");
    assert(page.$(".cmp-s-readin .cmp-sweep") && page.$(".cmp-s-readin .cmp-took"), "a cursor and the part read so far");
    post(page, frame.contentWindow, Object.assign({ prog: { state: "reading", read: 40000, total: 53501, pps: 373 } }, st));
    await page.waitFor(() => /latest messages/.test(page.text(page.$(".cmp-s-readin .cmp-note"))), "the recent messages");
    const recent = page.$$(".cmp-tape-in .cmp-ln").map((r) => page.text(r));
    assert(recent.length >= 2 && /check\.sh/.test(recent[recent.length - 1]), "the newest history lines: " + recent.join(" | "));
    noErrors(page);
  });

  await run("when the model has read the new context it says so, folds away and leaves a note", async () => {
    post(page, frame.contentWindow, { ph: "done", since, until: since + 524000, model: "m", ssd: 117392, tokens: { output: 5159 }, text: "" });
    await page.waitFor(() => ov() && ov().getAttribute("data-ph") === "done", "done");
    assert(page.text(page.$(".cmp-title")) === "Compaction finished", "title");
    assert(page.text(page.$(".cmp-done-n")) === "53,501 tokens now · 8 min 44 s", "numbers: " + page.text(page.$(".cmp-done-n")));
    assert(/^Read 199,335 \(117,392 of them already cached\), wrote a 5,159-token summary, then read the new context \(53,501 tokens\)\.$/
      .test(page.text(page.$(".cmp-done-l"))), "detail: " + page.text(page.$(".cmp-done-l")));
    await page.waitFor(() => !ov(), "the overlay to go", 4000);
    assert(pill() && pill().classList.contains("is-done") && page.text(page.$(".cmp-pill-t")) === "Compacted · 53,501 tokens now · 8 min 44 s",
      "the note: " + page.text(pill()));
    noErrors(page);
  });

  await run("a compaction that fails says so and goes; leaving the chat closes it", async () => {
    const k = Date.now();
    post(page, frame.contentWindow, read({ since: k }), TAPE);
    await page.waitFor(() => ov() && ov().getAttribute("data-ph") === "read", "a new compaction");
    post(page, frame.contentWindow, read({ since: k, ph: "fail", err: "the turn ended" }));
    await page.waitFor(() => ov() && ov().getAttribute("data-ph") === "fail", "the failure");
    assert(page.text(page.$(".cmp-fail")) === "It did not finish (the turn ended). The session goes on as it was.", "fail: " + page.text(page.$(".cmp-fail")));
    await page.waitFor(() => !ov(), "it goes", 5000);
    post(page, frame.contentWindow, read({ since: Date.now() }), TAPE);
    await page.waitFor(() => ov(), "another one");
    frame.remove();   // another chat
    await page.waitFor(() => !ov(), "closed with its window", 2500);
    noErrors(page);
  });
  finish();
})();
