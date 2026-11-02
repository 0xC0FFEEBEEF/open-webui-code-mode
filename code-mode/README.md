# code-mode

The Open WebUI side of Code mode and the coding agent's standing instructions. Deployed by `code-features`
(VM side) and `code-deploy` (cryoai), never by hand.

| File | Goes to |
|---|---|
| `owui/code_mode.py` | Open WebUI function `code_mode` (the Code pipe), cryoai |
| `owui/code-toggle.js` | Open WebUI `/static/loader.js` (Chat \| Code pill, Preview/Fixer/Features panel), cryoai |
| `owui/desktop_code_tool.py` | Open WebUI tool `desktop_code` (use_computer, start_coding, report_bug), cryoai |
| `agents/AGENTS.md` | `/home/coder/.config/opencode/AGENTS.md` (every coding session reads it), VM 129 |
| `agents/opencode-agents/*.md` | `/home/coder/.config/opencode/agents/` (`ask`: the first turn's questions; `review`: the end-of-turn review), VM 129 |

Typing into a chat from outside (2026-10-09): `owui/code-toggle.js` long-polls code-deploy `GET /api/compose` for the
open chat and, once the reply has finished and the box is empty, types a queued message into the message box (about
3 s, so the user sees it land) and sends it, then reports `sent`/`typed`/`failed` back. Admins queue with
`POST /api/compose {chat_id, text, send, mode}` (code-deploy, cryoai); `~/owui-code-mode/tools/compose.py` on cryoai
does it with a token signed in-process. A user typing in the box sends the message back to the queue.

GitHub button (pipe 0.10.0 sign-in and repos, 0.11.x work on a repo, 0.12.0 push, versions, roll back). A button after the thinking pill opens a panel to sign
in to GitHub, list the repos, create one (private unless unticked) and switch one between private and public (going
public asks first). Sign-in is GitHub's device flow with the Client ID of your own OAuth App ("Enable Device Flow"
ticked; the panel explains it on first use), so there is no client secret and no redirect. The pipe does all the
GitHub work: the page calls it with `POST /api/chat/completions` carrying `code_github: {op, ...}` and no chat id,
which Open WebUI answers inline with the pipe's dict (ops are the pipe's `_gh_op_*` methods). The token stays on
cryoai in `~/.config/code-mode/github.json` (mode 0600, valve `GITHUB_STATE`): no op returns it, and nothing hands
it to the VM, where the coding agent could read it. Sign out forgets it there; to revoke it, GitHub > Settings >
Applications > Authorized OAuth Apps. Tests: `tests/test_github.py` (pipe) and `tests/page/test_github_panel.cjs`
(the panel clicked through against the real pipe and `tests/fake_github.py`).

Working on a repo (0.11.0; 0.11.1: in the chat you are in). "Work on it" in a repo row makes the open chat work on
that repo: the panel calls the pipe's `attach` op, which clones it into the chat's project folder at once (making the
folder if the chat never ran the coding agent) and the chat switches to Code. A chat with a project of its own is
refused. On a new chat there is nothing to clone into yet, so the pick waits as a chip (× drops it) and the first
message adopts it. Either way the page keeps `codeToggle:repo:<chat>` and the chat's Code turns carry `code_repo`;
the turn after a clone starts with "Working on ..." and tells the agent the project is new (`.git/code-mode-new`).
Cloning is `_gh_attach`: it makes the folder a clone of the repo, sets the
commit identity to the user's GitHub no-reply address, and keeps `.env`, virtualenvs and `node_modules` out of commits
through `.git/info/exclude`. In such a chat the panel's first line shows repo, branch and changed files (`chat_status`,
read from the folder on the VM) with Pull (`pull`, fast-forward only). Git runs on the VM but the token never goes
there: each operation gets a deploy key for that one repo, made on the VM, added on GitHub and removed from both
right after (`_gh_key`; read-only here, it needs the user to be an admin of the repo). GitHub's host keys come from
its `/meta`, so SSH runs with strict checking. Everything put into a shell command is pattern-checked and quoted.
Tests: `tests/test_github_attach.py` (real git against `fake.make_remotes()`, on `tests/fake_opencode.py`) and
`tests/page/test_github_repo.cjs`.

Pushing, versions, rolling back (0.12.0). A chat with a project of its own can be given a repo that is still empty
("Work on it" there): nothing is cloned or changed, the folder gets the repo as its `origin`, and a push uploads the
project. In a chat that works on a repo the panel's first line has Pull, Push and Versions. Push lists the changed
files (`changes`), takes a name for the version, and offers the repo's branches or a new one: an empty
repo gets its default branch, a chat on a branch of its own keeps it, and from the default branch a new branch is
preselected so main is never changed by accident. `push` commits what is uncommitted, switches to the target branch
(made when new) and pushes with a write deploy key; it never forces, and when GitHub has commits the chat lacks it
says so and leaves both sides alone. Versions lists the last 40 commits (`history`, with "not pushed" marks);
`rollback` makes the files what they were at one of them AS A NEW COMMIT on top (uncommitted work is committed
first), so nothing is lost and no force-push is ever needed. A pull or a roll back from the panel leaves a line in
`.git/code-mode-notes`, which the chat's next turn passes to the agent once ("files changed: read them again").
The spark beside the push view's "What changed" box (0.12.1) asks a model to name the version: `suggest_message`
shows it the changed files and a short piece of the diff and cleans its answer to one line. It is the model that
writes chat titles and suggestions (`_task_model`: whatever the router has loaded, else the small CPU model), so it
never loads or swaps a model; with no usable answer (or none within a minute, e.g. the model is busy with a chat)
the name is put together from the file names. 0.12.2: a model that thinks first returned nothing for a one-line
request (the thinking used up the allowance), so the request turns thinking off where the router says the model
allows it (`slerm.thinking` levels include `none`), else asks for the least with more room.
Tests: `tests/test_github_push.py` and `tests/page/test_github_push.cjs`.

End-of-turn review (0.18.0, 2026-10-13). A turn that changed files does not end on the builder's word. A second
opencode session on the agent `review` (`agents/opencode-agents/review.md`, installed to
`~/.config/opencode/agents/review.md`; it can read, search and run commands, not edit) checks the result against what
the user asked for. It is given the user's messages, their answers to question dialogs, the builder's closing
summary, the files the turn changed and `refs/code-mode/turn-start`, a commit of the project as it was when the turn
began (the pipe makes it through a throwaway index, so the user's index, work tree and branches are untouched). It
fills in a worksheet, one line per item with a status: `ASKED` (each thing asked for: built / half built / missing),
`HAD` (each control or route the project had before: still there / hard to find / gone), `ADDED` (each piece of
content put in: asked for / not asked for), `TESTING` (traces of its testing: left behind / cleaned up) and `CLAIM`
(each claim of the summary: true / false / admits it was not done). `parse_review` turns the lines with a bad status
into findings of six kinds (removed, invented, left behind, given up, not built, not true), whatever the reply's
last line says. Why a worksheet: asked only for a list of findings, Qwen3.6 Q5 reported four true ones on the
2026-10-13 case and missed the made-up rooms, the menu that was gone and the part of the request never built.
Findings go back to the builder once (`AUTO_REVIEW`), which also gets one more `desktop verify` round, and its fixes
are reviewed again; what is still open is shown in the reply ("Review: N things still open") and in the status line.
The review runs on the model already loaded, in its own session, which is deleted afterwards; every round is
appended to `~/.local/state/code-mode/review/<chat>.jsonl` on the VM. Not reviewed: feature chats, turns that
changed no file, turns that only made an ideas board, and turns that changed fewer than `REVIEW_MIN_LINES` lines. A
review that fails or gives no verdict never fails the turn: the reply says it was not reviewed. Valves:
`AUTO_REVIEW` (0 = off), `REVIEW_MIN_LINES`, `REVIEW_MINUTES`. Tests: `tests/test_review.py`
(`tests/fake_opencode.py` also plays sessions and the event stream now, so whole turns run without a model).
`agents/AGENTS.md` tells the builder the same rules up front ("Keep what was there, add only what was asked") and
where to find a model for testing an app that needs one ("When your app needs a language model": the small CPU
model beside the router, through the gateway).

Tests: `sh check.sh` runs three things. Open WebUI itself is not reachable from this VM, so they stand in for it:

- compile + parse of every file;
- `tests/test_*.py` with pytest: pipe logic, with a throwaway HTTP server playing the services the pipe calls
  (`tests/test_model_choice.py` is the pattern; `tests/fake_github.py` plays GitHub the way the real one answers,
  `tests/fake_opencode.py` plays the VM's shell API by really running the commands under a throwaway home, and
  `fake.make_remotes()` makes real bare repositories so git clones and pushes for real: see `tests/test_fakes.py`);
- `tests/page/test_*.cjs` with node: `tests/page/harness.cjs` loads `owui/code-toggle.js` into a fake Open WebUI page
  (jsdom) and gives helpers to click, type and look (`test_boot.cjs` is the smallest example). With
  `load({bridge: true})` the page's GitHub calls reach the real pipe (`pipe_bridge.py`), so one test covers page
  script and pipe together. It catches code that throws, controls that never appear, wrong requests, and page and
  pipe disagreeing; it cannot see layout, position or colours, which are checked on the real page.

They use the main checkout's `.venv` (httpx, pydantic, pytest) and `node_modules` (jsdom, see `package.json`), shared
by feature worktrees. This VM has no npm: run `npm install` on another machine and copy `node_modules` over.

Addresses the user can open, and a Preview that stays up (0.19.0, 2026-10-13). An agent restarted its app with
`kill $(lsof -t -i:8000)`; `lsof -i` also lists clients of the port, the preview server was passing a request to the
app, and its unit did not restart after a clean kill, so the Preview was down for 1 h 40 min. The panel pointed its
frame at the dead address, Firefox showed its own error page there, and "Try Again" on that page reloads the whole
tab. Now: the four coder-run services restart after any kill (drop-ins `always-restart.conf`); the page script asks
the preview server before it loads the frame (`previewAnswers`, `loadPreviewFrame`) and shows "The preview is not
answering" itself while nothing answers, so the browser's error page never appears
(`tests/page/test_preview_down.cjs`); AGENTS.md says how to stop only your own server. Replies: the agent's
`http://localhost:PORT/...` becomes `http://<the Open WebUI host>/previews/<chat id>/...` when PORT is the chat's own
app (`user_links`; the preview server reports the port as `"app"` in `__version`; other ports and fenced code blocks
stay; compact output only; valves `PREVIEWS_URL`, `PREVIEWS_HOST`). That address is answered by `code-previews` on
the Open WebUI machine (not in this repo: `~/owui-code-mode/cryoai/previews` on the laptop), which hands the
visitor's Open WebUI sign-in to the pipe op `code_preview` and forwards the chat's owner to the signed link. It
forwards and never serves project pages itself: the Open WebUI cookie counts for every port of its host. In the chat
the page script opens such a link in the panel (`decorateFriendlyLinks`). Tests: `tests/test_user_links.py`.

Quality checks (0.20.0, 2026-10-16). The Learn installer conversation exposed false success and conflicting
verification rules. A post-edit `desktop open` or fresh `desktop verify`, followed by navigation/login and a
passing `desktop verify --here`, now counts for that screen. A stale tab or a failed tool call cannot certify a
new edit. The status says **page checked**, not that installation or all application behavior was verified.
When a check fails or a review is incomplete, that result precedes the builder's summary; the summary is folded
and the status says **Needs attention**. Reviewers receive the sequence of page checks and current task separately
from background conversation. They no longer navigate the builder's browser, reopen unrelated completed work,
or classify normal auth sessions and preview servers as unwanted test data. Missing evidence is **unverified**,
not an automatic pass or a demand to invent a test result.

Installer/deployment/auth/dependency changes receive review even below REVIEW_MIN_LINES. A bounded read-only
script check also catches the observed spinner-without-wait pattern, discarded checker failures and tsc's invalid
`-qq` option. These are specific regression guards, not a shell interpreter or proof that an installer works.
Standing instructions require failure-path testing, declared dependencies, workflow tests and explicit separation
of sandbox evidence from remote deployment claims. No generated project files are changed by this platform update.

The early context guard requests the installed opencode v2 `POST /api/session/{id}/compact` with `delivery: steer`
at a step boundary when input reaches CONTEXT_COMPACT_TOKENS (default 65536, 0 disables). It rearms only after
usage falls below 75% of the threshold, avoiding repeated requests while a checkpoint is pending. The native
compaction settings and selected model remain unchanged. This provides additional headroom; it cannot guarantee
that an arbitrarily large individual tool result will fit. Tests use a fake model and real isolated git/shell
operations; they validate routing and failure handling, not a benchmark improvement in the model itself.

Diagnosing a chat's project against its GitHub repo (0.23.0, 2026-10-19). A chat that cannot push used to leave the
user with one line of git and no way to see why. In a chat that works on a repo the panel's chat line now has a
Diagnose button (the hint line of a chat whose project is not on GitHub has one too). The pipe's `diagnose` op
compares the two and answers `{fetched, summary, findings, brief}`: GitHub's branch list, one `fetch -q --prune
origin` under a READ-ONLY deploy key, and the project's git state in a single shell command (`_gh_diagnose_facts`:
each section starts with its own word, so an empty one cannot shift the parsing). The comparison is done with git
refs (`refs/remotes/origin/<branch>`) after that fetch, never with the API's SHAs, which would be stale or faked.
Each finding is an id, a line in plain English, and the panel action that answers it: `behind` → Pull, `ahead` →
Push, `no_branch` → Push, `diverged` → Ask the agent, and `no_repo`, `stale`, `no_state`, `no_commits`, `uncommitted`
or `unlinked`/`empty` → the explanation only. Diagnose fixes nothing by itself: it commits, pulls, pushes and rolls
back nothing, and a divergence says so plainly ("Push is refused because that would erase what GitHub has; Pull would
not work either"). Ask the agent sends the brief the pipe wrote — the findings, up to 3 commit subjects from each
side, the safe way through (commit the local work, `git merge origin/<branch>`, then press Push in the panel) and the
limit (never rewrite history, force-push or delete commits unless the user says so) — as this chat's next message,
typed into the box the way start_coding does, so the user watches it land and the coding agent investigates in a
normal Code turn. A chat that is not on GitHub gets its own answer (how to get there), and does not go near GitHub for
it. Tests: `tests/test_github_diagnose.py` (the pipe: every finding, the facts parse, what the brief carries) and
`tests/page/test_github_diagnose.cjs` (the panel clicked through against the real pipe: Pull inside a finding brings
the commit in and compares again, Ask the agent types the brief into the chat).

Attached images reach the coding agent (0.24.0, 2026-10-19). A user attached a screenshot to a Code chat and asked
the agent about it - the agent could not see it, because the pipe's prompt was text-only: Open WebUI hands an attached
image as a `files` record whose url names its own upload storage (nowhere on the sandbox VM) plus, for model calls,
an `image_url` content part holding base64 bytes the pipe never looked at (fixer report 20261019-0744-8042). The pipe
now hands images over as opencode prompt `files`: `_attach_want` reads all three shapes (content parts, per-message
`files`, the turn's metadata files) and pairs a record and a part naming the same image so bytes, name and file id
land in one candidate; a record with no bytes beside it is read from Open WebUI's own storage inside the process the
pipe runs in (`_attach_bytes`, the way GET /files/{id}/content does). 0.24.1: Open WebUI 0.8 reads files with plain calls (`Files.get_file_by_id` is not async), so a result is awaited only when it is awaitable - 0.24.0 awaited it, the TypeError was swallowed, and every storage-only image came out as "could not be read". The read also makes Open WebUI's own check: the turn's user owns the file, is an admin, or `has_access_to_file` allows it. Delivered file ids are kept in the session
metadata (`owui_files`), so an attachment in the history is never handed over twice - and an image the session never
got (from before this fix, or from a text-only model that later changed) is delivered with the next turn, the agent
told it is seeing it "for the first time now". What opencode's model listing says about the picked model gates it
(`capabilities.input` from GET /api/model): a text-only model gets an `<attachment_note>` asking the agent to say
plainly that it could not see the image and that the model picker would fix that; a model the listing says nothing
about gets the image anyway. An image too big (`ATTACH_MAX_MB`, default 12), unreadable, or beyond the eighth of a
turn is left out with a note saying so - never in silence. In the prompt, the `<attached_files>` citation's
`url="<file id>"` becomes `url="attached with this message"` so the agent does not chase a path that only exists on
the Open WebUI machine. Tests: `tests/test_attachments.py` (the decoders and pairing; whole turns against the fake
opencode: bytes as a prompt file, no double-send, first-time delivery of an earlier attachment, text-only model,
storage read and its failure note, size cap, eight-image cap; and pipe() handing this turn's metadata files to the
run).

Approvals and questions wait in the page's own box (0.28.0, 2026-10-26). They used to be Open WebUI's pop-up
(`__event_call__`), which goes only to the tab that sent the message: after a reload or a click to another chat it was
gone for good, and Open WebUI waits for its answer forever (no `WEBSOCKET_EVENT_CALLER_TIMEOUT`), so the reply froze
while the agent sat waiting on an approval nobody could see. Now `_permission` and the question dialog (`_ask_one`)
wait through `PageAsk`: each request is listed in `PAGE_ASKS` (no time limit; it also ends when opencode no longer
holds the request, "answered elsewhere"). The page script asks for the list every few seconds (`code_ask {op: list}`,
2.5 s while something waits or a turn runs, 10 s otherwise, 30 s in a hidden tab) and shows the open chat's first one
as a dialog: an approval with its commands, Allow once / Deny… (an optional note: with one the agent carries on,
without one it stops); a question with its choices as buttons (the first one suggested), its own answer, Skip. Later or
Esc folds it into a pill above the message box; reloading, or opening the chat in another browser, shows it again.
Other chats' waiting items show as a notice at the top with a link to the chat. Requests no turn waits on any more
(Open WebUI restarted, the pipe updated mid-turn, a turn of an older pipe) are listed straight from opencode's active
sessions (`direct`) and answered there (`_direct_answer`, the chat's owner checked again). Valve `PAGE_ASKS` (off =
the old pop-up). Tests: `tests/test_page_asks.py`, `tests/page/test_ask_box.cjs`.

The Fixer button pauses the chat (0.28.0). A report the agent files itself (`report-bug`) already holds its turn: the
agent waits inside that command. One the user files with the Fixer button used to run beside a working agent (both on
the one model slot). Now `_fixer_op` leaves a hold in `FIXER_HOLDS`; the running turn of that chat interrupts its run
at the first moment no command is in flight (the command running when it was filed finishes first), says "Paused for
the Fixer", waits for the verdict (code-fixer `/api/incidents/<id>/wait`) and carries on with a `<fixer_result>`
prompt shaped like report-bug's output. A turn that starts while such a report runs waits for it first and gets the
result with the message. opencode 2.0.18 answers an interrupt of an idle session `{"interrupted": false}` with no
event: then the run had just ended, and the hold waits for the next run (or turn). Valve `FIXER_PAUSE`. Tests:
`tests/test_fixer_pause.py`.

Watch: the Fixer takes the stage (0.28.0). While a report runs for the chat (openFixer, or `code_fixer {op: active}`
every 8 s while Watch is open), the stage shows the fixer's viewer behind hazard tape with a pulsing red frame, "FIXER ·
INVESTIGATING", the report's title, CHAT PAUSED and its clock; the first time a report shows, an alarm card plays over
it ("SOMETHING BROKE · The agent is investigating itself", 2.6 s). Hide / Esc / Keep minimized (held across chats,
`code-mode.fixer-minimized`) turn it into a blinking pill on the stage. The viewer's `fixer:status` done stamps the
verdict on it, and 6 s later the stage goes back to the builder with a short note; a report shown to its verdict never
comes back. Test: `tests/page/test_watch_fixer.cjs`.

The Fixer hands what it cannot fix to Claude, and can be cancelled (0.29.0, 2026-10-26). A fixer verdict of
could_not_fix / needs_human no longer closes the report: code-fixer keeps it open as "escalated" (the chat keeps
waiting) for the code-claude runner on cryoai, which runs Claude Code headless (`--permission-mode auto`, the account
signed in there) with the evidence and a project map (code-fixer `claude-runner/CLAUDE.md`), streams Claude's steps
into the report and resolves it with Claude's result.json. The Fixer view shows the hand-off (FIXER ─ CLAUDE beam,
Claude's steps in violet, "CLAUDE'S VERDICT" with the fixer's beside it); Watch's takeover turns violet with a
"CALLING CLAUDE" flash. The view's Ask Claude button (any finished report) and Cancel button (fixer or Claude) reach
the pipe through the page (`code_fixer` ops escalate / cancel, only the chat's own report, only from a fixer frame);
Watch's bar has a two-click Cancel. A cancel is its own verdict ("cancelled") and never goes to Claude; a paused
chat resumes with it. `_fixer_wait` says who is working and stops waiting after FIXER_WAIT_MAX_S (2 h).

Watch never goes blank (0.29.0): after the fixer, its result stays on the stage until the chat's work picks up again
(a running live window), the verdict stamp moving aside; switching chats keeps Watch open on the new chat (a note
until its work starts); a stage that stops sending its state for 20 s, or a fixer view that stops its 5 s heartbeat
for 25 s, is reloaded (at most every 30 s); and every tab reloads itself when a new page script is deployed
(`/static/code-toggle.version` checked each minute against the version loader.js loaded, at the first quiet moment:
nothing typed, no box of ours open), reopening Watch on the same chat. Tests: `tests/test_fixer_escalation.py`,
`tests/page/test_watch_follow.cjs`.

One turn drives a chat's session at a time (0.29.1, 2026-10-27). Open WebUI lets the user send while a reply still
streams, and each message is a Code turn on the chat's one opencode session (the prompt joins the run in progress).
Two turns used to follow that one run, each sending its own automatic checks and starting its own reviewer at the end,
so two model runs competed for the one slot and the older reviewer ran its full REVIEW_MINUTES after its reply was
over. Now `CHAT_TURNS` marks a chat's newest turn once its prompt reaches opencode; an older turn sees that, ends its
reply with a note ("the work carries on in that message's reply") and sends or starts nothing more, and a review
round it has running stops within seconds. Test: `tests/test_turn_takeover.py`.
