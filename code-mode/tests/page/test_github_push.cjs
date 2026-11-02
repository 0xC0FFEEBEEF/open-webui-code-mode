/* Stage 3 of the GitHub button: a chat's own project into an empty repo, pushing to a branch, the versions, rolling
 * back. Page script + the real pipe + a fake GitHub + a fake VM that really runs git (see pipe_bridge.py).
 *
 * New controls:
 *   [data-gh="chat-hint"]                     in a chat whose project is not on GitHub: how to put it there
 *   [data-gh="push"]  [data-gh="history"]     on the chat line, next to Pull
 *   push view:    [data-gh="files"]  [data-gh="commit-message"]  [data-gh="suggest-message"] (the spark: a model
 *                 names the version)  [data-gh="branch"] (a select; "__new__" = new branch)
 *                 [data-gh="new-branch"]  [data-gh="do-push"]  [data-gh="pushed-link"]  [data-gh="back"]
 *   versions:     [data-gh="commit"] rows with data-sha, [data-gh="rollback"], [data-gh="rollback-yes"],
 *                 [data-gh="rollback-cancel"], and [data-gh="info"] for what happened
 */
const fs = require("fs");
const path = require("path");
const { load, run, assert, finish } = require("./harness.cjs");

const gh = (name) => '#github-panel [data-gh="' + name + '"]';
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let page, bridge, folder;
  const chip = () => page.$("#github-repo-chip");
  const work = (name) => page.$('[data-gh="work"]', page.$(gh("repo") + '[data-repo="' + name + '"]'));
  const remote = async (repo, branch) => (await bridge.control({ remote: { repo, branch } })).remote;
  const reopen = async () => {
    if (page.visible("#github-panel")) {
      page.press(page.document.body, "Escape");
      await page.waitFor(() => !page.visible("#github-panel"), "the panel closed");
    }
    page.click("#github-pill");
    await page.waitFor(() => page.$$(gh("repo")).length === 3, "the repos", 8000);
  };

  await run("a chat with a project of its own: sign in, and the panel says how to put it on GitHub", async () => {
    page = await load({ bridge: true, url: "http://owui.test/c/chat-p" });
    bridge = page.bridge;
    await bridge.control({ github: { interval: 0.05 } });
    folder = path.join((await bridge.info()).projects, "chats", "chat-p");
    fs.mkdirSync(folder, { recursive: true });
    fs.writeFileSync(path.join(folder, "app.py"), "print('v1')\n");
    fs.writeFileSync(path.join(folder, ".env"), "API_KEY=secret\n");
    await page.waitFor(() => page.$("#github-pill"), "#github-pill");
    page.click("#github-pill");
    await page.waitFor(() => page.visible(gh("client-id")), "the Client ID field");
    page.type(gh("client-id"), "Iv1.testclient");
    page.click(gh("save-client"));
    await page.waitFor(() => page.visible(gh("sign-in")), "the Sign in button");
    page.click(gh("sign-in"));
    await page.waitFor(() => page.$$(gh("repo")).length === 3, "the repos", 8000);
    await page.waitFor(() => page.visible(gh("chat-hint")) && /not on GitHub/.test(page.text(gh("chat-hint"))), "the hint for a project that is not on GitHub");
    assert(!page.visible(gh("push")), "nothing to push to yet");
    noErrors(page);
  });

  await run("its project is refused by a repo with content, and taken by an empty one", async () => {
    page.click(work("octo/alpha"));
    await page.waitFor(() => page.visible(gh("error")) && /is not empty/.test(page.text(gh("error"))), "the reason in [data-gh=error]", 8000);
    assert(!chip(), "nothing recorded");
    page.click(work("octo/gamma"));
    await page.waitFor(() => chip() && /octo\/gamma/.test(page.text(chip())), "the chip with octo/gamma", 8000);
    await page.waitFor(() => page.visible(gh("info")) && /Press Push/.test(page.text(gh("info"))), "what to do next, in [data-gh=info]");
    await page.waitFor(() => page.visible(gh("push")) && /octo\/gamma · .* · 1 changed file/.test(page.text(gh("chat-repo"))), "the chat line with Push");
    assert(fs.readFileSync(path.join(folder, "app.py"), "utf8") === "print('v1')\n", "the project's files are untouched");
    assert((await bridge.info()).key_log.length === 0, "no key was needed to link an empty repo");
    noErrors(page);
  });

  await run("Push: shows what will be saved, and the first push of an empty repo goes to its default branch", async () => {
    page.click(gh("push"));
    await page.waitFor(() => page.visible(gh("do-push")), "the push view");
    const files = page.text(gh("files"));
    assert(/app\.py/.test(files) && !/\.env/.test(files), "the changed files are listed, without .env: " + files);
    assert(page.$(gh("branch")).value === "main", "an empty repo gets its default branch, got " + page.$(gh("branch")).value);
    assert(!page.visible(gh("new-branch")), "no new-branch box unless New branch is picked");
    page.click(gh("suggest-message")); // the spark: the small model names the version from the changes
    await page.waitFor(() => page.$(gh("commit-message")).value === "Tidy the launch page", "the box filled with the model's line, cleaned up", 8000);
    const asked = (await bridge.info()).asked;
    assert(asked.length === 1 && /app\.py/.test(asked[0][1]) && !/API_KEY/.test(asked[0][1]), "the model was shown the changed files, not .env");
    page.type(gh("commit-message"), "First version");
    page.click(gh("do-push"));
    await page.waitFor(() => page.visible(gh("pushed-link")), "the link to the pushed branch", 8000);
    assert(/\/octo\/gamma\/tree\/main$/.test(page.$(gh("pushed-link")).getAttribute("href")), "it links to the branch on GitHub");
    const there = await remote("octo/gamma", "main");
    assert(there.files.join() === "app.py" && there.subject === "First version", "GitHub has the project, without .env: " + JSON.stringify(there));
    page.click(gh("back"));
    await page.waitFor(() => /octo\/gamma · main · no changes/.test(page.text(gh("chat-repo"))), "the chat line after the push");
    noErrors(page);
  });

  await run("the next push offers a new branch by default, so main is not changed by accident", async () => {
    fs.writeFileSync(path.join(folder, "app.py"), "print('v2')\n");
    fs.writeFileSync(path.join(folder, "extra.txt"), "more\n");
    await reopen();
    await page.waitFor(() => /2 changed files/.test(page.text(gh("chat-repo"))), "2 changed files");
    page.click(gh("push"));
    await page.waitFor(() => page.visible(gh("do-push")), "the push view");
    assert(page.$(gh("branch")).value === "__new__", "New branch is preselected when the chat is on the default branch");
    assert(page.visible(gh("new-branch")) && /^code-mode-\d{4}-\d{4}$/.test(page.$(gh("new-branch")).value), "with a suggested name, got " + page.$(gh("new-branch")).value);
    const names = page.$$("option", page.$(gh("branch"))).map((o) => o.value).join();
    assert(names === "main,__new__", "the repo's branches and New branch are offered: " + names);
    page.type(gh("new-branch"), "feature-x");
    page.type(gh("commit-message"), "Second version");
    page.click(gh("do-push"));
    await page.waitFor(() => page.visible(gh("pushed-link")) && /Pushed to feature-x/.test(page.text(gh("pushed"))), "pushed to feature-x", 8000);
    const side = await remote("octo/gamma", "feature-x");
    assert(side.files.sort().join() === "app.py,extra.txt" && side.subject === "Second version", "feature-x has the new version");
    assert((await remote("octo/gamma", "main")).files.join() === "app.py", "main is as it was");
    noErrors(page);
  });

  await run("Versions: the list, and rolling back restores the files as a new version", async () => {
    page.click(gh("back"));
    await page.waitFor(() => page.visible(gh("history")), "the Versions button");
    page.click(gh("history"));
    await page.waitFor(() => page.$$(gh("commit")).length === 2, "two versions");
    const rows = page.$$(gh("commit"));
    assert(/Second version/.test(page.text(rows[0])) && /First version/.test(page.text(rows[1])), "newest first");
    assert(!rows[0].querySelector('[data-gh="rollback"]'), "the version the project is at has no Roll back");
    page.click(rows[1].querySelector('[data-gh="rollback"]'));
    await page.waitFor(() => page.visible(gh("rollback-yes")), "the question, in the row");
    page.click(gh("rollback-cancel"));
    await page.waitFor(() => !page.visible(gh("rollback-yes")), "the question gone");
    assert(fs.readFileSync(path.join(folder, "app.py"), "utf8") === "print('v2')\n", "Cancel changes nothing");
    page.click(page.$$(gh("commit"))[1].querySelector('[data-gh="rollback"]'));
    await page.waitFor(() => page.visible(gh("rollback-yes")), "the question again");
    page.click(gh("rollback-yes"));
    await page.waitFor(() => page.visible(gh("info")) && /Rolled back to/.test(page.text(gh("info"))), "what happened, in [data-gh=info]", 8000);
    assert(fs.readFileSync(path.join(folder, "app.py"), "utf8") === "print('v1')\n", "app.py is the first version again");
    assert(!fs.existsSync(path.join(folder, "extra.txt")), "the file added later is gone");
    assert(fs.existsSync(path.join(folder, ".env")), "files git does not track are left alone");
    await page.waitFor(() => page.$$(gh("commit")).length === 3 && /^Roll back to /.test(page.text(page.$$(gh("commit"))[0])), "the roll back as a new version on top");
    assert(/not pushed/.test(page.text(page.$$(gh("commit"))[0])), "it is not on GitHub until pushed");
    assert((await remote("octo/gamma", "feature-x")).subject === "Second version", "GitHub is unchanged by a roll back");
    noErrors(page);
  });

  await run("every key was removed again, pushes used write keys, and the token never reached the VM", async () => {
    const info = await bridge.info();
    assert(Object.values(info.keys).every((k) => k.length === 0), "deploy keys left on GitHub: " + JSON.stringify(info.keys));
    assert(JSON.stringify(info.key_log) === JSON.stringify([["add", "octo/gamma", false], ["delete", "octo/gamma", false], ["add", "octo/gamma", false], ["delete", "octo/gamma", false]]),
      "two pushes, each with its own write key: " + JSON.stringify(info.key_log));
    assert(!JSON.stringify(info.vm_commands).includes(bridge.token), "the token appeared in a command on the VM");
  });

  finish();
})();
