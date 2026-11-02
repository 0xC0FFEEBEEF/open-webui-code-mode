/* The model a Code turn runs on. A continue or regenerate of a reply saved under the Code pipe's own name must not
 * reach the pipe as the pick: the pipe would otherwise be handed its own name instead of the picked model. */
const { load, run, assert, finish } = require("./harness.cjs");

const CODE_MODEL = "code_mode.ornith-1.5-35b-a3b";
const QWEN = "slerm-turing.qwen3.6-35b-a3b-ud-q5kxl";
const answerTurns = (call) => (call.path === "/api/chat/completions" ? { ok: true } : undefined);
const noErrors = (page) => assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));

// A chat turn the way Open WebUI sends it; the page script rewrites it before it leaves.
async function send(page, chatId, model) {
  await page.window.fetch("/api/chat/completions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, id: "m" + page.calls.length, session_id: "s1", model, stream: true,
      messages: [{ role: "user", content: "hi" }] }),
  });
  return page.calls.filter((c) => c.path === "/api/chat/completions" && c.body).pop().body;
}

(async () => {
  await run("a turn sent under the pipe's own name runs on the chat's last picked model", async () => {
    const page = await load({ fetch: answerTurns });
    page.window.localStorage.setItem("codeToggle:mode:chat-1", "code");
    const first = await send(page, "chat-1", QWEN);
    assert(first.model === CODE_MODEL && first.code_model === QWEN,
      "a normal Code turn goes to the pipe with the picked model, got " + JSON.stringify([first.model, first.code_model]));
    const again = await send(page, "chat-1", CODE_MODEL);
    assert(again.model === CODE_MODEL && again.code_model === QWEN,
      "a continue under the pipe's name still carries the picked model, got " + again.code_model);
    noErrors(page);
  });

  await run("with no pick known for the chat, no model is sent (the pipe then asks)", async () => {
    const page = await load({ fetch: answerTurns });
    page.window.localStorage.setItem("codeToggle:mode:chat-2", "code");
    const body = await send(page, "chat-2", CODE_MODEL);
    assert(body.model === CODE_MODEL && !body.code_model, "code_model should be empty, got " + body.code_model);
    noErrors(page);
  });
  finish();
})();
