/* A reply's finished live windows fold into one "N earlier rounds" line (10-24, the user: "every time i reload or come
 * back to this page a new opencode bar just pops up").  The newest is told by opencode's message id (time-sorted). */
const { load, run, assert, finish } = require("./harness.cjs");
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

function reply(page, n) {   // Open WebUI's layout: a row of boxes, one iframe each
  const row = page.document.createElement("div");
  row.className = "my-1 w-full flex overflow-x-auto gap-2 flex-wrap";
  const frames = [];
  for (let i = n; i >= 1; i--) {   // newest first, as the page shows them
    const box = page.document.createElement("div");
    box.className = "my-2 w-full";
    const f = page.document.createElement("iframe");
    f.src = "http://ai-sandbox:4099/w/chat-1/" + "0".repeat(32) + "/?s=ses_abc&m=msg_16f0" + String(i).padStart(4, "0");
    box.appendChild(f);
    row.appendChild(box);
    frames[i] = f;
  }
  page.document.getElementById("messages").appendChild(row);
  return { row, frames };
}
function report(page, f, live) {
  page.window.dispatchEvent(new page.window.MessageEvent("message", { data: { type: "iframe:height", height: 41, live }, source: f.contentWindow }));
}
const shown = (page, r) => Array.from(r.row.children).filter((c) => c.querySelector && c.querySelector("iframe") && !c.classList.contains("code-round-folded")).map((c) => c.querySelector("iframe").src.slice(-4));

(async () => {
  let page, r;
  await run("while a round runs, the finished ones fold into one line", async () => {
    page = await load();
    r = reply(page, 5);
    report(page, r.frames[5], true);
    for (let i = 1; i <= 4; i++) report(page, r.frames[i], false);
    const line = await page.waitFor(() => page.$(".code-rounds-fold"), "the fold line");
    assert(page.text(line) === "▸ 4 earlier rounds", "the line: " + page.text(line));
    assert(JSON.stringify(shown(page, r)) === JSON.stringify(["0005"]), "only the running window shows: " + shown(page, r));
    assert(line.previousElementSibling === r.frames[5].parentElement, "the line sits under the running window");
    noErrors(page);
  });
  await run("clicking the line shows them, clicking again folds them", async () => {
    const line = page.$(".code-rounds-fold");
    page.click(line);
    assert(shown(page, r).length === 5 && /fold them/.test(page.text(line)), "all five show: " + shown(page, r));
    page.click(line);
    assert(shown(page, r).length === 1 && page.text(line) === "▸ 4 earlier rounds", "folded again");
    noErrors(page);
  });
  await run("when the turn ends the newest finished box stays in view", async () => {
    report(page, r.frames[5], false);
    await page.waitFor(() => page.text(page.$(".code-rounds-fold")) === "▸ 4 earlier rounds" && shown(page, r).join() === "0005", "newest kept");
    noErrors(page);
  });
  await run("a reply with one finished box beside the running one has no line", async () => {
    page = await load();
    r = reply(page, 2);
    report(page, r.frames[2], true);
    report(page, r.frames[1], false);
    await page.sleep(100);
    assert(!page.$(".code-rounds-fold") && shown(page, r).length === 2, "nothing folded");
    noErrors(page);
  });
  finish();
})();
