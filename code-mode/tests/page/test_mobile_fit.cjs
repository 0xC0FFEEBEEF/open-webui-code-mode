/* A phone-sized Open WebUI and the chat's "scroll down" arrow.
 * jsdom has no layout, so each test gives the elements it needs the sizes a real page measured (390 px wide phone,
 * 2026-10-24) and checks what the page script decides from them. How it looks is checked on the real page. */
const { load, run, assert, finish } = require("./harness.cjs");

const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
const rect = (left, top, width, height) => () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top });
const settle = (page) => page.sleep(120);  // the page script reconciles on the next animation frame

// Open WebUI's arrow: a round button in an "absolute -top-12 ... justify-center" row, heroicons arrow-down.
function addArrow(page, onOwnClick) {
  const doc = page.document;
  const row = doc.createElement("div");
  row.className = "absolute -top-12 left-0 right-0 flex justify-center z-30 pointer-events-none";
  row.innerHTML = '<button class="bg-white border border-gray-100 dark:border-none dark:bg-white/20 p-1.5 rounded-full pointer-events-auto">' +
    '<svg viewBox="0 0 20 20"><path d="M10 3a.75.75 0 01.75.75v10.638l3.96-4.158a.75.75 0 111.08 1.04l-5.25 5.5a.75.75 0 01-1.08 0l-5.25-5.5a.75.75 0 111.08-1.04l3.96 4.158V3.75A.75.75 0 0110 3z"></path></svg></button>';
  doc.getElementById("message-input-container").prepend(row);
  const btn = row.querySelector("button");
  btn.addEventListener("click", onOwnClick);
  return btn;
}

function addLiveFrame(page, m) {
  const f = page.document.createElement("iframe");
  f.src = "http://ai-sandbox:4099/w/chat-1/" + "0".repeat(32) + "/?s=ses_abc&m=" + m;
  page.document.getElementById("messages").appendChild(f);
  f.getClientRects = () => [{}];  // on the page
  f.scrolls = [];
  f.scrollIntoView = (o) => f.scrolls.push(o);
  return f;
}

function report(page, frame, live) {
  page.window.dispatchEvent(new page.window.MessageEvent("message", {
    data: { type: "iframe:height", height: 420, live }, source: frame.contentWindow,
  }));
}

(async () => {
  await run("while a turn runs, the scroll-down arrow brings its live window to the middle instead of the bottom", async () => {
    const page = await load();
    let owui = 0;
    const arrow = addArrow(page, () => { owui += 1; });
    const older = addLiveFrame(page, "msg_old");
    const running = addLiveFrame(page, "msg_now");
    report(page, older, false);
    report(page, running, true);
    assert(running.getAttribute("data-code-live") === "1" && older.getAttribute("data-code-live") === "0",
      "each window's live state is noted on its frame");
    page.click(arrow);
    assert(running.scrolls.length === 1 && running.scrolls[0].block === "center", "the running window is centred: " + JSON.stringify(running.scrolls));
    assert(older.scrolls.length === 0, "an earlier round's window is not the target");
    assert(owui === 0, "Open WebUI's own jump to the bottom did not run");
    noErrors(page);
  });

  await run("with that window already in the middle, the arrow goes to the bottom as before", async () => {
    const page = await load();
    let owui = 0;
    const arrow = addArrow(page, () => { owui += 1; });
    const running = addLiveFrame(page, "msg_now");
    report(page, running, true);
    const vh = page.window.innerHeight;
    running.getBoundingClientRect = rect(20, vh / 2 - 210, 340, 420);
    page.click(arrow);
    assert(owui === 1 && running.scrolls.length === 0, "a second press reaches the bottom of the chat");
    noErrors(page);
  });

  await run("when no turn is running (or the window has finished), the arrow is Open WebUI's own", async () => {
    const page = await load();
    let owui = 0;
    const arrow = addArrow(page, () => { owui += 1; });
    page.click(arrow);
    assert(owui === 1, "no live window: Open WebUI scrolls to the bottom");
    const done = addLiveFrame(page, "msg_now");
    report(page, done, true);
    report(page, done, false);  // the turn ended
    page.click(arrow);
    assert(owui === 2 && done.scrolls.length === 0, "a finished window is not a reason to stop short");
    // a height message without the live field (an older live.html, or another embed) changes nothing
    report(page, done, undefined);
    assert(done.getAttribute("data-code-live") === "0", "a message without live leaves the frame as it was");
    noErrors(page);
  });

  await run("a narrow message box shows only the chosen thinking level; tapping it offers the others", async () => {
    const page = await load();
    const pill = await page.waitFor(() => page.$("#effort-pill"), "the thinking pill");
    const group = pill.parentElement;
    Object.defineProperty(group, "clientWidth", { configurable: true, get: () => 285 });
    Object.defineProperty(group, "scrollWidth", { configurable: true, get: () => 365 });  // runs 80 px past the box
    pill.getBoundingClientRect = rect(138, 785, 209, 28);
    page.window.dispatchEvent(new page.window.Event("resize"));
    await page.waitFor(() => pill.getAttribute("data-compact") === "1", "the pill to go compact");
    const shown = page.$$(".ef-seg", pill).filter((b) => page.visible(b));
    assert(shown.length === 1 && shown[0].getAttribute("aria-checked") === "true", "only the chosen level shows: " + shown.map((b) => b.textContent));
    page.click(shown[0]);
    const pop = await page.waitFor(() => page.$("#effort-pop"), "the level menu");
    const items = page.$$("button", pop).map((b) => b.textContent);
    const levels = page.$$(".ef-seg", pill).map((b) => b.textContent);
    assert(items.length === 4 && items.join(",") === levels.join(","), "the menu offers every level: " + items);
    page.click(page.$$("button", pop).find((b) => b.textContent === "Low"));
    assert(!page.$("#effort-pop"), "choosing a level closes the menu");
    assert(page.window.localStorage.getItem("effortPill:qwen3.8-27b") === "low", "the choice is saved for the model");
    assert(page.$('.ef-seg[aria-checked="true"]', pill).textContent === "Low", "the pill shows the new level");
    page.click(page.$('.ef-seg[aria-checked="true"]', pill));
    await page.waitFor(() => page.$("#effort-pop"), "the menu again");
    page.press(null, "Escape");
    assert(!page.$("#effort-pop"), "Escape closes the menu without a change");
    noErrors(page);
  });

  await run("with room again the pill shows every level", async () => {
    const page = await load();
    const pill = await page.waitFor(() => page.$("#effort-pill"), "the thinking pill");
    const group = pill.parentElement;
    let scroll = 365;
    Object.defineProperty(group, "clientWidth", { configurable: true, get: () => 285 });
    Object.defineProperty(group, "scrollWidth", { configurable: true, get: () => scroll });
    let w = 209;
    pill.getBoundingClientRect = () => rect(138, 785, w, 28)();
    page.window.dispatchEvent(new page.window.Event("resize"));
    await page.waitFor(() => pill.getAttribute("data-compact") === "1", "compact");
    w = 86;
    scroll = 260;  // a wider screen: 25 px free beside the compact pill, but the full one needs 123 more
    page.window.dispatchEvent(new page.window.Event("resize"));
    await settle(page);
    assert(pill.getAttribute("data-compact") === "1", "stays compact while the full pill would not fit");
    scroll = 150;  // much wider: 135 px free, the full pill needs 123
    page.window.dispatchEvent(new page.window.Event("resize"));
    await page.waitFor(() => pill.getAttribute("data-compact") === "0", "the full pill back");
    noErrors(page);
  });

  await run("on a phone the Chat/Code pill takes its own row under the bar, beside the checklist button", async () => {
    const page = await load();
    const doc = page.document;
    const nav = doc.querySelector("nav");
    for (const x of [238, 272, 308, 344]) {  // the bar's own controls on the right (new chat, menu, controls, avatar)
      const b = doc.createElement("button");
      b.getBoundingClientRect = rect(x, 4, 34, 36);
      nav.appendChild(b);
    }
    nav.getBoundingClientRect = rect(0, 0, 390, 44);
    doc.getElementById("model-selector-0-button").getBoundingClientRect = rect(48, 9, 164, 28);
    const pill = await page.waitFor(() => page.$("#code-toggle-pill"), "the pill");
    pill.getBoundingClientRect = () => rect(0, 0, pill.getAttribute("data-compact") === "1" ? 208 : 300, 30)();
    page.window.dispatchEvent(new page.window.Event("resize"));
    await settle(page);
    page.window.dispatchEvent(new page.window.Event("resize"));
    await settle(page);
    assert(doc.documentElement.classList.contains("code-narrow"), "the page is marked narrow");
    assert(pill.style.top === "50px", "the pill sits under the bar, got top " + pill.style.top);
    const band = page.$("#code-toggle-band");
    assert(band && !band.hidden && band.parentElement === nav, "a band in the page colour sits behind the bar and the row");
    assert(doc.documentElement.style.getPropertyValue("--code-row2-top") === "50px", "the checklist button gets the same row");
    // a wide window: back beside the model picker, no band
    nav.getBoundingClientRect = rect(0, 0, 1200, 44);
    nav.querySelectorAll("button:not(#model-selector-0-button)").forEach((b, i) => { b.getBoundingClientRect = rect(1050 + 36 * i, 4, 34, 36); });
    page.window.dispatchEvent(new page.window.Event("resize"));
    await settle(page);
    assert(!doc.documentElement.classList.contains("code-narrow"), "a wide window is not narrow");
    assert(band.hidden, "no band on a wide window");
    assert(pill.style.top !== "50px", "the pill is back on the bar's row");
    noErrors(page);
  });
  finish();
})();
