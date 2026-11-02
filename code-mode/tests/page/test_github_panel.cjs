/* Stage 1 of the GitHub button, clicked through end to end: page script + the real pipe + a fake GitHub.
 * Written by the reviewer as the acceptance check. Do not change it: make it pass.
 *
 * The controls it looks for (attributes on the elements the panel builds):
 *   #github-pill                       the button, inside the message bar group that holds #input-menu-button
 *   #github-panel                      the panel (hidden or absent while closed)
 *   [data-gh="client-id"]  [data-gh="save-client"]         first run: the field and its button
 *   [data-gh="sign-in"]                                    starts the sign-in
 *   [data-gh="user-code"]  [data-gh="open-github"]         the one-time code; the button copies it and opens GitHub
 *   [data-gh="search"]                                     filters the list as you type
 *   [data-gh="repo"]  with data-repo="owner/name" and data-private="true|false"     one row per repo
 *   [data-gh="visibility"]                                 inside a row: the Private/Public switch
 *   [data-gh="confirm-yes"]  [data-gh="confirm-cancel"]    the inline "Make public?" question
 *   [data-gh="new-name"]  [data-gh="new-desc"]  [data-gh="new-private"] (checkbox)  [data-gh="create"]
 *   [data-gh="sign-out"]
 *   [data-gh="error"]                                      one line for errors, visible only when there is one
 */
const { load, run, assert, finish } = require("./harness.cjs");

const gh = (name) => '#github-panel [data-gh="' + name + '"]';
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let page, bridge;
  const row = (name) => page.$(gh("repo") + '[data-repo="' + name + '"]');
  const shown = (p) => p.$$(gh("repo")).filter((r) => p.visible(r));
  const onGitHub = async (name) => (await bridge.info()).repos.find((r) => r.full_name === name);

  await run("the button sits in the message bar, panel closed", async () => {
    page = await load({ bridge: true });
    bridge = page.bridge;
    // Fast sign-in polls (the page must wait the `interval` the pipe passes on, in seconds, fractions allowed),
    // and GitHub pages of 2 so the 3 repos span two pages.
    await bridge.control({ github: { interval: 0.05, slow_down_step: 0.05, max_per_page: 2 } });
    const pill = await page.waitFor(() => page.$("#github-pill"), "#github-pill");
    assert(page.$("#input-menu-button").parentElement.contains(pill), "#github-pill must be in the group that holds #input-menu-button (effortGroup())");
    assert(page.visible(pill), "#github-pill is hidden");
    assert(!page.visible("#github-panel"), "the panel must stay closed until the button is clicked");
    noErrors(page);
  });

  await run("first run: asks for the Client ID", async () => {
    page.click("#github-pill");
    await page.waitFor(() => page.visible(gh("client-id")), "the Client ID field (the pipe has none saved)");
    assert(!page.visible(gh("sign-in")), "no Sign in button before a Client ID is saved");
    page.type(gh("client-id"), "Iv1.testclient");
    page.click(gh("save-client"));
    await page.waitFor(() => page.visible(gh("sign-in")), "the Sign in button after saving the Client ID");
    noErrors(page);
  });

  await run("sign-in: shows the code, opens GitHub from the button, waits, then lists the repos", async () => {
    await bridge.control({ github: { slow_down: 1, pending: 2 } });
    page.click(gh("sign-in"));
    await page.waitFor(() => page.text(gh("user-code")) === "WDJB-MJHT", "the one-time code WDJB-MJHT in [data-gh=user-code]");
    page.click(gh("open-github"));
    await page.waitFor(() => page.opened.some((u) => u.endsWith("/login/device")), "the verification page opened in a new tab");
    await page.waitFor(() => page.clipboard.includes("WDJB-MJHT"), "the code copied by the same button");
    await page.waitFor(() => shown(page).length === 3, "3 repos listed after one slow_down and two pending polls (they span two GitHub pages)", 8000);
    assert(/octo/.test(page.text("#github-panel")), "the panel should say who is signed in (octo)");
    const names = shown(page).map((r) => r.getAttribute("data-repo")).join();
    assert(names === "octo/alpha,octo/beta,octo/gamma", "rows carry data-repo, in GitHub's order (latest push first); got " + names);
    noErrors(page);
  });

  await run("search filters the list and keeps the cursor in the box", async () => {
    const box = page.$(gh("search"));
    page.type(box, "alp");
    await page.waitFor(() => shown(page).length === 1, "one row left for 'alp'");
    assert(page.$(gh("search")) === box, "the search box was rebuilt while typing (the cursor would jump out): keep the same element");
    assert(page.document.activeElement === box, "the search box lost focus while typing");
    page.type(box, "");
    await page.waitFor(() => shown(page).length === 3, "all rows back");
    noErrors(page);
  });

  await run("making a repo public asks first; making it private does not", async () => {
    assert(row("octo/alpha").getAttribute("data-private") === "true", "rows carry data-private");
    page.click(page.$('[data-gh="visibility"]', row("octo/alpha")));
    await page.waitFor(() => page.visible(gh("confirm-yes")), "the inline 'Make public?' question");
    assert((await onGitHub("octo/alpha")).private === true, "nothing may change before Yes");
    page.click(gh("confirm-cancel"));
    await page.waitFor(() => !page.visible(gh("confirm-yes")), "the question gone after Cancel");
    await page.sleep(150);
    assert((await onGitHub("octo/alpha")).private === true, "Cancel must leave the repo private");
    page.click(page.$('[data-gh="visibility"]', row("octo/alpha")));
    await page.waitFor(() => page.visible(gh("confirm-yes")), "the question again");
    page.click(gh("confirm-yes"));
    await page.waitFor(async () => (await onGitHub("octo/alpha")).private === false, "octo/alpha public on GitHub");
    await page.waitFor(() => row("octo/alpha") && row("octo/alpha").getAttribute("data-private") === "false", "the row showing Public");
    page.click(page.$('[data-gh="visibility"]', row("octo/beta")));
    await page.waitFor(async () => (await onGitHub("octo/beta")).private === true, "octo/beta private on GitHub, no question asked");
    await page.waitFor(() => row("octo/beta") && row("octo/beta").getAttribute("data-private") === "true", "the row showing Private");
    assert(!page.visible(gh("confirm-yes")), "making a repo private needs no question");
    noErrors(page);
  });

  await run("new repository: private unless unticked, description kept, GitHub's error shown in the panel", async () => {
    assert(page.$(gh("new-private")).checked === true, "Private must be ticked by default");
    page.type(gh("new-name"), "new-thing");
    page.type(gh("new-desc"), "made by the test");
    page.click(gh("create"));
    await page.waitFor(() => row("octo/new-thing"), "the new repo in the list");
    const made = await onGitHub("octo/new-thing");
    assert(made.private === true, "the new repo must be private");
    assert(made.description === "made by the test", "the description did not reach GitHub");
    page.type(gh("new-name"), "new-thing");
    page.click(gh("create"));
    await page.waitFor(() => page.visible(gh("error")) && /already exists/i.test(page.text(gh("error"))),
      "GitHub's own message ('name already exists on this account') in [data-gh=error]");
    noErrors(page);
  });

  await run("Esc and a click outside close the panel", async () => {
    page.press(page.document.body, "Escape");
    await page.waitFor(() => !page.visible("#github-panel"), "the panel closed by Esc");
    page.click("#github-pill");
    await page.waitFor(() => page.visible("#github-panel"), "the panel open again");
    page.click("#messages");
    await page.waitFor(() => !page.visible("#github-panel"), "the panel closed by a click outside it");
    noErrors(page);
  });

  await run("a second tab opens straight to the repos; a dead token leads back to Sign in", async () => {
    const tab = await load({ bridge });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    tab.click("#github-pill");
    await tab.waitFor(() => shown(tab).length === 4, "the 4 repos with no first-run or sign-in step (the panel asks `status` when it opens)");
    await bridge.control({ github: { revoked: true } });
    tab.press(tab.document.body, "Escape");
    await tab.waitFor(() => !tab.visible("#github-panel"), "the panel closed");
    tab.click("#github-pill");
    await tab.waitFor(() => tab.visible(gh("sign-in")), "the Sign in button once GitHub answers 401");
    const saved = JSON.stringify((await bridge.info()).state_file || {});
    assert(!saved.includes(bridge.token), "a dead token must be forgotten");
    assert(saved.includes("Iv1.testclient"), "the Client ID must be kept when the token is forgotten");
    noErrors(tab);
    await bridge.control({ github: { revoked: false } });
  });

  await run("sign out forgets the token and keeps the Client ID", async () => {
    page.click("#github-pill");
    await page.waitFor(() => page.visible(gh("sign-in")), "Sign in (the token was revoked in the other tab)");
    page.click(gh("sign-in"));
    await page.waitFor(() => shown(page).length === 4, "signed in again", 8000);
    page.click(gh("sign-out"));
    await page.waitFor(() => page.visible(gh("sign-in")), "the Sign in button after signing out");
    assert(!page.visible(gh("client-id")), "signing out must not ask for the Client ID again");
    const info = await bridge.info();
    const saved = JSON.stringify(info.state_file || {});
    assert(!saved.includes(bridge.token), "sign out must forget the token");
    assert(saved.includes("Iv1.testclient"), "sign out must keep the Client ID");
    assert(info.state_mode === "0o600", "the state file's mode is " + info.state_mode + ", want 0o600");
    noErrors(page);
  });

  await run("someone who is not an admin gets a plain message", async () => {
    await bridge.control({ role: "user" });
    const tab = await load({ bridge });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    tab.click("#github-pill");
    await tab.waitFor(() => tab.visible(gh("error")) && /admin/i.test(tab.text(gh("error"))), "an admins-only message in [data-gh=error]");
    noErrors(tab);
    await bridge.control({ role: "admin" });
  });

  finish();
})();
