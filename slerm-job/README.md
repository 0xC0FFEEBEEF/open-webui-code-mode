# slerm-job + later (built 2026-10-20)

Two things for Code mode agents (opencode as `coder` on VM 129):

- **`slerm-job`**: run a build or test command on slerm's CPU nodes. The agent only submits; slerm-head
  (slerm-orch's `wake_for_batch`) powers off nodes back on. Outputs named with `--get` come back.
- **`later`**: wait and come back. The agent sets a wake-up (command exits / timer / check succeeds), ends its
  turn, and the chat gets the result as its next message, as a Code mode turn on the same model.

## Flow

```
VM 129 (coder)                          slerm-head                         compute node (codebuild, uid 1102)
slerm-job run --get X -- CMD
  tar of the project (.gitignore'd     ssh codebuild@slerm-head            job.sh (sbatch, partition compute,
  files left out)  ───────────────────▶ ForceCommand slerm-job-server ───▶  account codebuild)
                                        sbatch / squeue / sacct / scancel    fence check, local image copy,
  follows the log, fetches out.tar ◀── /home/codebuild/jobs/<token>/        apptainer exec codebuild.sif
                                        slerm-orch wakes a node for a        runner.py: sync /work, run, pack --get
                                        pending job (1-3 min)

later run|in|until  ─▶ code-later (:4101, as coder) ─ wake queue ─▶ code-wake (cryoai) ─▶ Open WebUI chat
                                                                       next user message + Code turn
```

## Where things live

| Piece | Location | Source here |
|---|---|---|
| CLI `slerm-job` | VM 129 `/usr/local/bin/slerm-job` | `vm/slerm-job` |
| CLI `later` | VM 129 `/usr/local/bin/later` | `vm/later` |
| `code-later.service` (User=coder, :4101, JoinsNamespaceOf=opencode-code for the same /tmp) | VM 129 `/usr/local/lib/code-later/code-later.py`, state `~coder/.local/state/code-mode/later/` | `vm/code-later.py`, `vm/code-later.service` |
| ssh key (root:coder 0640), pinned head host key, later token (root:coder 0640) | VM 129 `/etc/code-mode/slerm-job.key`, `slerm-known_hosts`, `later.env` | never copied |
| Firewall: coder -> slerm-head:22; :4101 in from cryoai + lo | VM 129 `/etc/code-mode/firewall.nft` (backup `.bak-slermjob-20261020`) | `~/owui-code-mode/vm/code-mode.nft` |
| `code-wake.service` (User=owui, LIVE since 2026-10-20 11:42 CDT) | cryoai `~/.local/lib/code-wake/code-wake.py`, config `~/.config/code-wake/env` (0600: LATER_URL, LATER_TOKEN, DRY_RUN) | `cryoai/code-wake.py`, `cryoai/code-wake.service` |
| sshd `Match User codebuild` (end of file; ForceCommand, root-owned key file) | head `/etc/ssh/sshd_config` (backup `.bak-codebuild-20261020`), `/etc/ssh/authorized_keys/codebuild` (`from="ai-sandbox",restrict`) | `head/sshd-codebuild.conf` |
| `slerm-job-server` (forced command, Python 3.9) | head `/usr/local/bin/slerm-job-server` | `head/slerm-job-server` |
| batch script, in-container runner, image | `/apps/codebuild/{job.sh,runner.py,codebuild.sif -> codebuild-20261020.sif}` | `head/job.sh`, `head/runner.py`, `image/codebuild.def` |
| node setup (user, apptainer, fence, slurmd ordering) | `/apps/codebuild/node/provision-node.sh` (run as root on each node) | `node/` |
| Slurm | user + account `codebuild`: MaxJobs 3, MaxSubmitJobs 8, MaxWall 4h | |

## The fence (user's choice: same as coder on VM 129)

- Jobs run as `codebuild` (uid 1102, no sudo, locked password), never as owui.
- Model-written code runs only inside the image: `apptainer exec --containall --no-mount home,cwd,hostfs,bind-paths`
  with just this chat's node-local folder (`/work`), the package caches (`/cache`), the job's `out/` and read-only
  spec/input. No `/home/owui`, `/data`, `/apps`; own PID/IPC namespaces; `/tmp` on node-local disk (`--workdir`).
- `codebuild-fence.service` on each node: nft table `inet codebuild_fence`, uid 1102 -> internet yes, LAN no
  (DNS to 192.0.2.53 only), loopback to ssh/rpcbind/NFS/slurm/llama ports refused. slurmd starts after it.
  job.sh refuses to run when slerm-head:22 is reachable (fence missing).
- The ssh key on the VM can only run `slerm-job-server` (sshd ForceCommand + `restrict` + `from="ai-sandbox"`).

## Image

`image/codebuild.def` = Ubuntu 24.04 + build-essential, cmake/ninja, Python 3.12, Node 18.19 + npm 9.2 (the VM's
own npm wrapper is broken), Rust stable + 1.92.0, common -dev libraries. Matches VM 129. Rebuild on deadlock
(NOPASSWD sudo): `cd /var/tmp/codebuild-img && sudo apptainer build --force codebuild-YYYYMMDD.sif codebuild.def`,
copy to `/apps/codebuild/` as owui, repoint `codebuild.sif`. Nodes copy a new image on their next job (keep 2).

## Known gaps / notes

- **slerm-orch wake fix (live 2026-10-20 11:42 CDT):** with every compute node drained, squeue's reason is "Nodes
  required for job are DOWN, DRAINED ..." and `wake_for_batch` only matched `Nodes_required` (underscores), so
  nothing woke. Patched in head `~/slerm-orch/orch.py` (backup `orch.py.bak-wakereason-20261020`), orch restarted
  with nothing in flight; it re-adopted Strata and kept the model lock. (`~/slerm-dynamic/orch/orch.py` on Ghost
  is a stale 10-04 copy.)
- **Wake-up turns don't reap:** code-wake sends `code_wake: true`; pipe 0.24.2 (code-mode 6ff4649, synced) then
  skips stopping other chats' servers on that turn and only writes the Preview path (`reap_command`, tests
  `tests/test_wake_reap.py`).
- orch powers on every off node in `compute` for any pending job and powers them off after 60 min with no LLM
  request (user chose to leave this as is).
- slerm-node03 (byteme3) is down and not managed by orch; when it returns, run provision-node.sh there.
- Feature chats (code-features workspace AGENTS template) don't mention slerm-job / later yet.
- A `later run` command lives in code-later's cgroup: restarting code-later stops it (the chat is told).

## code-wake: settle wait and message guard (2026-10-20 evening)

First real wake (chat 00086b8e, 17:48 UTC) broke the chat: the previous turn ended at 17:48:08, the user's open tab
did its end-of-turn `POST /api/chat/completed` + full chat save TWICE (17:48:08 and 17:48:12), and the wake was
delivered in between (17:48:08.7). The tab's second save wrote its older copy over the wake's two messages, and the
reply then streamed into a message id the chat no longer had: Open WebUI's upsert made a bare stub (content + output,
no role/parent) and made it currentId, so the page sat on "Loading..." and sends failed.
Fix (cryoai `~/.local/lib/code-wake/code-wake.py`, backup `.bak-settle-20261020`, tests `cryoai/tests/test_code_wake.py`):
- `SETTLE_S = 30`: no delivery until the chat's `updated_at` is 30 s old.
- after a delivery, `guard()` re-reads the chat every 15 s for 30 min; if a tab dropped the wake's messages it puts them
  back (keeping what streamed in) and makes the wake current only if the chat was left on the stub or rolled back to
  before it (a user who carried on keeps their turn; the wake becomes a branch).
Still open: a tab that stays open through a wake turn holds a stale copy; the user's next send from it drops the wake turn
(outside the guard's 30 min). Idea: page script reloads the chat when a socket event names a message it doesn't have.

## 2026-10-22 fixes

- **Nested repos were sent empty.** Git lists a repo cloned inside the chat's repo as one `codex/` entry, and
  `pack()` added it with `recursive=False`, so jobs 4619/4620 (chat 00086b8e) synced 0 files. `project_files` now
  lists each nested repo from inside (its own .gitignore applies, 4 levels deep). Backup on VM
  `/usr/local/bin/slerm-job.bak-nested-20261022`, here `vm/slerm-job.bak-nested-20261022`.
- **Wrong folder caught early.** `check_folder`: a command starting with cargo/npm/npx/yarn/pnpm/make/cmake/go
  stops before submitting when its manifest is not in the run folder or above, and names the folders that have it.
- AGENTS.md (code-mode 8f19e65, live): slerm-job folder rules + "don't move the build back to the VM", and a new
  "Check scripts" section (step() template: full output logged, PASS/FAIL summary last, exit 1 on any failure; no
  piping checks into tail/grep). Old live copy `~coder/.config/opencode/AGENTS.md.bak-checks-20261022`.
- VM 129 disk 100G -> 200G (TBSSD2 thin pool is overcommitted: 1.64 TiB virtual on 912 GiB, ~33% used).
- **Rust toolchain in jobs.** Jobs had no RUSTUP_HOME (the image's %environment doesn't reach runner.py's env) and
  the image's rustup home is read-only, so a project's `rust-toolchain.toml` asking for components the image lacks
  (codex-rs: 1.92.0 + clippy/rustfmt/rust-src; the image's 1.92.0 is minimal) failed. runner.py `rust_home()` now
  sets RUSTUP_HOME=/cache/rustup, seeded per node from the image's toolchains (marker `.seeded-from-image` keeps the
  image's default = stable); extra versions/components download once per node. Backup
  `/apps/codebuild/runner.py.bak-rustup-20261022`. Verified job 4668 on node05 (pinned -> 1.92.0 + clippy/rustfmt,
  plain -> stable 1.99.1, 2.5 s); first use on node01/02 not yet seen. Owl chat 00086b8e's job 4667 = all 6 PASS.
- **code-wake held wakes forever (10-22).** busy_reason waited while the current reply lacked `done`; a reply that
  errored, or a wake reply the guard restored (it loses `done`), never gets it, so L6c69bd1b sat "not finished".
  Now an unfinished reply only blocks while the chat was saved in the last 10 min (STALE_REPLY_S); the task check
  still catches running turns. Backup `~/.local/lib/code-wake/code-wake.py.bak-donecheck-20261022`. Delivered at once.
- Seen the same night: a send from a stale tab bypassed Code mode (plain /api/chat/completions, no pipe statuses)
  and sent the whole 380k-char chat to Strata -> "prompt (153854 tokens) ... context (131072)" error.
- Panel history now uses `sacct --allusers` (slerm bc2986e) so codebuild jobs show once finished.
- **Ignored files named in the command (10-23).** Job 4677 ran `bash owl/.code-mode/install_selftest.sh`; `.code-mode/`
  is in .git/info/exclude so it wasn't sent (exit 127 after 0.7 s). `check_paths` now stops before submitting when a
  command word is an existing file that git ignores. Backup `/usr/local/bin/slerm-job.bak-ignored-20261023`.
  (Job 4681 in chat 00086b8e was my test of that, exit 127; the script had already been deleted.)

## code-wake also sends queued messages (2026-10-30)

Messages typed while a reply runs are now kept on code-deploy (`/api/queue`, see `~/owui-code-mode/README.md`
"Queued messages kept on cryoai"). A tab open on the chat sends them when the reply finishes; when none does,
code-wake sends a Code chat's queue: `queue_pending()` (`GET /api/queue/pending`, admin token signed for the first
Open WebUI admin), then per chat the same `busy_reason` as wakes, then waits while any other Code turn runs (one
model slot), then leaves it to a tab that polled visibly in the last 25 s for up to 10 min after the chat went quiet,
then claims (`by: code-wake`), posts the turn with the item's `code_model` / `code_effort` / `code_repo` /
`code_feature` and `code_queued: true` (no `code_wake`, so the turn reaps like a user turn), finishes the claim with the
user message id and guards it like a wake. Why it waits goes back as a note under the messages (only on change).
Queued messages go before wakes; one delivery per poll.
code-later: `GET /api/wakes?active=1` now reports `opencode_active` even with no wake ready, plus
`active_checked`; code-wake treats an answer without it (an older code-later) as "can't tell" and waits.
Backups `~/.local/lib/code-wake/code-wake.py.bak-queue-20261030` (cryoai),
`/usr/local/lib/code-later/code-later.py.bak-queue-20261030` (VM).
