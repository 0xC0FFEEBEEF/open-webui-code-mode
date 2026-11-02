/* The Checklist tab and its button at the right edge: the builder's CHECKLIST.md, parsed by code-features and served
 * by code-deploy (/api/checklist/<chat>). "Doing now" is the first box not ticked; the dot pulses only while Open
 * WebUI says the chat's turn is running (/api/tasks/chat/<chat>). Text from the checklist is never markup. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "d1dab701-0000-4000-8000-00000000c0de";
const DEPLOY = "http://owui.test:8095";
let data = {
  exists: true, kind: "feature", feature: 11, title: "Feature #11 — stage 3a", updated: 1000, server_time: 1010,
  done: 1, total: 3, now: 1, open: [],
  items: [
    { id: "A.1", label: "review session opens by `a` **or** `b`", detail: "commit ec0d6d9; acceptance", sha: "ec0d6d9", done: true, section: "Acceptance" },
    { id: "A.2", label: "header says `Review · round N`", detail: "", sha: "", done: false, section: "Acceptance" },
    { id: "A.3", label: "<img src=x onerror=alert(1)> stays text", detail: "", sha: "", done: false, section: "Acceptance" },
  ],
};
let working = true;

(async () => {
  let page;
  const rail = () => page.$("#code-ck-rail");
  const panel = () => page.$("#code-preview-panel");
  const box = () => page.$("#code-preview-panel .cp-ck");

  await run("a chat with a checklist gets the button: count, ring and a working dot", async () => {
    page = await load({
      url: "http://owui.test/c/" + CHAT,
      fetch: (call) => {
        if (call.origin === DEPLOY && call.path === "/api/checklist/" + CHAT) return data;
        if (call.origin === DEPLOY && call.path.startsWith("/api/checklist/")) return { exists: false };
        if (call.path === "/api/tasks/chat/" + CHAT) return { task_ids: working ? ["t1"] : [] };
        return undefined;
      },
    });
    await page.waitFor(() => page.visible(rail()), "the checklist button");
    assert(page.text(rail()) === "1/3", "the button's count: " + page.text(rail()));
    assert(rail().getAttribute("aria-label") === "Checklist: 1 of 3 done, working", rail().getAttribute("aria-label"));
    assert(page.visible(page.$("#code-ck-rail .ck-live")), "no working dot while the turn runs");
    const call = page.calls.find((c) => c.origin === DEPLOY && c.path.startsWith("/api/checklist/"));
    assert(call.headers.authorization === "Bearer test-token", "the checklist was asked for without the Open WebUI token");
  });

  await run("clicking it opens the panel on the Checklist tab: doing now, to do, done with evidence", async () => {
    page.click(rail());
    await page.waitFor(() => panel() && panel().classList.contains("open"), "the panel sliding open");
    assert(page.$('.cp-tab[data-tab="checklist"]').classList.contains("on"), "the Checklist tab is not the one in front");
    assert(page.visible(box()), "the checklist is not showing");
    assert(!page.visible(page.$("#code-preview-panel .cp-frame-preview")), "the preview frame still shows");
    assert(rail().classList.contains("on"), "the button does not show that its panel is open");
    const now = page.$(".ck-now", box());
    assert(now && now.classList.contains("live"), "the item in progress does not shimmer");
    assert(/DOING NOW/.test(page.text(box())), "no 'Doing now' heading");
    assert(page.text(now).includes("A.2") && page.$("code", now) && page.text(page.$("code", now)) === "Review · round N",
           "the item in progress (with its code span) is wrong: " + page.text(now));
    const todo = page.$$(".ck-row:not(.done)", box()).map((r) => page.text(r));
    assert(todo.some((t) => t.includes("A.3")) && !todo.some((t) => t.includes("A.2")), "to do: " + JSON.stringify(todo));
    const done = page.$$(".ck-row.done", box()).map((r) => page.text(r));
    assert(done.length === 1 && done[0].includes("A.1") && done[0].includes("ec0d6d9"), "done: " + JSON.stringify(done));
    const a1 = page.$(".ck-row.done", box());
    assert(page.$("b", a1) && page.text(page.$("b", a1)) === "or" && !page.text(a1).includes("**"), "**bold** was not drawn: " + page.text(a1));
    assert(!page.$("img", box()), "checklist text became markup");
    assert(page.text(page.$('.cp-tab[data-tab="checklist"]')) === "Checklist1/3", page.text(page.$('.cp-tab[data-tab="checklist"]')));
  });

  await run("a box ticked since the last look pops in, and 'now' moves on", async () => {
    data = JSON.parse(JSON.stringify(data));
    data.items[1].done = true;
    data.done = 2;
    data.now = 2;
    page.click("#code-preview-panel .cp-reload");
    await page.waitFor(() => page.$$(".ck-row.done", box()).length === 2, "the second ticked box");
    const a2 = page.$$(".ck-row.done", box()).find((r) => page.text(r).includes("A.2"));
    assert(a2 && page.$(".ck-pop", a2), "the newly ticked box did not pop in");
    const a1 = page.$$(".ck-row.done", box()).find((r) => page.text(r).includes("A.1"));
    assert(!page.$(".ck-pop", a1), "a box ticked earlier popped in again");
    assert(page.text(page.$(".ck-now", box())).includes("A.3"), "'now' did not move to the next box");
    assert(page.text(rail()) === "2/3", "the button's count did not follow: " + page.text(rail()));
  });

  await run("when the turn is not running: 'Next up', no shimmer, no dot", async () => {
    working = false;
    page.click("#code-preview-panel .cp-reload");
    await page.waitFor(() => /NEXT UP/.test(page.text(box())), "'Next up'");
    assert(!page.$(".ck-now", box()).classList.contains("live"), "the item still shimmers");
    assert(!page.visible(page.$("#code-ck-rail .ck-live")), "the working dot still shows");
  });

  await run("Done folds away and comes back", async () => {
    page.click(page.$(".ck-fold", box()));
    await page.waitFor(() => page.$$(".ck-row.done", box()).length === 0, "Done folded");
    assert(page.$(".ck-fold", box()).getAttribute("aria-expanded") === "false", "aria-expanded did not follow");
    page.click(page.$(".ck-fold", box()));
    await page.waitFor(() => page.$$(".ck-row.done", box()).length === 2, "Done open again");
  });

  await run("the button closes the panel again", async () => {
    page.click(rail());
    await page.waitFor(() => !panel().classList.contains("open"), "the panel closing");
    assert(!rail().classList.contains("on"), "the button still looks open");
  });

  await run("a chat without a checklist has no button and no tab", async () => {
    page.window.history.pushState({}, "", "/c/another-chat-0001");
    await page.waitFor(() => !page.visible(rail()), "the button going away");
    assert(page.$('.cp-tab[data-tab="checklist"]').hidden, "the Checklist tab is still offered");
    assert(page.errors.length === 0, page.errors.join("\n"));
  });

  finish();
})();
