/* Preview links in a Code reply: the plain [Preview] link opens the project, and a link to one page of it (the
 * "Ideas board" link, .../ideas/) opens that page in the same panel. */
const { load, run, assert, finish } = require("./harness.cjs");

const BASE = "http://ai-sandbox:4097/p/chat-1/" + "ab".repeat(16) + "/";

function reply(page, links) {
  const msg = page.document.createElement("div");
  msg.id = "message-11111111-2222-3333-4444-555555555555";
  msg.innerHTML = links.map((l) => '<p><a href="' + l.href + '">' + l.text + "</a></p>").join("");
  page.$("#messages").appendChild(msg);
  return msg;
}

(async () => {
  await run("an Ideas board link opens the board; the Preview link goes back to the project", async () => {
    const page = await load({
      fetch: (call) => (call.path.endsWith("/__version") ? { version: "v1", files: 3, entry: "index.html" } : undefined),
    });
    const msg = reply(page, [{ href: BASE, text: "Preview" }, { href: BASE + "ideas/", text: "Ideas board" }]);
    await page.waitFor(() => page.$$("a.code-preview-chip", msg).length === 2, "both links turned into chips");
    const [plain, ideas] = page.$$("a.code-preview-chip", msg);
    const frame = () => page.$("#code-preview-panel .cp-frame-preview");

    page.click(ideas);
    await page.waitFor(() => frame() && frame().getAttribute("src") === BASE + "ideas/", "the panel showing the board");
    await page.waitFor(() => page.$("#code-preview-panel").classList.contains("open"), "the panel sliding open");

    page.click(plain);
    await page.waitFor(() => frame().getAttribute("src") === BASE, "the panel back on the project");

    page.click(ideas);
    await page.waitFor(() => frame().getAttribute("src") === BASE + "ideas/", "the board again");
    assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
  });
  finish();
})();
