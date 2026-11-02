/* The approval / question box (pipe 0.28.0). Open WebUI's pop-up went only to the tab that sent the message and was
 * gone after a reload or a click away, while the turn waited forever (2026-10-26). Now the page asks the pipe what
 * waits (code_ask list) and shows it: the open chat's as a dialog, other chats' as a notice with a link. Later / Esc
 * fold it into a pill; a reload shows it again; answers go back with code_ask answer. */
const { load, run, assert, finish } = require("./harness.cjs");

const CHAT = "873c87b1-9f9d-4127-9fed-0795922ddf3a";
const OTHER = "00086b8e-ead1-4a11-bda0-01bf85c2a543";
const PERM = { id: "ask_1", chat_id: CHAT, title: "Rust/React recreation", kind: "permission", t: 1, direct: false,
  data: { action: "shell", resources: ["rm -rf gh-test", "git init -q --bare /tmp/gh-test/remote.git"] } };
const QUESTION = { id: "ask_2", chat_id: CHAT, title: "Rust/React recreation", kind: "question", t: 2, direct: false,
  data: { title: "Data", question: "Which data?", message: "Which data?\n\n1. Kp\n2. Everything", placeholder: "Kp",
          options: ["Kp", "Everything"], descriptions: ["just the index", ""] } };
const ELSEWHERE = { id: "ask_3", chat_id: OTHER, title: "Space weather", kind: "permission", t: 3, direct: false,
  data: { action: "shell", resources: ["git push origin main"] } };
const ORPHAN = { id: "per_9", sid: "ses_old", chat_id: CHAT, title: "Rust/React recreation", kind: "permission", t: 4, direct: true,
  data: { action: "shell", resources: ["rm -rf target"] } };
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let asks = [], answers = [], answerReply = { ok: true };
  const fetch = (call) => {
    if (call.path === "/api/chat/completions" && call.body && call.body.code_ask) {
      if (!call.body.code_github) return { ok: false, error: "no code_github guard" };
      const a = call.body.code_ask;
      if (a.op === "list") return { ok: true, asks };
      if (a.op === "answer") {
        answers.push(a);
        if (answerReply.ok || answerReply.gone) asks = asks.filter((x) => x.id !== a.id);
        return answerReply;
      }
    }
    return undefined;
  };
  let page = await load({ url: "http://owui.test/c/" + CHAT, fetch });
  const dlg = () => page.$("#code-qb");
  const poke = () => page.window.dispatchEvent(new page.window.Event("focus"));   // the page asks again on focus

  await run("an approval for this chat shows as a dialog with what the agent wants to run", async () => {
    asks = [PERM];
    poke();
    await page.waitFor(() => dlg() && page.visible(dlg()), "the dialog");
    assert(dlg().getAttribute("data-kind") === "permission");
    assert(page.text(page.$(".cmp-title", dlg())) === "Approval needed", page.text(dlg()));
    assert(/Rust\/React recreation/.test(page.text(".cmp-sub")), "names the chat");
    assert(page.text(".qb-act") === "shell");
    const lines = page.$$(".qb-res div").map((d) => d.textContent);
    assert(lines.length === 2 && lines[0] === "rm -rf gh-test", JSON.stringify(lines));
    assert(page.text(".qb-yes") === "Allow once" && page.text(".qb-no") === "Deny…");
    noErrors(page);
  });

  await run("Later folds it into a pill above the message box; Show brings it back", async () => {
    page.click(".qb-later");
    assert(!dlg(), "the dialog goes");
    await page.waitFor(() => page.$("#message-input-container #code-qb-pill"), "the pill");
    assert(/Approval waiting · shell: rm -rf gh-test/.test(page.text("#code-qb-pill")), page.text("#code-qb-pill"));
    page.click("#code-qb-pill .cmp-pill-b");
    await page.waitFor(() => dlg(), "the dialog again");
    assert(!page.$("#code-qb-pill"), "no pill while the dialog shows");
  });

  await run("Esc does the same, and a reload shows the dialog again (nothing is lost)", async () => {
    page.press(page.document.body, "Escape");
    assert(!dlg() && page.$("#code-qb-pill"), "Esc = Later");
    page.close();
    page = await load({ url: "http://owui.test/c/" + CHAT, fetch });
    await page.waitFor(() => dlg() && page.visible(dlg()), "the dialog after the reload");
  });

  await run("Allow once answers it and the dialog closes", async () => {
    page.click(".qb-yes");
    await page.waitFor(() => answers.length === 1, "the answer");
    assert(answers[0].op === "answer" && answers[0].id === "ask_1" && answers[0].value.allow === true, JSON.stringify(answers));
    assert(!("sid" in answers[0]), "a turn's own ask needs no session");
    await page.waitFor(() => !dlg(), "the dialog to close");
    noErrors(page);
  });

  await run("Deny… asks for an optional note; with one the agent carries on, without one it stops", async () => {
    asks = [Object.assign({}, PERM, { id: "ask_4" })];
    poke();
    await page.waitFor(() => dlg(), "the dialog");
    page.click(".qb-no");
    assert(page.visible(".qb-deny") && page.text(".qb-no") === "Back", "the note box");
    assert(page.text(".qb-stop") === "Deny and stop", page.text(".qb-row"));
    page.type(".qb-txt", "keep gh-test, use a temp dir");
    assert(page.text(".qb-stop") === "Deny with this note");
    page.click(".qb-stop");
    await page.waitFor(() => answers.length === 2, "the answer");
    assert(answers[1].value.allow === false && answers[1].value.message === "keep gh-test, use a temp dir", JSON.stringify(answers[1]));
  });

  await run("a question shows its choices as buttons, the first one suggested; a click answers with it", async () => {
    asks = [QUESTION];
    poke();
    await page.waitFor(() => dlg() && dlg().getAttribute("data-kind") === "question", "the question");
    assert(page.text(page.$(".cmp-title", dlg())) === "Data" && page.text(".qb-q") === "Which data?");
    const opts = page.$$(".qb-opt");
    assert(opts.length === 2 && /SUGGESTED/.test(opts[0].textContent) && /just the index/.test(opts[0].textContent), page.text(".qb-opts"));
    page.click(opts[1]);
    await page.waitFor(() => answers.length === 3, "the answer");
    assert(answers[2].id === "ask_2" && answers[2].value === "2", JSON.stringify(answers[2]));
  });

  await run("its own answer goes with Enter; Skip leaves the question for the next message", async () => {
    asks = [Object.assign({}, QUESTION, { id: "ask_5" })];
    poke();
    await page.waitFor(() => dlg() && page.$(".qb-in"), "the question");
    page.type(".qb-in", "only the aurora forecast");
    page.press(".qb-in", "Enter");
    await page.waitFor(() => answers.length === 4, "the typed answer");
    assert(answers[3].value === "only the aurora forecast");
    asks = [Object.assign({}, QUESTION, { id: "ask_6" })];
    poke();
    await page.waitFor(() => dlg(), "the next question");
    page.click(".qb-skip");
    await page.waitFor(() => answers.length === 5, "the skip");
    assert(answers[4].value === false);
  });

  await run("another chat's approval shows a notice that opens that chat; × hides it", async () => {
    asks = [ELSEWHERE];
    poke();
    await page.waitFor(() => page.$("#code-qb-note .qb-note"), "the notice");
    assert(!dlg(), "no dialog for another chat");
    assert(/Approval waiting in “Space weather”/.test(page.text("#code-qb-note")), page.text("#code-qb-note"));
    assert(page.$("#code-qb-note a").getAttribute("href") === "/c/" + OTHER);
    page.click("#code-qb-note button");
    assert(!page.$("#code-qb-note"), "hidden");
  });

  await run("one no turn waits on any more is answered with its session; an error keeps the dialog", async () => {
    asks = [ORPHAN];
    answerReply = { ok: false, error: "opencode refused it (HTTP 500)" };
    poke();
    await page.waitFor(() => dlg(), "the dialog");
    page.click(".qb-yes");
    await page.waitFor(() => page.visible(".qb-err"), "the error");
    assert(/refused it/.test(page.text(".qb-err")) && dlg(), "it stays to try again");
    assert(answers[5].id === "per_9" && answers[5].sid === "ses_old", JSON.stringify(answers[5]));
    answerReply = { ok: false, error: "This was already answered.", gone: true };
    page.click(".qb-yes");
    await page.waitFor(() => !dlg(), "gone = closed");
    noErrors(page);
  });

  finish();
})();
