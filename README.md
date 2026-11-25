# pebble-index-mcp

MCP bridge that exposes a local Obsidian vault and a Hermes Agent instance to
the Pebble Index 01 smart ring's double-click MCP sandbox. Voice captures from
the ring are routed by the Pebble app's cloud agent into this server's tools,
and the answer comes back as a phone notification.

## Architecture

```
[Index ring] → [Pebble app] → [Pebble cloud agent] → [this MCP server]
                                                        ├─ respond → zero-LLM notification wrapper
                                                        ├─ vault tools (local files, single-pass rg)
                                                        ├─ ha_control → Home Assistant REST API
                                                        ├─ ha_announce → assist_satellite.announce
                                                        ├─ ha_music → Music Assistant / Deezer
                                                        ├─ shopping_list → HA todo.shopping_list
                                                        ├─ weather_report → HA weather.home + sun
                                                        ├─ calendar_lookup → local khal CalDAV mirror
                                                        ├─ quick_calc → math, units, ABV, tip/split, dice
                                                        ├─ vinyl_lookup → vinyl catalog API
                                                        └─ web_search → Exa + fast 1-round compose
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
| `src/pebble_index_mcp/hermes.py` | `HermesClient`: forwards questions to an OpenAI-compatible chat completions endpoint with a short-answer system hint. Uses a persistent HTTP connection and supports optional OpenRouter provider ordering. Maps timeouts/transport failures/bad shapes onto `HermesTimeout`/`HermesError`. |
| `src/pebble_index_mcp/websearch.py` | `ExaClient`: live web search against the Exa API, snippets truncated locally. Errors map to `ExaError`; a missing key short-circuits with a clear message. |
| `src/pebble_index_mcp/homeassistant.py` | `HAClient`: deterministic smart-home commands against the Home Assistant REST API. Parses the action from the command text, resolves the target entity by token match (preferring room group entities over individual fixtures and available over unavailable entities), calls the service, then polls the state readback to confirm. No LLM in the loop. Also `announce()`: intercom messages to every live `assist_satellite` (offline satellites are pre-filtered because HA returns 200 for announce regardless). |
| `src/pebble_index_mcp/vinyl.py` | `VinylClient`: lookups against the operator's self-hosted vinyl catalog API. Routes command text to a random pick, most-played list, or artist/album search; conversational filler is stripped before the catalog's full-text query. Base URL is operator config only. |
| `src/pebble_index_mcp/calendar.py` | `CalendarClient`: queries the local `khal` Fastmail CalDAV mirror (with `RRULE` recurrence expansion) for today/tomorrow/weekday/7-day schedules and event keyword searches. |
| `src/pebble_index_mcp/calc.py` | Deterministic AST arithmetic, unit conversions (temp, volume, mass, length), mead gravity ABV, tip/bill splits, coin flips, and dice rolls. |
| `src/pebble_index_mcp/server.py` | FastMCP app: registers the tools and the `ring_persona` prompt, wraps the streamable-http app in bearer auth (constant-time compare) and configures host allow-listing. |

## Tools

- `respond(answer)` — zero-LLM notification wrapper: the Repebble cloud agent passes its 1–3 sentence plain-text answer directly so the phone notification displays it without a second LLM round-trip. Used for general knowledge and after `vault_search` / `vault_read`.
- `vault_search(query, max_results=5)` — filename/path + fixed-string + multi-term search over the vault; returns `path: excerpt` lines.
- `vault_read(note_path, max_chars=1500, tail=False)` — reads the head (or tail when `tail=True`, for append-only logs) of a note; resolves missing `.md` extensions case-insensitively.
- `vault_append(note_path, text)` — appends a dated `- YYYY-MM-DD HH:MM text` line, creating the note if needed, wrapped in `coreSchema` `Response` for one-shot confirmation.
- `ha_control(command)` — smart-home commands: `"living room lights on"`, `"dim living room to 30%"`, `"set basement to relax"`, `"bedroom nightlight scene"`, `"all lights off"`, `"toggle the hallway"`, `"open the blinds"`, `"is the living room tv on"`. Resolves lights, switches, fans, covers, media players, and Hue scenes (`scene.*`), prefers room groups over individual fixtures, and reports brightness on light status queries. Requires `HASS_TOKEN`.
- `ha_announce(message)` — intercom: speaks `message` on every live `assist_satellite` device with a pre-announce chime. Offline satellites are skipped (HA returns 200 for announce even when the device is down, so the tool pre-filters). Requires `HASS_TOKEN`.
- `ha_music(command)` — music control: playback (`"play Spiritbox in the basement"`), transport (`"pause"`, `"resume"`, `"skip"`, `"previous"`, `"stop"`), volume (`"volume 40 in the kitchen"`, `"turn it down"`, `"mute basement"`), and status (`"what's playing"`). Discovers Music Assistant players (`media_player.*_2`) dynamically from HA states (with optional `HASS_MUSIC_ROOMS` aliases and `HASS_DEFAULT_PLAYER` fallback chain), targets the actively playing speaker for roomless transport commands, and verifies playback within a bounded timeout. Requires `HASS_TOKEN` and Music Assistant.
- `shopping_list(command)` — reads, adds to (including multi-item adds and `"we need ..."` phrasing), or safely checks off items on Home Assistant's `todo.shopping_list`. Requires `HASS_TOKEN`.
- `weather_report(query)` — local current weather (including feels-like temperature when notable), daily and hourly forecasts (`"will it rain tonight"`, `"weather at 5pm"`), wind gusts, and sunrise/sunset times from Home Assistant (`weather.home` + `sensor.sun_next_*`), avoiding external web search latency. Requires `HASS_TOKEN`.
- `calendar_lookup(query)` — queries the local `khal` CalDAV mirror for today, tomorrow, explicit dates (`"December 5th"`), specific weekdays (`"next Thursday"`), this weekend, next week, next 7 days, or chronological event keyword searches (`"when is the flea market"`), formatted in 12-hour time.
- `quick_calc(expression)` — deterministic arithmetic (including `^` and spoken compound numbers), percentages and discounts (`"15% of $240"`, `"20% off $80"`), unit conversions (`"350 F to C"`, `"12 oz in ml"`), mead ABV (`"abv 1.105 to 1.000"`), tips/splits (`"split 84 3 ways"`, `"tip on 64.50"`), coin flips, and dice rolls (`"roll 2d6"`).
- `vinyl_lookup(command)` — queries the operator's self-hosted vinyl catalog: owned-record search ("do I own Eternal Blue"), a dormancy-weighted random pick, or most-played records. Requires `VINYL_URL`.
- `web_search(query, max_results=3)` — live web search via self-hosted Trivet metasearch (`TRIVET_URL`, with automatic Exa fallback via `EXA_API_KEY`), composed into a short spoken answer by the configured fast model.
- `ask_hermes(question)` / `hermes_task(task)` — optional legacy/full-agent chat tools, registered only when `ENABLE_ASK_HERMES=1` or `HERMES_AGENT_API_URL` is set.

Every user-facing tool wraps its result in the Pebble app's private `coreSchema` contract (`_meta: {"coreSchema": 1}` plus `structuredContent` carrying a `Response` semantic result). Without it, the app's completion notification shows the transcription of the question instead of the answer. If the compose step fails, `web_search` falls back to raw results (the notification then shows the question; the agent still answers in the feed). `GET /healthz` returns `200 {"ok": true}` without auth for liveness monitoring.

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
| `HERMES_API_KEY` | — | Fast chat endpoint bearer key |
| `HERMES_AGENT_API_URL` | — | Optional full Hermes chat completions URL; enables `hermes_task` |
| `HERMES_AGENT_API_KEY` | — | Full Hermes API bearer key |
| `HERMES_AGENT_MODEL` | `pebble-ring` | Model alias or per-request model override for the full Hermes path |
| `HERMES_AGENT_PROVIDER` | — | Optional Hermes provider override, e.g. `openrouter` |
| `HERMES_AGENT_REASONING_EFFORT` | — | Optional reasoning level passed as `model_options.reasoning_effort`, e.g. `low` |
| `HERMES_AGENT_TIMEOUT` | `120` | Full Hermes request timeout in seconds |
| `RING_MODEL` | `pebble-ring` | Model or alias sent upstream (map an alias to a cheap model via Hermes's `model_routes`, or use a direct provider model ID) |
| `RING_PROVIDER_ORDER` | — | Optional comma-separated OpenRouter provider order, e.g. `Crusoe,Cloudflare`; fallbacks remain enabled |
| `TRIVET_URL` | — | Optional self-hosted Trivet base URL (e.g. `http://127.0.0.1:8952`) used as primary backend for `web_search` |
| `TRIVET_USER` / `TRIVET_PASS` | — | Optional HTTP Basic credentials for Trivet `/api/search` |
| `EXA_API_KEY` | — | Exa API key for `web_search` (primary if `TRIVET_URL` unset, automatic fallback if Trivet fails) |
| `HASS_URL` | `http://127.0.0.1:8123` | Home Assistant base URL for `ha_*`, `shopping_list`, `weather_report` |
| `HASS_TOKEN` | — | Home Assistant long-lived access token; without it HA tools report a clear config error |
| `HASS_MUSIC_ROOMS` | — | Optional comma-separated `room=media_player.entity` aliases for `ha_music` (supplementing dynamic HA discovery) |
| `HASS_DEFAULT_PLAYER` | — | Optional comma-separated fallback list of default `media_player.*` entity IDs when no room is named |
| `KHAL_BIN` | auto | Optional explicit path to the `khal` binary for `calendar_lookup` |
| `ENABLE_ASK_HERMES` | `0` | Set to `1` to register the legacy `ask_hermes` tool (leave off to use `respond`) |
| `VINYL_URL` | — | Base URL of a vinyl catalog API exposing `GET /api/records?q=`, `GET /api/random`, `GET /api/plays/top` for `vinyl_lookup`; without it the tool reports an error but the rest of the server works |
| `RING_PERSONA_FILE` | — | Optional path to a text file replacing the generic cloud-agent persona |

`HERMES_API_URL` accepts any OpenAI-compatible chat completions endpoint, so
`ask_hermes` works against OpenRouter directly, Ollama, or any other
compatible API — Hermes is just the default.

For the lowest notification latency, point it directly at the model provider.
Going through Hermes adds agent session, memory, and title-generation work;
direct mode skips that overhead but is plain chat and cannot invoke Hermes
agent tools. The dedicated vault, Home Assistant, vinyl, and web tools work
the same in either mode. Configure `HERMES_AGENT_API_URL` as well to expose
`hermes_task` for explicit full-agent requests without slowing down normal
questions.

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
