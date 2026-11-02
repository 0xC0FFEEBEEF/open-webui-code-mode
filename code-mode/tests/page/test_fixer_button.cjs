/* The Fixer button in the pill: the wrench opens a box that says which model the fixer runs on, Investigate files a
 * report for this chat (code_fixer op "report", with what was typed) and opens the Fixer tab; while that report runs
 * the wrench pulses and a click shows it. */
const { load, run, assert, finish } = require("./harness.cjs");

const VIEW = "http://ai-sandbox:4098/f/20261020-1200-ab12/" + "c".repeat(32) + "/";

(async () => {
  const ops = [];
  let status = { ok: true, active: "", model: "ornith-1.5-35b-a3b", loaded: false };
  const page = await load({
    url: "http://owui.test/c/chat-1",
    fetch: (call) => {
      if (call.path === "/api/chat/completions" && call.body && call.body.code_fixer) {
        ops.push(call.body.code_fixer);
        if (!call.body.code_github) return { ok: false, error: "no code_github guard" };
        return call.body.code_fixer.op === "status" ? status : { ok: true, view: VIEW, id: "20261020-1200-ab12" };
      }
      return undefined;
    },
  });
  const box = () => page.$("#fixer-ask");

  await run("the wrench sits in the pill on a chat", async () => {
    await page.waitFor(() => page.$("#code-toggle-pill .ct-fix") && page.visible("#code-toggle-pill .ct-fix"), "the Fixer button");
    assert(page.text("#code-toggle-pill .ct-fix") === "Fixer");
  });

  await run("a click opens the box and warns when the run would load another model", async () => {
    page.click("#code-toggle-pill .ct-fix");
    await page.waitFor(() => box() && page.visible(box()), "the box");
    await page.waitFor(() => /loads ornith-1\.5-35b-a3b/.test(page.text(".fx-model")), "the model line");
    assert(ops[0].op === "status" && ops[0].chat_id === "chat-1", JSON.stringify(ops));
    page.click(".fx-cancel");
    assert(!page.visible(box()), "Cancel closes it");
  });

  await run("Investigate files the report and opens the Fixer tab", async () => {
    status = { ok: true, active: "", model: "qwen3.8-27b", loaded: true };
    page.click("#code-toggle-pill .ct-fix");
    await page.waitFor(() => page.text(".fx-model") === "Runs on qwen3.8-27b, already loaded.", "the loaded model line");
    page.type(".fx-text", "The preview stays blank");
    page.click(".fx-go");
    await page.waitFor(() => {
      const f = page.$("#code-preview-panel .cp-frame-fixer");
      return f && f.getAttribute("src") === VIEW;
    }, "the Fixer tab on the new report");
    const report = ops.find((o) => o.op === "report");
    assert(report && report.chat_id === "chat-1" && report.details === "The preview stays blank", JSON.stringify(ops));
    assert(!page.visible(box()), "the box closes");
    assert(page.$("#code-toggle-pill").getAttribute("data-fixing") === "1", "the wrench pulses while it runs");
  });

  await run("while it runs a click shows it, no second report", async () => {
    const before = ops.length;
    page.click("#code-toggle-pill .ct-fix");
    await page.sleep(50);
    assert(ops.length === before && !page.visible(box()), "no box, no call");
    page.window.dispatchEvent(new page.window.MessageEvent("message", {
      origin: "http://ai-sandbox:4098", data: { type: "fixer:status", done: true, verdict: "fixed" },
    }));
    await page.waitFor(() => page.$("#code-toggle-pill").getAttribute("data-fixing") === "0", "the pulse stopping");
    assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
  });
  finish();
})();
