/* A preview server that does not answer never reaches the frame. The browser would show its own error page there,
 * and "Try Again" on Firefox's error page reloads the whole tab, chat included (2026-10-13: an agent's kill had
 * taken the preview server down). The panel says so itself and loads the project when the server is back. Also: the
 * address a reply gives for the chat's app (/previews/<chat id>/...) opens in the panel. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "cf63c77d-0000-4000-8000-000000000001";
const ORIGIN = "http://ai-sandbox:4097";
const BASE = ORIGIN + "/p/" + CHAT + "/" + "ab".repeat(16) + "/";
const FRIENDLY = "http://owui.test/previews/" + CHAT + "/";

function reply(page, links) {
  const msg = page.document.createElement("div");
  msg.id = "message-11111111-2222-3333-4444-555555555555";
  msg.innerHTML = links.map((l) => '<p><a href="' + l.href + '">' + l.text + "</a></p>").join("");
  page.$("#messages").appendChild(msg);
  return msg;
}

(async () => {
  let down = true;
  let page, msg;
  const frame = () => page.$("#code-preview-panel .cp-frame-preview");
  const cover = () => page.$("#code-preview-panel .cp-down");
  const probes = () => page.calls.filter((c) => c.origin === ORIGIN).length;

  await run("a dead preview server: the panel says so and the frame stays empty", async () => {
    page = await load({
      url: "http://owui.test/c/" + CHAT,
      fetch: (call) => {
        if (call.origin !== ORIGIN) return undefined;
        if (down) throw new TypeError("NetworkError when attempting to fetch resource.");
        return { version: "v1", files: 3, entry: "index.html", app: 8000 };
      },
    });
    msg = reply(page, [
      { href: BASE, text: "Preview" },
      { href: FRIENDLY + "admin?tab=2", text: FRIENDLY + "admin?tab=2" },
      { href: "http://owui.test/previews/another-chat-0001/", text: "someone else's" },
    ]);
    await page.waitFor(() => page.$$("a.code-preview-chip", msg).length === 1, "the Preview chip");
    page.click(page.$("a.code-preview-chip", msg));
    await page.waitFor(() => page.$("#code-preview-panel").classList.contains("open"), "the panel sliding open");
    await page.waitFor(() => page.visible(cover()), "the panel's own 'not answering' message");
    assert(/not answering/i.test(page.text(cover())), "the message does not say what is wrong: " + page.text(cover()));
    assert(!frame().getAttribute("src"), "the frame was pointed at a server that does not answer");
  });

  await run("it keeps asking, and Reload does not point the frame at it either", async () => {
    const before = probes();
    page.click("#code-preview-panel .cp-reload");
    await page.waitFor(() => probes() > before, "another try");
    await page.sleep(200);
    assert(!frame().getAttribute("src"), "Reload pointed the frame at a server that does not answer");
    assert(page.visible(cover()), "the message went away while the server is still down");
  });

  await run("the project loads by itself when the server answers again", async () => {
    down = false;
    await page.waitFor(() => frame().getAttribute("src") === BASE, "the project in the frame", 8000);
    assert(!page.visible(cover()), "the message stayed after the project loaded");
  });

  await run("the server goes away while the panel is open: the frame is covered", async () => {
    down = true;
    await page.waitFor(() => page.visible(cover()), "the cover over the frame", 8000);
    down = false;
    await page.waitFor(() => !page.visible(cover()), "the cover gone once it answers", 8000);
    assert(frame().getAttribute("src") === BASE, "the frame lost the project: " + frame().getAttribute("src"));
  });

  await run("the app's address in a reply opens in the panel, at that page", async () => {
    const [mine, other] = page.$$('a[href*="/previews/"]', msg);
    page.click(mine);
    await page.waitFor(() => frame().getAttribute("src") === BASE + "admin?tab=2", "the app's admin page in the panel");
    const ev = new page.window.MouseEvent("click", { bubbles: true, cancelable: true });
    other.dispatchEvent(ev);
    assert(!ev.defaultPrevented, "a link to another chat's preview was taken over");
    const newTab = new page.window.MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true });
    mine.dispatchEvent(newTab);
    assert(!newTab.defaultPrevented, "Ctrl+click (a new tab) was taken over");
    assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
  });
  finish();
})();
