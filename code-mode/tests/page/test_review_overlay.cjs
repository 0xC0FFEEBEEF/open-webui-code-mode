/* The review overlay (10-25, the user: "a version of this for the reviewer" - a full dialog every time, with an option
 * to keep it minimized that holds across chats and can be switched back). A review's live window posts its round
 * ("code:review", live.html rUpdate); the builder's next window posts the findings it got ("code:review-handback").
 * jsdom has no layout: these check what is shown and when; the look is checked on the real page. */
const { load, run, assert, finish } = require("./harness.cjs");

const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
const KEEP = "code-mode.review-minimized";
const BRIEF = [
  { k: "rules", s: "The reviewer's standing instructions and tools", w: 36000 },
  { k: "task", s: "Current task to review: continue", w: 200 },
  { k: "asked", s: "6 requests, oldest first", w: 900 },
  { k: "claims", s: "The builder's closing summary: claims to check, not facts <img src=x onerror=alert(1)>", w: 665 },
  { k: "reply", s: "Answer: worksheet lines, then REVIEW: PASS, FAIL or INCOMPLETE", w: 600 },
];
const ROWS = [
  { tag: "ASKED", s: "Strip ChatGPT/OpenAI account surfaces from owl", st: "built", kind: "", uv: false },
  { tag: "ASKED", s: "This turn's own cluster re-verification", st: "unverified", kind: "", uv: true },
  { tag: "ASKED", s: "ChatGPT stripped from everything", st: "half built (explicitly staged)", kind: "not built", uv: false },
  { tag: "HAD", s: "Connect screen from earlier batch", st: "still there", kind: "", uv: false },
  { tag: "CLAIM", s: "Re-submitted job 4709", st: "true", kind: "", uv: false },
];

function liveFrame(page, m) {
  const f = page.document.createElement("iframe");
  f.src = "http://ai-sandbox:4099/w/chat-1/" + "0".repeat(32) + "/?s=ses_" + m + "&m=msg_" + m;
  page.document.getElementById("messages").appendChild(f);
  return f;
}
function post(page, source, data) {
  page.window.dispatchEvent(new page.window.MessageEvent("message", { data, source }));
}
const round = (since, extra) => Object.assign({ round: 2, since, model: "STRATA-qwen3.8-flash-next-iq3xxs", brief: BRIEF, recheck: false,
  ph: "brief", prog: { state: "reading", read: 30000, total: 41600 }, cmds: [], nCmds: 0, rows: [], verdict: null }, extra || {});
const CMDS = Array.from({ length: 17 }, (_, i) => ({ k: "sh", s: "git diff --stat refs/code-mode/turn-start # " + (i + 1), st: i < 16 ? "completed" : "running" }));

(async () => {
  let page, rf, bf;
  const ov = () => page.$("#code-rv");
  const pill = () => page.$("#code-rv-pill");

  await run("a review round opens its dialog: the reviewer reading the brief Code mode gave it", async () => {
    page = await load();
    post(page, page.window, { type: "code:review", state: round(Date.now()) });
    await page.sleep(50);
    assert(!ov(), "only a live window may open it");
    rf = liveFrame(page, "review");
    const since = Date.now() - 65000;
    post(page, rf.contentWindow, { type: "code:review", state: round(since) });
    await page.waitFor(() => ov() && page.visible(ov()), "the dialog");
    assert(ov().getAttribute("data-ph") === "brief", "phase brief");
    assert(page.text(page.$(".cmp-title", ov())) === "Reviewing the turn", "title");
    assert(/^Round 2 · an independent check of the builder's turn · STRATA-/.test(page.text(page.$(".cmp-sub", ov()))), "sub: " + page.text(page.$(".cmp-sub", ov())));
    assert(page.text(page.$(".rv-s-brief .cmp-num")) === "30,000 / 41,600 tokens", "tokens: " + page.text(page.$(".rv-s-brief .cmp-num")));
    const rows = page.$$(".rv-s-brief .cmp-ln");
    assert(rows.length === BRIEF.length, "the brief's rows: " + rows.length);
    assert(page.$$(".rv-s-brief .cmp-ln.is-head").length === 1, "one read head");
    assert(!page.$(".rv-s-brief img") && page.text(rows[3]).includes("<img src=x"), "brief text stays text");
    assert(page.$(".rv-switch").getAttribute("aria-checked") === "false", "keep minimized starts off");
    assert(/^1:0\d$/.test(page.text(page.$(".cmp-clock", ov()))), "clock from the round's start: " + page.text(page.$(".cmp-clock", ov())));
    noErrors(page);
  });

  await run("then its read-only commands run past, the newest one live", async () => {
    post(page, rf.contentWindow, { type: "code:review", state: round(Date.now() - 300000, { ph: "check", cmds: CMDS, nCmds: 17, checkFrom: Date.now() - 200000 }) });
    await page.waitFor(() => ov().getAttribute("data-ph") === "check", "phase check");
    assert(page.text(page.$(".rv-s-check .cmp-num")) === "17 commands", "count: " + page.text(page.$(".rv-s-check .cmp-num")));
    const lines = page.$$(".rv-cmds .rv-ln");
    assert(lines.length === 10, "the last ten: " + lines.length);
    assert(lines[9].classList.contains("is-now") && page.text(page.$(".cmp-k", lines[9])) === "now", "the running one");
    assert(page.text(page.$(".cmp-k", lines[0])) === "#8", "numbered: " + page.text(page.$(".cmp-k", lines[0])));
    noErrors(page);
  });

  await run("the worksheet fills in: findings red with their kind, unverified amber", async () => {
    const since = Date.now() - 700000;
    post(page, rf.contentWindow, { type: "code:review", state: round(since, { ph: "sheet", cmds: CMDS, nCmds: 31, rows: ROWS,
      checkFrom: since + 65000, sheetFrom: since + 734000 }) });
    await page.waitFor(() => ov().getAttribute("data-ph") === "sheet", "phase sheet");
    assert(page.text(page.$(".rv-s-sheet .cmp-fold")) === "31 commands run · 11 min 9 s of checking", "fold: " + page.text(page.$(".rv-s-sheet .cmp-fold")));
    const rows = page.$$(".rv-sheet .rv-row");
    assert(rows.length === 5 && page.text(page.$(".rv-s-sheet .cmp-num")) === "5 lines", "rows");
    assert(page.$(".rv-st", rows[2]).classList.contains("st-bad") && page.text(page.$(".rv-st", rows[2])) === "not built", "a finding");
    assert(page.$(".rv-st", rows[1]).classList.contains("st-warn"), "unverified");
    assert(page.$(".rv-st", rows[0]).classList.contains("st-ok"), "built");
    noErrors(page);
  });

  await run("the verdict, the finding going back, and the card folding into the builder's window", async () => {
    const since = Date.now() - 784000;
    post(page, rf.contentWindow, { type: "code:review", state: round(since, { ph: "verdict", verdict: "INCOMPLETE", rows: ROWS, nCmds: 31,
      ended: true, until: since + 784000 }) });
    await page.waitFor(() => page.text(page.$(".cmp-title", ov())) === "Review finished", "the verdict");
    assert(page.text(page.$(".rv-stamp")) === "INCOMPLETE" && page.$(".rv-stamp").classList.contains("v-incomplete"), "stamp");
    const tally = page.$$(".rv-tally > div").map((d) => page.text(d.firstChild) + " = " + page.text(d.lastChild));
    assert(tally[0] === "ASKED 3 = 1 built · 1 unverified · 1 not built" && tally[1] === "HAD 1 = 1 still there", "tally: " + tally.join(" | "));
    assert(page.text(page.$(".rv-fh")) === "1 finding in the worksheet", "predicted: " + page.text(page.$(".rv-fh")));
    assert(page.$(".rv-hb.is-now"), "handing it over");
    bf = liveFrame(page, "builder");
    post(page, bf.contentWindow, { type: "code:review-handback", findings: ["[not built] ChatGPT stripped from everything (half built) - core/src/auth.rs"] });
    await page.waitFor(() => page.text(page.$(".rv-fh")) === "1 finding sent back to the builder", "the real findings");
    assert(page.text(page.$(".rv-find .rv-chip")) === "not built", "its kind");
    await page.waitFor(() => !ov(), "the card to fold away", 5000);
    assert(pill() && pill().classList.contains("is-done") && page.text(pill()) === "Review: 1 finding sent back to the builder · 13 min 4 s",
      "the note: " + (pill() && page.text(pill())));
    noErrors(page);
  });

  await run("Keep minimized: the switch holds for later rounds (and chats) until switched back; the verdict still pops up", async () => {
    const r2 = liveFrame(page, "review2");
    post(page, r2.contentWindow, { type: "code:review", state: round(Date.now(), { round: 3 }) });
    await page.waitFor(() => ov() && page.visible(ov()), "the next round's dialog");
    page.click(".rv-switch");
    assert(page.window.localStorage.getItem(KEEP) === "1", "saved for every chat");
    await page.waitFor(() => !page.visible(ov()) && pill() && page.visible(pill()), "the pill instead");
    assert(/^Reviewing · round 3 · reading the brief · 30,000 \/ 41,600$/.test(page.text(page.$(".cmp-pill-t", pill()))), "pill: " + page.text(pill()));
    const r3 = liveFrame(page, "review3");   // a later round (or another chat): starts minimized
    post(page, r3.contentWindow, { type: "code:review", state: round(Date.now() + 5, { round: 4 }) });
    await page.waitFor(() => pill() && /round 4/.test(page.text(pill())), "the next round in the pill");
    assert(!page.visible(ov()), "no dialog while kept minimized");
    post(page, r3.contentWindow, { type: "code:review", state: round(Date.now() + 5, { round: 4, ph: "verdict", verdict: "PASS", rows: ROWS.slice(0, 2), ended: true, until: Date.now() + 200000 }) });
    await page.waitFor(() => page.$("#code-rv-pop"), "the verdict pop");
    assert(page.text(page.$("#code-rv-pop .rv-pst")) === "PASS" && page.text(page.$("#code-rv-pop .rv-pl")) === "Nothing goes back to the builder.", "pop: " + page.text(page.$("#code-rv-pop")));
    await page.waitFor(() => !page.$("#code-rv"), "the round to close", 5000);
    const r4 = liveFrame(page, "review4");
    post(page, r4.contentWindow, { type: "code:review", state: round(Date.now() + 9, { round: 5 }) });
    await page.waitFor(() => pill() && /round 5/.test(page.text(pill())), "round 5 minimized");
    page.click(".cmp-pill-b", pill());
    await page.waitFor(() => page.visible(ov()), "Show opens it");
    assert(page.$(".rv-switch").getAttribute("aria-checked") === "true", "with the switch on");
    page.click(".rv-switch");
    assert(page.window.localStorage.getItem(KEEP) === null, "switched back");
    assert(page.visible(ov()), "the dialog stays");
    page.press(null, "Escape");
    await page.waitFor(() => !page.visible(ov()) && pill(), "Esc hides this one");
    noErrors(page);
  });

  await run("a pass with nothing to send back folds after a moment; a round whose window goes away closes", async () => {
    page.window.localStorage.removeItem(KEEP);
    const r5 = liveFrame(page, "review5");
    const since = Date.now() - 240000;
    post(page, r5.contentWindow, { type: "code:review", state: round(since, { round: 6 }) });
    await page.waitFor(() => ov() && page.visible(ov()) && /Round 6/.test(page.text(page.$(".cmp-sub", ov()))), "round 6");
    post(page, r5.contentWindow, { type: "code:review", state: round(since, { round: 6, ph: "verdict", verdict: "PASS", rows: [ROWS[0]], ended: true, until: since + 240000 }) });
    await page.waitFor(() => page.text(page.$(".rv-fh")) === "Nothing goes back to the builder.", "pass");
    await page.waitFor(() => !ov(), "folds", 6000);
    assert(pill() && page.text(pill()) === "Review passed · round 6 · 4 min 0 s", "note: " + (pill() && page.text(pill())));
    const r6 = liveFrame(page, "review6");
    post(page, r6.contentWindow, { type: "code:review", state: round(Date.now(), { round: 7 }) });
    await page.waitFor(() => ov() && /Round 7/.test(page.text(page.$(".cmp-sub", ov()))), "round 7");
    r6.remove();
    await page.waitFor(() => !ov(), "closed with its window", 2500);
    noErrors(page);
  });
  finish();
})();
