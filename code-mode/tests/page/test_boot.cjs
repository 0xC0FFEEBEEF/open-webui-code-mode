/* The page script loads into the fake Open WebUI page and shows its top-bar pill. Also the smallest example of a
 * page test: see harness.cjs for what `page` offers. */
const { load, run, assert, finish } = require("./harness.cjs");

(async () => {
  await run("the page script loads and shows the Chat | Code pill", async () => {
    const page = await load();
    const pill = await page.waitFor(() => page.$("nav #code-toggle-pill"), "the Chat | Code pill in the top bar");
    assert(page.visible(pill), "the pill is hidden");
    assert(page.errors.length === 0, "page errors:\n  " + page.errors.join("\n  "));
  });
  finish();
})();
