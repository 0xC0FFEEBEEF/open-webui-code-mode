/* Messages queued while a reply runs are kept on cryoai (2026-10-30, after chat 0c682232 lost its queue: Open WebUI
 * kept it in the tab, the laptop slept through the end of a four-hour turn and then rebooted). The page script moves
 * each message Open WebUI queues to code-deploy's /api/queue and out of Open WebUI's queue, shows the list, sends it
 * from this tab once the reply has finished, holds it on Stop, and reloads a tab that missed what cryoai sent.
 * Open WebUI's queue box, Stop button and Send are faked the way 0.8.10 draws them. */
const { load, run, assert, finish, sleep } = require("./harness.cjs");

const CHAT = "0c682232-21f6-455d-9f69-f5844ab822de";
const DEPLOY = "http://owui.test:8095";
const STOP_D = "M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12z";
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

// ---- a fake code-deploy queue, the way code-deploy.py answers
const srv = { items: [], claims: {}, down: false, n: 0, calls: [] };
function queueApi(call) {
  if (call.origin !== DEPLOY || !call.path.startsWith("/api/queue")) return undefined;
  srv.calls.push({ method: call.method, path: call.path, body: call.body });
  if (srv.down) return { status: 500, error: "down" };
  const b = call.body || {};
  if (call.path === "/api/queue" && call.method === "GET") {
    return { items: srv.items.filter((it) => it.chat_id === CHAT).map((it) => Object.assign({}, it)) };
  }
  if (call.path === "/api/queue" && call.method === "POST") {
    const same = srv.items.find((it) => it.lid === b.lid);
    if (same) return { item: same };
    const it = { id: "q" + ++srv.n, lid: b.lid, chat_id: b.chat, text: b.text, mode: b.mode, extras: b.extras,
                 status: b.held ? "held" : "queued", note: "", at: Date.now() / 1000, send_now: false };
    srv.items.push(it);
    return { item: it };
  }
  if (call.path === "/api/queue/hold") {
    let n = 0;
    srv.items.forEach((it) => { if (it.status === "queued" && !it.send_now) { it.status = "held"; n++; } });
    return { held: n };
  }
  if (call.path === "/api/queue/claim") {
    if (Object.keys(srv.claims).length) return { claim: null };
    const mine = srv.items.filter((it) => it.chat_id === b.chat && it.status === "queued");
    if (!mine.length) return { claim: null };
    const cid = "c" + ++srv.n;
    srv.claims[cid] = { by: b.by, ids: mine.map((it) => it.id) };
    mine.forEach((it) => { it.status = "claimed"; });
    const last = mine[mine.length - 1];
    return { claim: cid, text: mine.map((it) => it.text).join("\n\n"), mode: last.mode, extras: last.extras };
  }
  if (call.path.startsWith("/api/queue/claim/")) {
    const cid = call.path.split("/").pop();
    const c = srv.claims[cid];
    if (!c) return { status: 404, error: "no such claim" };
    delete srv.claims[cid];
    srv.items.filter((it) => c.ids.includes(it.id)).forEach((it) => {
      it.status = b.status === "requeue" ? "queued" : b.status;
      it.note = b.note || "";
      if (b.status === "sent") { it.sent_by = c.by; it.finished_at = Date.now() / 1000; }
    });
    return { finished: c.ids.length };
  }
  const id = call.path.split("/").pop();
  const it = srv.items.find((x) => x.id === id);
  if (!it) return { status: 404, error: "no such message" };
  if (b.op === "cancel") {
    if (it.status === "claimed") return { status: 409, error: "it is being sent right now" };
    it.status = "cancelled";
  } else if (b.op === "send_now") {
    it.status = "queued";
    it.send_now = true;
  }
  return { item: it };
}

// ---- Open WebUI's side of the page
const owui = { running: true, tasks: true, sent: [], deleted: [] };
function setRunning(page, on) {
  owui.running = on;
  owui.tasks = on;
  const right = page.$(".right-group");
  const send = page.$("#send-message-button");
  const stop = page.$("#fake-stop");
  if (on && !stop) {
    if (send) send.remove();
    const b = page.document.createElement("button");
    b.id = "fake-stop";
    b.type = "button";
    b.innerHTML = '<svg viewBox="0 0 24 24"><path d="' + STOP_D + '"></path></svg>';
    b.addEventListener("click", () => setRunning(page, false)); // Open WebUI's stopResponse
    right.appendChild(b);
  } else if (!on && stop) {
    stop.remove();
    const s = page.document.createElement("button");
    s.id = "send-message-button";
    s.type = "button";
    s.textContent = "send";
    s.addEventListener("click", () => { // Open WebUI takes the message and empties the box
      const box = page.$("#chat-input");
      owui.sent.push(box.textContent);
      box.textContent = "";
    });
    right.appendChild(s);
  }
  const last = page.$("#message-11111111-1111-4111-8111-111111111111");
  if (last) last.innerHTML = on ? "working" : '<button class="regenerate-response-button">regenerate</button>';
}
function owuiQueue(page, text, withFile) { // Open WebUI 0.8.10 queues a message: its box right before the message box
  const mic = page.$("#message-input-container");
  let box = page.$("#owui-queue");
  if (!box) {
    box = page.document.createElement("div");
    box.id = "owui-queue";
    mic.parentNode.insertBefore(box, mic);
  }
  const row = page.document.createElement("div");
  row.className = "flex items-center gap-2 px-2 py-1.5";
  row.innerHTML = '<div class="shrink-0">→</div><div class="flex-1 min-w-0 flex items-center gap-2">' +
    (withFile ? '<div class="flex items-center gap-1 shrink-0"><span>notes.pdf</span></div>' : "") +
    '<p class="text-sm truncate"></p></div><div class="flex items-center gap-1 shrink-0">' +
    '<button type="button">now</button><button type="button">edit</button><button type="button">del</button></div>';
  row.querySelector("p").textContent = text;
  row.querySelectorAll("button")[2].addEventListener("click", () => { owui.deleted.push(text); row.remove(); });
  box.appendChild(row);
  return row;
}
const ours = (page) => page.$$("#code-queue .cq-row:not(.cq-banner)");
const status = (page, i) => page.text(page.$$("#code-queue .cq-row:not(.cq-banner) .cq-st")[i || 0]);

async function openChat(extra) {
  const page = await load({
    url: "http://owui.test/c/" + CHAT,
    fetch: (call) => {
      const q = queueApi(call);
      if (q !== undefined) return q;
      if (call.path === "/api/tasks/chat/" + CHAT) return { task_ids: owui.tasks ? ["t1"] : [] };
      return extra ? extra(call) : undefined;
    },
  });
  page.window.localStorage.setItem("codeToggle:mode:" + CHAT, "code");
  page.window.localStorage.setItem("codeToggle:pick:" + CHAT, "slerm-turing.qwen3.6-35b-a3b-ud-q5kxl");
  page.$("#messages").innerHTML = '<div id="message-00000000-0000-4000-8000-000000000000">fix the bugs</div>' +
    '<div id="message-11111111-1111-4111-8111-111111111111">working</div>';
  page.reloads = 0;
  page.window.__codeToggle.reload = () => { page.reloads++; };
  setRunning(page, true);
  return page;
}

(async () => {
  let page;

  await run("a message Open WebUI queues moves to cryoai and out of Open WebUI's queue", async () => {
    page = await openChat();
    await page.waitFor(() => srv.calls.some((c) => c.method === "GET" && c.path === "/api/queue"), "the first poll");
    await sleep(50);
    owuiQueue(page, "also check the mobile layout");
    await page.waitFor(() => srv.items.length === 1, "the message on cryoai");
    const it = srv.items[0];
    assert(it.text === "also check the mobile layout" && it.mode === "code", "text and mode: " + JSON.stringify(it));
    assert(it.extras.code_model === "slerm-turing.qwen3.6-35b-a3b-ud-q5kxl", "the chat's picked model goes along: " + JSON.stringify(it.extras));
    assert(owui.deleted.length === 1 && !page.$("#owui-queue").children.length, "taken out of Open WebUI's queue");
    await page.waitFor(() => ours(page).length === 1 && status(page) === "Sends when this reply finishes", "our list");
    assert(page.text(page.$("#code-queue .cq-text")) === "also check the mobile layout", "the text is shown");
    assert(page.$("#code-queue").nextElementSibling === page.$("#message-input-container"), "it sits right above the message box");
    assert(page.$$("#code-queue button").every((b) => b.type === "button"), "its buttons never submit Open WebUI's form");
    assert(!page.window.localStorage.getItem("codeQueue:outbox") || page.window.localStorage.getItem("codeQueue:outbox") === "[]",
      "nothing left in the browser once cryoai has it");
    noErrors(page);
  });

  await run("a message with a file stays in Open WebUI's own queue", async () => {
    owuiQueue(page, "see the attached notes", true);
    await sleep(300);
    assert(srv.items.length === 1 && owui.deleted.length === 1, "it was not moved");
    assert(page.$("#owui-queue").children.length === 1, "Open WebUI still shows it");
    page.$("#owui-queue").innerHTML = "";
    noErrors(page);
  });

  await run("when cryoai doesn't take it, it stays in this browser and goes once cryoai answers", async () => {
    // The step before left a poll due in 300 ms. Landing just after cryoai went down, it failed, and the page then
    // (rightly) left Open WebUI's queue alone: this check timed out 4 runs in 5. Go down right after a fresh poll
    // was answered, with the next one seconds away, so the page meets a cryoai that answered and now refuses.
    const polls = () => srv.calls.filter((c) => c.method === "GET" && c.path === "/api/queue").length;
    const asked = polls();
    await page.waitFor(() => polls() > asked, "a fresh poll of cryoai's queue", 9000);
    await sleep(50);
    srv.down = true;
    owuiQueue(page, "second thing");
    await page.waitFor(() => owui.deleted.length === 2, "out of Open WebUI's queue (the page holds it)");
    const kept = JSON.parse(page.window.localStorage.getItem("codeQueue:outbox"));
    assert(kept.length === 1 && kept[0].text === "second thing", "kept in this browser: " + JSON.stringify(kept));
    await page.waitFor(() => /Not saved on cryoai yet/.test(status(page, 1)), "says it is not on cryoai yet", 9000);
    srv.down = false;
    await page.waitFor(() => srv.items.length === 2, "sent once cryoai answers", 9000);
    await page.waitFor(() => JSON.parse(page.window.localStorage.getItem("codeQueue:outbox")).length === 0, "the browser copy dropped");
    noErrors(page);
  });

  await run("Stop holds the queue; Send now stops the reply and sends just that one", async () => {
    page.click("#fake-stop");
    await page.waitFor(() => srv.calls.some((c) => c.path === "/api/queue/hold"), "the hold");
    await page.waitFor(() => status(page) === "Held because you stopped the reply", "shown as held");
    await sleep(2500);
    assert(owui.sent.length === 0, "nothing was sent after Stop: " + JSON.stringify(owui.sent));
    setRunning(page, true); // the user sends something else meanwhile; a reply runs again
    const btn = page.$$("#code-queue .cq-row")[1].querySelector('[data-cq-act="now"]');
    page.click(btn);
    await page.waitFor(() => owui.sent.length === 1, "the Send now message sent", 8000);
    assert(owui.sent[0] === "second thing", "just that one went: " + JSON.stringify(owui.sent));
    assert(!owui.running, "the reply was stopped first");
    await page.waitFor(() => srv.items[1].status === "sent" && srv.items[1].sent_by === "tab:" + srv.items[1].sent_by.slice(4),
      "finished as sent by this tab");
    assert(srv.items[0].status === "held", "the other one stays held");
    noErrors(page);
  });

  await run("when the reply finishes, this tab types the queue into the box and sends it", async () => {
    owui.sent = [];
    setRunning(page, true);
    srv.items[0].status = "queued";
    srv.items.push({ id: "q90", chat_id: CHAT, text: "and then the docs", mode: "code", extras: {}, status: "queued",
                     note: "", at: Date.now() / 1000 });
    await page.waitFor(() => ours(page).length === 2, "both listed");
    await sleep(2500);
    assert(owui.sent.length === 0, "nothing sent while the reply runs");
    setRunning(page, false);
    await page.waitFor(() => owui.sent.length === 1, "sent once it finished", 9000);
    assert(owui.sent[0] === "also check the mobile layout\n\nand then the docs", "one message, in order: " + JSON.stringify(owui.sent));
    await page.waitFor(() => srv.items.every((it) => it.status === "sent"), "both finished as sent");
    await page.waitFor(() => !page.$("#code-queue"), "the list goes when nothing waits");
    noErrors(page);
  });

  await run("Edit puts a queued message back in the box; Delete removes it", async () => {
    setRunning(page, true);
    srv.items.push({ id: "q91", chat_id: CHAT, text: "rename the button", mode: "code", extras: {}, status: "queued", at: 1 });
    srv.items.push({ id: "q92", chat_id: CHAT, text: "never mind this", mode: "code", extras: {}, status: "queued", at: 2 });
    await page.waitFor(() => ours(page).length === 2, "listed at the next poll (every 6 s while a reply runs)", 9000);
    page.click(page.$$("#code-queue .cq-row")[1].querySelector('[data-cq-act="del"]'));
    await page.waitFor(() => srv.items.find((x) => x.id === "q92").status === "cancelled", "deleted on cryoai");
    page.click(page.$$("#code-queue .cq-row")[0].querySelector('[data-cq-act="edit"]'));
    await page.waitFor(() => page.text("#chat-input") === "rename the button", "back in the box");
    assert(srv.items.find((x) => x.id === "q91").status === "cancelled", "and off the queue");
    await page.waitFor(() => !page.$("#code-queue"), "nothing listed");
    page.$("#chat-input").textContent = "";
    noErrors(page);
  });

  await run("a tab that missed what cryoai sent while it was away loads the chat again", async () => {
    srv.items.push({ id: "q93", chat_id: CHAT, text: "sent while you slept", mode: "code", extras: {}, status: "sent",
                     sent_by: "code-wake", message_id: "22222222-2222-4222-8222-222222222222",
                     finished_at: Date.now() / 1000 + 1, at: 3 });
    await page.waitFor(() => page.reloads === 1, "a reload", 9000);
    noErrors(page);
  });

  await run("a tab stuck on Stop after the reply ended (it slept through the end) reloads to send the queue", async () => {
    const p2 = await openChat();
    p2.window.sessionStorage.clear();
    srv.items = [{ id: "q94", chat_id: CHAT, text: "queued before the laptop slept", mode: "code", extras: {},
                   status: "queued", at: 4 }];
    owui.tasks = false; // the turn ended long ago; this page never heard
    await p2.waitFor(() => ours(p2).length === 1, "listed");
    assert(owui.running, "the page still shows Stop");
    await p2.waitFor(() => p2.reloads === 1, "a reload once the page has been stuck for a while", 40000);
    assert(owui.sent.length === 0 || owui.sent.every((t) => t !== "queued before the laptop slept"), "not sent from the stuck page");
    noErrors(p2);
  });

  finish();
})();
