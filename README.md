# Open WebUI Code mode

Add-ons that turn a self-hosted [Open WebUI](https://github.com/open-webui/open-webui) into a coding workspace. A
**Chat | Code** switch in the message bar sends a chat's turns to its own [opencode](https://opencode.ai) session,
which works in its own project folder on a sandbox VM. Chat and Code turns can mix in one conversation. Everything
runs on local models served by a llama.cpp cluster.

What a Code turn gets:

- **A live window** in the reply that follows the agent: the file it's writing, its terminal, the desktop browser it
  is testing with, diffs of every edit, and the end-of-turn review.
- **A preview** of the app it built, in a side panel and at a plain address the user can open.
- **Checks before "done".** The agent opens its own page in a real browser on the sandbox desktop (`desktop verify`)
  and runs linters (`check`). A second, read-only session then reviews the turn against what was asked and sends
  findings back once.
- **Question dialogs** on the first turn, so it asks before it builds.
- **A GitHub panel**: sign in with your own OAuth app (device flow), clone a repo into the chat, push, see versions
  and roll back. The token stays on the Open WebUI host and never reaches the VM where the agent runs.
- **Builds on the cluster** (`slerm-job`) and **wake-ups** (`later`): the agent can submit a long build as a Slurm job,
  stop, and get woken as the chat's next turn when it finishes.
- **A Fixer**: a model or the user reports a broken tool, and a fresh agent session investigates, fixes and tests it,
  then the chat carries on.
- **Feature chats**: a chat that extends Code mode itself, on a branch, with Deploy / Roll back buttons.

## Layout

| Folder | What it is | Runs on |
|---|---|---|
| `code-mode/` | The Open WebUI side: the `code_mode` pipe, the page script `code-toggle.js` (Chat \| Code pill, panels, live data), the `desktop_code` tool, and the agent's standing rules (`agents/AGENTS.md`, `ask`/`review` agents, a design skill). Has its own README and tests. | Open WebUI host + VM |
| `code-live/` | The live window served inside each Code reply (`:4099`) | VM |
| `code-preview/` | Signed per-chat previews of the agent's app, passing requests through to the chat's own server (`:4097`) | VM |
| `code-fixer/` | The bug fixer service and its viewer (`:4098`), plus the `report-bug` CLI | VM |
| `code-features/` | Feature chats: worktrees per feature, deploy = merge + tests + install, roll back = revert (`:4100`, `feature` CLI) | VM |
| `desktop-cli/` | `desktop`: the agent's hands on the sandbox desktop (open, look, click, type, upload, verify, errors) | VM |
| `code-deploy/` | Features page and admin API next to Open WebUI; installs the pipe, tool and page script (`:8095`) | Open WebUI host |
| `code-previews/` | `http://<host>/previews/<chat>/` → the chat's signed preview, for the signed-in owner only (`:80`) | Open WebUI host |
| `slerm-job/` | `slerm-job` + `later` CLIs, `code-later` (VM, `:4101`), `code-wake` (Open WebUI host), the head-node job server, the per-node network fence and the build image | VM, cluster, Open WebUI host |
| `embeddings/` | Local embeddings for uploads and web search: llama.cpp `llama-server` + EmbeddingGemma behind a small proxy that trims long inputs and caches | Open WebUI host |
| `openwebui/` | Open WebUI's unit and drop-ins (installs the page script at start), and the **Auto Compact** filter | Open WebUI host |
| `vm/` | Sandbox VM setup: install scripts, opencode config and unit, firewall, sudoers rule, `check` (TypeScript/Biome/Ruff/ShellCheck), `doc-shot`, session watchers | VM |
| `tools/` | Workstation helpers for driving and screenshotting Open WebUI during development | anywhere |

The sandbox desktop itself (Chrome on the VM, driven over an API, and the Computer Agent pipe) is a separate
project, [openwebui-computer-agent](https://github.com/0xC0FFEEBEEF/openwebui-computer-agent).

```
browser ── Open WebUI ─┬─ code_mode pipe ──────────── opencode serve (VM, user "coder", firewalled)
                       │   code-toggle.js                ├─ desktop CLI ── sandbox desktop (Chrome)
                       │                                 ├─ check, doc-shot
                       ├─ code-deploy, code-previews     ├─ slerm-job ── Slurm job on a CPU node
                       ├─ code-wake  ◄──────────────── code-later
                       └─ embeddings                     code-preview · code-live · code-fixer · code-features
                                    models: llama.cpp router behind the cluster gateway
```

## About this copy

A cleaned public snapshot. The working sources are git repos on my sandbox VM and files installed on the Open
WebUI host; changes are deployed from there through the Features page. Host names stand in for addresses and
`owui` for the service user. Tokens were never in the code: they live in env files on the hosts, and every token
setting defaults to empty. Left out on purpose: the prompts the components were built from, recordings of real
chats (so the `code-live` replay tests need a recording of your own) and third-party Open WebUI tools.

The models come from my Slurm cluster, [slerm-cluster](https://github.com/0xC0FFEEBEEF/slerm-cluster).

## License

GNU General Public License v3.0 or later, see [`LICENSE`](LICENSE).

<sub>Preparing this public copy from my private repo was done with help from Claude.</sub>
