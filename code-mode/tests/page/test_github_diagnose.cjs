/* Diagnose: the GitHub panel compares a chat's project with its repository and says what is out of step, with the
 * panel action that answers each finding (Pull / Push / Versions) or Ask the agent when the fix is git work.
 * Page script + the REAL pipe + a fake GitHub + a fake VM that really runs git (see pipe_bridge.py).
 *
 * Diagnose is read-only: the only thing it sends through git is a fetch, under a read-only deploy key; it never
 * commits, pulls, pushes or rolls anything back by itself.
 *
 * New controls:
 *   [data-gh="diagnose"]          the button, in the chat's line (and on the hint line of a chat not on GitHub)
 *   [data-gh="finding"]           one row per finding, [data-finding="<id>"], with the action button in it
 *   [data-gh="diag-pull"]         Pull from the diagnose view, then compare again
 *   [data-gh="diagnose-again"]    compare again
 *   [data-gh="ask-agent"]         send the pipe's brief to this chat's coding agent
 */
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const { load, run, assert, finish } = require("./harness.cjs");

const gh = (name) => '#github-panel [data-gh="' + name + '"]';
const row = (p, id) => p.$(gh("finding") + '[data-finding="' + id + '"]');
const btn = (p, id, name) => (row(p, id) || { querySelector: () => null }).querySelector('[data-gh="' + name + '"]');
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

(async () => {
  let page, bridge;
  const folder = async (chat) => path.join((await bridge.info()).projects, "chats", chat);
  const git = (cwd, ...args) => cp.execFileSync("git", ["-c", "user.name=octo", "-c", "user.email=o@example.com", ...args],
                                                { cwd, stdio: ["ignore", "pipe", "pipe"] });
  const title = (p) => p.text("#github-panel .gh-title");

  /** Open the panel and the diagnose view, and wait until the comparison is on screen. */
  const diagnose = async (p) => {
    if (!p.visible("#github-panel")) p.click("#github-pill");
    await p.waitFor(() => p.visible(gh("diagnose")), "the Diagnose button in the panel", 8000);
    p.click(gh("diagnose"));
    await p.waitFor(() => p.visible(gh("diagnose-again")), "the diagnose view (Check again)", 10000);
  };
  const findings = (p) => p.$$(gh("finding"));

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
    await page.waitFor(() => page.$$(gh("repo")).length === 3, "3 repos in the panel", 8000);
    noErrors(page);
  });

  await run("a chat in step with its repository: the Diagnose button, and nothing to fix", async () => {
    const made = await bridge.control({ attach: { chat: "diag-a", repo: "octo/alpha" } });
    assert(made.attach && made.attach.ok, "attached octo/alpha to chat diag-a: " + JSON.stringify(made.attach));
    const tab = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    tab.click("#github-pill");
    await tab.waitFor(() => tab.visible(gh("chat-repo")), "the chat's repo line", 8000);
    assert(tab.visible(gh("diagnose")), "the chat's line has a Diagnose button next to Pull and Push");
    await diagnose(tab);
    assert(/Diagnose/.test(title(tab)) && /octo\/alpha/.test(title(tab)), "the view is headed " + title(tab));
    assert(findings(tab).length === 0, "nothing was found, so there are no findings rows, got " + findings(tab).length);
    assert(/The project and GitHub match\./.test(tab.text("#github-panel")), "the panel says it matches");
    const info = await bridge.info();
    assert(info.key_log.every((k) => k[2] === true), "diagnose only ever uses read-only keys: " + JSON.stringify(info.key_log));
    noErrors(tab);
  });

  await run("behind GitHub: the finding names GitHub's commit, and its Pull brings it in and compares again", async () => {
    const tab = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await tab.waitFor(() => tab.$("#github-pill"), "#github-pill");
    await bridge.control({ push: { repo: "octo/alpha", file: "from-github.txt", text: "hello" } });
    await diagnose(tab);
    await tab.waitFor(() => row(tab, "behind"), "a finding for the commit GitHub has", 10000);
    const text = tab.text(row(tab, "behind"));
    assert(/GitHub has 1 commit\(s\) on main/.test(text), "what is missing here: " + text);
    assert(/add from-github\.txt/.test(text), "the finding names the commit: " + text);
    assert(btn(tab, "behind", "diag-pull"), "the finding carries the panel's Pull action");
    assert(!btn(tab, "behind", "diag-push"), "it does not offer Push: this project has nothing to send");
    tab.click(btn(tab, "behind", "diag-pull"));
    const dir = await folder("diag-a");
    await tab.waitFor(() => fs.existsSync(path.join(dir, "from-github.txt")), "the commit pulled into the chat's folder", 10000);
    assert(fs.readFileSync(path.join(dir, "from-github.txt"), "utf8") === "hello", "the file is what GitHub had");
    await tab.waitFor(() => findings(tab).length === 0 && /The project and GitHub match\./.test(tab.text("#github-panel")),
      "the view compares again and now finds nothing", 10000);
    assert(/Pulled\. What is left:/.test(tab.text('[data-gh="info"]')), "the view says what it just did: " + tab.text('[data-gh="info"]'));
    assert(tab.visible(gh("back")), "the view has its own way back to the chat's line");
    noErrors(tab);
  });

  await run("ahead of GitHub: the finding names the project's commit and opens the panel's Push", async () => {
    const dir = await folder("diag-a");
    fs.writeFileSync(path.join(dir, "local-note.txt"), "work done here\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-q", "-m", "add the local note");
    const at = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "ahead"), "a finding for the commit that is not on GitHub", 10000);
    assert(row(at, "behind") === null, "GitHub has nothing new, so there is no behind finding");
    const text = at.text(row(at, "ahead"));
    assert(/1 commit\(s\) not yet on GitHub/.test(text) && /add the local note/.test(text), "what is waiting to go: " + text);
    assert(btn(at, "ahead", "diag-push"), "the finding carries the panel's Push action");
    at.click(btn(at, "ahead", "diag-push"));
    await at.waitFor(() => /Push octo\/alpha/.test(title(at)), "the panel's own push view", 8000);
    at.click(gh("back"));
    await at.waitFor(() => at.visible(gh("chat-repo")), "back to the chat's line");
    noErrors(at);
  });

  await run("the two sides have diverged: the finding names both, says why Pull and Push are both refused, and hands it to the agent", async () => {
    const dir = await folder("diag-a");
    await bridge.control({ push: { repo: "octo/alpha", file: "second-from-github.txt", text: "again" } });
    fs.writeFileSync(path.join(dir, "second-note.txt"), "more work here\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-q", "-m", "add the second note");
    const at = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "diverged"), "a finding for the divergence", 10000);
    const text = at.text(row(at, "diverged"));
    assert(/main has diverged/.test(text), "what it says: " + text);
    assert(/add the second note/.test(text), "it names this project's commit: " + text);
    assert(/add second-from-github\.txt/.test(text), "it names GitHub's commit: " + text);
    assert(/Push is refused/.test(text), "it says why the panel's own buttons cannot fix this: " + text);
    assert(!btn(at, "diverged", "diag-pull") && !btn(at, "diverged", "diag-push"),
      "it offers neither Pull nor Push: neither is safe here");
    assert(btn(at, "diverged", "ask-agent"), "it offers Ask the agent");
    noErrors(at);
    // Ask the agent: the panel steps aside, and the brief the pipe wrote lands in this chat's message box.
    at.document.getElementById("messages").innerHTML =
      '<div id="message-00000000-0000-4000-8000-000000000000"><button class="regenerate-response-button">regenerate</button></div>';
    at.click(btn(at, "diverged", "ask-agent"));
    await at.waitFor(() => !at.visible("#github-panel"), "the panel closes so the user watches the chat");
    await at.waitFor(() => at.text("#chat-input").length > 0, "the brief typed into the chat's message box", 12000);
    const brief = at.text("#chat-input");
    assert(/GitHub check: octo\/alpha branch main/.test(brief), "the brief says what was compared: " + brief.slice(0, 120));
    assert(/Your commits, not on GitHub: [0-9a-f]+ add the second note/.test(brief), "it carries this project's commit: " + brief);
    assert(/GitHub's commits, not here: [0-9a-f]+ add second-from-github\.txt/.test(brief), "and GitHub's commit: " + brief);
    assert(/git merge origin\/main/.test(brief), "it names the safe way through: " + brief);
    assert(/Never rewrite history/.test(brief), "it sets the limit: " + brief);
    assert(/GitHub panel/.test(brief), "it tells the agent pushing and pulling stay in the panel: " + brief);
    noErrors(at);
    // and again, in a page that has execCommand the way a browser does (Open WebUI's editor is what gets the text)
    at.document.getElementById("chat-input").textContent = "";
    at.window.document.execCommand = (cmd, ui, value) => {
      const box = at.document.getElementById("chat-input");
      if (cmd !== "insertText" || !box) return false;
      box.textContent = (box.textContent ? box.textContent + "\n" : "") + String(value);
      box.dispatchEvent(new at.window.Event("input", { bubbles: true }));
      return true;
    };
    at.click("#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "diverged"), "the divergence again", 10000);
    at.click(btn(at, "diverged", "ask-agent"));
    await at.waitFor(() => at.text("#chat-input").length > 0, "the brief typed in by the keystroke path", 12000);
    assert(/GitHub check: octo\/alpha branch main/.test(at.text("#chat-input")), "the same brief, the same way");
    noErrors(at);
  });

  await run("Ask the agent against an editor that swallows every keystroke: it says so and leaves no blank entry", async () => {
    const at = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "diverged"), "a finding for the divergence", 10000);
    at.document.getElementById("messages").innerHTML =
      '<div id="message-00000000-0000-4000-8000-000000000000"><button class="regenerate-response-button">regenerate</button></div>';
    const logs = [];
    at.window.console.log = (...a) => logs.push(a.map(String).join(" "));
    at.window.document.execCommand = () => false; // an editor that takes nothing, the way the deployed failure did
    let sent = 0;
    at.document.getElementById("send-message-button").addEventListener("click", () => { sent++; });
    let enters = 0;
    at.document.getElementById("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") enters++; });
    at.click(btn(at, "diverged", "ask-agent"));
    await at.waitFor(() => logs.some((l) => /nothing was sent/.test(l)),
                     "the page giving up out loud instead of pressing Enter into an empty box", 12000);
    assert(at.text("#chat-input") === "", "the box is left exactly as it was: no blank pasted entry");
    assert(sent === 0, "the send button was never clicked on a box without the text");
    assert(enters === 0, "no Enter was dispatched into an editor that does not hold the message");
    noErrors(at);
  });

  await run("the paste fallback lands in pieces under Open WebUI's 1000-character limit (one longer paste becomes a file entry, not text)", async () => {
    const dir = await folder("diag-a");
    await bridge.control({ push: { repo: "octo/alpha", file: "third-from-github.txt", text: "third" } });
    fs.writeFileSync(path.join(dir, "third-note.txt"), "more local work\n");
    git(dir, "add", "-A");
    git(dir, "commit", "-q", "-m", "add the third note"); // both sides grow, so the brief gets long
    const at = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "diverged"), "a finding for the divergence", 10000);
    at.document.getElementById("messages").innerHTML =
      '<div id="message-00000000-0000-4000-8000-000000000000"><button class="regenerate-response-button">regenerate</button></div>';
    class FakeDataTransfer {
      constructor() { this.data = {}; }
      setData(type, value) { this.data[type] = value; }
      getData(type) { return this.data[type] || ""; }
    }
    class FakeClipboardEvent extends at.window.Event {
      constructor(type, init) { super(type, init); this.clipboardData = (init && init.clipboardData) || null; }
    }
    at.window.DataTransfer = FakeDataTransfer;
    at.window.ClipboardEvent = FakeClipboardEvent;
    at.window.document.execCommand = () => false; // the editor takes no keystrokes: only the paste path can land the text
    const box = at.document.getElementById("chat-input");
    const chunks = [];
    box.addEventListener("paste", (e) => {
      chunks.push(e.clipboardData.getData("text/plain"));
      box.textContent += chunks[chunks.length - 1];
    });
    let sentWith = null;
    at.document.getElementById("send-message-button").addEventListener("click", () => { sentWith = box.textContent; });
    at.click(btn(at, "diverged", "ask-agent"));
    await at.waitFor(() => chunks.length > 0, "the paste fallback reaching the editor", 12000);
    await at.waitFor(() => sentWith !== null, "the send button clicked with the text in the box", 12000);
    const full = chunks.join("");
    assert(/GitHub check: octo\/alpha branch main/.test(full), "the pieces put this chat's brief back together");
    assert(full.length > 900, "this brief is long enough to cross the limit (" + full.length + " characters)");
    assert(chunks.length >= 2, "and it came in " + chunks.length + " pieces, not one paste");
    assert(Math.max(...chunks.map((c) => c.length)) <= 900,
           "every piece stays under Open WebUI's 1000-character file-entry limit");
    assert(/branch main/.test(sentWith), "Open WebUI's send button was clicked only once the brief was in the box");
    noErrors(at);
  });

  await run("Ask the agent on Open WebUI's ProseMirror editor: typed text it puts back at once is never trusted; the brief is pasted and sent", async () => {
    // Measured headless on the live Open WebUI 0.8.10 page (2026-10-19): typed in line by line in one go
    // (insertParagraph + insertText), the brief IS in the box for an instant, so a check made at once said "landed"
    // and skipped the paste; on the next tick ProseMirror puts its own state back and leaves empty paragraphs - the
    // blank entry the user still saw after the first fix. A paste of the same text stays and enables Send.
    // This editor does exactly that.
    const at = await load({ bridge, url: "http://owui.test/c/diag-a" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    await diagnose(at);
    await at.waitFor(() => row(at, "diverged"), "a finding for the divergence", 10000);
    at.document.getElementById("messages").innerHTML =
      '<div id="message-00000000-0000-4000-8000-000000000000"><button class="regenerate-response-button">regenerate</button></div>';
    class FakeDataTransfer {
      constructor() { this.data = {}; }
      setData(type, value) { this.data[type] = value; }
      getData(type) { return this.data[type] || ""; }
    }
    class FakeClipboardEvent extends at.window.Event {
      constructor(type, init) { super(type, init); this.clipboardData = (init && init.clipboardData) || null; }
    }
    at.window.DataTransfer = FakeDataTransfer;
    at.window.ClipboardEvent = FakeClipboardEvent;
    const box = at.document.getElementById("chat-input");
    box.classList.add("tiptap", "ProseMirror");
    let state = ""; // what ProseMirror holds; the DOM is put back to it after every typed change
    let typed = 0;
    const render = () => {
      const lines = state ? state.split("\n") : [""];
      box.innerHTML = lines.map((l) => "<p>" + l.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</p>").join("");
    };
    let burst = null; // the execCommand calls of one tick, judged when the tick ends
    at.window.document.execCommand = (cmd, ui, value) => {
      if (cmd !== "insertParagraph" && cmd !== "insertText") return false;
      typed++;
      if (cmd === "insertParagraph") box.appendChild(at.document.createElement("p"));
      else box.appendChild(at.document.createTextNode(value)); // in the DOM at once, as measured
      if (!burst) {
        burst = [];
        at.window.setTimeout(() => {
          const calls = burst;
          burst = null;
          if (calls.length === 1 && calls[0][0] === "insertText") { // one line typed in: taken ("Let's build it.")
            state += calls[0][1];
            render();
            return;
          }
          const blanks = calls.filter((c) => c[0] === "insertParagraph").length; // multi-line: put back, blanks kept
          render();
          for (let i = 0; i < blanks; i++) box.appendChild(at.document.createElement("p"));
        }, 0);
      }
      burst.push([cmd, value]);
      return true;
    };
    const chunks = [];
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      chunks.push(e.clipboardData.getData("text/plain"));
      state += chunks[chunks.length - 1];
      render();
    });
    let sentWith = null;
    at.document.getElementById("send-message-button").addEventListener("click", () => { sentWith = box.textContent; });
    at.click(btn(at, "diverged", "ask-agent"));
    await at.waitFor(() => sentWith !== null, "the send button clicked with the brief in the box", 12000);
    assert(/GitHub check: octo\/alpha branch main/.test(sentWith), "the brief was in the box when Send was clicked");
    assert(chunks.length >= 1 && /GitHub check: octo\/alpha/.test(chunks.join("")), "the brief came in as a paste");
    assert(Math.max(...chunks.map((c) => c.length)) <= 900, "every pasted piece stays under the 1000-character limit");
    assert(typed === 0, "nothing was typed line by line into ProseMirror (" + typed + " execCommand calls)");
    noErrors(at);
  });

  await run("a chat whose project is not on GitHub: the hint line has Diagnose too, and it says how to get there", async () => {
    const dir = path.join((await bridge.info()).projects, "chats", "diag-e");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "app.py"), "print(1)\n");
    const before = (await bridge.info()).key_log.length;
    const at = await load({ bridge, url: "http://owui.test/c/diag-e" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    at.click("#github-pill");
    await at.waitFor(() => at.visible(gh("chat-hint")), "the hint line: how to put this project on GitHub", 8000);
    assert(at.visible(gh("diagnose")), "the hint line carries the Diagnose button too");
    await diagnose(at);
    assert(/Diagnose/.test(title(at)), "the view is headed " + title(at));
    await at.waitFor(() => row(at, "unlinked"), "a finding saying the project is not on GitHub yet", 10000);
    assert(/not on GitHub yet/.test(at.text(row(at, "unlinked"))), "what it says: " + at.text(row(at, "unlinked")));
    assert(!row(at, "uncommitted") && !row(at, "behind") && !row(at, "ahead"), "nothing is invented about a repo it does not have");
    assert((await bridge.info()).key_log.length === before, "it did not go near GitHub with a key for a project that has no repo");
    noErrors(at);
  });

  await run("a chat with no project of its own has nothing to compare, so the panel offers no Diagnose", async () => {
    const at = await load({ bridge, url: "http://owui.test/c/diag-none" });
    await at.waitFor(() => at.$("#github-pill"), "#github-pill");
    at.click("#github-pill");
    await at.waitFor(() => at.$$(gh("repo")).length === 3, "the repo list", 8000);
    assert(!at.visible(gh("chat-repo")), "the panel has no chat line: this chat has no project");
    assert(!at.visible(gh("diagnose")), "and no Diagnose button");
    assert(at.githubOps().every((o) => o.op !== "diagnose"), "the page asked for no comparison");
    noErrors(at);
  });

  await run("every key diagnose used was read-only and removed again, and the token never reached the VM", async () => {
    const info = await bridge.info();
    assert(info.key_log.every((k) => k[2] === true), "a key was write-capable: " + JSON.stringify(info.key_log));
    const adds = info.key_log.filter((k) => k[0] === "add").length;
    assert(adds > 0 && adds === info.key_log.filter((k) => k[0] === "delete").length,
      "each key added was deleted again: " + JSON.stringify(info.key_log));
    assert(Object.values(info.keys).every((k) => k.length === 0), "deploy keys left on GitHub: " + JSON.stringify(info.keys));
    assert(!JSON.stringify(info.vm_commands).includes(bridge.token), "the token appeared in a command on the VM");
  });

  finish();
})();
