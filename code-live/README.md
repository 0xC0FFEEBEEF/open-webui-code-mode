# code-live

A read-only live window onto a Code chat's opencode turn. The Code pipe embeds it in the Open WebUI reply as soon
as the turn's first step starts: thinking, commentary, tool calls, and the code of every write/edit as the model
types it (from `session.tool.input.delta`). When the turn ends it folds into a one-line bar; on a reloaded chat the
bar replays the turn on click (`&full=1`).

- `code-live.py` (stdlib only, port 4099, runs as coder) + `live.html`. Deployed copies:
  `/home/coder/.local/lib/code-mode/{code-live.py,live.html}`; unit `code-live.service`.
- Link: `/w/<chat id>/<sig>/?s=<session>&m=<user message>`; sig = HMAC-SHA256(PREVIEW_SECRET, "live:" + chat id)[:32]
  (the pipe's PREVIEW_SECRET valve, `/etc/code-mode/preview.env`). The session must carry the chat id in its
  metadata (`owui_chat_id`).
- Reads only: GET `/api/session/...` and one shared GET `/api/event` (opencode password from coder's service.json).
  A finished turn's history is read forward from its prompt with opencode 2.0.18's base64-JSON cursor.
- **Diffs for edit/write.** The server keeps what each call's file looked like before it (read from the session's
  own directory, through its realpath, so a path outside it is refused) and sends `{"k":"diff", id, path, hunks}`:
  `difflib` groups with 3 context lines, rows `[" "/"-"/"+", old_no, new_no, text, word_spans]`, hunks joined when
  closer than 6 lines. How alike two lines are is measured on characters; where they differ is worked out on the
  line's pieces (letters, digits, punctuation, spaces), so a mark covers a whole word rather than the leftover
  letters two similar words happen to share. While a call is still being typed the `+` rows grow and the tail
  that was not reached yet collapses to "N more old lines below"; the final diff is recomputed once at
  completion. A call whose before is missing (a mid-flight join, a dropped tool payload) replays with no diff
  and says so in the log view.
  `step_from_message` puts the same `diff` on a snapshot step. Limits: 1200 rows, 400 lines per hunk, files over
  1 MB / 2000 lines / 12000 lines are not read or not diffed.
  A path whose JSON string has no closing quote yet names no file: the server waits for it before reading or naming
  a diff, and the window keeps showing the previous file instead of opening a tab for every fragment of a name.
- **Reads as file views.** Every file the turn read — with the `read` tool or with a shell command that reads one
  (`cat`, `nl`, `head`, `tail`, `sed -n`, `awk`, `grep`) — shows as a file view whose asked-for lines are
  highlighted; context lines stay unmarked. `read_targets` works on the command *text as data* (never run or
  expanded). One call shows one view per file: every range it asked of the same file shares that view
  (`sed -n '1,5p' a.py; sed -n '20,25p' a.py` is one view of `a.py` marking both reads), and a read that
  asked for the whole file makes the whole file the range; at most 4 views per call.
  A view goes through the same guard as diffs: only inside the session's directory, over realpath, no symlinks,
  no binary, no file over 1 MB. Files up to 2000 lines arrive whole; larger ones arrive as a window around the
  read ±200 lines, and the Log shows "lines 1–81 not shown" markers where the window cut — except for gaps
  shorter than 4 lines inside the window, which are shown unmarked rather than folded. When the command's own
  output was cut short (`head -30`, a capped `grep`), the view says its marks may be incomplete. The server sends `{"k":"view", id, path, total, from, lines, ranges, how}`
  after its tool event; a collapsed Log row says e.g. "lines 55–91 read · show", or shows the lines with their
  context when few are marked (a `grep` gets 3 context lines), a `▤ file.py` tab opens it in
  the Follow stage while the turn runs. Refused, missing, binary or over-large reads get no view and stay plain
  tool rows. A `grep`/`glob` hit on a file the window cannot name also stays a plain tool row.
- Tests: `python3 -m unittest -v test_live.py test_diff.py` (fake opencode plus a real captured turn, no network).
- Try a turn locally: `python3 code-live.py --fixture fixtures/learnlab-iq3-turn.json --port 4117` prints a window
  URL for a captured Code turn and serves it with no opencode and no event stream (`fixtures/README.md`).
  Add `--live` to replay that capture through the event stream at 1/10 speed — the window opens in Follow and
  fills in live, then folds into the finished Log, so the live path can be tried without a running opencode.
  The replay starts when a window connects and every window gets its own, so a window opened a minute late
  still watches the whole turn live, from its start.
- Review sessions and git diffs (stage 3, part 1). A window can open the **reviewer's session for a chat**: a
  session belongs to chat `C` when its opencode metadata says `owui_chat_id == C` *or* `owui_review_of == C`, and
  the snapshot of a review session carries `"review": {"round": N}` (`N` from `owui_review_round`, 0 when missing)
  so the window names itself **Review · round N** in amber where the model name would show. Chat sessions carry no
  review field. `git diff` / `git show` calls (also `git -C dir diff`, `git --no-pager diff`, `git diff --cached`,
  a diff after `cd x &&`) are detected from the command text only — never by running git — and the **full** tool
  output (not the capped preview) is parsed into per-file entries `{path, hunks, added, removed}`: a snapshot
  finished-turn item gains `"gitdiff"`/`"gitnote"`, and the live stream gains a `{"k":"gitdiff", id, files, note?}`
  event right after the tool's completed event, keyed by the call id (`note` when the patch passes the 2000-row
  cap and was stopped). The page draws each entry's files like the patches on an edit tab: `✎` file tabs in the
  Follow stage (with the terminal's focus moved to them), and per-file `path · −a +b` headers plus hunks in the
  Log, folded past 24 rows when the call is closed. A call whose output is not a git patch (`cat changes.diff`,
  `git diff --stat`) keeps its raw text, and a call with a parsed diff shows no duplicate raw output while opened.
- Deploy: `install -m 0644 code-live.py live.html ~/.local/lib/code-mode/` as coder, then
  `sudo systemctl restart code-live` (an open window reconnects and re-reads the turn).

Watch fits the desktop (2026-10-26). In the Watch stage (`#fill=1`) the stage is wide and short, and the desktop
viewer is a 16:10 screen under its toolbar as wide as its frame, so a frame as wide as the stage cut the bottom of the
desktop off. `fitDesk()` sizes the frame to the biggest one the stage holds that shows all of it, centred
(`DESK_CHROME` 48 px = the viewer's toolbar and border as measured in Chrome, plus 2 px). The viewer frame also gets
`color-scheme: light` (the viewer's own): with the page's dark scheme Chrome painted white behind its rounded corners.
