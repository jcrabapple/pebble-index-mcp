# Pebble Index MCP Bridge — Design Spec

Date: 2026-08-21
Status: Draft, pending review
Owner: personal deployment

## 1. Context

The user owns a Pebble Index 01 smart ring: a button + microphone on the finger
that records 1s–2min clips and streams them to the open-source Pebble app on
his phone. The app transcribes on-device and runs an agent that picks an
action (note, reminder, list).

Two extensibility rails exist:

1. **Webhook** — the app POSTs each recording (transcription, audio, or both)
   to one HTTPS endpoint. Fire-and-forget, no retries, fires only after a
   successful transcription. (Out of scope for v1; see Future Work.)
2. **MCP Sandbox** — a double-click-and-hold recording is routed to a Repebble
   cloud agent (free, included in the device price; OpenRouter-backed) that
   can call HTTP MCP servers (SSE or Streamable HTTP, `Authorization` header
   auth only). Answers arrive as a phone notification. The client caps tool
   execution at 3 rounds per recording and caches the tool list for 30s.

This project builds the MCP rail: a small server that exposes the user's local
world (Obsidian vault, Hermes) to the double-click agent, making the ring a
voice terminal for his notes and his agent.

## 2. Goals

- Double-click the ring, speak, and get a short answer sourced from the
  Obsidian vault or from Hermes.
- Append thoughts/notes to the vault by voice.
- All custom code runs on the home desktop; no new cloud infrastructure.
- Security: bearer-token auth, path-sandboxed file access, tunnel-only
  exposure.

## 3. Non-goals (v1)

- HA control tools, vinyl catalog tools, Deezer, or any tool beyond vault +
  Hermes passthrough. (Chosen tool surface: Obsidian + ask_hermes.)
- The capture rail (webhook into Obsidian for every recording). The app's
  native Obsidian sync may already cover this; evaluate separately.
- Audio processing (the app's transcription is authoritative in v1).
- `hermes mcp serve` bridging. It is stdio-only; the Pebble app requires
  HTTP MCP. Not useful here.

## 4. Architecture

```
 [Index ring] --BLE--> [Pebble app] --HTTPS--> [Repebble cloud agent]
                                                   |  MCP (Streamable HTTP)
                                                   |  Authorization: Bearer <token>
                                                   v
                                       [cloudflared tunnel, *.example.com]
                                                   |
                                                   v
                                    [pebble-index-mcp.service :8765]
                                     |        |         |
                                     |        |         +--> ask_hermes --> http://127.0.0.1:8642/v1/chat/completions
                                     |        |                        (Hermes API server, model_routes -> flash)
                                     |        +--> vault_read/append/search
                                     |        (~/Documents/Obsidian Vault/, path-sandboxed)
                                     +--> MCP prompt resource (system-prompt injection)
```

Components:

1. **pebble-index-mcp** — Python service using the official `mcp` SDK with
   FastMCP, Streamable HTTP transport, listening on 127.0.0.1:8765.
2. **cloudflared tunnel** — new `cloudflared-pebble-index` user service
   following the existing `cloudflared-hermes-webui` pattern, restricted to
   the MCP service port.
3. **Pebble app config** — one sandbox group, model type `Default`, one HTTP
   MCP server entry (Streamable HTTP, bearer token), assigned to
   "Double click and hold", plus the MCP prompt selected.
4. **Hermes API server** — existing, already live on 127.0.0.1:8642 (public
   public mirror). No code changes; one config addition:
   a `model_routes` alias so ring-originated calls run a cheap model.

## 5. Tool contracts

Design rules: every tool returns compact text sized for a phone notification;
every tool is one-shot (one round should answer the recording). Tool
descriptions teach the cloud agent when to use them.

### 5.1 vault_search

- Params: `query` (string), `max_results` (int, default 5, max 10).
- Behavior: ripgrep over the vault, excluding `.git`, `.obsidian`, and hidden
  dirs. Returns ranked filename + one-line excerpt per hit.
- Output: "N matches. 1) path/to/note.md: excerpt …" capped at ~1200 chars.
- Notes: no semantic search in v1 (ripgrep only). If quality disappoints,
  embeddings are a later option.

### 5.2 vault_read

- Params: `note_path` (string, vault-relative), `max_chars` (int, default
  1500, max 4000).
- Behavior: resolves path, verifies it is inside the vault root and is a
  regular file, returns head of content.
- Errors: "Note not found" / "Path outside vault".

### 5.3 vault_append

- Params: `note_path` (string, vault-relative), `text` (string).
- Behavior: resolves and clamps to vault root; creates file (and parent
  dirs inside vault) if missing; appends `- HH:MM text` as a list line. No
  deletes, no overwrites, no writes outside the vault.
- Output: "Appended to path/to/note.md".
- Note: the vault is git-synced to Gitea; local appends propagate via the
  existing sync, no git work in this service.

### 5.4 ask_hermes

- Params: `question` (string).
- Behavior: POST to `http://127.0.0.1:8642/v1/chat/completions` with
  `Authorization: Bearer $HERMES_API_KEY`, body
  `{"model": "pebble-ring", "messages": [{"role": "user", "content": question}]}`.
  The question is postfixed with: "Answer in at most 3 sentences; the answer
  will be shown as a phone notification." Client timeout 60s.
- Success: returns `choices[0].message.content` verbatim.
- Timeout: returns "Hermes is still working on it. It will finish shortly;
  re-ask or check Telegram." (v1.1: have Hermes push the late answer to
  Telegram; v1 keeps the honest ack only.)
- Model routing: the API server's `model_routes` maps alias `pebble-ring` to
  a cheap model (default `deepseek/deepseek-v4-flash` via `provider:
  openrouter`). Verified schema from api_server.py:
  `model_routes.<alias>: {model, provider?, api_key?, base_url?}`.
- Measured baseline (spike, 2026-08-21): trivial prompt round trip 3.5s via
  public endpoint, 4.5s local; prompt cost ~34K tokens per call, hence the
  cheap-model route.

## 6. MCP prompt

A prompt resource on the server, selected in the app's MCP server settings,
injected into the cloud agent's system prompt. Content (v1 draft):

- Who the user is and their vault conventions (name, layout, hobbies),
  supplied by the operator via a local persona file rather than hardcoded.
- Vault layout hint: notes live in ~/Documents/Obsidian Vault, with a
  Writing/ folder; append ideas there unless a target is named.
- Answer style: max 3 short sentences, plain language, no markdown.
- Facts about the vault must come from vault_search/vault_read. Never invent
  note contents.
- Unknowns get an honest "I don't have that" rather than a guess.

## 7. Security

- **Transport**: cloudflared tunnel terminates TLS at the edge; origin is
  plain HTTP on loopback only.
- **Auth**: static bearer token (32 random bytes, hex) checked on every
  request by the server; token stored in Infisical and injected into the
  systemd unit environment. The app sends it as the MCP `Authorization`
  header.
- **Path sandbox**: all vault paths resolved (`Path.resolve()`) and checked
  with `is_relative_to(vault_root)`; symlinks resolved before the check.
  Reads require a regular file. Writes only append or create.
- **Hermes key**: `API_SERVER_KEY` (already in ~/.hermes/.env) passed to the
  service via the same Infisical/env path as the gateway; never logged.
- **Privacy tradeoff**: double-click recordings and tool results transit
  Repebble's cloud agent and OpenRouter. Sensitive/private thoughts should
  use single-click (local, on-device processing). State this in the user
  docs, not as a hidden caveat.

## 8. Configuration

Service config via env (systemd `Environment=` or env file, 0600):

| Var | Default | Purpose |
|---|---|---|
| `VAULT_PATH` | `/home/<user>/Documents/Obsidian Vault` | Vault root |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | Listen address |
| `MCP_BEARER_TOKEN` | — | Required; request auth |
| `HERMES_API_URL` | `http://127.0.0.1:8642/v1/chat/completions` | ask_hermes target |
| `HERMES_API_KEY` | from ~/.hermes/.env | API server bearer |
| `RING_MODEL` | `pebble-ring` | model alias sent upstream |

Hermes config addition (gateway config.yaml, platforms.api_server.extra):

```yaml
model_routes:
  pebble-ring:
    model: deepseek/deepseek-v4-flash
    provider: openrouter
```

## 9. Deployment

1. `pebble-index-mcp.service` (systemd --user): venv python, FastMCP app,
   `Restart=on-failure`.
2. `cloudflared-pebble-index.service`: tunnel with hostname on
   `*.example.com`, ingress to `http://127.0.0.1:8765`.
3. Pebble app manual setup (documented checklist): sandbox group (model:
   `Default`) → add MCP server (Streamable HTTP, URL = tunnel hostname,
   bearer token) → assign to Double click and hold → select the prompt.

## 10. Testing

- Unit: path sandbox (traversal, symlink escape, outside-vault paths,
  missing files), append formatting.
- Integration: MCP Inspector against the local endpoint with and without
  token; verify tool list, prompt listing.
- Hermes: ask_hermes latency matrix (short factual, vault-referencing,
  open-ended prompts); confirm `pebble-ring` route resolves to the cheap
  model.
- E2E via tunnel: MCP handshake + one tool call through the public hostname.
- Manual ring checklist when hardware arrives: transcription phrasing
  ("search my notes for X", "add to note Y", "ask Hermes Z"), notification
  latency, Default vs High Capability behavior.

## 11. Risks and open questions

- **Cloud-side tool timeout unknown.** Client code shows no limit; behavior
  of Repebble's backend is unverifiable. Mitigation: fast tools, one-shot
  design, ack on 60s timeout. If real-world use shows impatient timeouts,
  add the v1.1 Telegram handoff.
- **Two writers to the vault.** The app's native Obsidian sync and this
  service both write notes; git sync conflicts are possible. Mitigation:
  append-only tool, prefer distinct target files, surface conflicts in the
  existing git flow.
- **Native Obsidian integration mechanics unknown.** What the app's
  first-party Obsidian sync does (local folder on phone? cloud relay?) needs
  hands-on verification before deciding whether the capture rail needs any
  custom plumbing.
- **Repebble pricing may change.** Free today, explicitly "may release
  additional features that may require a subscription" later.
- **Experimental software.** MCP/webhook features are labeled basic and
  experimental by Repebble; app updates may change behavior.
- **Open question**: whether `Default` model handles 4 tools reliably;
  escalate to `High Capability` if tool selection gets flaky.

## 12. Future work (not v1)

- **Capture rail**: webhook (transcription) → Hermes inbox → daily review.
  Constraints from forum findings: fires only after successful
  transcription, no retries, app agent still runs in parallel.
- **HA tools** (lights/scenes/music) via HA REST API; also investigate the
  app's button-only action config (clicks without voice) for physical HA
  control.
- **Vinyl catalog tools** (lookup/add via Discogs) once v1 is stable.
- **Semantic vault search** if ripgrep relevance disappoints.
- **Late-answer Telegram handoff** for ask_hermes timeouts.

## 13. Repo layout (planned)

```
pebble-index-mcp/
  pyproject.toml          # deps: mcp[cli], fastmcp, uvicorn
  src/pebble_index_mcp/
    __init__.py
    server.py             # FastMCP app, tools, prompt
    vault.py              # path sandbox + search/read/append
    hermes.py             # ask_hermes client
  tests/                  # unit + integration
  deploy/                 # systemd units, cloudflared config
  docs/                   # this spec, user-facing README
```
