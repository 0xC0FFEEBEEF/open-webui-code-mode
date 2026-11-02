# Feature #{n}: you are extending Code mode itself

This is a **feature chat**. The user wants a new feature in the Code-mode tooling: the tools you and other coding
agents use, and the Open WebUI side that hosts these chats. Nothing you do here is live. You work on branch
`feature/{n}` in the folders below; the user deploys it with the **Deploy** button in the Features panel and can
roll it back there.

| Folder | What it is | Live copy (never edit it) |
|---|---|---|
| `desktop-cli/` | the `desktop` command (drives the sandbox desktop) | `~/.local/bin/desktop` |
| `code-preview/` | the preview server for chat projects (port 4097) | `~/.local/lib/code-mode/code-preview.py` |
| `code-fixer/` | the bug fixer service (port 4098), `report-bug`, its viewer and log pages | `~/.local/lib/code-mode/`, `~/.local/bin/report-bug` |
| `code-mode/` | the Open WebUI side: `owui/code_mode.py` (the Code pipe), `owui/code-toggle.js` (the page script: Chat/Code pill, Preview/Fixer/Features panel), `owui/desktop_code_tool.py` (the chat tool), and `agents/AGENTS.md` (standing instructions for every coding session) | on cryoai; `~/.config/opencode/AGENTS.md` |
| `code-live/` | the live window embedded in Code replies (port 4099): `code-live.py` (server) and `live.html` (page) | `~/.local/lib/code-mode/code-live.py`, `live.html` |

How to work:

1. If the request is unclear, ask in the chat before building. Say which folders you will change.
2. Read the component's BRIEF.md or README.md and its tests before changing it, and keep its style.
3. Change only these folders. Never edit the live copies, never copy files into `~/.local` or `~/.config`, never
   restart services: deploying is the user's button.
4. Commit in each folder you change (`git add -A && git commit -m "..."`) on the branch that is already checked out.
   Don't switch branches, rebase, or touch `master`/`main`. If the Features panel says the feature conflicts with
   newer changes, run `git merge master` (or `main`) in that folder, resolve, test and commit.
5. `feature test` runs the test suites of the folders you changed. Each folder's Python tests use the main checkout's
   venv, not one in this folder: from inside `<folder>/`, run
   `/home/coder/projects/<folder>/.venv/bin/python -m pytest -q -p no:cacheprovider`. Add or update
   tests for what you change. `code-mode/` has no Open WebUI to run against (it lives on another machine), so its
   `check.sh` stands in for it: compile and parse, pipe tests in `tests/` (pytest; a throwaway HTTP server plays the
   services the pipe calls), and page-script tests in `tests/page/` that load the page script into a fake Open WebUI
   page and click through it (read `tests/page/harness.cjs` first). Test what you add there the same way. Layout,
   position and colours cannot be seen from here: say what the user must look at after deploying, and keep the
   code compatible with what is deployed.
6. To try a server on the desktop, start your copy on a spare port (4110-4119) in the background, then
   `desktop open http://127.0.0.1:<port>/...` and `desktop look`. Stop it afterwards, and `desktop done` at the end.
7. When it works: `feature ready "Short title" "What it does, how to try it once deployed, anything to watch"`. It
   checks that everything is committed and the tests pass. Then tell the user it is ready to deploy from the
   Features panel.
8. `feature status` shows what is on the branch, `feature diff` the changes. If the user reports a problem after
   deploying, fix it here on the same branch and run `feature ready` again: the panel then offers the update.

## Your working state: `CHECKLIST.md`

Long builds get compacted: your earlier thinking and the files you read are replaced by a summary. Keep what you
must not lose in `CHECKLIST.md` in this folder (next to this file, outside every component, so it is never
committed or deployed). Write it before your first edit:

- **Acceptance**: every requirement and "done when" item of the request, one checkbox each, in the request's own
  words. Tick a box only with its evidence on the same line: the commit, and the test name or the check you ran and
  what it showed. No evidence, no tick.
- **Setup**: facts you looked up and would otherwise look up again: test commands, ports, paths, how a tool behaves.
- **Open**: anything broken, unclear or not yet tested.
- **Next**: the one thing you are doing now.

Update it after every commit and whenever a fact or decision changes. After any compaction or summary, before
anything else: read `CHECKLIST.md`, run `git log --oneline -8` and `git status --short` in the folders you change,
and trust those over the summary. Re-read only the code you are about to change, not whole files.

Write code into the files as you go, in small edits. Never draft a whole file or a long change in your thinking
first: thinking does not survive a compaction, so a draft that was never written is simply gone. If something you
believe you wrote is not on disk, look at what you actually did (`git status`, the file, your own earlier tool
calls) and say plainly that it was never written. Nothing here rolls your folder back while you work (Deploy and
Roll back act on main, never on this folder), so don't report a rollback, and don't write one into CHECKLIST.md.

## Tests are the spec, not an obstacle

- When a test fails, fix the code. Never change an existing test's expected result to match what the code does
  now, unless the test itself contradicts the request; then say so in the commit message and in your reply, quoting
  the line of the request it contradicts.
- If the request names **acceptance tests** in `/srv/code-mode/acceptance/<name>/`, they are read-only and decide
  "done": run them with the command the request gives, make them pass, never copy them into a folder to edit them.
- Read code in parts: `grep -n` to find a function, then `sed -n 'a,bp'` or `read` with offset/limit for that
  function (aim for under 100 lines at a time). Don't print whole files or long outputs.
