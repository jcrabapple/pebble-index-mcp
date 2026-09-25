# pebble-index-mcp

MCP bridge that exposes a local Obsidian vault and a Hermes Agent instance to
the Pebble Index 01 smart ring's double-click MCP sandbox. Voice captures from
the ring are routed by the Pebble app's cloud agent into this server's tools,
and the answer comes back as a phone notification.

## Architecture

```
[Index ring] → [Pebble app] → [Pebble cloud agent] → [this MCP server]
                                                        ├─ vault tools (local files)
                                                        ├─ ha_control → Home Assistant REST API
                                                        ├─ ha_announce → assist_satellite.announce
                                                        ├─ vinyl_lookup → vinyl catalog API
                                                        └─ ask_hermes → Hermes API server
```

- The server speaks MCP over Streamable HTTP (official `mcp` SDK, FastMCP).
- Every request must carry a bearer token; anything else gets 401.
- FastMCP's DNS-rebinding protection is on: allowed hosts are loopback plus
  anything listed in `MCP_ALLOWED_HOSTS`.
- The public exposure is a cloudflared tunnel to the loopback port; no cloud
  infrastructure is required beyond that.

## Components

| Path | Role |
|---|---|
| `src/pebble_index_mcp/vault.py` | `Vault`: path-sandboxed read/append + ripgrep search over the vault. All paths resolve inside the vault root; absolute paths, `..` traversal, and symlink escapes are rejected. Appends are append-only, timestamped, and never overwrite. |
| `src/pebble_index_mcp/hermes.py` | `HermesClient`: forwards questions to an OpenAI-compatible chat completions endpoint with a short-answer system hint. Maps timeouts/transport failures/bad shapes onto `HermesTimeout`/`HermesError`. |
| `src/pebble_index_mcp/websearch.py` | `ExaClient`: live web search against the Exa API, snippets truncated locally. Errors map to `ExaError`; a missing key short-circuits with a clear message. |
| `src/pebble_index_mcp/homeassistant.py` | `HAClient`: deterministic smart-home commands against the Home Assistant REST API. Parses the action from the command text, resolves the target entity by token match (preferring room group entities over individual fixtures and available over unavailable entities), calls the service, then polls the state readback to confirm. No LLM in the loop. Also `announce()`: intercom messages to every live `assist_satellite` (offline satellites are pre-filtered because HA returns 200 for announce regardless). |
| `src/pebble_index_mcp/vinyl.py` | `VinylClient`: lookups against the operator's self-hosted vinyl catalog API. Routes command text to a random pick, most-played list, or artist/album search; conversational filler is stripped before the catalog's full-text query. Base URL is operator config only. |
| `src/pebble_index_mcp/server.py` | FastMCP app: registers the eight tools and the `ring_persona` prompt, wraps the streamable-http app in bearer auth (constant-time compare) and configures host allow-listing. |

## Tools

- `vault_search(query, max_results=5)` — case-insensitive text search; returns `path: excerpt` lines.
- `vault_read(note_path, max_chars=1500)` — reads the head of a note.
- `vault_append(note_path, text)` — appends a timestamped `- HH:MM text` line, creating the note if needed.
- `ask_hermes(question)` — forwards to the Hermes API server; 60s timeout with an honest acknowledgment on miss.
- `ha_control(command)` — smart-home commands: `"living room lights on"`, `"bedroom off"`, `"toggle the hallway"`, `"is the living room tv on"`. Resolves the entity against Home Assistant's state list (room groups win over individual fixtures; ambiguous matches return the candidates instead of guessing), calls the service, and polls the state readback so the confirmation reflects the NEW state. Domains: light, switch, fan, cover, media_player. Requires `HASS_TOKEN`. Milliseconds-to-seconds latency; never route smart-home commands through `ask_hermes`.
- `ha_announce(message)` — intercom: speaks `message` on every live `assist_satellite` device with a pre-announce chime. Offline satellites are skipped (HA returns 200 for announce even when the device is down, so the tool pre-filters). Requires `HASS_TOKEN`.
- `vinyl_lookup(command)` — queries the operator's self-hosted vinyl catalog: owned-record search ("do I own Eternal Blue"), a dormancy-weighted random pick, or most-played records. Requires `VINYL_URL`.
- `web_search(query, max_results=3)` — live web search via the Exa API, composed into a short spoken answer by the configured model. Requires `EXA_API_KEY`.

Both answer tools wrap their result in the Pebble app's private `coreSchema` contract (`_meta: {"coreSchema": 1}` plus `structuredContent` carrying a `Response` semantic result). Without it, the app's completion notification shows the transcription of the question instead of the answer. If the compose step fails, `web_search` falls back to raw results (the notification then shows the question; the agent still answers in the feed).

## Security

- **Auth**: static bearer token (`MCP_BEARER_TOKEN`), constant-time compared, required on every request. An empty token fails closed (all requests 401) and logs a warning at startup.
- **Transport**: TLS terminates at the tunnel edge; the origin listens on loopback only.
- **Host validation**: DNS-rebinding protection rejects requests whose Host header is not loopback or in `MCP_ALLOWED_HOSTS`.
- **Path sandbox**: vault tools resolve all paths and require them to stay inside the vault root; symlink escapes are rejected and file opens use `O_NOFOLLOW` on the final component. The sandbox guards against accidental and remote misuse; a hostile local process that races a parent-directory swap can still win (documented TOCTOU boundary — the vault is assumed to be a trusted single-user directory).
- **Config is read once at import**: rotating the bearer token or changing allowed hosts requires a service restart.
- **Secrets**: the Hermes API key and MCP token live in a 0600 env file outside this repo, never in code, logs, or commits.
- **Privacy note**: recordings routed through the double-click sandbox transit the Pebble app's cloud agent. Single-click captures stay on-device. Sensitive thoughts belong on single-click.

## Configuration (env)

| Var | Default | Purpose |
|---|---|---|
| `VAULT_PATH` | — | **Required.** Vault root for the file tools |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8765` | Listen address |
| `MCP_BEARER_TOKEN` | — | Required; request auth token |
| `MCP_ALLOWED_HOSTS` | — | Comma-separated extra allowed Host values (the public tunnel hostname goes here) |
| `HERMES_API_URL` | `http://127.0.0.1:8642/v1/chat/completions` | ask_hermes target |
| `HERMES_API_KEY` | — | API server bearer key |
| `RING_MODEL` | `pebble-ring` | Model alias sent upstream (map it to a cheap model via the API server's `model_routes`) |
| `EXA_API_KEY` | — | Exa API key for `web_search`; without it the tool reports an error but the rest of the server works |
| `HASS_URL` | `http://127.0.0.1:8123` | Home Assistant base URL for `ha_control` / `ha_announce` |
| `HASS_TOKEN` | — | Home Assistant long-lived access token for `ha_control` / `ha_announce`; without it those tools report an error but the rest of the server works |
| `VINYL_URL` | — | Base URL of a vinyl catalog API exposing `GET /api/records?q=`, `GET /api/random`, `GET /api/plays/top` for `vinyl_lookup`; without it the tool reports an error but the rest of the server works |
| `RING_PERSONA_FILE` | — | Optional path to a text file replacing the generic cloud-agent persona |

`HERMES_API_URL` accepts any OpenAI-compatible chat completions endpoint, so
`ask_hermes` works against OpenRouter directly, Ollama, or any other
compatible API — Hermes is just the default.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
```

Run locally: set the env vars above and `python -m pebble_index_mcp.server`.

## Deployment

`deploy/` contains a systemd user unit template and a cloudflared tunnel
template (hostname and credentials filled in on the host, not in this repo).
See `docs/ring-checklist.md` for the phone-side setup and test matrix.

## License

MIT — see [LICENSE](LICENSE).
