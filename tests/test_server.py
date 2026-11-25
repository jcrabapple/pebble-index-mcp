import importlib
from contextlib import asynccontextmanager

import httpx
import pytest


@pytest.fixture()
def app_with_token(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


@asynccontextmanager
async def app_lifespan(app):
    """Enter the inner Starlette app's lifespan so FastMCP's task group initializes."""
    inner = app.inner
    ctx = inner.router.lifespan_context(inner)
    await ctx.__aenter__()
    try:
        yield
    finally:
        await ctx.__aexit__(None, None, None)


INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    },
}


def _client(app):
    # Host must include a port: FastMCP's DNS-rebinding defaults allow
    # "127.0.0.1:*" patterns, and a portless Host fails the check (421).
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    )


async def test_unauthorized_no_header(app_with_token):
    async with _client(app_with_token) as client:
        r = await client.post("/mcp", json=INIT_PAYLOAD)
    assert r.status_code == 401


async def test_unauthorized_bad_token(app_with_token):
    async with _client(app_with_token) as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={"Authorization": "Bearer wrong"},
        )
    assert r.status_code == 401


async def test_healthz_unauthenticated(app_with_token):
    async with _client(app_with_token) as client:
        r = await client.get("/healthz")
        r_head = await client.head("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert r_head.status_code == 200


@pytest.fixture()
def app_empty_token(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


@pytest.fixture()
def app_with_host(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "ring.example.com")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


async def test_initialize_with_token(app_with_token):
    async with app_lifespan(app_with_token):
        async with _client(app_with_token) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert r.status_code == 200


async def test_empty_token_denies_all(app_empty_token):
    async with _client(app_empty_token) as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={"Authorization": "Bearer anything"},
        )
    assert r.status_code == 401


async def test_unallowed_host_rejected(app_with_token):
    async with app_lifespan(app_with_token):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_with_token),
            base_url="http://evil.example.com:8765",
        ) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={"Authorization": "Bearer sekret"},
            )
    assert r.status_code == 421


async def test_configured_host_allowed(app_with_host):
    async with app_lifespan(app_with_host):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_with_host),
            base_url="http://ring.example.com",
        ) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert r.status_code == 200


def test_vault_tool_without_vault_path(monkeypatch):
    import pebble_index_mcp.server as srv

    monkeypatch.delenv("VAULT_PATH", raising=False)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    importlib.reload(srv)
    r = srv.vault("search", query="anything")
    assert "VAULT_PATH is not configured" in r.content[0].text


def _reload(monkeypatch, **env):
    for clear_key in (
        "HERMES_AGENT_API_URL",
        "HERMES_AGENT_API_KEY",
        "ENABLE_ASK_HERMES",
        "RING_PERSONA_FILE",
        "TRIVET_URL",
        "TRIVET_USER",
        "TRIVET_PASS",
        "OVERSEERR_URL",
        "OVERSEERR_API_KEY",
    ):
        monkeypatch.delenv(clear_key, raising=False)
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv


def test_web_search_returns_core_schema_response(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    monkeypatch.setattr(srv.EXA, "search", lambda q, max_results=3: "Result (https://a)\nSnip")
    monkeypatch.setattr(srv.HERMES, "compose_answer", lambda q, r: "The launch is Friday.")

    r = srv.web_search("when is the launch")
    assert r.meta == {"coreSchema": 1}
    sc = r.structuredContent
    assert sc["output"].startswith("The launch is Friday.")
    assert "Result (https://a)" in sc["output"]  # sources ride along for the LLM
    assert sc["semanticResult"] == {
        "type": "Response",
        "text": "The launch is Friday.",
        "question": "when is the launch",
    }
    assert r.content[0].text == sc["output"]


def test_web_search_falls_back_to_plain_text_without_compose(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")

    def boom(q, r):
        raise srv.HermesTimeout("slow")

    monkeypatch.setattr(srv.EXA, "search", lambda q, max_results=3: "Result (https://a)\nSnip")
    monkeypatch.setattr(srv.HERMES, "compose_answer", boom)

    r = srv.web_search("q")
    assert r.meta is None
    assert r.structuredContent is None
    assert "Result (https://a)" in r.content[0].text


def test_web_search_exa_error_is_content(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="", TRIVET_URL="")

    def boom(q, max_results=3):
        raise srv.ExaError("HTTP 500: sad")

    monkeypatch.setattr(srv.EXA, "search", boom)
    r = srv.web_search("q")
    assert "Web search unavailable" in r.content[0].text


def test_web_search_prefers_trivet_and_falls_back_to_exa(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="", TRIVET_URL="http://trivet.test", EXA_API_KEY="exa-k")
    monkeypatch.setattr(srv.TRIVET, "search", lambda q, n=3: "Trivet Hit (https://t)\nDetails")
    monkeypatch.setattr(srv.HERMES, "compose_answer", lambda q, r: "Trivet answered.")
    r = srv.web_search("test query")
    assert r.structuredContent["semanticResult"]["text"] == "Trivet answered."
    assert "Trivet Hit" in r.structuredContent["output"]

    # When Trivet fails, fall back cleanly to Exa
    def trivet_down(q, n=3):
        raise srv.TrivetError("connection refused")

    monkeypatch.setattr(srv.TRIVET, "search", trivet_down)
    monkeypatch.setattr(srv.EXA, "search", lambda q, n=3: "Exa Hit (https://e)\nBackup")
    monkeypatch.setattr(srv.HERMES, "compose_answer", lambda q, r: "Exa backup answered.")
    r2 = srv.web_search("test query")
    assert r2.structuredContent["semanticResult"]["text"] == "Exa backup answered."
    assert "Exa Hit" in r2.structuredContent["output"]


def test_ask_hermes_returns_core_schema_response(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    monkeypatch.setattr(srv.HERMES, "ask", lambda q: "Two plus two is four.")

    r = srv.ask_hermes("what is 2 plus 2")
    assert r.meta == {"coreSchema": 1}
    assert r.structuredContent["semanticResult"]["type"] == "Response"
    assert r.structuredContent["semanticResult"]["text"] == "Two plus two is four."
    assert r.structuredContent["output"] == "Two plus two is four."


def test_hermes_task_uses_full_agent(monkeypatch):
    srv = _reload(
        monkeypatch,
        VAULT_PATH="",
        HERMES_AGENT_API_URL="http://hermes/v1/chat/completions",
        HERMES_AGENT_API_KEY="agent-key",
    )
    monkeypatch.setattr(srv.HERMES_AGENT, "ask", lambda task: "Hermes handled it.")

    r = srv.hermes_task("ask Hermes to handle this")
    assert r.structuredContent["semanticResult"] == {
        "type": "Response",
        "text": "Hermes handled it.",
        "question": "ask Hermes to handle this",
    }


def test_hermes_task_without_agent_configuration(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    r = srv.hermes_task("do something")
    assert "not configured" in r.structuredContent["semanticResult"]["text"]


def test_smart_home_announce_error_returns_original_message(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")

    def fail(message):
        raise srv.HAError("No available speaker")

    monkeypatch.setattr(srv.HA, "announce", fail)
    r = srv.smart_home("announce", "dinner is ready")
    assert r.structuredContent["semanticResult"] == {
        "type": "Response",
        "text": "No available speaker",
        "question": "dinner is ready",
    }


def test_respond_returns_core_schema_without_llm(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    r = srv.respond("  Helena is the capital of Montana.  ")
    assert r.meta == {"coreSchema": 1}
    assert r.structuredContent["semanticResult"] == {
        "type": "Response",
        "text": "Helena is the capital of Montana.",
    }


def test_vault_consolidated_actions(monkeypatch, tmp_path):
    srv = _reload(monkeypatch, VAULT_PATH=str(tmp_path))
    r_app = srv.vault("append", note_path="Inbox.md", text="buy yeast")
    assert r_app.meta == {"coreSchema": 1}
    assert r_app.structuredContent["semanticResult"]["text"] == "Appended to Inbox.md"
    assert (tmp_path / "Inbox.md").is_file()

    r_search = srv.vault("search", query="yeast")
    assert r_search.meta is None
    assert "Inbox.md" in r_search.content[0].text

    r_read = srv.vault("read", note_path="Inbox.md", tail=True)
    assert r_read.meta is None
    assert "buy yeast" in r_read.content[0].text


def test_plex_media_tool_actions(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="", OVERSEERR_URL="https://o.test", OVERSEERR_API_KEY="k")
    monkeypatch.setattr(srv.OVERSEERR, "request_media", lambda q, media_type="", season=None: f"Requested {q}.")
    monkeypatch.setattr(srv.OVERSEERR, "status", lambda q, media_type="": f"{q}: available on Plex.")
    monkeypatch.setattr(srv.OVERSEERR, "recent_requests", lambda: "Recent Plex requests: Severance (available on Plex).")

    r_req = srv.plex_media("request", query="Sinners", media_type="movie")
    assert r_req.meta == {"coreSchema": 1}
    assert r_req.structuredContent["semanticResult"]["text"] == "Requested Sinners."

    r_st = srv.plex_media("status", query="Severance")
    assert r_st.structuredContent["semanticResult"]["text"] == "Severance: available on Plex."

    r_list = srv.plex_media("requests")
    assert "Severance" in r_list.structuredContent["semanticResult"]["text"]


def test_new_deterministic_tools_wrap_spoken_response(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    monkeypatch.setattr(srv.HA, "shopping_list", lambda cmd: "Added oat milk to the shopping list.")
    monkeypatch.setattr(srv.HA, "weather", lambda q: "Currently 44°F and sunny.")
    monkeypatch.setattr(srv.CALENDAR, "lookup", lambda q: "Nothing on the calendar for today.")

    r_shop = srv.shopping_list("add oat milk")
    assert r_shop.meta == {"coreSchema": 1}
    assert r_shop.structuredContent["semanticResult"]["text"] == "Added oat milk to the shopping list."

    r_wea = srv.weather_report("what is the weather")
    assert r_wea.meta == {"coreSchema": 1}
    assert r_wea.structuredContent["semanticResult"]["text"] == "Currently 44°F and sunny."

    r_cal = srv.calendar_lookup("today")
    assert r_cal.meta == {"coreSchema": 1}
    assert r_cal.structuredContent["semanticResult"]["text"] == "Nothing on the calendar for today."

    r_calc = srv.quick_calc("350 F to C")
    assert r_calc.meta == {"coreSchema": 1}
    assert "176.7°C" in r_calc.structuredContent["semanticResult"]["text"]


async def test_prompts_list_arguments_null(app_with_token):
    """The Pebble app hides prompts whose `arguments` field is a non-null
    list, so the wire format must carry arguments: null."""
    import json

    async with app_lifespan(app_with_token):
        async with _client(app_with_token) as client:
            init = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
            sid = init.headers.get("mcp-session-id")
            assert sid, "initialize did not return an mcp-session-id"
            r = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                    "mcp-session-id": sid,
                },
            )
    assert r.status_code == 200
    data_line = [l for l in r.text.splitlines() if l.startswith("data:")][0][5:].strip()
    payload = json.loads(data_line)
    prompts = payload["result"]["prompts"]
    assert [p["name"] for p in prompts] == ["ring_persona"]
    # The Pebble client treats a missing or null `arguments` field the same
    # way (null); an empty list hides the prompt. Accept both wire forms.
    assert prompts[0].get("arguments") is None
