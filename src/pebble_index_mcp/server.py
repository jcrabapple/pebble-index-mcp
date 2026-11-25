"""Pebble Index MCP bridge: vault + Hermes tools over Streamable HTTP."""
from __future__ import annotations

import hmac
import logging
import os
import time

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

from .calc import CalcError, evaluate as eval_calc
from .calendar import CalendarClient, CalendarError
from .hermes import HermesClient, HermesError, HermesTimeout
from .homeassistant import HAClient, HAError, parse_music_rooms_env
from .overseerr import OverseerrClient, OverseerrError
from .vault import Vault, VaultAccessError
from .vinyl import VinylClient, VinylError
from .websearch import ExaClient, ExaError, TrivetClient, TrivetError

logger = logging.getLogger("pebble_index_mcp")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _log_tool(name: str, started: float, *, ok: bool = True, detail: str = "") -> None:
    ms = int((time.perf_counter() - started) * 1000)
    suffix = f" ({detail[:80]})" if detail else ""
    logger.info("tool=%s ok=%s ms=%d%s", name, ok, ms, suffix)

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
    provider_order=os.environ.get("RING_PROVIDER_ORDER", ""),
)
EXA = ExaClient(api_key=os.environ.get("EXA_API_KEY", ""))
TRIVET = TrivetClient(
    url=os.environ.get("TRIVET_URL", "").strip(),
    username=os.environ.get("TRIVET_USER", "").strip(),
    password=os.environ.get("TRIVET_PASS", "").strip(),
)
HERMES_AGENT = None
if os.environ.get("HERMES_AGENT_API_URL", "").strip():
    HERMES_AGENT = HermesClient(
        url=os.environ["HERMES_AGENT_API_URL"],
        api_key=os.environ.get("HERMES_AGENT_API_KEY", ""),
        model=os.environ.get("HERMES_AGENT_MODEL", "pebble-ring"),
        timeout=float(os.environ.get("HERMES_AGENT_TIMEOUT", "120")),
        api_provider=os.environ.get("HERMES_AGENT_PROVIDER", ""),
        reasoning_effort=os.environ.get("HERMES_AGENT_REASONING_EFFORT", ""),
    )
if not TRIVET.url and not EXA.api_key:
    logger.warning("Neither TRIVET_URL nor EXA_API_KEY is set; web_search will report an error")
HA = HAClient(
    url=os.environ.get("HASS_URL", "http://127.0.0.1:8123"),
    token=os.environ.get("HASS_TOKEN", ""),
    music_rooms=parse_music_rooms_env(os.environ.get("HASS_MUSIC_ROOMS", "")),
    default_players=os.environ.get("HASS_DEFAULT_PLAYER", ""),
)
if not HA.token:
    logger.warning("HASS_TOKEN is not set; ha_control will report an error")
VINYL = VinylClient(url=os.environ.get("VINYL_URL", "").strip())
if not VINYL.url:
    logger.warning("VINYL_URL is not set; vinyl_lookup will report an error")
OVERSEERR = OverseerrClient(
    url=os.environ.get("OVERSEERR_URL", "").strip(),
    api_key=os.environ.get("OVERSEERR_API_KEY", "").strip(),
)
if not OVERSEERR.url or not OVERSEERR.api_key:
    logger.warning("OVERSEERR_URL/OVERSEERR_API_KEY not set; plex_media will report an error")
CALENDAR = CalendarClient(khal_bin=os.environ.get("KHAL_BIN", "").strip())

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
def vault(
    action: str,
    query: str = "",
    note_path: str = "",
    text: str = "",
    max_results: int = 5,
    max_chars: int = 1500,
    tail: bool = False,
) -> CallToolResult:
    """Search, read, or append to notes in the user's Obsidian vault.

    action:
    - "search": search filenames and note contents using query (returns
      "relative/path.md: excerpt" lines; call respond(answer) afterward).
    - "read": read note_path (relative to vault root, e.g. "Writing/ideas.md").
      Set tail=True for append-only logs like Inbox.md. Call respond(answer)
      afterward with your summary.
    - "append": append a dated "- YYYY-MM-DD HH:MM text" line to note_path
      (default "Inbox.md"), creating it if needed. Never overwrites.
    """
    started = time.perf_counter()
    act = (action or "").strip().lower()
    if act == "search":
        try:
            out = _vault_or_error().search(query, max_results)
            _log_tool("vault:search", started, ok=True)
        except VaultAccessError as e:
            out = str(e)
            _log_tool("vault:search", started, ok=False, detail=str(e))
        return CallToolResult(content=[TextContent(type="text", text=out)])
    if act == "read":
        try:
            out = _vault_or_error().read(note_path, max_chars, tail=tail)
            _log_tool("vault:read", started, ok=True)
        except VaultAccessError as e:
            out = str(e)
            _log_tool("vault:read", started, ok=False, detail=str(e))
        return CallToolResult(content=[TextContent(type="text", text=out)])
    if act == "append":
        target = note_path.strip() or "Inbox.md"
        try:
            out = _vault_or_error().append(target, text)
            _log_tool("vault:append", started, ok=True)
        except VaultAccessError as e:
            out = f"Could not append: {e}"
            _log_tool("vault:append", started, ok=False, detail=str(e))
        return _spoken_response(out, question=f"Append to {target}")
    _log_tool("vault", started, ok=False, detail=f"unknown action {act}")
    return _spoken_response('Unknown vault action; use "search", "read", or "append".')


@mcp.tool()
def respond(answer: str) -> CallToolResult:
    """Deliver the final spoken answer to the user's phone notification.

    ALWAYS call this tool as your final action when answering general
    knowledge questions or summarizing results from vault search/read.
    Without it, the phone notification shows the user's question instead
    of your answer. Keep answer to at most 3 short sentences, plain text,
    no markdown."""
    started = time.perf_counter()
    cleaned = answer.strip() or "No answer provided."
    _log_tool("respond", started, ok=True)
    return _spoken_response(cleaned)


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

    Use for news, sports scores, prices, schedules, recent events, or anything
    that changes over time. Prefers self-hosted Trivet metasearch when
    configured (falling back to Exa), then composes a short spoken answer.
    For local weather prefer weather_report; for notes prefer vault_search.
    """
    started = time.perf_counter()
    backend = "exa"
    try:
        if TRIVET.url:
            backend = "trivet"
            try:
                results = TRIVET.search(query, max_results)
            except TrivetError as te:
                if EXA.api_key:
                    logger.warning("Trivet search failed (%s); falling back to Exa", te)
                    backend = "exa-fallback"
                    results = EXA.search(query, max_results)
                else:
                    raise
        else:
            results = EXA.search(query, max_results)
    except ExaError as e:
        _log_tool("web_search", started, ok=False, detail=f"{backend}: {e}")
        return CallToolResult(
            content=[TextContent(type="text", text=f"Web search unavailable ({e})")]
        )
    search_done = time.perf_counter()
    try:
        answer = HERMES.compose_answer(query, results)
    except (HermesTimeout, HermesError):
        # Compose is best-effort; fall back to raw results so the cloud
        # agent can still answer in the feed (notification shows the question).
        answer = None
    compose_done = time.perf_counter()
    logger.info(
        "web_search backend=%s search=%.3fs compose=%.3fs total=%.3fs",
        backend,
        search_done - started,
        compose_done - search_done,
        compose_done - started,
    )
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


def ask_hermes(question: str) -> CallToolResult:
    """Answer a general question with the configured chat model.

    Deprecated in favor of `respond(answer)` (which avoids a second LLM hop).
    Registered as an MCP tool only when ENABLE_ASK_HERMES=1."""
    started = time.perf_counter()
    try:
        answer = HERMES.ask(question)
    except HermesTimeout:
        answer = "The answer service timed out. Please try again."
    except HermesError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Hermes unavailable ({e})")]
        )
    logger.info("ask_hermes timing total=%.3fs", time.perf_counter() - started)
    return _spoken_response(answer, question=question)


if os.environ.get("ENABLE_ASK_HERMES", "").strip() in ("1", "true", "yes"):
    mcp.tool()(ask_hermes)


def hermes_task(task: str) -> CallToolResult:
    """Run a task with the user's full Hermes agent, including its tools.

    Use when the user explicitly says "ask Hermes" or "tell Hermes", or when
    the request needs Hermes agent capabilities beyond the dedicated tools.
    This is slower than direct tools. task is the complete spoken request."""
    if HERMES_AGENT is None:
        return _spoken_response("The full Hermes agent is not configured.", question=task)
    started = time.perf_counter()
    try:
        answer = HERMES_AGENT.ask(task)
        _log_tool("hermes_task", started, ok=True)
    except HermesTimeout:
        answer = "Hermes timed out. Please try again or check Matrix."
        _log_tool("hermes_task", started, ok=False, detail="timeout")
    except HermesError as e:
        answer = f"Hermes unavailable ({e})"
        _log_tool("hermes_task", started, ok=False, detail=str(e))
    return _spoken_response(answer, question=task)


if HERMES_AGENT is not None:
    mcp.tool()(hermes_task)


@mcp.tool()
def smart_home(action: str, command: str) -> CallToolResult:
    """Control Home Assistant devices/scenes or make a house intercom announcement.

    action:
    - "control": lights, Hue scenes, dimming, all lights off/on, switches,
      fans, covers, TVs. Pass the spoken command (e.g. "living room lights
      on", "dim living room to 30%", "set basement to relax", "all lights
      off", "is the hallway light on").
    - "announce": intercom announcement on live house speaker(s). Pass ONLY
      the words to speak (e.g. "dinner is ready").
    """
    started = time.perf_counter()
    act = (action or "").strip().lower()
    try:
        if act == "announce":
            text = HA.announce(command)
            _log_tool("smart_home:announce", started, ok=True)
        elif act in ("control", ""):
            text = HA.control(command)
            _log_tool("smart_home:control", started, ok=True)
        else:
            _log_tool("smart_home", started, ok=False, detail=f"unknown action {act}")
            return _spoken_response('Unknown smart_home action; use "control" or "announce".')
    except HAError as e:
        _log_tool(f"smart_home:{act or 'control'}", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def plex_media(
    action: str,
    query: str = "",
    media_type: str = "",
    season: int | None = None,
) -> CallToolResult:
    """Check Plex availability, request movies/TV shows for Plex, or list recent requests via Overseerr.

    action:
    - "request": download/request a movie or TV show to Plex (e.g. query="Sinners",
      media_type="movie"; or query="Severance", media_type="tv", season=2).
      Never re-downloads items already on Plex.
    - "status": check whether a movie or TV show is already on Plex or downloading.
    - "requests": list recent Plex media requests and their status.
    """
    started = time.perf_counter()
    act = (action or "").strip().lower()
    try:
        if act in ("request", "download", "add"):
            text = OVERSEERR.request_media(query, media_type=media_type, season=season)
            _log_tool("plex_media:request", started, ok=True)
        elif act in ("status", "search", "check"):
            text = OVERSEERR.status(query, media_type=media_type)
            _log_tool("plex_media:status", started, ok=True)
        elif act in ("requests", "list", "recent"):
            text = OVERSEERR.recent_requests()
            _log_tool("plex_media:requests", started, ok=True)
        else:
            _log_tool("plex_media", started, ok=False, detail=f"unknown action {act}")
            return _spoken_response('Unknown plex_media action; use "request", "status", or "requests".')
    except OverseerrError as e:
        _log_tool(f"plex_media:{act}", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=query or act)
    return _spoken_response(text, question=query or act)


@mcp.tool()
def vinyl_lookup(command: str) -> CallToolResult:
    """Look things up in the user's vinyl record catalog.

    Use when the user asks whether they own a record (essential before
    buying a duplicate at a record store), wants a random record to spin,
    or asks for their most-played records. command is the spoken request in
    plain text, e.g. "do I own Eternal Blue by Spiritbox", "pick a random
    record", "most played records this year". Returns what they own,
    including pressing counts."""
    started = time.perf_counter()
    try:
        text = VINYL.lookup(command)
        _log_tool("vinyl_lookup", started, ok=True)
    except VinylError as e:
        _log_tool("vinyl_lookup", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def ha_music(command: str) -> CallToolResult:
    """Play, control, or query music on the house speakers (Deezer via Music Assistant).

    Use for playback ("play Spiritbox in the basement", "play the album Blue
    Rev in the kitchen"), transport ("pause", "resume", "skip", "previous",
    "stop the music"), volume ("volume 40 in the basement", "turn it down",
    "mute kitchen"), or status ("what's playing"). No room named uses the
    active playing speaker or default speaker."""
    started = time.perf_counter()
    try:
        text = HA.music(command)
        _log_tool("ha_music", started, ok=True)
    except HAError as e:
        _log_tool("ha_music", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def shopping_list(command: str) -> CallToolResult:
    """Read, add to, or check off items on the Home Assistant shopping list.

    ALWAYS use this for shopping or grocery list requests like "add oat milk
    to the shopping list", "we need milk and eggs", "what's on the shopping
    list", or "check off milk". command is the spoken request in plain text."""
    started = time.perf_counter()
    try:
        text = HA.shopping_list(command)
        _log_tool("shopping_list", started, ok=True)
    except HAError as e:
        _log_tool("shopping_list", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=command)
    return _spoken_response(text, question=command)


@mcp.tool()
def weather_report(query: str = "weather") -> CallToolResult:
    """Get local home weather, hourly/daily forecast, feels-like, gusts, or sunrise/sunset.

    ALWAYS use this instead of web_search for local weather questions like
    "what's the weather", "do I need a jacket", "will it rain tonight",
    "weather at 5pm", "will it rain Thursday", or "when is sunset". Fast
    and local."""
    started = time.perf_counter()
    try:
        text = HA.weather(query)
        _log_tool("weather_report", started, ok=True)
    except HAError as e:
        _log_tool("weather_report", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=query)
    return _spoken_response(text, question=query)


@mcp.tool()
def calendar_lookup(query: str = "this week") -> CallToolResult:
    """Check the user's Fastmail calendar schedule or search for an event.

    ALWAYS use this for schedule questions like "what's on my calendar
    today", "what do I have tomorrow", "what's happening next week",
    "am I free December 5th", or "when is the flea market"."""
    started = time.perf_counter()
    try:
        text = CALENDAR.lookup(query)
        _log_tool("calendar_lookup", started, ok=True)
    except CalendarError as e:
        _log_tool("calendar_lookup", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=query)
    return _spoken_response(text, question=query)


@mcp.tool()
def quick_calc(expression: str) -> CallToolResult:
    """Deterministic math, unit conversions, mead ABV, tip/split, and dice/coin.

    ALWAYS use this for arithmetic, percentages ("15% of 240", "20% off $80"),
    tips and bill splits ("split 84 3 ways", "20% tip on 64.50", "tip on 64.50"),
    unit conversions ("350 F to C", "12 oz in ml"), mead gravity ABV ("abv
    1.105 to 1.000"), coin flips ("flip a coin"), or dice rolls ("roll 2d6").
    Never guess math or randomness in the LLM."""
    started = time.perf_counter()
    try:
        text = eval_calc(expression)
        _log_tool("quick_calc", started, ok=True)
    except CalcError as e:
        _log_tool("quick_calc", started, ok=False, detail=str(e))
        return _spoken_response(str(e), question=expression)
    return _spoken_response(text, question=expression)


_GENERIC_PERSONA = (
    "You are a voice assistant answering through a Pebble Index smart ring. "
    "The user's Obsidian vault lives at the vault root.\n"
    "Rules:\n"
    "- Answer in at most 3 short sentences. No markdown. The answer is "
    "shown as a phone notification.\n"
    "- Facts about the vault must come from vault(action='search' or 'read'). "
    "Never invent note contents or file names. After searching or reading "
    "the vault, call respond(answer) with your summary so it appears in the "
    "phone notification.\n"
    "- When asked to save a note, use vault(action='append'); if the target "
    "note is unclear, use Inbox.md.\n"
    "- For smart-home commands (lights, dimming, Hue scenes, all lights off, "
    "switches, fans, covers, TVs), ALWAYS use smart_home(action='control').\n"
    "- For house intercom/announcement requests, ALWAYS use "
    "smart_home(action='announce') with only the words to speak.\n"
    "- For music playback, transport, volume, or now-playing queries on house "
    "speakers, ALWAYS use ha_music.\n"
    "- For movies or TV shows on Plex (downloading/requesting via Overseerr, "
    "checking if something is on Plex, or listing recent requests), ALWAYS "
    "use plex_media.\n"
    "- For shopping/grocery list items, ALWAYS use shopping_list.\n"
    "- For local weather, hourly/daily forecast, rain, gusts, sunrise, or "
    "sunset, ALWAYS use weather_report instead of web_search.\n"
    "- For schedule, calendar, appointments, or when an event is, ALWAYS "
    "use calendar_lookup.\n"
    "- For math, percentages, discounts, unit conversions, tips, bill "
    "splits, ABV, coin flips, or dice rolls, ALWAYS use quick_calc.\n"
    "- For vinyl record collection lookups, ALWAYS use vinyl_lookup.\n"
    "- For news, prices, scores, or live web facts, use web_search.\n"
    "- For general knowledge questions not covered by a dedicated tool, "
    "call respond(answer) with a 1-3 sentence plain-text answer.\n"
    "- If you do not know, say so via respond(answer)."
)

_PERSONA_CACHE: tuple[str, float, str] | None = None


def _ring_persona_text() -> str:
    global _PERSONA_CACHE
    # Operators can override the generic persona with their own file
    # (e.g. to teach the agent the user's name, hobbies, and vault layout).
    path = os.environ.get("RING_PERSONA_FILE", "").strip()
    if path:
        try:
            mtime = os.path.getmtime(path)
            if _PERSONA_CACHE and _PERSONA_CACHE[0] == path and _PERSONA_CACHE[1] == mtime:
                return _PERSONA_CACHE[2]
            text = open(path, encoding="utf-8").read().strip()
            _PERSONA_CACHE = (path, mtime, text)
            return text
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
        if scope.get("path") == "/healthz" and scope.get("method") in ("GET", "HEAD"):
            response = JSONResponse({"ok": True}, status_code=200)
            return await response(scope, receive, send)
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
