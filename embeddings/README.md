# Open WebUI embeddings on cryoai

Open WebUI turns uploaded files (and web search results) into search vectors here, on cryoai's
own CPU. Until 2026-10-19 it used LM Studio on CYCLOPS, so uploads and web search failed whenever
CYCLOPS was off.

    Open WebUI -> 127.0.0.1:8099 owui-embed-proxy -> 127.0.0.1:8098 owui-embed (llama-server)

| Piece | What |
|---|---|
| `owui-embed.service` | `bin/llama-server` (upstream llama.cpp, CPU-only, built on cryoai with `-march=native`) serving `models/embeddinggemma-300M-Q8_0.gguf` (768 dims, 2048-token context). 20 threads, 8 slots, nice 10. |
| `owui-embed-proxy.service` | `embed-proxy.py` (stdlib only). Cuts inputs longer than 2040 tokens instead of failing (LM Studio did this; llama-server returns an error), and caches the last 20,000 embeddings so a re-uploaded file is instant. `curl 127.0.0.1:8099/stats` shows counts. |
| Open WebUI | Admin > Settings > Documents: engine OpenAI, URL `http://127.0.0.1:8099/v1`. EmbeddingGemma's prompts are set in `/etc/systemd/system/openwebui.service.d/embedding-prefixes.conf` (env only, needs a restart). |

Speed on this CPU (E5-2683 v4, 120 chunks of 1000 characters): ~1,400 tokens/s, ~6 chunks/s.
A 20-page PDF takes about 10 s; a 2.7 MB CSV about 8 minutes. RAM ~0.5 GB.

Rebuild llama.cpp: `cd ~/llama.cpp && git pull && cmake --build build-cpu --target llama-server -j 4`,
then `sudo install -m 755 build-cpu/bin/llama-server /opt/owui-embed/bin/ && sudo systemctl restart owui-embed`.
The binary is built for this CPU; if the VM moves to a host with an older CPU it may crash with
SIGILL, so rebuild it there.

Changing the model means every old upload's vectors become stale: re-embed them (see
`reindex-files.py`) or the old chats' files will search badly.
