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
    assert "VAULT_PATH is not configured" in srv.vault_search("anything")


def _reload(monkeypatch, **env):
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
    srv = _reload(monkeypatch, VAULT_PATH="")

    def boom(q, max_results=3):
        raise srv.ExaError("HTTP 500: sad")

    monkeypatch.setattr(srv.EXA, "search", boom)
    r = srv.web_search("q")
    assert "Web search unavailable" in r.content[0].text


def test_ask_hermes_returns_core_schema_response(monkeypatch):
    srv = _reload(monkeypatch, VAULT_PATH="")
    monkeypatch.setattr(srv.HERMES, "ask", lambda q: "Two plus two is four.")

    r = srv.ask_hermes("what is 2 plus 2")
    assert r.meta == {"coreSchema": 1}
    assert r.structuredContent["semanticResult"]["type"] == "Response"
    assert r.structuredContent["semanticResult"]["text"] == "Two plus two is four."
    assert r.structuredContent["output"] == "Two plus two is four."


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
