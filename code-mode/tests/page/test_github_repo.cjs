/* Stage 2 of the GitHub button: pick a repo and work on it in a chat. Page script + the real pipe + a fake GitHub
 * + a fake VM that really runs git (see pipe_bridge.py).
 *
 * New controls:
 *   [data-gh="work"]         in a repo row: start a chat that works on it
 *   #github-repo-chip        after the button: the picked repo (with [data-gh="drop-repo"], the ×) or the chat's repo
 *   [data-gh="chat-repo"]    first line of the panel in a chat that works on a repo, with [data-gh="pull"]
 */
const fs = require("fs");
const path = require("path");
const { load, run, assert, finish } = require("./harness.cjs");

const gh = (name) => '#github-panel [data-gh="' + name + '"]';
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
const CODE_MODEL = "code_mode.ornith-1.5-35b-a3b";
const PICKED = "slerm-turing.qwen3.8-27b";

(async () => {
  let page, bridge;
  const stored = (p, key) => p.window.localStorage.getItem(key);
  const chip = (p) => p.$("#github-repo-chip");
  const work = (p, name) => p.$('[data-gh="work"]', p.$(gh("repo") + '[data-repo="' + name + '"]'));
  // A chat message the way Open WebUI sends it; the page script rewrites it before it leaves.
  const send = async (p, chatId, userTurns) => {
    const messages = [];
    for (let i = 0; i < userTurns; i++) messages.push({ role: "user", content: "message " + i }, { role: "assistant", content: "ok" });
    messages.pop();
    await p.window.fetch("/api/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, id: "m" + userTurns, session_id: "s1", model: PICKED, stream: true, messages }),
    });
    return p.calls.filter((c) => c.path === "/api/chat/completions" && c.body && !c.body.code_github).pop().body;
  };
  const openRepos = async (p, count) => {
    if (!p.visible("#github-panel")) p.click("#github-pill");
    await p.waitFor(() => p.$$(gh("repo")).length === count, count + " repos in the panel", 8000);
  };

  await run("sign in", async () => {
    page = await load({ bridge: true });
    bridge = page.bridge;
    await bridge.control({ github: { interval: 0.05 } });
    await page.waitFor(() => page.$("#github-pill"), "#github-pill");
    page.click("#github-pill");
    await page.waitFor(() => page.visible(gh("client-id")), "the Client ID field");
    page.type(gh("client-id"), "Iv1.testclient");
    page.click(gh("save-client"));
    await page.waitFor(() => page.visible(gh("sign-in")), "the Sign in button");
    page.click(gh("sign-in"));
    await openRepos(page, 3);
    assert(!page.visible(gh("chat-repo")), "no chat line on a new chat");
    noErrors(page);
  });

  await run("on a new chat, Work on it keeps the pick for the first message: panel closes, pill goes to Code, chip shows", async () => {
    assert(work(page, "octo/alpha"), 'each repo row has a [data-gh="work"] button');
    page.click(work(page, "octo/alpha"));
    await page.waitFor(() => !page.visible("#github-panel"), "the panel closed");
    await page.waitFor(() => chip(page) && /octo\/alpha/.test(page.text(chip(page))), "the chip with octo/alpha");
    assert(page.$("#github-pill").parentElement.contains(chip(page)), "the chip sits in the message bar, with the button");
    assert(stored(page, "codeToggle:newRepo") === "octo/alpha", "the pick is kept in localStorage codeToggle:newRepo");
    assert(stored(page, "codeToggle:newChat") === "code", "the new chat is set to Code mode");
    assert(page.window.location.pathname === "/", "the user stays where they are");
    noErrors(page);
  });

  await run("the × drops the pick", async () => {
    page.click('#github-repo-chip [data-gh="drop-repo"]');
    await page.waitFor(() => !chip(page), "the chip gone");
    assert(stored(page, "codeToggle:newRepo") === null, "codeToggle:newRepo cleared");
    noErrors(page);
  });

  await run("the first message makes the chat work on the repo; its Code turns carry code_repo", async () => {
    await openRepos(page, 3);
    page.click(work(page, "octo/alpha"));
    await page.waitFor(() => chip(page), "the chip");
    const first = await send(page, "chat-a", 1);
    assert(first.code_repo === "octo/alpha", "the first message must carry code_repo, got " + first.code_repo);
    assert(first.model === CODE_MODEL && first.code_model === PICKED, "it goes to the coding agent, on the picked model");
    assert(stored(page, "codeToggle:repo:chat-a") === "octo/alpha", "the chat remembers its repo");
    assert(stored(page, "codeToggle:mode:chat-a") === "code", "the chat is a Code chat");
    assert(stored(page, "codeToggle:newRepo") === null, "the pick is used up");
    await page.waitFor(() => !chip(page), "the pick's chip gone from the new-chat page");
    const second = await send(page, "chat-a", 2);
    assert(second.code_repo === "octo/alpha", "later Code turns carry it too (the pipe checks the folder each time)");
    const other = await send(page, "chat-b", 1);
    assert(!("code_repo" in other), "a chat that was not started with Work on it carries none");
    noErrors(page);
  });

  await run("a pick is dropped when the next message goes to an existing chat instead", async () => {
    await openRepos(page, 3);
    page.click(work(page, "octo/beta"));
    await page.waitFor(() => chip(page), "the chip");
    const body = await send(page, "chat-c", 2);
    assert(!("code_repo" in body), "an existing chat must not be turned into a repo chat");
    assert(stored(page, "codeToggle:repo:chat-c") === null && stored(page, "codeToggle:newRepo") === null, "nothing kept");
    await page.waitFor(() => !chip(page), "the chip gone");
    noErrors(page);
  });

  await run("in a chat that exists, Work on it clones into THAT chat right away", async () => {
    const tab = await load({ bridge, url: "http://owui.test/c/chat-d" });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    await openRepos(tab, 3);
    tab.click(work(tab, "octo/beta"));
    await tab.waitFor(() => chip(tab) && /octo\/beta/.test(tab.text(chip(tab))), "the chip with octo/beta once it is cloned", 8000);
    assert(tab.window.location.pathname === "/c/chat-d", "the user stays in the chat (no new chat)");
    assert(!tab.$('#github-repo-chip [data-gh="drop-repo"]'), "it is cloned, not a pick: no ×");
    assert(!tab.visible("#github-panel"), "the panel closes when it is done");
    assert(stored(tab, "codeToggle:repo:chat-d") === "octo/beta" && stored(tab, "codeToggle:mode:chat-d") === "code", "the chat works on the repo, in Code mode");
    const folder = path.join((await bridge.info()).projects, "chats", "chat-d");
    assert(fs.existsSync(path.join(folder, "README.md")), "the clone is in this chat's folder (made for it: the chat had none)");
    assert((await send(tab, "chat-d", 2)).code_repo === "octo/beta", "its Code turns carry code_repo");
    noErrors(tab);
  });

  await run("a chat with a project of its own is left alone, and the panel says why", async () => {
    const folder = path.join((await bridge.info()).projects, "chats", "chat-e");
    fs.mkdirSync(folder, { recursive: true });
    fs.writeFileSync(path.join(folder, "app.py"), "print(1)\n");
    const tab = await load({ bridge, url: "http://owui.test/c/chat-e" });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    await openRepos(tab, 3);
    tab.click(work(tab, "octo/beta"));
    await tab.waitFor(() => tab.visible(gh("error")) && /project of its own/.test(tab.text(gh("error"))), "the reason in [data-gh=error]", 8000);
    assert(tab.visible("#github-panel") && tab.text(work(tab, "octo/beta")) === "Work on it", "the panel stays open, the button is usable again");
    assert(!chip(tab) && stored(tab, "codeToggle:repo:chat-e") === null, "nothing is recorded for the chat");
    assert(fs.readdirSync(folder).filter((f) => f !== ".git").join() === "app.py", "the chat's own files are untouched");
    noErrors(tab);
  });

  await run("a chat that works on a repo: chip, status line, and Pull", async () => {
    const made = await bridge.control({ attach: { chat: "chat-a", repo: "octo/alpha" } }); // as its first turn does
    assert(made.attach && made.attach.ok && made.attach.fresh, "the pipe attached the repo: " + JSON.stringify(made.attach));
    const folder = path.join((await bridge.info()).projects, "chats", "chat-a");
    assert(fs.existsSync(path.join(folder, "README.md")), "the clone is in the chat's folder");
    // Another browser: it has no record of this chat. Opening the panel asks the pipe, which looks at the folder.
    const tab = await load({ bridge, url: "http://owui.test/c/chat-a" });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    assert(!chip(tab), "no chip before this browser knows the chat's repo");
    tab.click("#github-pill");
    await tab.waitFor(() => tab.visible(gh("chat-repo")) && /octo\/alpha · main · no changes/.test(tab.text(gh("chat-repo"))),
      'the line "octo/alpha · main · no changes" in [data-gh=chat-repo]', 8000);
    await tab.waitFor(() => chip(tab) && /octo\/alpha/.test(tab.text(chip(tab))), "the chip, now that the browser knows");
    assert(!tab.$('#github-repo-chip [data-gh="drop-repo"]'), "a chat's own repo has no ×");
    await bridge.control({ push: { repo: "octo/alpha", file: "from-github.txt", text: "hello" } });
    tab.click(gh("pull"));
    await tab.waitFor(() => fs.existsSync(path.join(folder, "from-github.txt")), "the pushed file pulled into the chat's folder", 8000);
    await tab.waitFor(() => /no changes/.test(tab.text(gh("chat-repo"))) && tab.text(gh("pull")) === "Pull", "the line refreshed after the pull");
    fs.writeFileSync(path.join(folder, "notes.txt"), "work in progress");
    tab.press(tab.document.body, "Escape");
    await tab.waitFor(() => !tab.visible("#github-panel"), "the panel closed");
    tab.click("#github-pill");
    await tab.waitFor(() => /1 changed file/.test(tab.text(gh("chat-repo"))), '"1 changed file" after an edit');
    assert(!("code_repo" in (await send(tab, "chat-a", 3))), "a Chat-mode message is left alone");
    tab.window.localStorage.setItem("codeToggle:mode:chat-a", "code"); // the pill switched to Code in this browser
    const body = await send(tab, "chat-a", 3);
    assert(body.code_repo === "octo/alpha", "this browser's Code turns in the chat now carry code_repo too");
    noErrors(tab);
  });

  await run("Pull that cannot fast-forward says why, in the panel", async () => {
    const tab = await load({ bridge, url: "http://owui.test/c/chat-b" });
    const made = await bridge.control({ attach: { chat: "chat-b", repo: "octo/gamma" } }); // an empty repo: nothing to pull
    assert(made.attach && made.attach.ok, "attached the empty repo: " + JSON.stringify(made.attach));
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    tab.click("#github-pill");
    await tab.waitFor(() => tab.visible(gh("pull")), "the Pull button");
    tab.click(gh("pull"));
    await tab.waitFor(() => tab.visible(gh("error")) && /Could not pull/.test(tab.text(gh("error"))), "git's reason in [data-gh=error]", 8000);
    assert(tab.text(gh("pull")) === "Pull", "the button is usable again");
    noErrors(tab);
  });

  await run("every key was removed again, and the token never reached the VM", async () => {
    const info = await bridge.info();
    assert(Object.values(info.keys).every((k) => k.length === 0), "deploy keys left on GitHub: " + JSON.stringify(info.keys));
    const adds = info.key_log.filter((k) => k[0] === "add").length;
    assert(adds >= 3 && adds === info.key_log.filter((k) => k[0] === "delete").length, "each key added was deleted: " + JSON.stringify(info.key_log));
    assert(info.key_log.every((k) => k[2] === true), "stage 2 only ever uses read-only keys");
    assert(!JSON.stringify(info.vm_commands).includes(bridge.token), "the token appeared in a command on the VM");
  });

  finish();
})();
