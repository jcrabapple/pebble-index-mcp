# Ring Setup Checklist

Run through this when the Index 01 hardware arrives. Fill `<PUBLIC_URL>` and
`<TOKEN>` from the host's env file (`~/.config/pebble-index-mcp/env` on the
deployment machine) — they are deliberately not committed to this repo.

## Phone-side setup (Pebble app)

1. Index tab → MCP & Tool Settings → create a sandbox group.
   - Model type: `Default` (switch to `High Capability` only if tool selection
     gets flaky).
2. MCP Servers tab → add HTTP MCP server:
   - Name: `pebble-index`
   - URL: `https://<PUBLIC_URL>/mcp`
   - Type: **Streamable**
   - Authorization: `Bearer <TOKEN>` (the `Bearer ` prefix is REQUIRED — entering
     the raw token alone causes 401 on every request and the prompts list never
     loads)
   - Group: the sandbox group from step 1.
3. Index settings → "Double click and hold" → assign the sandbox group.
4. In the MCP server settings, select the `ring_persona` prompt so it joins
   the agent's system prompt.
5. Keep single click and hold as the default Index agent for private,
   offline-capable notes.

## Test matrix

Note: the app caches the tool list for ~30s; wait a moment after config
changes before retesting.

| Say (double-click) | Expected | Result |
|---|---|---|
| "Search my notes for mead cyser" | `vault(search)` -> `respond`: notification listing `Mead/...` note | ☐ |
| "Add racked the cyser to mead log" | `vault(append)`: dated `- YYYY-MM-DD HH:MM ...` line in `Mead Log.md` | ☐ |
| "What is 14 times 12" | `quick_calc`: `"168."` in ~1s | ☐ |
| "20% off $80" | `quick_calc`: `"20% off $80.00 is $64.00 (save $16.00)."` | ☐ |
| "ABV 1.105 to 1.000" | `quick_calc`: `"1.105 to 1.000 is 13.8% ABV."` | ☐ |
| "What is the capital of Montana" | `respond`: `"Helena."` (zero extra LLM hop) | ☐ |
| "Search the web for the next SpaceX launch" | `web_search`: spoken answer (not the question) with current info | ☐ |
| "What's the weather today" | `weather_report`: local HA current + feels-like + forecast + gusts | ☐ |
| "Will it rain tonight" | `weather_report`: hourly forecast | ☐ |
| "When is sunset" | `weather_report`: local HA sunrise/sunset | ☐ |
| "What's on my calendar tomorrow" | `calendar_lookup`: 12-hour formatted schedule | ☐ |
| "Add oat milk and eggs to the shopping list" | `shopping_list`: adds both items | ☐ |
| "Check off milk from the shopping list" | `shopping_list`: removes Milk safely | ☐ |
| "Turn on the living room lights" | `smart_home(control)`: `"Living Room is now on."` in ~2-5s | ☐ |
| "Dim the living room to 30%" | `smart_home(control)`: `"Living Room set to 30%."` | ☐ |
| "Set basement to relax" | `smart_home(control)`: `"Activated Basement Relax."` | ☐ |
| "Is the hallway light on" | `smart_home(control)`: `"Hallway: on."` | ☐ |
| "Lights on" (no room) | `smart_home(control)` asks which room (ambiguity candidates) | ☐ |
| "Turn on the garage lights" | `smart_home(control)`: `"No device matches"` (honest miss) | ☐ |
| "Tell the house dinner is ready" | `smart_home(announce)`: pre-announce chime then message on live Voice PE | ☐ |
| "Is Severance on Plex" | `plex_media(status)`: `"Severance (2022, TV): available on Plex."` | ☐ |
| "Download the movie Sinners to Plex" | `plex_media(request)`: submits Overseerr request or reports already on Plex | ☐ |
| "What's downloading on Plex" | `plex_media(requests)`: lists recent Overseerr requests with titles | ☐ |
| "Do I own Eternal Blue by Spiritbox" | `vinyl_lookup`: `"You own it: ..."` with pressing count | ☐ |
| "Pick a random record" | `vinyl_lookup`: dormancy-weighted pick | ☐ |
| "What are my most played records" | `vinyl_lookup`: top plays with counts | ☐ |
| "Play Spiritbox in the basement" | `ha_music`: `"Playing <track> by Spiritbox on the basement."` | ☐ |
| "Stop the music in the basement" | `ha_music`: `"Stopped."` | ☐ |
| Single click: "remind me to call the dentist tomorrow" | Native reminder (Pebble app), nothing in vault | ☐ |
| Speak a secret/private thought on single click | Stays on-device; nothing hits the MCP server (check its logs) | ☐ |

## Troubleshooting

- **421 Misdirected Request** — the Host header isn't allowed. Add the public
  hostname to `MCP_ALLOWED_HOSTS` and restart `pebble-index-mcp.service`.
- **401 Unauthorized** — token mismatch between app and env file.
- **"Hermes unavailable"** — the Hermes API server isn't reachable from the
  MCP host or the API key is wrong. Check `pebble-index-mcp.service` logs.
- **Prompts row missing** — the app only shows the Prompts section after a
  debounced fetch triggered by changing URL/transport/auth. Toggle Transport
  SSE→Streamable to retrigger. Also: the server MUST emit prompts with a
  missing/null `arguments` field — the app silently drops prompts whose
  `arguments` is an empty list (our server handles this; keep the regression
  test `test_prompts_list_arguments_null` green).
- **Tool list missing a tool** — wait ~30s (tool cache) or toggle the
  double-click sandbox group.
- **Cloud agent struggles to pick tools** — bump the sandbox model to
  `High Capability` in the app.
