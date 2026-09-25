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
| "Search my notes for cyser" | Notification listing `mead.md: …` excerpt | ☐ |
| "Add racked the cyser to mead log" | Confirmation; `mead log` note gains a `- HH:MM …` line | ☐ |
| "What is 2 plus 2" | Short answer via ask_hermes, under ~15s | ☐ |
| "Search the web for the next SpaceX launch" | Notification contains the spoken answer (not the question), current info | ☐ |
| "What is 2 plus 2" via ask_hermes | Notification contains the answer, under ~15s | ☐ |
| "Where did I put the shopping list" | vault_search result or honest "not found" | ☐ |
| "Turn on the living room lights" | ha_control; notification "Living Room is now on." in ~2-5s (never ask_hermes) | ☐ |
| "Turn off the living room lights" | ha_control; "Living Room is now off." | ☐ |
| "Is the hallway light on" | ha_control status; "Hallway: on." | ☐ |
| "Lights on" (no room) | ha_control asks which room (ambiguity candidates) | ☐ |
| "Turn on the garage lights" | ha_control: "No device matches" (honest miss) | ☐ |
| "Tell the house dinner is ready" | ha_announce; pre-announce chime then the message on the Voice PE | ☐ |
| "Do I own Eternal Blue by Spiritbox" | vinyl_lookup; "You own it: ..." with pressing count | ☐ |
| "Pick a random record" | vinyl_lookup; dormancy-weighted pick | ☐ |
| "What are my most played records" | vinyl_lookup; top plays with counts | ☐ |
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
