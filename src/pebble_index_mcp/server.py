"""Pebble Index MCP bridge: vault + Hermes tools over Streamable HTTP."""
from __future__ import annotations

import hmac
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    PromptMessage,
    TextContent,
)
from mcp.types import Prompt as MCPPrompt
from starlette.responses import JSONResponse

from .hermes import HermesClient, HermesError, HermesTimeout
from .homeassistant import HAClient, HAError
from .vault import Vault, VaultAccessError
from .vinyl import VinylClient, VinylError
from .websearch import ExaClient, ExaError

logger = logging.getLogger("pebble_index_mcp")

_VAULT_PATH = os.environ.get("VAULT_PATH", "").strip()
if _VAULT_PATH:
    VAULT: Vault | None = Vault(_VAULT_PATH)
else:
    VAULT = None
    logger.warning("VAULT_PATH is not set; vault tools will report an error")
TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
HERMES = HermesClient(
    url=os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions"),
    api_key=os.environ.get("HERMES_API_KEY", ""),
    model=os.environ.get("RING_MODEL", "pebble-ring"),
)
EXA = ExaClient(api_key=os.environ.get("EXA_API_KEY", ""))
if not EXA.api_key:
    logger.warning("EXA_API_KEY is not set; web_search will report an error")
HA = HAClient(
    url=os.environ.get("HASS_URL", "http://127.0.0.1:8123"),
    token=os.environ.get("HASS_TOKEN", ""),
)
if not HA.token:
    logger.warning("HASS_TOKEN is not set; ha_control will report an error")
VINYL = VinylClient(url=os.environ.get("VINYL_URL", "").strip())
if not VINYL.url:
    logger.warning("VINYL_URL is not set; vinyl_lookup will report an error")

# DNS-rebinding protection: allow loopback plus any hosts named in
# MCP_ALLOWED_HOSTS (comma-separated). The public tunnel hostname goes there.
_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_ALLOWED_HOSTS = _LOOPBACK_HOSTS + [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]

mcp = FastMCP(
    "pebble-index-mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
    ),
)

_VAULT_UNSET_MSG = "VAULT_PATH is not configured; vault tools are unavailable"


def _vault_or_error() -> Vault:
    if VAULT is None:
        raise VaultAccessError(_VAULT_UNSET_MSG)
    return VAULT


@mcp.tool()
def vault_search(query: str, max_results: int = 5) -> str:
    """Search the user's Obsidian vault (case-insensitive text search).

    Use when the user asks where something is or whether a note exists.
    Returns "relative/path.md: excerpt" lines. Facts about the vault must
    come from this tool or vault_read; never invent them.
    """
    try:
        return _vault_or_error().search(query, max_results)
    except VaultAccessError as e:
        return str(e)


@mcp.tool()
def vault_read(note_path: str, max_chars: int = 1500) -> str:
    """Read a note from the Obsidian vault. note_path is relative to the vault
    root, e.g. "Writing/ideas.md". Returns the first max_chars characters."""
    try:
        return _vault_or_error().read(note_path, max_chars)
    except VaultAccessError as e:
        return str(e)


@mcp.tool()
def vault_append(note_path: str, text: str) -> str:
    """Append a line to a note in the Obsidian vault, creating it if needed.

    Use only when the user explicitly asks to save or add something. Appends
    a timestamped "- HH:MM text" line. Never overwrites or deletes."""
    try:
        return _vault_or_error().append(note_path, text)
    except VaultAccessError as e:
        return f"Could not append: {e}"


def _spoken_response(text: str, question: str | None = None) -> CallToolResult:
    """Wrap a final answer in the Pebble app's coreSchema Response contract.

    The app builds the completion notification from the LAST tool action's
    semantic result; a plain-text result makes it show the transcription
    (the question) instead of the answer. `Response` is the variant whose
    text is surfaced in the notification. `output` is what the cloud agent's
    LLM receives; `content` mirrors it for non-Pebble MCP clients.
    """
    semantic: dict = {"type": "Response", "text": text}
    if question is not None:
        semantic["question"] = question
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"output": text, "semanticResult": semantic},
    )
    result.meta = {"coreSchema": 1}
    return result


@mcp.tool()
def web_search(query: str, max_results: int = 3) -> CallToolResult:
    """Search the live web for current information.

    Use for news, weather, sports scores, prices, schedules, recent events,
    or anything that changes over time. Returns a short answer composed from
    live search results, plus the raw results for reference. For questions
    about the user's notes, prefer vault_search instead.
    """
    try:
        results = EXA.search(query, max_results)
    except ExaError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Web search unavailable ({e})")]
        )
    try:
        answer = HERMES.compose_answer(query, results)
    except (HermesTimeout, HermesError):
        # Compose is best-effort; fall back to raw results so the cloud
        # agent can still answer in the feed (notification shows the question).
        answer = None
    if not answer:
        return CallToolResult(content=[TextContent(type="text", text=results)])
    spoken = answer
    llm_text = f"{answer}\n\nSources:\n{results}"
    result = CallToolResult(
        content=[TextContent(type="text", text=llm_text)],
        structuredContent={
            "output": llm_text,
            "semanticResult": {"type": "Response", "text": spoken, "question": query},
        },
    )
    result.meta = {"coreSchema": 1}
    return result


@mcp.tool()
def ask_hermes(question: str) -> CallToolResult:
    """Forward a question or task to the user's AI agent (Hermes).

    Use for questions the vault tools cannot answer: lookups, research, or
    tasks. Returns the agent's final answer (kept short by instruction)."""
    try:
        answer = HERMES.ask(question)
    except HermesTimeout:
        answer = (
            "Hermes is still working on it. It will finish shortly; "
            "re-ask or check Telegram."
        )
    except HermesError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Hermes unavailable ({e})")]
        )
    return _spoken_response(answer, question=question)


@mcp.tool()
def ha_control(command: str) -> CallToolResult:
    """Control the user's smart home: lights, switches, fans, and media players.

    ALWAYS use this for smart-home commands like "living room lights on",
    "bedroom off", "toggle the hallway", or "is the living room tv on",
    instead of ask_hermes. command is the spoken request in plain text.
    Supported actions: on, off, toggle, status. Verifies the change and
    returns the device's new state."""
    try:
        text = HA.control(command)
    except HAError as e:
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def ha_announce(message: str) -> CallToolResult:
    """Make a spoken announcement on the house speaker(s), like an intercom.

    Use when the user says "tell everyone ...", "announce ...", or "tell the
    house ...". message is ONLY the words to speak, with the routing phrase
    stripped (for "tell the house dinner is ready", message is "dinner is
    ready"). Keep it short. Returns the number of speakers it played on."""
    try:
        text = HA.announce(message)
    except HAError as e:
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=message)


@mcp.tool()
def vinyl_lookup(command: str) -> CallToolResult:
    """Look things up in the user's vinyl record catalog.

    Use when the user asks whether they own a record (essential before
    buying a duplicate at a record store), wants a random record to spin,
    or asks for their most-played records. command is the spoken request in
    plain text, e.g. "do I own Eternal Blue by Spiritbox", "pick a random
    record", "most played records this year". Returns what they own,
    including pressing counts."""
    try:
        text = VINYL.lookup(command)
    except VinylError as e:
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def ha_music(command: str) -> CallToolResult:
    """Play or stop music on the house speakers (Deezer via Music Assistant).

    Use for requests like "play Spiritbox in the basement", "play the album
    Blue Rev in the kitchen", "play the song Granite", or "stop the music in
    the living room". command is the spoken request. No room named means the
    Voice PE speaker. Verifies playback started and returns what is playing."""
    try:
        text = HA.music(command)
    except HAError as e:
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


_GENERIC_PERSONA = (
    "You are a voice assistant answering through a Pebble Index smart ring. "
    "The user's Obsidian vault lives at the vault root.\n"
    "Rules:\n"
    "- Answer in at most 3 short sentences. No markdown. The answer is "
    "shown as a phone notification.\n"
    "- Facts about the vault must come from vault_search or vault_read. "
    "Never invent note contents or file names.\n"
    "- When asked to save something, use vault_append; if the target note "
    "is unclear, use Inbox.md.\n"
    "- For anything beyond the vault, forward to ask_hermes rather than "
    "guessing.\n"
    "- For news, weather, prices, scores, or anything current, use "
    "web_search first, then answer from its results.\n"
    "- If you do not know, say so."
)


def _ring_persona_text() -> str:
    # Operators can override the generic persona with their own file
    # (e.g. to teach the agent the user's name, hobbies, and vault layout).
    path = os.environ.get("RING_PERSONA_FILE", "").strip()
    if path:
        try:
            return open(path, encoding="utf-8").read().strip()
        except OSError:
            logger.warning("RING_PERSONA_FILE unreadable (%s); using generic persona", path)
    return _GENERIC_PERSONA


# The prompt is registered on the LOW-LEVEL server rather than through
# FastMCP's prompt manager because FastMCP serializes `arguments` as an empty
# list, and the Pebble app's Kotlin client drops any prompt whose `arguments`
# field is a non-null list (it checks `arguments == null`). Emitting null
# makes ring_persona selectable in the app.
PROMPT_NAME = "ring_persona"
PROMPT_DESCRIPTION = (
    "Persona for the Pebble cloud agent. Select this prompt in the Pebble "
    "app's MCP server settings."
)


async def _handle_list_prompts() -> ListPromptsResult:
    return ListPromptsResult(
        prompts=[
            MCPPrompt(
                name=PROMPT_NAME,
                description=PROMPT_DESCRIPTION,
                arguments=None,
            )
        ]
    )


async def _handle_get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    if name != PROMPT_NAME:
        raise ValueError(f"Unknown prompt: {name}")
    return GetPromptResult(
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=_ring_persona_text()),
            )
        ]
    )


mcp._mcp_server.list_prompts()(_handle_list_prompts)
mcp._mcp_server.get_prompt()(_handle_get_prompt)


def make_app():
    """Return the FastMCP streamable-http app wrapped in bearer-token auth.

    All config (TOKEN, VAULT, allowed hosts) is read once at import time;
    rotating the bearer token or changing allowed hosts requires a service
    restart, which is the accepted cost of not re-reading env per request.
    """
    if not TOKEN:
        logger.warning(
            "MCP_BEARER_TOKEN is empty; every request will be rejected (401). "
            "Set it before deploying."
        )
    inner = mcp.streamable_http_app()
    expected = f"Bearer {TOKEN}"

    async def auth_app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not TOKEN or not hmac.compare_digest(auth, expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await response(scope, receive, send)
        return await inner(scope, receive, send)

    auth_app.inner = inner  # exposed for tests (lifespan management)
    return auth_app


app = make_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "pebble_index_mcp.server:app",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8765")),
        log_level="info",
    )
